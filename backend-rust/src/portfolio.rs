use std::{collections::HashMap, str::FromStr, sync::LazyLock};

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get},
    Json, Router,
};
use chrono::{DateTime, Datelike, NaiveDate, Utc};
use regex::Regex;
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::{FromRow, PgPool, Postgres, Row, Transaction};
use uuid::Uuid;

use crate::{auth::CurrentUser, AppError, AppResult, AppState};

const MAX_TRANSACTIONS: i64 = 5_000;

static MAX_AMOUNT: LazyLock<Decimal> = LazyLock::new(|| Decimal::from(1_000_000_000_000_u64));
static ISIN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$").expect("valid ISIN regex"));

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/portfolio-instruments", get(portfolio_instruments))
        .route(
            "/customers/{customer_id}/portfolios",
            get(list_portfolios).post(create_portfolio),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}",
            get(get_portfolio).patch(update_portfolio),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/plan-tracking",
            get(plan_tracking),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/transactions",
            get(list_transactions).post(add_transaction),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/transactions/{transaction_id}",
            delete(remove_transaction),
        )
}

#[derive(Debug, Deserialize)]
struct Pagination {
    limit: Option<i64>,
    offset: Option<i64>,
}

fn pagination(query: Pagination) -> AppResult<(i64, i64)> {
    let limit = query.limit.unwrap_or(20);
    let offset = query.offset.unwrap_or(0);
    if !(1..=100).contains(&limit) || offset < 0 {
        return Err(AppError::unprocessable("Invalid pagination parameters"));
    }
    Ok((limit, offset))
}

pub async fn require_customer(pool: &PgPool, user_id: &str, customer_id: &str) -> AppResult<()> {
    let exists = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS(
             SELECT 1 FROM planner_customers WHERE id = $1 AND user_id = $2
         )",
    )
    .bind(customer_id)
    .bind(user_id)
    .fetch_one(pool)
    .await?;
    if !exists {
        return Err(AppError::not_found("Customer not found."));
    }
    Ok(())
}

async fn require_plan(
    pool: &PgPool,
    user_id: &str,
    customer_id: &str,
    plan_id: &str,
) -> AppResult<()> {
    let exists = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS(
             SELECT 1 FROM planner_simulations
             WHERE id = $1 AND user_id = $2 AND customer_id = $3
               AND is_plan = true AND saved_at IS NOT NULL
         )",
    )
    .bind(plan_id)
    .bind(user_id)
    .bind(customer_id)
    .fetch_one(pool)
    .await?;
    if !exists {
        return Err(AppError::not_found("Plan not found."));
    }
    Ok(())
}

#[derive(Debug, FromRow, Clone)]
pub struct Portfolio {
    pub id: String,
    pub user_id: String,
    pub customer_id: String,
    pub name: String,
    pub plan_id: Option<String>,
    pub currency: String,
    pub created_at: DateTime<Utc>,
    pub revision: i64,
}

pub(crate) async fn require_portfolio(
    pool: &PgPool,
    user_id: &str,
    customer_id: &str,
    portfolio_id: &str,
) -> AppResult<Portfolio> {
    require_customer(pool, user_id, customer_id).await?;
    sqlx::query_as::<_, Portfolio>(
        "SELECT id, user_id, customer_id, name, plan_id, currency, created_at, revision
         FROM portfolios
         WHERE id = $1 AND user_id = $2 AND customer_id = $3",
    )
    .bind(portfolio_id)
    .bind(user_id)
    .bind(customer_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Portfolio not found."))
}

pub(crate) async fn lock_portfolio(
    transaction: &mut Transaction<'_, Postgres>,
    user_id: &str,
    customer_id: &str,
    portfolio_id: &str,
) -> AppResult<Portfolio> {
    sqlx::query_as::<_, Portfolio>(
        "SELECT id, user_id, customer_id, name, plan_id, currency, created_at, revision
         FROM portfolios
         WHERE id = $1 AND user_id = $2 AND customer_id = $3
         FOR UPDATE",
    )
    .bind(portfolio_id)
    .bind(user_id)
    .bind(customer_id)
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| AppError::not_found("Portfolio not found."))
}

fn metadata(portfolio: &Portfolio) -> Value {
    json!({
        "id": portfolio.id,
        "customer_id": portfolio.customer_id,
        "name": portfolio.name,
        "plan_id": portfolio.plan_id,
        "currency": portfolio.currency,
        "created_at": portfolio.created_at,
        "revision": portfolio.revision,
    })
}

#[derive(Debug, FromRow, Clone)]
pub(crate) struct LedgerEvent {
    pub(crate) id: String,
    pub(crate) client_id: String,
    pub(crate) sequence: i32,
    pub(crate) transaction_type: String,
    pub(crate) transaction_date: NaiveDate,
    pub(crate) amount_eur: Option<Decimal>,
    pub(crate) quantity: Option<Decimal>,
    pub(crate) price_eur: Option<Decimal>,
    pub(crate) commission_eur: Option<Decimal>,
    pub(crate) instrument_id: Option<String>,
    pub(crate) instrument_name: Option<String>,
    pub(crate) symbol: Option<String>,
    pub(crate) exchange: Option<String>,
    pub(crate) currency: Option<String>,
    pub(crate) created_at: DateTime<Utc>,
    pub(crate) removed_at: Option<DateTime<Utc>>,
    pub(crate) source: Option<String>,
    pub(crate) source_rows: Option<Value>,
    pub(crate) request: Value,
}

impl LedgerEvent {
    fn instrument(&self) -> Option<Instrument> {
        Some(Instrument {
            instrument_id: self.instrument_id.clone()?,
            name: self.instrument_name.clone()?,
            symbol: self.symbol.clone()?,
            exchange: self.exchange.clone()?,
            currency: self.currency.clone()?,
        })
    }

