use std::{collections::HashMap, str::FromStr};

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use chrono::{Months, NaiveDate, Utc};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::{
    auth::CurrentUser,
    portfolio::{self, LedgerEvent, Portfolio},
    AppError, AppResult, AppState,
};

const NAMESPACE: Uuid = Uuid::from_u128(0xb377ae0eeeb44f0dafb2ff7679f91c01);

pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/recommendations",
            get(recommendations),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/recommendations/preview",
            post(preview),
        )
        .route(
            "/customers/{customer_id}/portfolios/{portfolio_id}/recommendations/fulfill",
            post(fulfill),
        )
}

fn hash(value: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).unwrap_or_default())
    )
}

fn money(value: Decimal) -> String {
    format!(
        "{:.2}",
        value.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero)
    )
}

fn contribution_dates(origin: NaiveDate, end: NaiveDate) -> AppResult<Vec<NaiveDate>> {
    let mut dates = Vec::new();
    for month in 1..=600 {
        let date = origin
            .checked_add_months(Months::new(month))
            .ok_or_else(|| AppError::unprocessable("Invalid saved payment horizon"))?;
        if date > end {
            break;
        }
        dates.push(date);
    }
    Ok(dates)
}

#[derive(Debug)]
struct Plan {
    id: String,
    inputs: Value,
    result: Value,
}

async fn load_plan(pool: &PgPool, portfolio: &Portfolio) -> AppResult<Plan> {
    let plan_id = portfolio.plan_id.as_deref().ok_or_else(|| {
        AppError::unprocessable("Assign a saved plan to receive recommendations.")
    })?;
    let row = sqlx::query(
        "SELECT id, inputs, result FROM planner_simulations
         WHERE id = $1 AND user_id = $2 AND customer_id = $3
           AND saved_at IS NOT NULL AND is_plan = true",
    )
    .bind(plan_id)
    .bind(&portfolio.user_id)
    .bind(&portfolio.customer_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::unprocessable("Assigned plan is unavailable."))?;
    Ok(Plan {
        id: row.get("id"),
        inputs: row.get("inputs"),
        result: row.get("result"),
    })
}

fn decimal_at(value: &Value, key: &str) -> AppResult<Decimal> {
    value
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| Decimal::from_str(value).ok())
        .ok_or_else(|| AppError::unprocessable("Malformed saved plan"))
}

