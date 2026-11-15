use std::{
    collections::{HashMap, HashSet},
    io::Cursor,
    str::FromStr,
    sync::LazyLock,
};

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::post,
    Json, Router,
};
use chrono::{NaiveDate, NaiveDateTime, Utc};
use regex::Regex;
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::{
    auth::CurrentUser,
    portfolio::{self, LedgerEvent, Portfolio},
    AppError, AppResult, AppState,
};

const MAX_BYTES: usize = 1_000_000;
const MAX_ROWS: usize = 10_000;
const MAX_TRANSACTIONS: usize = 5_000;
const HEADER: [&str; 12] = [
    "Date",
    "Time",
    "Value date",
    "Product",
    "ISIN",
    "Description",
    "FX",
    "Change",
    "",
    "Balance",
    "",
    "Order Id",
];
const NAMESPACE: Uuid = Uuid::from_u128(0xa5a90a0df6f84dbcbda357bc5a023ca9);
const NUMBER_PATTERN: &str = r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,8})?";

static TRADE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^(Koop|Verkoop|Buy|Sell) ({NUMBER_PATTERN}) @ ({NUMBER_PATTERN}) ([A-Z]{{3}})$"
    ))
    .expect("trade regex")
});
static TRANSFER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^(?:Overboeking (?P<nl>van|naar) uw geldrekening bij|Transfer (?P<en>from|to) your cash account at) flatexDEGIRO Bank (?P<amount>{NUMBER_PATTERN}) EUR$"
    ))
    .expect("transfer regex")
});
static EUROPEAN_NUMBER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"^{NUMBER_PATTERN}$")).expect("number regex"));

pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/imports/degiro/preview",
            post(preview),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/imports/degiro",
            post(import_statement),
        )
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PreviewRequest {
    csv: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ImportRequest {
    csv: String,
    preview_token: String,
}

#[derive(Debug, Clone)]
struct StatementRow {
    line: usize,
    cells: Vec<String>,
    stamp: NaiveDateTime,
    value_date: NaiveDate,
    amount: Option<Decimal>,
}

impl StatementRow {
    fn description(&self) -> &str {
        &self.cells[5]
    }

    fn order(&self) -> &str {
        &self.cells[11]
    }
}

#[derive(Debug, Clone)]
struct Candidate {
    body: Value,
    source_rows: Vec<usize>,
    stamp: NaiveDateTime,
    line: usize,
}

#[derive(Debug, Default)]
struct ParsedStatement {
    candidates: Vec<Candidate>,
    ignored: Vec<Value>,
    errors: Vec<Value>,
}

fn bounded_csv(text: &str) -> AppResult<()> {
    let size = text.len();
    if size == 0 || size > MAX_BYTES {
        return Err(AppError::unprocessable(
            "CSV must contain 1 to 1,000,000 UTF-8 bytes.",
        ));
    }
    Ok(())
}

fn european_number(value: &str, places: u32) -> Result<Decimal, String> {
    if value.len() > 40 || !EUROPEAN_NUMBER.is_match(value) {
        return Err("Expected a European-format decimal number (decimal comma).".into());
    }
    let normalized = value.replace('.', "").replace(',', ".");
    let result = Decimal::from_str(&normalized)
        .map_err(|_| "Expected a European-format decimal number (decimal comma).".to_owned())?;
    if result.abs() > Decimal::from(1_000_000_000_000_u64) || result.scale() > places {
        return Err(format!(
            "Amount exceeds the ledger limit or {places} decimal places."
        ));
    }
    Ok(result)
}

fn error(errors: &mut Vec<Value>, row: Option<usize>, message: impl Into<String>) {
    errors.push(json!({"row": row, "message": message.into()}));
}

fn ignore(ignored: &mut Vec<Value>, row: &StatementRow, reason: &str) {
    ignored.push(json!({
        "row": row.line,
        "description": row.description(),
        "reason": reason,
    }));
}

fn candidate(
    parsed: &mut ParsedStatement,
    identities: &mut HashSet<String>,
    row: &StatementRow,
    mut body: Value,
    source_rows: Vec<usize>,
    identity: String,
    effective: Option<&StatementRow>,
) {
    if !identities.insert(identity.clone()) {
        error(
            &mut parsed.errors,
            Some(row.line),
            "Indistinguishable duplicate cash/order rows; cannot safely identify separate transactions.",
        );
        return;
    }
    body["client_id"] = json!(Uuid::new_v5(&NAMESPACE, identity.as_bytes()).to_string());
    let funding = effective.unwrap_or(row);
    let mut source_rows = source_rows;
    source_rows.sort_unstable();
    parsed.candidates.push(Candidate {
        body,
        source_rows,
        stamp: funding.stamp,
        line: funding.line,
    });
}

fn parse_statement(raw: &str) -> ParsedStatement {
    let mut parsed = ParsedStatement::default();
    if raw.len() > MAX_BYTES {
        error(
            &mut parsed.errors,
            None,
            "The CSV exceeds the 1 MB upload limit.",
        );
        return parsed;
    }
    let text = raw.strip_prefix('\u{feff}').unwrap_or(raw);
    let delimiter = if text.starts_with("Date;Time;") {
        b';'
    } else {
        b','
    };
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(delimiter)
        .has_headers(false)
        .flexible(false)
        .from_reader(Cursor::new(text.as_bytes()));
    let mut records = reader.records();
    match records.next() {
        Some(Ok(record))
            if record.len() == HEADER.len()
                && record.iter().zip(HEADER).all(|(left, right)| left == right) => {}
        _ => {
            error(
                &mut parsed.errors,
                Some(1),
                "Use a DEGIRO Account CSV with the English Date, Time, Value date … Order Id header.",
            );
            return parsed;
        }
    }
    let mut rows = Vec::new();
    let mut previous: Option<NaiveDateTime> = None;
    for (index, record) in records.enumerate() {
        if index >= MAX_ROWS {
            error(
                &mut parsed.errors,
                None,
                format!("The statement exceeds {MAX_ROWS} rows."),
            );
            break;
        }
        let line = index + 2;
        let record = match record {
            Ok(record) => record,
            Err(_) => {
                error(&mut parsed.errors, Some(line), "Malformed CSV quoting.");
                continue;
            }
        };
        if record.iter().all(|cell| cell.trim().is_empty()) {
            continue;
        }
        if record.len() != HEADER.len() || record.iter().any(|cell| cell.len() > 1_000) {
            error(
                &mut parsed.errors,
                Some(line),
                "Expected exactly 12 bounded columns.",
            );
            continue;
        }
        let cells = record
            .iter()
            .map(|cell| cell.trim().to_owned())
            .collect::<Vec<_>>();
        let parsed_row = (|| -> Result<StatementRow, String> {
            let stamp = NaiveDateTime::parse_from_str(
                &format!("{} {}", cells[0], cells[1]),
                "%d-%m-%Y %H:%M",
            )
            .map_err(|_| "Use DD-MM-YYYY dates and HH:MM times.".to_owned())?;
            let value_date = NaiveDate::parse_from_str(&cells[2], "%d-%m-%Y")
                .map_err(|_| "Use DD-MM-YYYY dates and HH:MM times.".to_owned())?;
            if stamp.date().max(value_date) > Utc::now().date_naive() {
                return Err("Future statement dates are not supported.".into());
            }
            if previous.is_some_and(|prior| stamp > prior) {
                return Err(
                    "Export the account statement newest first; row order is used to resolve equal timestamps."
                        .into(),
                );
            }
            if !cells[6].is_empty() {
                return Err("FX rows are not supported.".into());
            }
            if cells[9] != "EUR" || !matches!(cells[7].as_str(), "" | "EUR") {
                return Err("Only EUR account balances and EUR cash changes are supported.".into());
            }
            european_number(&cells[10], 2)?;
            let amount = if cells[8].is_empty() {
                None
            } else {
                Some(european_number(&cells[8], 2)?)
            };
            if cells[7].is_empty() == amount.is_some() {
                return Err("Cash change must include both currency and amount.".into());
            }
            Ok(StatementRow {
                line,
                cells,
                stamp,
                value_date,
                amount,
            })
        })();
        match parsed_row {
            Ok(row) => {
                previous = Some(row.stamp);
                rows.push(row);
            }
            Err(message) => error(&mut parsed.errors, Some(line), message),
        }
    }
    if rows.is_empty() {
        error(
            &mut parsed.errors,
            None,
            "The statement contains no account rows.",
        );
    }

    let mut trades = HashMap::<String, (StatementRow, Value)>::new();
    let mut fees = HashMap::<String, Vec<StatementRow>>::new();
    let mut reservations = Vec::new();
    let mut deposits = Vec::new();
    let mut sweeps = Vec::new();
    let mut transfers = Vec::new();
    let mut identities = HashSet::new();

    for row in rows {
        let description = row.description().to_lowercase();
        let result = (|| -> Result<(), String> {
            if let Some(captures) = TRADE.captures(row.description()) {
                if row.order().is_empty() || Uuid::parse_str(row.order()).is_err() {
                    return Err("A trade must have an Order Id for safe repeated imports.".into());
                }
                if trades.contains_key(row.order()) {
                    return Err(
                        "Multiple executions for one order are not supported; import this order manually."
                            .into(),
                    );
                }
                let quantity = european_number(&captures[2], 8)?;
                let unit_price = european_number(&captures[3], 8)?;
                let currency = &captures[4];
                if !currency.eq_ignore_ascii_case("EUR")
                    || quantity <= Decimal::ZERO
                    || unit_price <= Decimal::ZERO
                {
                    return Err(
                        "Only positive-quantity, positive-price EUR trades are supported.".into(),
                    );
                }
                let kind = if matches!(captures[1].to_lowercase().as_str(), "koop" | "buy") {
                    "buy"
                } else {
                    "sell"
                };
                let gross = (quantity * unit_price)
                    .round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero);
                let expected = if kind == "buy" { -gross } else { gross };
                if gross <= Decimal::ZERO || row.amount != Some(expected) {
                    return Err(
                        "Trade cash change does not match quantity × execution price.".into(),
                    );
                }
                let body = json!({
                    "type": kind,
                    "date": row.stamp.date(),
                    "instrument_id": row.cells[4],
                    "quantity": quantity.normalize().to_string(),
                    "price_eur": unit_price.normalize().to_string(),
                    "commission_eur": "0.00",
                });
                trades.insert(row.order().to_owned(), (row.clone(), body));
            } else if matches!(
                description.as_str(),
                "degiro transactiekosten en/of kosten van derden"
                    | "degiro transaction and/or third party fees"
            ) {
                if row.order().is_empty()
                    || row.amount.is_none()
                    || row.amount.is_some_and(|value| value > Decimal::ZERO)
                {
                    return Err("Trade fees need an Order Id and a non-positive EUR change.".into());
                }
                fees.entry(row.order().to_owned())
                    .or_default()
                    .push(row.clone());
            } else if description == "flatex interest income" {
                if !row.cells[4].is_empty()
                    || !row.order().is_empty()
                    || row.amount != Some(Decimal::ZERO)
                {
                    return Err("Only zero-value Flatex Interest Income without an ISIN or Order Id can be ignored; non-zero interest is not supported.".into());
                }
                ignore(
                    &mut parsed.ignored,
                    &row,
                    "Zero-value Flatex interest income; no cash or position change.",
                );
            } else if description == "reservation ideal" {
                if !row.cells[4].is_empty()
                    || !row.order().is_empty()
                    || row.amount.is_none_or(|value| value == Decimal::ZERO)
                {
                    return Err("Invalid iDEAL reservation.".into());
                }
                reservations.push(row.clone());
            } else if matches!(
                description.as_str(),
                "ideal deposit" | "deposit" | "storting" | "withdrawal" | "terugstorting"
            ) {
                let is_deposit = matches!(
                    description.as_str(),
                    "ideal deposit" | "deposit" | "storting"
                );
                if !row.cells[4].is_empty()
                    || !row.order().is_empty()
                    || row.amount.is_none_or(|value| value == Decimal::ZERO)
                {
                    return Err("Invalid external cash movement.".into());
                }
                if row.amount.is_some_and(|value| {
                    (is_deposit && value < Decimal::ZERO) || (!is_deposit && value > Decimal::ZERO)
                }) {
                    return Err("Cash movement sign does not match its description.".into());
                }
                deposits.push(row.clone());
            } else if description == "degiro cash sweep transfer" {
                if !row.cells[4].is_empty()
                    || !row.order().is_empty()
                    || row.amount.is_none_or(|value| value == Decimal::ZERO)
                {
                    return Err("Invalid internal cash sweep.".into());
                }
                sweeps.push(row.clone());
            } else if let Some(captures) = TRANSFER.captures(row.description()) {
                if european_number(&captures["amount"], 2)? <= Decimal::ZERO {
                    return Err(
                        "Internal transfer amount must be positive; direction is recorded in its description."
                            .into(),
                    );
                }
                if !row.cells[4].is_empty() || !row.order().is_empty() || row.amount.is_some() {
                    return Err("Unrecognized flatex cash-account transfer structure.".into());
                }
                transfers.push(row.clone());
            } else {
                return Err("Unsupported account activity. Dividends, interest, taxes, FX and corporate actions must not be silently skipped.".into());
            }
            Ok(())
        })();
        if let Err(message) = result {
            error(&mut parsed.errors, Some(row.line), message);
        }
    }

    for (order, (row, mut body)) in trades {
        let associated = fees.remove(&order).unwrap_or_default();
        let unique = associated
            .iter()
            .map(|fee| fee.cells[..9].to_vec())
            .collect::<HashSet<_>>();
        if unique.len() != associated.len() {
            error(
                &mut parsed.errors,
                Some(row.line),
                "Indistinguishable duplicate fee rows; review this order manually.",
            );
        }
        if associated
            .iter()
            .any(|fee| fee.cells[4] != row.cells[4] || fee.stamp != row.stamp)
        {
            error(
                &mut parsed.errors,
                Some(row.line),
                "Fees must match the trade's ISIN and timestamp.",
            );
        }
        let fee = -associated
            .iter()
            .filter_map(|row| row.amount)
            .sum::<Decimal>();
        body["commission_eur"] = json!(format!("{fee:.2}"));
        let mut sources = vec![row.line];
        sources.extend(associated.iter().map(|fee| fee.line));
        candidate(
            &mut parsed,
            &mut identities,
            &row,
            body,
            sources,
            format!("order:{}", Uuid::parse_str(&order).unwrap()),
            None,
        );
    }
    for group in fees.into_values() {
        for row in group {
            error(
                &mut parsed.errors,
                Some(row.line),
                "Orphan trade fee: export the associated order in the same statement.",
            );
        }
    }

    let mut used_reservations = HashSet::new();
    for row in deposits {
        let mut effective = row.clone();
        let mut sources = vec![row.line];
        if row.description().eq_ignore_ascii_case("ideal deposit") {
            let reversals = reservations
                .iter()
                .filter(|item| {
                    item.amount == row.amount.map(|value| -value)
                        && item.stamp == row.stamp
                        && item.value_date == row.value_date
                })
                .collect::<Vec<_>>();
            let originals = reservations
                .iter()
                .filter(|item| {
                    item.amount == row.amount
                        && item.value_date == row.value_date
                        && item.stamp <= row.stamp
                })
                .collect::<Vec<_>>();
            if !reversals.is_empty() || !originals.is_empty() {
                let reused = reversals
                    .iter()
                    .chain(&originals)
                    .any(|item| used_reservations.contains(&item.line));
                if reversals.len() != 1 || originals.len() != 1 || reused {
                    error(
                        &mut parsed.errors,
                        Some(row.line),
                        "Ambiguous/incomplete iDEAL reservation settlement; export the complete funding period.",
                    );
                    continue;
                }
                effective = originals[0].clone();
                for item in reversals.iter().chain(&originals) {
                    used_reservations.insert(item.line);
                    sources.push(item.line);
                    ignore(
                        &mut parsed.ignored,
                        item,
                        "Matched iDEAL reservation/reversal; the final deposit is counted once at funding time.",
                    );
                }
            }
        }
        let amount = row.amount.unwrap_or_default();
        let body = json!({
            "type": if amount > Decimal::ZERO { "deposit" } else { "withdrawal" },
            "date": effective.stamp.date(),
            "amount_eur": format!("{:.2}", amount.abs()),
            "currency": "EUR",
        });
        let identity = format!(
            "cash:{}",
            hash_json(&json!([
                row.stamp.to_string(),
                row.value_date,
                row.description().to_lowercase(),
                format!("{amount:.2}"),
            ]))
        );
        candidate(
            &mut parsed,
            &mut identities,
            &row,
            body,
            sources,
            identity,
            Some(&effective),
        );
    }
    for row in reservations {
        if !used_reservations.contains(&row.line) {
            error(
                &mut parsed.errors,
                Some(row.line),
                "Unmatched iDEAL reservation: include the final deposit and its reversal in the export.",
            );
        }
    }

    let mut used_transfers = HashSet::new();
    for row in sweeps {
        let matches = transfers
            .iter()
            .filter(|item| {
                if item.stamp != row.stamp || item.value_date != row.value_date {
                    return false;
                }
                let Some(captures) = TRANSFER.captures(item.description()) else {
                    return false;
                };
                let Ok(amount) = european_number(&captures["amount"], 2) else {
                    return false;
                };
                let direction = captures
                    .name("nl")
                    .or_else(|| captures.name("en"))
                    .map(|value| value.as_str().to_lowercase());
                amount == row.amount.unwrap_or_default().abs()
                    && matches!(direction.as_deref(), Some("van" | "from"))
                        == row.amount.is_some_and(|value| value > Decimal::ZERO)
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 || used_transfers.contains(&matches[0].line) {
            error(
                &mut parsed.errors,
                Some(row.line),
                "Unmatched internal cash sweep; include its flatex cash-account counterpart.",
            );
            continue;
        }
        used_transfers.insert(matches[0].line);
        for item in [&row, matches[0]] {
            ignore(
                &mut parsed.ignored,
                item,
                "Internal DEGIRO/flatex cash sweep, not an external deposit or withdrawal.",
            );
        }
    }
    for row in transfers {
        if !used_transfers.contains(&row.line) {
            error(
                &mut parsed.errors,
                Some(row.line),
                "Unmatched flatex cash-account transfer.",
            );
        }
    }

    parsed
        .candidates
        .sort_by_key(|item| (item.stamp, std::cmp::Reverse(item.line)));
    parsed
        .ignored
        .sort_by_key(|item| item["row"].as_u64().unwrap_or_default());
    if parsed.candidates.is_empty() && parsed.errors.is_empty() {
        error(
            &mut parsed.errors,
            None,
            "No supported external cash movements or trades were found.",
        );
    }
    parsed
}

fn hash_json(value: &Value) -> String {
    let bytes = serde_json::to_vec(value).unwrap_or_default();
    format!("{:x}", Sha256::digest(bytes))
}

struct PreparedImport {
    result: Value,
    additions: Vec<LedgerEvent>,
    revision: i64,
}

async fn prepare(
    state: &AppState,
    portfolio: &Portfolio,
    parsed: ParsedStatement,
) -> AppResult<PreparedImport> {
    let existing_events = portfolio::load_events(&state.pool, &portfolio.id).await?;
    let existing = existing_events
        .iter()
        .map(|event| (event.client_id.clone(), event))
        .collect::<HashMap<_, _>>();
    let mut additions = Vec::new();
    let mut transactions = Vec::new();
    let mut errors = parsed.errors;
    let mut deposits = Decimal::ZERO;
    let mut withdrawals = Decimal::ZERO;
    let mut commissions = Decimal::ZERO;
    let mut duplicates = 0_usize;
    let mut sequence = existing_events
        .iter()
        .map(|event| event.sequence)
        .max()
        .unwrap_or(0);

    for candidate in &parsed.candidates {
        let mut public = candidate.body.clone();
        public
            .as_object_mut()
            .expect("candidate object")
            .remove("client_id");
        public["source_rows"] = json!(candidate.source_rows);
        public["status"] = json!("new");
        match portfolio::parse_new_event(&state.pool, candidate.body.clone()).await {
            Err(problem) => error(
                &mut errors,
                candidate.source_rows.first().copied(),
                problem.to_string(),
            ),
            Ok(new) => {
                sequence += 1;
                let mut event = portfolio::event_from_new(&new, sequence);
                if let Some(prior) = existing.get(&event.client_id) {
                    if prior.request != event.request {
                        error(
                            &mut errors,
                            candidate.source_rows.first().copied(),
                            "An imported transaction has changed. Check the order/statement; it cannot be imported twice with different amounts or dates.",
                        );
                    } else if prior.removed_at.is_some() {
                        public["status"] = json!("removed");
                        error(
                            &mut errors,
                            candidate.source_rows.first().copied(),
                            "This imported transaction was removed and cannot be silently restored.",
                        );
                    } else {
                        public["status"] = json!("duplicate");
                        duplicates += 1;
                        if let Some(instrument) = prior.public().get("instrument") {
                            public["instrument"] = instrument.clone();
                        }
                    }
                } else {
                    event.source = Some("degiro".into());
                    event.source_rows = Some(json!(candidate.source_rows));
                    if let Some(instrument) = event.public().get("instrument") {
                        public["instrument"] = instrument.clone();
                    }
                    match event.transaction_type.as_str() {
                        "deposit" => deposits += event.amount_eur.unwrap_or_default(),
                        "withdrawal" => withdrawals += event.amount_eur.unwrap_or_default(),
                        _ => commissions += event.commission_eur.unwrap_or_default(),
                    }
                    additions.push(event);
                }
            }
        }
        transactions.push(public);
    }
    if existing_events.len() + additions.len() > MAX_TRANSACTIONS {
        error(
            &mut errors,
            None,
            format!(
                "Import would exceed the {MAX_TRANSACTIONS}-transaction lifetime limit (including removed entries)."
            ),
        );
    }
    let mut cash = Value::Null;
    let mut positions = Vec::new();
    if errors.is_empty() {
        let mut all = existing_events.clone();
        all.extend(additions.clone());
        match portfolio::replay(&all, &HashMap::new()) {
            Ok(ledger) => {
                cash = json!(format!("{:.2}", ledger.cash));
                positions = ledger
                    .positions
                    .into_iter()
                    .filter(|(_, position)| position.quantity != Decimal::ZERO)
                    .map(|(instrument_id, position)| {
                        json!({
                            "instrument_id": instrument_id,
                            "quantity": position.quantity.normalize().to_string(),
                            "cost_basis_eur": format!("{:.2}", position.cost),
                        })
                    })
                    .collect();
                positions.sort_by(|left, right| {
                    left["instrument_id"]
                        .as_str()
                        .cmp(&right["instrument_id"].as_str())
                });
            }
            Err(problem) => error(
                &mut errors,
                None,
                format!(
                    "{} Import complete funding history; no opening cash is invented.",
                    problem
                ),
            ),
        }
    }
    let mut result = json!({
        "can_import": errors.is_empty(),
        "transactions": transactions,
        "ignored_rows": parsed.ignored,
        "errors": errors,
        "warnings": [
            "ISINs do not identify an exchange. Verify every matched acquired EUR listing before confirming.",
            "Only new entries are added. Existing same-day entries retain their original order; use a separate empty portfolio for initial history.",
            "Cash entries without broker IDs are matched by timestamp, value date, description and amount. Indistinguishable movements need manual review.",
            "Running broker balances include internal cash sweeps and are not used as opening cash or a portfolio valuation. Manual entries are not deduplicated against imports."
        ],
        "summary": {
            "new_count": additions.len(),
            "duplicate_count": duplicates,
            "cash_eur": cash,
            "positions": positions,
            "deposits_eur": format!("{deposits:.2}"),
            "withdrawals_eur": format!("{withdrawals:.2}"),
            "commission_eur": format!("{commissions:.2}"),
        }
    });
    let token_material = json!({
        "version": "degiro-v1",
        "portfolio_id": portfolio.id,
        "user_id": portfolio.user_id,
        "revision": portfolio.revision,
        "candidates": parsed.candidates.iter().map(|item| &item.body).collect::<Vec<_>>(),
        "result": result,
    });
    result["preview_token"] = json!(hash_json(&token_material));
    Ok(PreparedImport {
        result,
        additions,
        revision: portfolio.revision,
    })
}

async fn preview(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Json(body): Json<PreviewRequest>,
) -> AppResult<Json<Value>> {
    bounded_csv(&body.csv)?;
    let portfolio =
        portfolio::require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    Ok(Json(
        prepare(&state, &portfolio, parse_statement(&body.csv))
            .await?
            .result,
    ))
}

async fn import_statement(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Json(body): Json<ImportRequest>,
) -> AppResult<impl IntoResponse> {
    bounded_csv(&body.csv)?;
    if body.preview_token.len() != 64
        || !body
            .preview_token
            .bytes()
            .all(|value| value.is_ascii_hexdigit() && !value.is_ascii_uppercase())
    {
        return Err(AppError::unprocessable("Invalid preview_token"));
    }
    let portfolio =
        portfolio::require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    let prepared = prepare(&state, &portfolio, parse_statement(&body.csv)).await?;
    if prepared.result["can_import"] != Value::Bool(true) {
        return Err(AppError::unprocessable(
            "Import blocked. Preview again and resolve every reported issue; nothing was saved.",
        ));
    }
    if prepared.result["preview_token"].as_str() != Some(&body.preview_token) {
        return Err(AppError::conflict(
            "The portfolio or import preview changed. Preview again before confirming.",
        ));
    }
    let duplicate_count = prepared.result["summary"]["duplicate_count"]
        .as_u64()
        .unwrap_or_default();
    if prepared.additions.is_empty() {
        return Ok((
            StatusCode::OK,
            Json(json!({"imported_count": 0, "duplicate_count": duplicate_count})),
        ));
    }
    let mut transaction = state.pool.begin().await?;
    let locked =
        portfolio::lock_portfolio(&mut transaction, &user.id, &customer_id, &portfolio_id).await?;
    if locked.revision != prepared.revision {
        return Err(AppError::conflict(
            "The portfolio or import preview changed. Preview again before confirming.",
        ));
    }
    for event in &prepared.additions {
        sqlx::query(
            "INSERT INTO portfolio_transactions
                (id, portfolio_id, client_id, sequence, transaction_type, transaction_date,
                 amount_eur, quantity, price_eur, commission_eur,
                 instrument_id, instrument_name, symbol, exchange, currency,
                 created_at, source, source_rows, request)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                     $11, $12, $13, $14, $15, $16, $17, $18, $19)",
        )
        .bind(&event.id)
        .bind(&portfolio_id)
        .bind(&event.client_id)
        .bind(event.sequence)
        .bind(&event.transaction_type)
        .bind(event.transaction_date)
        .bind(event.amount_eur)
        .bind(event.quantity)
        .bind(event.price_eur)
        .bind(event.commission_eur)
        .bind(&event.instrument_id)
        .bind(&event.instrument_name)
        .bind(&event.symbol)
        .bind(&event.exchange)
        .bind(&event.currency)
        .bind(event.created_at)
        .bind(&event.source)
        .bind(&event.source_rows)
        .bind(&event.request)
        .execute(&mut *transaction)
        .await?;
    }
    sqlx::query("UPDATE portfolios SET revision = revision + 1 WHERE id = $1")
        .bind(&portfolio_id)
        .execute(&mut *transaction)
        .await?;
    transaction.commit().await?;
    Ok((
        StatusCode::OK,
        Json(json!({
            "imported_count": prepared.additions.len(),
            "duplicate_count": duplicate_count,
        })),
    ))
}