    pub(crate) fn public(&self) -> Value {
        let mut value = json!({
            "id": self.id,
            "client_id": self.client_id,
            "sequence": self.sequence,
            "type": self.transaction_type,
            "date": self.transaction_date,
            "created_at": self.created_at,
            "source": self.source,
        });
        let object = value.as_object_mut().expect("event object");
        if let Some(source_rows) = &self.source_rows {
            object.insert("source_rows".into(), source_rows.clone());
        }
        if matches!(self.transaction_type.as_str(), "deposit" | "withdrawal") {
            object.insert(
                "amount_eur".into(),
                json!(money(self.amount_eur.unwrap_or_default())),
            );
            object.insert("currency".into(), json!("EUR"));
        } else {
            object.insert("instrument".into(), json!(self.instrument()));
            object.insert(
                "quantity".into(),
                json!(decimal(self.quantity.unwrap_or_default())),
            );
            object.insert(
                "price_eur".into(),
                json!(decimal(self.price_eur.unwrap_or_default())),
            );
            object.insert(
                "commission_eur".into(),
                json!(money(self.commission_eur.unwrap_or_default())),
            );
        }
        value
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub(crate) struct Instrument {
    pub(crate) instrument_id: String,
    pub(crate) name: String,
    pub(crate) symbol: String,
    pub(crate) exchange: String,
    pub(crate) currency: String,
}

#[derive(Debug, Clone)]
pub(crate) struct Position {
    pub(crate) instrument: Instrument,
    pub(crate) quantity: Decimal,
    pub(crate) cost: Decimal,
    pub(crate) reference: Decimal,
}

#[derive(Debug)]
pub(crate) struct LedgerState {
    pub(crate) cash: Decimal,
    pub(crate) realized: Decimal,
    pub(crate) positions: HashMap<String, Position>,
}

pub(crate) fn replay(
    events: &[LedgerEvent],
    anchors: &HashMap<String, (NaiveDate, Decimal)>,
) -> AppResult<LedgerState> {
    let mut ordered = events
        .iter()
        .filter(|event| event.removed_at.is_none())
        .collect::<Vec<_>>();
    ordered.sort_by_key(|event| (event.transaction_date, event.sequence));
    let mut cash = Decimal::ZERO;
    let mut realized = Decimal::ZERO;
    let mut positions = HashMap::<String, Position>::new();
    for event in ordered {
        match event.transaction_type.as_str() {
            "deposit" => cash += event.amount_eur.unwrap_or_default(),
            "withdrawal" => cash -= event.amount_eur.unwrap_or_default(),
            "buy" | "sell" => {
                let instrument = event.instrument().ok_or_else(|| {
                    AppError::Internal("trade event has incomplete instrument identity".into())
                })?;
                let quantity = event.quantity.unwrap_or_default();
                let unit_price = event.price_eur.unwrap_or_default();
                let commission = event.commission_eur.unwrap_or_default();
                let gross = round_money(quantity * unit_price);
                let position =
                    positions
                        .entry(instrument.instrument_id.clone())
                        .or_insert(Position {
                            instrument,
                            quantity: Decimal::ZERO,
                            cost: Decimal::ZERO,
                            reference: Decimal::ZERO,
                        });
                if event.transaction_type == "buy" {
                    cash -= gross + commission;
                    position.quantity += quantity;
                    position.cost += gross;
                    position.reference += anchors
                        .get(&position.instrument.instrument_id)
                        .filter(|(date, _)| event.transaction_date <= *date)
                        .map_or(gross, |(_, price)| quantity * *price);
                    realized -= commission;
                } else {
                    if quantity > position.quantity {
                        return Err(AppError::unprocessable(format!(
                            "Insufficient holdings on {} for {}; short positions are not supported.",
                            event.transaction_date, position.instrument.instrument_id
                        )));
                    }
                    let all = quantity == position.quantity;
                    let cost = if all {
                        position.cost
                    } else {
                        position.cost * quantity / position.quantity
                    };
                    let reference = if all {
                        position.reference
                    } else {
                        position.reference * quantity / position.quantity
                    };
                    cash += gross - commission;
                    realized += gross - cost - commission;
                    position.quantity -= quantity;
                    position.cost -= cost;
                    position.reference -= reference;
                }
            }
            _ => return Err(AppError::Internal("unsupported transaction type".into())),
        }
        if cash < Decimal::ZERO {
            return Err(AppError::unprocessable(format!(
                "Insufficient EUR cash on {}. Add a deposit before this transaction.",
                event.transaction_date
            )));
        }
    }
    Ok(LedgerState {
        cash,
        realized,
        positions,
    })
}

fn round_money(value: Decimal) -> Decimal {
    value.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero)
}

fn money(value: Decimal) -> String {
    format!("{:.2}", round_money(value))
}

fn decimal(value: Decimal) -> String {
    value.normalize().to_string()
}

pub(crate) async fn load_events(pool: &PgPool, portfolio_id: &str) -> AppResult<Vec<LedgerEvent>> {
    Ok(sqlx::query_as::<_, LedgerEvent>(
        "SELECT id, client_id, sequence, transaction_type, transaction_date,
                amount_eur, quantity, price_eur, commission_eur,
                instrument_id, instrument_name, symbol, exchange, currency,
                created_at, removed_at, source, source_rows, request
         FROM portfolio_transactions
         WHERE portfolio_id = $1
         ORDER BY transaction_date, sequence",
    )
    .bind(portfolio_id)
    .fetch_all(pool)
    .await?)
}

#[derive(Debug, Clone, Copy)]
struct DailyPair {
    previous_date: NaiveDate,
    previous: Decimal,
    latest_date: NaiveDate,
    latest: Decimal,
}

#[derive(Debug, Clone)]
struct QuoteMark {
    latest_date: NaiveDate,
    price_date: NaiveDate,
    latest: Decimal,
    stale: bool,
    daily_pairs: Vec<DailyPair>,
}

async fn quote_mark(pool: &PgPool, instrument: &Instrument) -> AppResult<Option<QuoteMark>> {
    let state = sqlx::query_as::<_, (Option<Value>, Option<String>)>(
        "SELECT listing, price_currency_override
         FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(&instrument.instrument_id)
    .fetch_optional(pool)
    .await?
    .unwrap_or((None, None));
    let expected = json!({"symbol": instrument.symbol, "exchange": instrument.exchange});
    if state.0.as_ref() != Some(&expected) {
        return Ok(None);
    }
    let generation = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
           AND listing = $2
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC
         LIMIT 1",
    )
    .bind(&instrument.instrument_id)
    .bind(&expected)
    .fetch_optional(pool)
    .await?;
    let Some(generation) = generation else {
        return Ok(None);
    };
    let raw_rows = sqlx::query_as::<_, (NaiveDate, Option<Decimal>, Option<String>)>(
        "SELECT quote_date, close, price_currency
         FROM quotes
         WHERE job_id = $1 AND quote_date <= CURRENT_DATE
         ORDER BY quote_date
         LIMIT 20001",
    )
    .bind(generation)
    .fetch_all(pool)
    .await?;
    if raw_rows.len() > 20_000 {
        return Err(AppError::unprocessable(
            "Saved history exceeds the 20,000-date valuation limit.",
        ));
    }
    let mut rows = raw_rows
        .into_iter()
        .map(|(date, close, currency)| crate::quote_fill::QuoteRow {
            date,
            close,
            adjusted: None,
            currency,
            carried_from_date: None,
        })
        .collect::<Vec<_>>();
    let policy =
        crate::quote_fill::Policy::from_json(&crate::market_data::quote_fill_policy(pool).await?);
    crate::quote_fill::fill(
        &mut rows,
        &instrument.instrument_id,
        &policy,
        state.1.as_deref(),
        &[crate::quote_fill::PriceField::Close],
    );
    crate::quote_fill::carry_trailing_smooth(&mut rows, &policy);
    if !rows
        .iter()
        .all(|row| currency_supported(row.currency.as_deref(), state.1.as_deref()))
    {
        return Ok(None);
    }
    let Some(latest_row) = rows.last() else {
        return Ok(None);
    };
    let Some(latest) = latest_row.close.filter(|value| *value > Decimal::ZERO) else {
        return Ok(None);
    };
    let daily_pairs = rows
        .windows(2)
        .rev()
        .filter_map(|pair| {
            let previous = &pair[0];
            let latest = &pair[1];
            if previous.carried_from_date.is_some()
                || latest.carried_from_date.is_some()
                || !daily_interval(previous.date, latest.date)
            {
                return None;
            }
            Some(DailyPair {
                previous_date: previous.date,
                previous: previous.close.filter(|value| *value > Decimal::ZERO)?,
                latest_date: latest.date,
                latest: latest.close.filter(|value| *value > Decimal::ZERO)?,
            })
        })
        .collect::<Vec<_>>();
    Ok(Some(QuoteMark {
        latest_date: latest_row.date,
        price_date: latest_row.carried_from_date.unwrap_or(latest_row.date),
        latest,
        stale: latest_row.carried_from_date.is_some(),
        daily_pairs,
    }))
}

fn currency_supported(provider: Option<&str>, override_currency: Option<&str>) -> bool {
    match provider.map(str::trim).filter(|value| !value.is_empty()) {
        Some(value) => value.eq_ignore_ascii_case("EUR"),
        None => override_currency.is_some_and(|value| value.eq_ignore_ascii_case("EUR")),
    }
}

fn daily_interval(previous: NaiveDate, latest: NaiveDate) -> bool {
    let gap = (latest - previous).num_days();
    previous.weekday().number_from_monday() <= 5
        && latest.weekday().number_from_monday() <= 5
        && (1..=3).contains(&gap)
        && (1..=gap)
            .filter(|step| {
                (previous + chrono::Duration::days(*step))
                    .weekday()
                    .number_from_monday()
                    <= 5
            })
            .count()
            == 1
}

pub async fn valuation(pool: &PgPool, portfolio: &Portfolio) -> AppResult<Value> {
    let events = load_events(pool, &portfolio.id).await?;
    let initial = replay(&events, &HashMap::new())?;
    let mut marks = HashMap::new();
    for position in initial
        .positions
        .values()
        .filter(|position| position.quantity != Decimal::ZERO)
    {
        if let Some(mark) = quote_mark(pool, &position.instrument).await? {
            marks.insert(position.instrument.instrument_id.clone(), mark);
        }
    }
    let mut selected_pairs = marks
        .iter()
        .filter_map(|(instrument_id, mark)| {
            mark.daily_pairs
                .first()
                .copied()
                .map(|pair| (instrument_id.clone(), pair))
        })
        .collect::<HashMap<_, _>>();
    if marks.len() > 1 {
        let mut common = marks.values().map(|mark| {
            mark.daily_pairs
                .iter()
                .map(|pair| (pair.previous_date, pair.latest_date))
                .collect::<HashSet<_>>()
        });
        if let Some(mut intersection) = common.next() {
            for intervals in common {
                intersection.retain(|interval| intervals.contains(interval));
            }
            if let Some(interval) = intersection.into_iter().max() {
                selected_pairs = marks
                    .iter()
                    .filter_map(|(instrument_id, mark)| {
                        mark.daily_pairs
                            .iter()
                            .find(|pair| (pair.previous_date, pair.latest_date) == interval)
                            .copied()
                            .map(|pair| (instrument_id.clone(), pair))
                    })
                    .collect();
            }
        }
    }
    let anchors = selected_pairs
        .iter()
        .map(|(instrument_id, pair)| (instrument_id.clone(), (pair.previous_date, pair.previous)))
        .collect::<HashMap<_, _>>();
    let marked = replay(&events, &anchors)?;
    let mut items = Vec::new();
    let mut positions_value = Decimal::ZERO;
    let mut unrealized = Decimal::ZERO;
    let mut daily = Decimal::ZERO;
    let mut daily_positions_value = Decimal::ZERO;
    let mut cost_basis = Decimal::ZERO;
    let mut complete = true;
    let mut daily_complete = true;
    let mut periods = HashMap::<(Option<NaiveDate>, Option<NaiveDate>), usize>::new();
    let mut warnings = Vec::new();
    for position in marked
        .positions
        .values()
        .filter(|position| position.quantity != Decimal::ZERO)
    {
        cost_basis += position.cost;
        let mark = marks.get(&position.instrument.instrument_id);
        let value = mark.map(|mark| position.quantity * mark.latest);
        let position_unrealized = value.map(|value| value - position.cost);
        let pair = selected_pairs.get(&position.instrument.instrument_id);
        let position_daily = pair.map(|pair| position.quantity * pair.latest - position.reference);
        let daily_value = pair.map(|pair| position.quantity * pair.latest);
        if let Some(value) = value {
            positions_value += value;
            unrealized += position_unrealized.unwrap_or_default();
        } else {
            complete = false;
        }
        if let (Some(value), Some(marked_value)) = (position_daily, daily_value) {
            daily += value;
            daily_positions_value += marked_value;
        } else {
            daily_complete = false;
        }
        let period = (
            pair.map(|pair| pair.previous_date),
            pair.map(|pair| pair.latest_date),
        );
        *periods.entry(period).or_default() += 1;
        let warning = match mark {
            None => Some("EUR valuation unavailable: latest saved raw close is missing, invalid, or currency is unconfirmed/conflicting.".to_owned()),
            Some(mark) if mark.stale => Some(format!(
                "Stale valuation: using last valid close from {}; saved close missing through {}.",
                mark.price_date, mark.latest_date
            )),
            Some(_) if position_daily.is_none() => Some(
                "Daily unrealized PnL unavailable: two distinct valid EUR closing dates are required."
                    .to_owned(),
            ),
            Some(_) => None,
        };
        if let Some(warning) = &warning {
            warnings.push(format!("{}: {warning}", position.instrument.instrument_id));
        }
        items.push(json!({
            "instrument": position.instrument,
            "quantity": decimal(position.quantity),
            "average_price_eur": format!("{:.8}", position.cost / position.quantity),
            "cost_basis_eur": money(position.cost),
            "market_price_eur": mark.map(|mark| decimal(mark.latest)),
            "price_date": mark.map(|mark| mark.price_date),
            "price_is_stale": mark.is_some_and(|mark| mark.stale),
            "latest_quote_date": mark.map(|mark| mark.latest_date),
            "previous_price_date": pair.map(|pair| pair.previous_date),
            "daily_price_date": pair.map(|pair| pair.latest_date),
            "position_value_eur": value.map(money),
            "unrealized_pnl_eur": position_unrealized.map(money),
            "daily_unrealized_pnl_eur": position_daily.map(money),
            "warning": warning,
            "daily_previous_value_eur": position_daily.zip(daily_value).map(|(daily, value)| money(value - daily)),
        }));
    }
    items.sort_by(|left, right| {
        left["instrument"]["name"]
            .as_str()
            .cmp(&right["instrument"]["name"].as_str())
            .then_with(|| {
                left["instrument"]["instrument_id"]
                    .as_str()
                    .cmp(&right["instrument"]["instrument_id"].as_str())
            })
    });
    if periods.len() > 1 {
        daily_complete = false;
        warnings.push("Portfolio daily unrealized PnL unavailable: positions have different closing-date intervals. Update their market data.".into());
    }
    let period = if daily_complete && periods.len() == 1 {
        periods.keys().next().copied().unwrap_or((None, None))
    } else {
        (None, None)
    };
    let equity = complete.then(|| marked.cash + positions_value);
    Ok(json!({
        "portfolio": metadata(portfolio),
        "cash_eur": money(marked.cash),
        "positions_value_eur": complete.then(|| money(positions_value)),
        "equity_eur": equity.map(money),
        "realized_pnl_eur": money(marked.realized),
        "unrealized_pnl_eur": complete.then(|| money(unrealized)),
        "daily_unrealized_pnl_eur": daily_complete.then(|| money(daily)),
        "cost_basis_eur": money(cost_basis),
        "previous_value_eur": daily_complete.then(|| money(daily_positions_value - daily)),
        "daily_from_date": period.0,
        "daily_to_date": period.1,
        "positions": items,
        "warnings": warnings,
    }))
}

async fn portfolio_summary(pool: &PgPool, portfolio: &Portfolio) -> AppResult<Value> {
    let snapshot = valuation(pool, portfolio).await?;
    let realized = parse_money(snapshot.get("realized_pnl_eur"));
    let unrealized = parse_money(snapshot.get("unrealized_pnl_eur"));
    let total = realized
        .zip(unrealized)
        .map(|(left, right)| money(left + right));
    let mut output = metadata(portfolio).as_object().cloned().unwrap_or_default();
    output.insert("net_worth_eur".into(), snapshot["equity_eur"].clone());
    output.insert(
        "realized_pnl_eur".into(),
        snapshot["realized_pnl_eur"].clone(),
    );
    output.insert(
        "unrealized_pnl_eur".into(),
        snapshot["unrealized_pnl_eur"].clone(),
    );
    output.insert("total_pnl_eur".into(), json!(total));
    output.insert("cost_basis_eur".into(), snapshot["cost_basis_eur"].clone());
    output.insert(
        "previous_value_eur".into(),
        snapshot["previous_value_eur"].clone(),
    );
    output.insert(
        "today_pnl_eur".into(),
        snapshot["daily_unrealized_pnl_eur"].clone(),
    );
    output.insert(
        "today_from_date".into(),
        snapshot["daily_from_date"].clone(),
    );
    output.insert("today_to_date".into(), snapshot["daily_to_date"].clone());
    Ok(Value::Object(output))
}

fn parse_money(value: Option<&Value>) -> Option<Decimal> {
    value?
        .as_str()
        .and_then(|value| Decimal::from_str(value).ok())
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PortfolioBody {
    name: String,
    plan_id: Option<String>,
}

fn validate_portfolio_body(body: &mut PortfolioBody) -> AppResult<()> {
    body.name = body.name.trim().to_owned();
    if body.name.is_empty() || body.name.len() > 200 {
        return Err(AppError::unprocessable("Portfolio name is required"));
    }
    if body
        .plan_id
        .as_ref()
        .is_some_and(|plan_id| plan_id.is_empty() || plan_id.len() > 100)
    {
        return Err(AppError::unprocessable("Invalid plan identifier"));
    }
    Ok(())
}

async fn list_portfolios(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(customer_id): Path<String>,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    require_customer(&state.pool, &user.id, &customer_id).await?;
    let (limit, offset) = pagination(query)?;
    let total = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM portfolios WHERE user_id = $1 AND customer_id = $2",
    )
    .bind(&user.id)
    .bind(&customer_id)
    .fetch_one(&state.pool)
    .await?;
    let portfolios = sqlx::query_as::<_, Portfolio>(
        "SELECT id, user_id, customer_id, name, plan_id, currency, created_at, revision
         FROM portfolios
         WHERE user_id = $1 AND customer_id = $2
         ORDER BY created_at DESC, id DESC
         LIMIT $3 OFFSET $4",
    )
    .bind(&user.id)
    .bind(&customer_id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&state.pool)
    .await?;
    let mut items = Vec::new();
    for portfolio in portfolios {
        items.push(portfolio_summary(&state.pool, &portfolio).await?);
    }
    Ok(Json(
        json!({"items": items, "total": total, "limit": limit, "offset": offset}),
    ))
}

async fn create_portfolio(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(customer_id): Path<String>,
    Json(mut body): Json<PortfolioBody>,
) -> AppResult<impl IntoResponse> {
    validate_portfolio_body(&mut body)?;
    require_customer(&state.pool, &user.id, &customer_id).await?;
    if let Some(plan_id) = body.plan_id.as_deref() {
        require_plan(&state.pool, &user.id, &customer_id, plan_id).await?;
    }
    let id = opaque_id();
    let portfolio = sqlx::query_as::<_, Portfolio>(
        "INSERT INTO portfolios
            (id, user_id, customer_id, name, plan_id, currency, created_at, revision)
         VALUES ($1, $2, $3, $4, $5, 'EUR', now(), 0)
         RETURNING id, user_id, customer_id, name, plan_id, currency, created_at, revision",
    )
    .bind(id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(body.name)
    .bind(body.plan_id)
    .fetch_one(&state.pool)
    .await?;
    Ok((StatusCode::CREATED, Json(metadata(&portfolio))))
}

async fn get_portfolio(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let portfolio = require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    Ok(Json(valuation(&state.pool, &portfolio).await?))
}

async fn update_portfolio(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Json(mut body): Json<PortfolioBody>,
) -> AppResult<Json<Value>> {
    validate_portfolio_body(&mut body)?;
    require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    if let Some(plan_id) = body.plan_id.as_deref() {
        require_plan(&state.pool, &user.id, &customer_id, plan_id).await?;
    }
    let portfolio = sqlx::query_as::<_, Portfolio>(
        "UPDATE portfolios SET name = $4, plan_id = $5, revision = revision + 1
         WHERE id = $1 AND user_id = $2 AND customer_id = $3
         RETURNING id, user_id, customer_id, name, plan_id, currency, created_at, revision",
    )
    .bind(&portfolio_id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(body.name)
    .bind(body.plan_id)
    .fetch_one(&state.pool)
    .await?;
    Ok(Json(metadata(&portfolio)))
}

async fn list_transactions(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    let (limit, offset) = pagination(query)?;
    let total = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM portfolio_transactions
         WHERE portfolio_id = $1 AND removed_at IS NULL",
    )
    .bind(&portfolio_id)
    .fetch_one(&state.pool)
    .await?;
    let rows = sqlx::query_as::<_, LedgerEvent>(
        "SELECT id, client_id, sequence, transaction_type, transaction_date,
                amount_eur, quantity, price_eur, commission_eur,
                instrument_id, instrument_name, symbol, exchange, currency,
                created_at, removed_at, source, source_rows, request
         FROM portfolio_transactions
         WHERE portfolio_id = $1 AND removed_at IS NULL
         ORDER BY transaction_date DESC, sequence DESC
         LIMIT $2 OFFSET $3",
    )
    .bind(&portfolio_id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&state.pool)
    .await?;
    Ok(Json(json!({
        "items": rows.iter().map(LedgerEvent::public).collect::<Vec<_>>(),
        "total": total, "limit": limit, "offset": offset,
    })))
}

async fn acquired_instrument(pool: &PgPool, instrument_id: &str) -> AppResult<Instrument> {
    if !ISIN.is_match(instrument_id) {
        return Err(AppError::unprocessable(
            "Indices are reference data, not tradable portfolio instruments.",
        ));
    }
    let row = sqlx::query(
        "WITH catalogue AS (
             SELECT name FROM etfs WHERE isin = $1 ORDER BY import_date DESC LIMIT 1
         ), publication AS (
             SELECT id, listing FROM acquisition_jobs
             WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
             ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC LIMIT 1
         )
         SELECT catalogue.name, publication.id, publication.listing,
                state.price_currency_override
         FROM catalogue, publication
         JOIN instrument_state state ON state.instrument_id = $1
         WHERE state.listing = publication.listing
           AND EXISTS (SELECT 1 FROM instruments WHERE job_id = publication.id)",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| {
        AppError::unprocessable("Request Yahoo acquisition for this instrument before trading.")
    })?;
    let listing = row.get::<Value, _>("listing");
    let symbol = listing
        .get("symbol")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            AppError::unprocessable("Acquire a EUR instrument listing before trading.")
        })?;
    let exchange = listing
        .get("exchange")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            AppError::unprocessable("Acquire a EUR instrument listing before trading.")
        })?;
    let quote_currencies = sqlx::query_scalar::<_, Option<String>>(
        "SELECT price_currency FROM quotes WHERE job_id = $1",
    )
    .bind(row.get::<String, _>("id"))
    .fetch_all(pool)
    .await?;
    let explicit_currencies = quote_currencies
        .iter()
        .filter_map(|value| value.as_deref())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase)
        .collect::<HashSet<_>>();
    let override_currency = row.get::<Option<String>, _>("price_currency_override");
    let supported = explicit_currencies
        .iter()
        .all(|value| value.eq_ignore_ascii_case("EUR"))
        && override_currency
            .as_deref()
            .is_none_or(|value| value.eq_ignore_ascii_case("EUR"))
        && (override_currency
            .as_deref()
            .is_some_and(|value| value.eq_ignore_ascii_case("EUR"))
            || (!quote_currencies.is_empty()
                && quote_currencies.iter().all(|value| {
                    value
                        .as_deref()
                        .map(str::trim)
                        .is_some_and(|value| value.eq_ignore_ascii_case("EUR"))
                })));
    if !supported {
        return Err(AppError::unprocessable(
            "Only confirmed EUR-priced instruments are supported. Check price currency in instrument details.",
        ));
    }
    Ok(Instrument {
        instrument_id: instrument_id.into(),
        name: row.get::<String, _>("name"),
        symbol: symbol.into(),
        exchange: exchange.into(),
        currency: "EUR".into(),
    })
}