async fn current_mark(pool: &PgPool, instrument_id: &str) -> AppResult<Value> {
    let state = sqlx::query_as::<_, (Option<Value>, Option<String>)>(
        "SELECT listing, price_currency_override FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?;
    let Some((listing, override_currency)) = state else {
        return Ok(json!({
            "instrument_id": instrument_id, "eligible": false, "price_eur": Value::Null,
            "blockers": ["Request Yahoo acquisition for this instrument before trading."]
        }));
    };
    let generation = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
           AND listing = $2
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC LIMIT 1",
    )
    .bind(instrument_id)
    .bind(&listing)
    .fetch_optional(pool)
    .await?;
    let Some(generation) = generation else {
        return Ok(json!({
            "instrument_id": instrument_id, "eligible": false, "price_eur": Value::Null,
            "blockers": ["Request Yahoo acquisition for this instrument before trading."]
        }));
    };
    let quote = sqlx::query(
        "SELECT quote_date, close, price_currency FROM quotes
         WHERE job_id = $1 AND quote_date <= CURRENT_DATE
         ORDER BY quote_date DESC LIMIT 1",
    )
    .bind(generation)
    .fetch_optional(pool)
    .await?;
    let profile = sqlx::query(
        "SELECT COALESCE(data->>'longName', data->>'shortName', instrument_id) AS name
         FROM instruments
         WHERE instrument_id = $1 AND job_id = $2
         ORDER BY collected_at DESC LIMIT 1",
    )
    .bind(instrument_id)
    .bind(
        sqlx::query_scalar::<_, String>(
            "SELECT id FROM acquisition_jobs
             WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
             ORDER BY requested_at DESC LIMIT 1",
        )
        .bind(instrument_id)
        .fetch_one(pool)
        .await?,
    )
    .fetch_optional(pool)
    .await?;
    let symbol = listing
        .as_ref()
        .and_then(|value| value.get("symbol"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let exchange = listing
        .as_ref()
        .and_then(|value| value.get("exchange"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let name = profile
        .as_ref()
        .and_then(|row| row.try_get::<Option<String>, _>("name").ok().flatten())
        .unwrap_or_else(|| instrument_id.to_owned());
    let Some(quote) = quote else {
        return Ok(json!({
            "instrument_id": instrument_id, "eligible": false, "price_eur": Value::Null,
            "blockers": ["A valid recent raw EUR close or price override is required."]
        }));
    };
    let day = quote.get::<NaiveDate, _>("quote_date");
    let price = quote.try_get::<Option<Decimal>, _>("close")?;
    let currency = quote
        .try_get::<Option<String>, _>("price_currency")?
        .map(|value| value.trim().to_ascii_uppercase());
    let eur = override_currency
        .as_deref()
        .is_none_or(|value| value == "EUR")
        && (currency.as_deref() == Some("EUR")
            || (currency.is_none() && override_currency.as_deref() == Some("EUR")));
    let eligible = price.is_some_and(|value| value > Decimal::ZERO)
        && eur
        && (Utc::now().date_naive() - day).num_days() <= 7;
    Ok(json!({
        "instrument_id": instrument_id,
        "eligible": eligible,
        "price_eur": price.filter(|_| eligible).map(|value| value.normalize().to_string()),
        "price_date": day,
        "price_source": "saved_raw_close",
        "listing": {
            "instrument_id": instrument_id,
            "name": name,
            "symbol": symbol,
            "exchange": exchange,
            "currency": "EUR",
        },
        "blockers": if eligible { Vec::<String>::new() } else { vec!["A valid recent raw EUR close or price override is required.".to_owned()] },
    }))
}

async fn snapshot(
    pool: &PgPool,
    portfolio: &Portfolio,
    events: &[LedgerEvent],
) -> AppResult<Value> {
    let day = Utc::now().date_naive();
    let mut result = json!({
        "plan_id": portfolio.plan_id,
        "revision": portfolio.revision,
        "date": day,
        "status": "blocked",
        "funding": Value::Null,
        "targets": [],
        "actions": [],
        "blockers": [],
        "policy": {
            "version": "rebalance-v1", "min_drift": "0.05", "min_trade_eur": "25",
            "max_fee_ratio": "0.01", "cooldown_days": 30, "commission_eur": "0.00",
            "max_price_age_days": 7, "last_rebalance_date": Value::Null,
            "next_eligible_date": Value::Null,
        },
        "allocation": {"complete": false},
        "forecast": {"status": "available_separately", "endpoint": format!("/customers/{}/portfolios/{}/plan-tracking", portfolio.customer_id, portfolio.id)},
    });
    let plan = match load_plan(pool, portfolio).await {
        Ok(plan) => plan,
        Err(problem) => {
            result["status"] = json!("unsupported");
            result["blockers"] = json!([problem.to_string()]);
            result["snapshot_token"] = json!(hash(&result));
            return Ok(result);
        }
    };
    let origin = plan
        .result
        .get("valuation_date")
        .and_then(Value::as_str)
        .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
        .ok_or_else(|| AppError::unprocessable("Invalid saved horizon"))?;
    let end = plan
        .inputs
        .get("goal_date")
        .and_then(Value::as_str)
        .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
        .ok_or_else(|| AppError::unprocessable("Invalid saved horizon"))?;
    if day < origin || day >= end {
        result["status"] = json!("unsupported");
        result["blockers"] = json!(["Evaluation is outside the saved plan's active horizon."]);
        result["snapshot_token"] = json!(hash(&result));
        return Ok(result);
    }
    let dates = contribution_dates(origin, end)?;
    let initial = decimal_at(&plan.inputs, "starting_capital_eur")?;
    let monthly = decimal_at(&plan.result, "monthly_contribution_eur")?;
    let due_count = dates.iter().filter(|date| **date <= day).count();
    let due = initial + monthly * Decimal::from(due_count as u64);
    let funded = events
        .iter()
        .filter(|event| event.removed_at.is_none() && event.transaction_date <= day)
        .fold(Decimal::ZERO, |total, event| {
            match event.transaction_type.as_str() {
                "deposit" => total + event.amount_eur.unwrap_or_default(),
                "withdrawal" => total - event.amount_eur.unwrap_or_default(),
                _ => total,
            }
        });
    let shortfall = (due - funded).max(Decimal::ZERO);
    result["funding"] = json!({
        "scheduled_due_eur": money(due),
        "net_funded_eur": money(funded),
        "shortfall_eur": money(shortfall),
        "excess_eur": money((funded - due).max(Decimal::ZERO)),
        "due_count": due_count,
        "next_payment": dates.iter().find(|date| **date > day).map(|date| json!({"date": date, "amount_eur": money(monthly)})),
    });
    let instrument_id = plan
        .inputs
        .pointer("/strategy/parameters/instrument")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::unprocessable("Malformed saved strategy"))?;
    let ledger = portfolio::replay(events, &HashMap::new())?;
    let mut market = HashMap::new();
    for identifier in ledger
        .positions
        .keys()
        .map(String::as_str)
        .chain(std::iter::once(instrument_id))
    {
        if !market.contains_key(identifier) {
            market.insert(identifier.to_owned(), current_mark(pool, identifier).await?);
        }
    }
    let mut actions = Vec::new();
    if shortfall > Decimal::ZERO {
        actions.push(json!({
            "id": "deposit", "type": "deposit", "amount_eur": money(shortfall),
            "dependencies": [], "eligible": true, "reviewable": true,
            "rebalance": false, "blockers": [],
        }));
    }
    let mut values = HashMap::new();
    let mut blockers = Vec::new();
    for (identifier, position) in &ledger.positions {
        if position.quantity == Decimal::ZERO {
            continue;
        }
        let mark = &market[identifier];
        if !mark["eligible"].as_bool().unwrap_or(false) {
            blockers.push(format!("Incomplete valuation: {identifier}"));
        } else if let Some(price) = mark["price_eur"]
            .as_str()
            .and_then(|value| Decimal::from_str(value).ok())
        {
            values.insert(identifier.clone(), position.quantity * price);
        }
    }
    let target_mark = &market[instrument_id];
    if !target_mark["eligible"].as_bool().unwrap_or(false) {
        blockers.push(format!("Incomplete valuation: {instrument_id}"));
    }
    if blockers.is_empty() {
        let equity = ledger.cash + values.values().copied().sum::<Decimal>();
        let sale_ids = ledger
            .positions
            .iter()
            .filter(|(identifier, position)| identifier.as_str() != instrument_id && position.quantity > Decimal::ZERO)
            .map(|(identifier, position)| {
                let mark = &market[identifier];
                let id = format!("sell:{identifier}:rebalance");
                let price = mark["price_eur"].as_str().unwrap_or_default();
                let amount = values.get(identifier).copied().unwrap_or_default();
                actions.push(json!({
                    "id": id, "type": "sell", "instrument_id": identifier,
                    "amount_eur": money(amount), "quantity": position.quantity.normalize().to_string(),
                    "price_eur": price, "commission_eur": "0.00", "price_date": mark["price_date"],
                    "price_source": "saved_raw_close", "listing": mark["listing"],
                    "dependencies": [], "rebalance": true, "eligible": amount >= Decimal::from(25),
                    "reviewable": true, "blockers": if amount >= Decimal::from(25) { Vec::<String>::new() } else { vec!["Rebalance trade is below the minimum gross.".to_owned()] },
                }));
                id
            })
            .collect::<Vec<_>>();
        let available = ledger.cash
            + shortfall
            + ledger
                .positions
                .iter()
                .filter(|(identifier, _)| identifier.as_str() != instrument_id)
                .filter_map(|(identifier, _)| values.get(identifier))
                .copied()
                .sum::<Decimal>();
        if available >= Decimal::new(1, 2) {
            let price = Decimal::from_str(target_mark["price_eur"].as_str().unwrap_or("0"))
                .unwrap_or_default();
            let quantity = (available / price).round_dp_with_strategy(8, RoundingStrategy::ToZero);
            let mut dependencies = sale_ids.clone();
            if shortfall > Decimal::ZERO {
                dependencies.push("deposit".into());
            }
            actions.push(json!({
                "id": format!("buy:{instrument_id}:cash"), "type": "buy", "instrument_id": instrument_id,
                "amount_eur": money(available), "quantity": quantity.normalize().to_string(),
                "price_eur": price.normalize().to_string(), "commission_eur": "0.00",
                "price_date": target_mark["price_date"], "price_source": "saved_raw_close",
                "listing": target_mark["listing"], "dependencies": dependencies,
                "rebalance": !sale_ids.is_empty(), "eligible": true, "reviewable": true, "blockers": [],
            }));
        }
        result["targets"] = json!([{
            "instrument_id": instrument_id, "weight": "1", "actual_eur": money(values.get(instrument_id).copied().unwrap_or_default()),
            "price_eur": target_mark["price_eur"], "target_eur": money(equity + shortfall),
            "deviation": if equity > Decimal::ZERO { (values.get(instrument_id).copied().unwrap_or_default() / equity - Decimal::ONE).normalize().to_string() } else { "-1".into() },
        }]);
        result["allocation"] =
            json!({"complete": true, "equity_eur": money(equity), "max_deviation": "0"});
        result["status"] = json!(if actions
            .iter()
            .any(|action| action["eligible"] == Value::Bool(true))
        {
            "ready"
        } else {
            "aligned"
        });
    } else {
        result["blockers"] = json!(blockers);
        result["status"] = json!("blocked");
    }
    result["actions"] = Value::Array(actions);
    result["snapshot_token"] = json!(hash(&json!({
        "version": 1, "portfolio": portfolio.id, "revision": portfolio.revision,
        "plan": plan.id, "market": market, "result": result,
    })));
    Ok(result)
}

async fn recommendations(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let portfolio =
        portfolio::require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    let events = portfolio::load_events(&state.pool, &portfolio_id).await?;
    Ok(Json(snapshot(&state.pool, &portfolio, &events).await?))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReviewRequest {
    snapshot_token: String,
    action_ids: Vec<String>,
    date: NaiveDate,
    #[serde(default)]
    overrides: HashMap<String, Override>,
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Override {
    price_eur: Option<Value>,
    commission_eur: Option<Value>,
    quantity: Option<Value>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FulfillRequest {
    snapshot_token: String,
    action_ids: Vec<String>,
    date: NaiveDate,
    #[serde(default)]
    overrides: HashMap<String, Override>,
    operation_id: Uuid,
    preview_token: String,
}

struct Prepared {
    result: Value,
    entries: Vec<LedgerEvent>,
    revision: i64,
    plan_id: Option<String>,
}

fn parse_decimal(value: Option<&Value>, default: Decimal, scale: u32) -> AppResult<Decimal> {
    let Some(value) = value else {
        return Ok(default);
    };
    let raw = value
        .as_str()
        .map(ToOwned::to_owned)
        .or_else(|| value.as_number().map(ToString::to_string))
        .ok_or_else(|| AppError::unprocessable("Invalid override"))?;
    let value = Decimal::from_str(&raw).map_err(|_| AppError::unprocessable("Invalid override"))?;
    if value < Decimal::ZERO || value.scale() > scale {
        return Err(AppError::unprocessable("Invalid override"));
    }
    Ok(value)
}

async fn prepare(
    state: &AppState,
    portfolio: &Portfolio,
    review: &ReviewRequest,
) -> AppResult<Prepared> {
    if review.action_ids.is_empty()
        || review.action_ids.len() > 5_000
        || review
            .action_ids
            .iter()
            .collect::<std::collections::HashSet<_>>()
            .len()
            != review.action_ids.len()
    {
        return Err(AppError::unprocessable("Select each action only once."));
    }
    let events = portfolio::load_events(&state.pool, &portfolio.id).await?;
    let recommendation = snapshot(&state.pool, portfolio, &events).await?;
    if review.date != Utc::now().date_naive()
        || recommendation["snapshot_token"].as_str() != Some(&review.snapshot_token)
    {
        return Err(AppError::conflict(
            "Recommendations changed. Refresh and review again.",
        ));
    }
    let actions = recommendation["actions"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let by_id = actions
        .iter()
        .filter_map(|action| Some((action["id"].as_str()?.to_owned(), action)))
        .collect::<HashMap<_, _>>();
    let selected = review
        .action_ids
        .iter()
        .collect::<std::collections::HashSet<_>>();
    if selected.iter().any(|id| !by_id.contains_key(id.as_str()))
        || review.overrides.keys().any(|id| !selected.contains(id))
    {
        return Err(AppError::unprocessable("Unknown selection or override."));
    }
    for id in &review.action_ids {
        let action = by_id[id];
        if action["eligible"] != Value::Bool(true) {
            return Err(AppError::unprocessable(format!(
                "Selected action is blocked: {}",
                action["blockers"]
                    .as_array()
                    .map(|items| items
                        .iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<_>>()
                        .join("; "))
                    .unwrap_or_default()
            )));
        }
        let missing = action["dependencies"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .any(|dependency| !selected.iter().any(|id| id.as_str() == dependency));
        if missing {
            return Err(AppError::unprocessable(
                "Select every prerequisite and all trades in the rebalance group.",
            ));
        }
    }
    let mut additions = Vec::new();
    let mut all = events.clone();
    let mut sequence = all.iter().map(|event| event.sequence).max().unwrap_or(0);
    let mut public_entries = Vec::new();
    let mut total_fees = Decimal::ZERO;
    for id in &review.action_ids {
        let action = by_id[id];
        let kind = action["type"].as_str().unwrap_or_default();
        let client_id = Uuid::new_v5(
            &NAMESPACE,
            format!("{}:{id}", review.snapshot_token).as_bytes(),
        );
        let body = if kind == "deposit" {
            if review.overrides.contains_key(id) {
                return Err(AppError::unprocessable(
                    "Funding amounts are server generated and cannot be overridden.",
                ));
            }
            json!({
                "client_id": client_id, "date": review.date, "type": "deposit",
                "amount_eur": action["amount_eur"], "currency": "EUR",
            })
        } else {
            let override_values = review.overrides.get(id);
            let price = parse_decimal(
                override_values.and_then(|value| value.price_eur.as_ref()),
                Decimal::from_str(action["price_eur"].as_str().unwrap_or("0")).unwrap_or_default(),
                8,
            )?;
            let fee = parse_decimal(
                override_values.and_then(|value| value.commission_eur.as_ref()),
                Decimal::ZERO,
                2,
            )?;
            let quantity = parse_decimal(
                override_values.and_then(|value| value.quantity.as_ref()),
                Decimal::from_str(action["quantity"].as_str().unwrap_or("0")).unwrap_or_default(),
                8,
            )?;
            if price <= Decimal::ZERO || quantity <= Decimal::ZERO {
                return Err(AppError::unprocessable("Trade rounds to zero units."));
            }
            let budget =
                Decimal::from_str(action["amount_eur"].as_str().unwrap_or("0")).unwrap_or_default();
            let gross = (quantity * price)
                .round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero);
            if gross > budget + Decimal::new(5, 3) {
                return Err(AppError::unprocessable(
                    "Quantity exceeds the recommended allocation budget.",
                ));
            }
            total_fees += fee;
            json!({
                "client_id": client_id, "date": review.date, "type": kind,
                "instrument_id": action["instrument_id"], "quantity": quantity.normalize().to_string(),
                "price_eur": price.normalize().to_string(), "commission_eur": money(fee),
            })
        };
        let new = portfolio::parse_new_event(&state.pool, body).await?;
        sequence += 1;
        let mut event = portfolio::event_from_new(&new, sequence);
        event.source = Some("recommendation".into());
        all.push(event.clone());
        public_entries.push(event.public());
        additions.push(event);
    }
    portfolio::replay(&all, &HashMap::new())?;
    let normalized = json!({
        "snapshot_token": review.snapshot_token,
        "action_ids": review.action_ids,
        "date": review.date,
        "overrides": review.overrides.keys().collect::<Vec<_>>(),
    });
    let normalized_entries = public_entries
        .iter()
        .map(|entry| {
            let mut entry = entry.as_object().cloned().unwrap_or_default();
            entry.remove("id");
            entry.remove("created_at");
            Value::Object(entry)
        })
        .collect::<Vec<_>>();
    let preview_token = hash(&json!({
        "review": normalized, "entries": normalized_entries, "revision": portfolio.revision,
    }));
    let result = json!({
        "snapshot_token": review.snapshot_token,
        "action_ids": review.action_ids,
        "date": review.date,
        "overrides": review.overrides.keys().collect::<Vec<_>>(),
        "entries": public_entries,
        "before": portfolio::valuation(&state.pool, portfolio).await?,
        "after": {"complete": true},
        "selection": review.action_ids.iter().filter_map(|id| by_id.get(id).map(|value| (*value).clone())).collect::<Vec<_>>(),
        "omitted_action_ids": actions.iter().filter_map(|action| action["id"].as_str()).filter(|id| !selected.iter().any(|selected| selected.as_str() == *id)).collect::<Vec<_>>(),
        "policy": recommendation["policy"],
        "total_commission_eur": money(total_fees),
        "residual_eur": Value::Null,
        "preview_token": preview_token,
    });
    Ok(Prepared {
        result,
        entries: additions,
        revision: portfolio.revision,
        plan_id: portfolio.plan_id.clone(),
    })
}

async fn preview(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Json(body): Json<ReviewRequest>,
) -> AppResult<Json<Value>> {
    let portfolio =
        portfolio::require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    Ok(Json(prepare(&state, &portfolio, &body).await?.result))
}

async fn fulfill(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Json(body): Json<FulfillRequest>,
) -> AppResult<impl IntoResponse> {
    let fingerprint = hash(&json!({
        "snapshot_token": body.snapshot_token, "action_ids": body.action_ids,
        "date": body.date, "overrides": body.overrides.keys().collect::<Vec<_>>(),
        "preview_token": body.preview_token,
    }));
    if let Some((prior_fingerprint, response)) = sqlx::query_as::<_, (String, Value)>(
        "SELECT fingerprint, response FROM portfolio_recommendation_receipts
         WHERE portfolio_id = $1 AND operation_id = $2",
    )
    .bind(&portfolio_id)
    .bind(body.operation_id.to_string())
    .fetch_optional(&state.pool)
    .await?
    {
        if prior_fingerprint != fingerprint {
            return Err(AppError::conflict(
                "Operation ID was already used with different reviewed inputs.",
            ));
        }
        return Ok((StatusCode::OK, Json(response)));
    }
    let portfolio =
        portfolio::require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    let review = ReviewRequest {
        snapshot_token: body.snapshot_token.clone(),
        action_ids: body.action_ids.clone(),
        date: body.date,
        overrides: body.overrides,
    };
    let mut prepared = prepare(&state, &portfolio, &review).await?;
    if prepared.result["preview_token"].as_str() != Some(&body.preview_token) {
        return Err(AppError::conflict(
            "Preview changed. Review again before confirming.",
        ));
    }
    let mut transaction = state.pool.begin().await?;
    let locked =
        portfolio::lock_portfolio(&mut transaction, &user.id, &customer_id, &portfolio_id).await?;
    if locked.revision != prepared.revision || locked.plan_id != prepared.plan_id {
        return Err(AppError::conflict(
            "Portfolio changed concurrently. Refresh and review again.",
        ));
    }
    let mut public_entries = Vec::new();
    for (event, action_id) in prepared.entries.iter_mut().zip(&review.action_ids) {
        let id = Uuid::new_v5(&body.operation_id, action_id.as_bytes()).to_string();
        event.id = id.clone();
        event.client_id = id.clone();
        event.request["client_id"] = json!(id);
        public_entries.push(event.public());
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
    let response = json!({
        "operation_id": body.operation_id,
        "plan_id": locked.plan_id,
        "date": review.date,
        "revision": locked.revision + 1,
        "event_ids": prepared.entries.iter().map(|event| &event.id).collect::<Vec<_>>(),
        "entries": public_entries,
    });
    sqlx::query("UPDATE portfolios SET revision = revision + 1 WHERE id = $1")
        .bind(&portfolio_id)
        .execute(&mut *transaction)
        .await?;
    sqlx::query(
        "INSERT INTO portfolio_recommendation_receipts
            (id, portfolio_id, operation_id, fingerprint, revision, response, created_at)
         VALUES ($1, $2, $3, $4, $5, $6, now())",
    )
    .bind(Uuid::new_v4().simple().to_string()[..24].to_owned())
    .bind(&portfolio_id)
    .bind(body.operation_id.to_string())
    .bind(fingerprint)
    .bind(locked.revision + 1)
    .bind(&response)
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok((StatusCode::OK, Json(response)))
}