use std::collections::HashSet;

#[derive(Debug)]
pub(crate) struct NewEvent {
    client_id: String,
    transaction_type: String,
    transaction_date: NaiveDate,
    amount_eur: Option<Decimal>,
    quantity: Option<Decimal>,
    price_eur: Option<Decimal>,
    commission_eur: Option<Decimal>,
    instrument: Option<Instrument>,
    request: Value,
}

fn parse_decimal_field(body: &Value, key: &str, positive: bool, scale: u32) -> AppResult<Decimal> {
    let value = body
        .get(key)
        .ok_or_else(|| AppError::unprocessable(format!("{key} is required")))?;
    if value.is_boolean() {
        return Err(AppError::unprocessable(format!("Invalid {key}")));
    }
    let raw = value
        .as_str()
        .map(ToOwned::to_owned)
        .or_else(|| value.as_number().map(ToString::to_string))
        .ok_or_else(|| AppError::unprocessable(format!("Invalid {key}")))?;
    let value =
        Decimal::from_str(&raw).map_err(|_| AppError::unprocessable(format!("Invalid {key}")))?;
    if value.scale() > scale
        || value > *MAX_AMOUNT
        || (positive && value <= Decimal::ZERO)
        || (!positive && value < Decimal::ZERO)
    {
        return Err(AppError::unprocessable(format!("Invalid {key}")));
    }
    Ok(value)
}

pub(crate) async fn parse_new_event(pool: &PgPool, body: Value) -> AppResult<NewEvent> {
    let object = body
        .as_object()
        .ok_or_else(|| AppError::unprocessable("Transaction body must be an object"))?;
    let allowed = match body.get("type").and_then(Value::as_str) {
        Some("deposit" | "withdrawal") => {
            ["client_id", "date", "type", "amount_eur", "currency"].as_slice()
        }
        Some("buy" | "sell") => [
            "client_id",
            "date",
            "type",
            "instrument_id",
            "quantity",
            "price_eur",
            "commission_eur",
        ]
        .as_slice(),
        _ => return Err(AppError::unprocessable("Unsupported transaction type")),
    };
    if object.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(AppError::unprocessable("Unexpected transaction field"));
    }
    let transaction_type = body["type"].as_str().unwrap().to_owned();
    let client_id = body
        .get("client_id")
        .and_then(Value::as_str)
        .and_then(|value| Uuid::parse_str(value).ok())
        .map(|value| value.to_string())
        .ok_or_else(|| AppError::unprocessable("Invalid client_id"))?;
    let transaction_date = body
        .get("date")
        .and_then(Value::as_str)
        .filter(|value| value.len() == 10)
        .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
        .ok_or_else(|| AppError::unprocessable("Use a YYYY-MM-DD date."))?;
    if transaction_date > Utc::now().date_naive() {
        return Err(AppError::unprocessable(
            "Transaction date cannot be in the future (UTC).",
        ));
    }
    let mut normalized = body.clone();
    normalized["client_id"] = json!(client_id);
    normalized["date"] = json!(transaction_date);
    if matches!(transaction_type.as_str(), "deposit" | "withdrawal") {
        if body
            .get("currency")
            .and_then(Value::as_str)
            .unwrap_or("EUR")
            != "EUR"
        {
            return Err(AppError::unprocessable("Only EUR cash is supported"));
        }
        let amount = parse_decimal_field(&body, "amount_eur", true, 2)?;
        normalized["amount_eur"] = json!(decimal(amount));
        normalized["currency"] = json!("EUR");
        Ok(NewEvent {
            client_id,
            transaction_type,
            transaction_date,
            amount_eur: Some(amount),
            quantity: None,
            price_eur: None,
            commission_eur: None,
            instrument: None,
            request: normalized,
        })
    } else {
        let instrument_id = body
            .get("instrument_id")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::unprocessable("instrument_id is required"))?;
        let quantity = parse_decimal_field(&body, "quantity", true, 8)?;
        let price = parse_decimal_field(&body, "price_eur", true, 8)?;
        let commission = body
            .get("commission_eur")
            .map(|_| parse_decimal_field(&body, "commission_eur", false, 2))
            .transpose()?
            .unwrap_or_default();
        let gross = round_money(quantity * price);
        if gross <= Decimal::ZERO || gross > *MAX_AMOUNT {
            return Err(AppError::unprocessable(
                "Trade value must round to at least EUR 0.01 and be no more than EUR 1 trillion.",
            ));
        }
        if transaction_type == "sell" && commission > gross {
            return Err(AppError::unprocessable(
                "Sell commission cannot exceed the sale proceeds.",
            ));
        }
        let instrument = acquired_instrument(pool, instrument_id).await?;
        normalized.as_object_mut().unwrap().remove("instrument_id");
        normalized["quantity"] = json!(decimal(quantity));
        normalized["price_eur"] = json!(decimal(price));
        normalized["commission_eur"] = json!(money(commission));
        Ok(NewEvent {
            client_id,
            transaction_type,
            transaction_date,
            amount_eur: None,
            quantity: Some(quantity),
            price_eur: Some(price),
            commission_eur: Some(commission),
            instrument: Some(instrument),
            request: normalized,
        })
    }
}

pub(crate) fn event_from_new(event: &NewEvent, sequence: i32) -> LedgerEvent {
    LedgerEvent {
        id: opaque_id(),
        client_id: event.client_id.clone(),
        sequence,
        transaction_type: event.transaction_type.clone(),
        transaction_date: event.transaction_date,
        amount_eur: event.amount_eur,
        quantity: event.quantity,
        price_eur: event.price_eur,
        commission_eur: event.commission_eur,
        instrument_id: event
            .instrument
            .as_ref()
            .map(|value| value.instrument_id.clone()),
        instrument_name: event.instrument.as_ref().map(|value| value.name.clone()),
        symbol: event.instrument.as_ref().map(|value| value.symbol.clone()),
        exchange: event
            .instrument
            .as_ref()
            .map(|value| value.exchange.clone()),
        currency: event
            .instrument
            .as_ref()
            .map(|value| value.currency.clone()),
        created_at: Utc::now(),
        removed_at: None,
        source: None,
        source_rows: None,
        request: event.request.clone(),
    }
}

async fn add_transaction(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Json(body): Json<Value>,
) -> AppResult<impl IntoResponse> {
    require_customer(&state.pool, &user.id, &customer_id).await?;
    let new = parse_new_event(&state.pool, body).await?;
    let mut transaction = state.pool.begin().await?;
    let portfolio = lock_portfolio(&mut transaction, &user.id, &customer_id, &portfolio_id).await?;
    let existing = sqlx::query_as::<_, LedgerEvent>(
        "SELECT id, client_id, sequence, transaction_type, transaction_date,
                amount_eur, quantity, price_eur, commission_eur,
                instrument_id, instrument_name, symbol, exchange, currency,
                created_at, removed_at, source, source_rows, request
         FROM portfolio_transactions
         WHERE portfolio_id = $1 AND client_id = $2",
    )
    .bind(&portfolio_id)
    .bind(&new.client_id)
    .fetch_optional(&mut *transaction)
    .await?;
    if let Some(existing) = existing {
        if existing.request != new.request {
            return Err(AppError::conflict(
                "This transaction ID was already used with different inputs.",
            ));
        }
        if existing.removed_at.is_some() {
            return Err(AppError::conflict(
                "This transaction was removed. Use a new transaction ID to record a new entry.",
            ));
        }
        transaction.commit().await?;
        return Ok((StatusCode::OK, Json(existing.public())));
    }
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM portfolio_transactions WHERE portfolio_id = $1",
    )
    .bind(&portfolio_id)
    .fetch_one(&mut *transaction)
    .await?;
    if count >= MAX_TRANSACTIONS {
        return Err(AppError::conflict(format!(
            "This portfolio has reached the {MAX_TRANSACTIONS}-transaction limit."
        )));
    }
    let sequence = sqlx::query_scalar::<_, i32>(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM portfolio_transactions WHERE portfolio_id = $1",
    )
    .bind(&portfolio_id)
    .fetch_one(&mut *transaction)
    .await?;
    let event = event_from_new(&new, sequence);
    let mut events = load_events_transaction(&mut transaction, &portfolio_id).await?;
    events.push(event.clone());
    replay(&events, &HashMap::new())?;
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
    sqlx::query("UPDATE portfolios SET revision = revision + 1 WHERE id = $1 AND revision = $2")
        .bind(&portfolio_id)
        .bind(portfolio.revision)
        .execute(&mut *transaction)
        .await?;
    transaction.commit().await?;
    Ok((StatusCode::CREATED, Json(event.public())))
}

async fn load_events_transaction(
    transaction: &mut Transaction<'_, Postgres>,
    portfolio_id: &str,
) -> AppResult<Vec<LedgerEvent>> {
    Ok(sqlx::query_as::<_, LedgerEvent>(
        "SELECT id, client_id, sequence, transaction_type, transaction_date,
                amount_eur, quantity, price_eur, commission_eur,
                instrument_id, instrument_name, symbol, exchange, currency,
                created_at, removed_at, source, source_rows, request
         FROM portfolio_transactions WHERE portfolio_id = $1
         ORDER BY transaction_date, sequence",
    )
    .bind(portfolio_id)
    .fetch_all(&mut **transaction)
    .await?)
}

async fn remove_transaction(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id, transaction_id)): Path<(String, String, String)>,
) -> AppResult<Json<Value>> {
    require_customer(&state.pool, &user.id, &customer_id).await?;
    let mut transaction = state.pool.begin().await?;
    let portfolio = lock_portfolio(&mut transaction, &user.id, &customer_id, &portfolio_id).await?;
    let events = load_events_transaction(&mut transaction, &portfolio_id).await?;
    let Some(target) = events.iter().find(|event| event.id == transaction_id) else {
        return Err(AppError::not_found("Transaction not found."));
    };
    if target.removed_at.is_some() {
        transaction.commit().await?;
        return Ok(Json(json!({"id": transaction_id, "removed": true})));
    }
    let remaining = events
        .iter()
        .filter(|event| event.id != transaction_id)
        .cloned()
        .collect::<Vec<_>>();
    if let Err(error) = replay(&remaining, &HashMap::new()) {
        return Err(AppError::unprocessable(format!(
            "Cannot remove this transaction: {error} Remove dependent transactions first."
        )));
    }
    sqlx::query("UPDATE portfolio_transactions SET removed_at = now() WHERE id = $1")
        .bind(&transaction_id)
        .execute(&mut *transaction)
        .await?;
    sqlx::query("UPDATE portfolios SET revision = revision + 1 WHERE id = $1 AND revision = $2")
        .bind(&portfolio_id)
        .bind(portfolio.revision)
        .execute(&mut *transaction)
        .await?;
    transaction.commit().await?;
    Ok(Json(json!({"id": transaction_id, "removed": true})))
}

async fn portfolio_instruments(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    let (limit, offset) = pagination(query)?;
    let ids = sqlx::query_scalar::<_, String>(
        "SELECT DISTINCT instrument_id FROM instruments
         WHERE instrument_id NOT LIKE 'idx-%'
         ORDER BY instrument_id",
    )
    .fetch_all(&state.pool)
    .await?;
    let mut items = Vec::new();
    for instrument_id in ids {
        if let Ok(instrument) = acquired_instrument(&state.pool, &instrument_id).await {
            items.push(json!(instrument));
        }
    }
    let total = items.len();
    let items = items
        .into_iter()
        .skip(offset as usize)
        .take(limit as usize)
        .collect::<Vec<_>>();
    Ok(Json(
        json!({"items": items, "total": total, "limit": limit, "offset": offset}),
    ))
}

async fn plan_tracking(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let portfolio = require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    let plan_id = portfolio.plan_id.as_deref().ok_or_else(|| {
        AppError::unprocessable("Assign a saved plan to this portfolio to track its progress.")
    })?;
    let plan = sqlx::query_as::<_, (Value, Value)>(
        "SELECT inputs, result FROM planner_simulations
         WHERE id = $1 AND user_id = $2 AND customer_id = $3
           AND is_plan = true AND saved_at IS NOT NULL",
    )
    .bind(plan_id)
    .bind(&user.id)
    .bind(&customer_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::not_found("Plan not found."))?;
    let snapshot = valuation(&state.pool, &portfolio).await?;
    let equity_text = snapshot
        .get("equity_eur")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::unprocessable("Plan tracking requires a complete EUR portfolio valuation.")
        })?;
    let equity = Decimal::from_str(equity_text).map_err(|_| {
        AppError::unprocessable("Plan tracking requires a complete EUR portfolio valuation.")
    })?;
    let goal_date = plan
        .0
        .get("goal_date")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::unprocessable(
                "The saved plan does not contain a supported simulation snapshot.",
            )
        })?;
    let goal_date = NaiveDate::parse_from_str(goal_date, "%Y-%m-%d").map_err(|_| {
        AppError::unprocessable("The saved plan does not contain a supported simulation snapshot.")
    })?;
    if goal_date <= Utc::now().date_naive() {
        return Err(AppError::unprocessable(
            "The plan deadline has been reached; no future monthly investment can change its outcome.",
        ));
    }
    let original_probability = plan.1.get("success_probability").and_then(Value::as_f64);
    let projection =
        crate::planner::tracking_projection(&state.pool, &plan.0, &plan.1, equity).await?;
    let success_probability = projection
        .get("success_probability")
        .and_then(Value::as_f64);
    let mut warnings = snapshot
        .get("warnings")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    warnings.extend(
        projection
            .get("warnings")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
    );
    let mut response = json!({
        "plan_id": plan_id,
        "valuation_date": Utc::now().date_naive(),
        "goal_date": goal_date,
        "goal_eur": plan.0.get("goal_eur"),
        "equity_eur": money(equity),
        "strategy_name": "Buy and hold single instrument",
        "monthly_contribution_eur": plan.1.get("monthly_contribution_eur"),
        "original_probability": original_probability,
        "probability_change_pp": success_probability.zip(original_probability).map(|(current, original)| (current - original) * 100.0),
    });
    response
        .as_object_mut()
        .expect("tracking response")
        .extend(projection.as_object().cloned().unwrap_or_default());
    response["warnings"] = Value::Array(warnings);
    Ok(Json(response))
}

pub async fn customer_summaries(
    pool: &PgPool,
    user_id: &str,
    customer_ids: &[String],
) -> AppResult<HashMap<String, Value>> {
    let mut result = customer_ids
        .iter()
        .map(|id| {
            (
                id.clone(),
                json!({
                    "net_worth_eur": "0.00",
                    "realized_pnl_eur": "0.00",
                    "unrealized_pnl_eur": "0.00",
                    "total_pnl_eur": "0.00",
                    "cost_basis_eur": "0.00",
                    "previous_value_eur": "0.00",
                    "today_pnl_eur": "0.00",
                    "today_from_date": Value::Null,
                    "today_to_date": Value::Null,
                }),
            )
        })
        .collect::<HashMap<_, _>>();
    let portfolios = sqlx::query_as::<_, Portfolio>(
        "SELECT id, user_id, customer_id, name, plan_id, currency, created_at, revision
         FROM portfolios
         WHERE user_id = $1 AND customer_id = ANY($2)
         ORDER BY created_at DESC, id DESC",
    )
    .bind(user_id)
    .bind(customer_ids)
    .fetch_all(pool)
    .await?;
    let mut totals =
        HashMap::<String, (Decimal, Decimal, Decimal, Decimal, Decimal, Decimal)>::new();
    for portfolio in portfolios {
        let snapshot = valuation(pool, &portfolio).await?;
        let values = [
            parse_money(snapshot.get("equity_eur")),
            parse_money(snapshot.get("realized_pnl_eur")),
            parse_money(snapshot.get("unrealized_pnl_eur")),
            parse_money(snapshot.get("cost_basis_eur")),
            parse_money(snapshot.get("previous_value_eur")),
            parse_money(snapshot.get("daily_unrealized_pnl_eur")),
        ];
        let customer_id = portfolio.customer_id.clone();
        let entry = totals.entry(customer_id.clone()).or_insert((
            Decimal::ZERO,
            Decimal::ZERO,
            Decimal::ZERO,
            Decimal::ZERO,
            Decimal::ZERO,
            Decimal::ZERO,
        ));
        if let [Some(equity), Some(realized), Some(unrealized), Some(cost), Some(previous), Some(daily)] =
            values
        {
            entry.0 += equity;
            entry.1 += realized;
            entry.2 += unrealized;
            entry.3 += cost;
            entry.4 += previous;
            entry.5 += daily;
        } else {
            result.insert(
                customer_id,
                json!({
                    "net_worth_eur": Value::Null,
                    "realized_pnl_eur": Value::Null,
                    "unrealized_pnl_eur": Value::Null,
                    "total_pnl_eur": Value::Null,
                    "cost_basis_eur": Value::Null,
                    "previous_value_eur": Value::Null,
                    "today_pnl_eur": Value::Null,
                    "today_from_date": Value::Null,
                    "today_to_date": Value::Null,
                }),
            );
        }
    }
    for (customer_id, values) in totals {
        result.insert(
            customer_id,
            json!({
                "net_worth_eur": money(values.0),
                "realized_pnl_eur": money(values.1),
                "unrealized_pnl_eur": money(values.2),
                "total_pnl_eur": money(values.1 + values.2),
                "cost_basis_eur": money(values.3),
                "previous_value_eur": money(values.4),
                "today_pnl_eur": money(values.5),
                "today_from_date": Value::Null,
                "today_to_date": Value::Null,
            }),
        );
    }
    Ok(result)
}

fn opaque_id() -> String {
    Uuid::new_v4().simple().to_string()[..24].to_owned()
}
