use std::{
    cmp::Ordering,
    collections::{BTreeMap, HashMap, HashSet},
    str::FromStr,
};

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Datelike, Months, NaiveDate, Utc};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, PgPool};
use uuid::Uuid;

use crate::{
    auth::CurrentUser,
    portfolio::{self, Portfolio},
    AppError, AppResult, AppState,
};

const PATHS: usize = 10_000;
const SEED: u64 = 20_260_919;
const MAX_MONTHS: usize = 600;
const MIN_RETURNS: usize = 60;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/goals/calibrate", post(calibrate))
        .route("/goals/calibrations", get(calibrations))
        .route("/goals/calibrations/{run_id}", get(calibration_run))
        .route("/goals/strategies", get(strategies))
        .route(
            "/goals/strategies/{strategy_id}/calibration",
            get(saved_calibration),
        )
        .route("/goals/simulate", post(simulate_goal))
        .route("/customers", get(customers).post(create_customer))
        .route("/customers/{customer_id}", get(customer))
        .route("/customers/{customer_id}/plans", get(plans))
        .route(
            "/customers/{customer_id}/goals",
            get(goals).post(create_goal),
        )
        .route(
            "/customers/{customer_id}/goals/{goal_id}",
            get(goal).patch(update_goal),
        )
        .route(
            "/customers/{customer_id}/goals/{goal_id}/simulate",
            post(simulate_customer_goal),
        )
        .route(
            "/customers/{customer_id}/goals/{goal_id}/simulations",
            get(simulations),
        )
        .route(
            "/customers/{customer_id}/goals/{goal_id}/simulations/{simulation_id}",
            get(simulation),
        )
        .route(
            "/customers/{customer_id}/goals/{goal_id}/simulations/{simulation_id}/save",
            post(save_simulation),
        )
        .route(
            "/customers/{customer_id}/goals/{goal_id}/simulations/{simulation_id}/plan",
            post(make_plan),
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

fn opaque_id() -> String {
    Uuid::new_v4().simple().to_string()[..24].to_owned()
}

fn validate_name(value: &str) -> AppResult<String> {
    let value = value.trim();
    if value.is_empty() || value.len() > 200 {
        return Err(AppError::unprocessable(
            "Name must contain 1 to 200 characters",
        ));
    }
    Ok(value.to_owned())
}

fn parse_money(value: &Value, field: &str, positive: bool) -> AppResult<Decimal> {
    let raw = value
        .as_str()
        .map(ToOwned::to_owned)
        .or_else(|| value.as_number().map(ToString::to_string))
        .ok_or_else(|| AppError::unprocessable(format!("Invalid {field}")))?;
    let value =
        Decimal::from_str(&raw).map_err(|_| AppError::unprocessable(format!("Invalid {field}")))?;
    if value.scale() > 2
        || value < Decimal::ZERO
        || (positive && value == Decimal::ZERO)
        || value > Decimal::from(1_000_000_000_000_u64)
    {
        return Err(AppError::unprocessable(format!("Invalid {field}")));
    }
    Ok(value)
}

fn money(value: Decimal) -> String {
    format!(
        "{:.2}",
        value.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero)
    )
}

#[derive(Debug, FromRow)]
struct Customer {
    id: String,
    name: String,
    created_at: DateTime<Utc>,
}

fn customer_value(customer: &Customer) -> Value {
    json!({"id": customer.id, "name": customer.name, "created_at": customer.created_at})
}

async fn load_customer(pool: &PgPool, user_id: &str, customer_id: &str) -> AppResult<Customer> {
    sqlx::query_as::<_, Customer>(
        "SELECT id, name, created_at FROM planner_customers
         WHERE id = $1 AND user_id = $2",
    )
    .bind(customer_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Customer not found."))
}

async fn customers(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    let (limit, offset) = pagination(query)?;
    let total =
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM planner_customers WHERE user_id = $1")
            .bind(&user.id)
            .fetch_one(&state.pool)
            .await?;
    let rows = sqlx::query_as::<_, Customer>(
        "SELECT id, name, created_at FROM planner_customers
         WHERE user_id = $1
         ORDER BY created_at DESC, id DESC
         LIMIT $2 OFFSET $3",
    )
    .bind(&user.id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&state.pool)
    .await?;
    let ids = rows.iter().map(|row| row.id.clone()).collect::<Vec<_>>();
    let summaries = portfolio::customer_summaries(&state.pool, &user.id, &ids).await?;
    let goal_counts = sqlx::query_as::<_, (String, i64)>(
        "SELECT customer_id, COUNT(*) FROM planner_goals
         WHERE user_id = $1 AND customer_id = ANY($2)
         GROUP BY customer_id",
    )
    .bind(&user.id)
    .bind(&ids)
    .fetch_all(&state.pool)
    .await?
    .into_iter()
    .collect::<HashMap<_, _>>();
    let items = rows
        .iter()
        .map(|row| {
            let mut item = customer_value(row).as_object().cloned().unwrap_or_default();
            if let Some(summary) = summaries.get(&row.id).and_then(Value::as_object) {
                item.extend(summary.clone());
            }
            item.insert(
                "goal_count".into(),
                json!(goal_counts.get(&row.id).copied().unwrap_or(0)),
            );
            Value::Object(item)
        })
        .collect::<Vec<_>>();
    Ok(Json(
        json!({"items": items, "total": total, "limit": limit, "offset": offset}),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CustomerBody {
    name: String,
}

async fn create_customer(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Json(body): Json<CustomerBody>,
) -> AppResult<impl IntoResponse> {
    let customer = sqlx::query_as::<_, Customer>(
        "INSERT INTO planner_customers (id, user_id, name, created_at)
         VALUES ($1, $2, $3, now())
         RETURNING id, name, created_at",
    )
    .bind(opaque_id())
    .bind(user.id)
    .bind(validate_name(&body.name)?)
    .fetch_one(&state.pool)
    .await?;
    Ok((StatusCode::CREATED, Json(customer_value(&customer))))
}

async fn customer(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(customer_id): Path<String>,
) -> AppResult<Json<Value>> {
    Ok(Json(customer_value(
        &load_customer(&state.pool, &user.id, &customer_id).await?,
    )))
}

#[derive(Debug, FromRow)]
struct Goal {
    id: String,
    customer_id: String,
    name: String,
    goal_eur: Decimal,
    goal_date: NaiveDate,
    created_at: DateTime<Utc>,
}

fn goal_value(goal: &Goal) -> Value {
    json!({
        "id": goal.id,
        "customer_id": goal.customer_id,
        "name": goal.name,
        "goal_eur": money(goal.goal_eur),
        "goal_date": goal.goal_date,
        "created_at": goal.created_at,
    })
}

async fn load_goal(
    pool: &PgPool,
    user_id: &str,
    customer_id: &str,
    goal_id: &str,
) -> AppResult<Goal> {
    load_customer(pool, user_id, customer_id).await?;
    sqlx::query_as::<_, Goal>(
        "SELECT id, customer_id, name, goal_eur, goal_date, created_at
         FROM planner_goals
         WHERE id = $1 AND user_id = $2 AND customer_id = $3",
    )
    .bind(goal_id)
    .bind(user_id)
    .bind(customer_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Goal not found."))
}

async fn goals(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(customer_id): Path<String>,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    load_customer(&state.pool, &user.id, &customer_id).await?;
    let (limit, offset) = pagination(query)?;
    let total = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM planner_goals WHERE user_id = $1 AND customer_id = $2",
    )
    .bind(&user.id)
    .bind(&customer_id)
    .fetch_one(&state.pool)
    .await?;
    let rows = sqlx::query_as::<_, Goal>(
        "SELECT id, customer_id, name, goal_eur, goal_date, created_at
         FROM planner_goals
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
    for goal in rows {
        let portfolios = sqlx::query_as::<_, Portfolio>(
            "SELECT p.id, p.user_id, p.customer_id, p.name, p.plan_id, p.currency,
                    p.created_at, p.revision
             FROM portfolios p
             JOIN planner_simulations s ON s.id = p.plan_id
             WHERE p.user_id = $1 AND p.customer_id = $2 AND s.goal_id = $3
               AND s.is_plan = true AND s.saved_at IS NOT NULL",
        )
        .bind(&user.id)
        .bind(&customer_id)
        .bind(&goal.id)
        .fetch_all(&state.pool)
        .await?;
        let mut linked_equity = Decimal::ZERO;
        let mut complete = true;
        for portfolio in &portfolios {
            let snapshot = portfolio::valuation(&state.pool, portfolio).await?;
            if let Some(value) = snapshot
                .get("equity_eur")
                .and_then(Value::as_str)
                .and_then(|value| Decimal::from_str(value).ok())
            {
                linked_equity += value;
            } else {
                complete = false;
            }
        }
        let mut item = goal_value(&goal).as_object().cloned().unwrap_or_default();
        item.insert("linked_portfolio_count".into(), json!(portfolios.len()));
        item.insert(
            "linked_equity_eur".into(),
            if portfolios.is_empty() || !complete {
                Value::Null
            } else {
                Value::String(money(linked_equity))
            },
        );
        items.push(Value::Object(item));
    }
    Ok(Json(
        json!({"items": items, "total": total, "limit": limit, "offset": offset}),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GoalBody {
    name: String,
    goal_eur: Value,
    goal_date: NaiveDate,
}

fn contribution_dates(today: NaiveDate, goal_date: NaiveDate) -> AppResult<Vec<NaiveDate>> {
    let first = add_months(today, 1)?;
    if goal_date < first {
        return Err(AppError::unprocessable(
            "Goal date must allow at least one monthly contribution, starting one month from today (UTC).",
        ));
    }
    if goal_date > add_months(today, MAX_MONTHS as u32)? {
        return Err(AppError::unprocessable(
            "Goal date must be within 50 years of today (UTC).",
        ));
    }
    let mut dates = Vec::new();
    for month in 1..=MAX_MONTHS {
        let date = add_months(today, month as u32)?;
        if date > goal_date {
            break;
        }
        dates.push(date);
    }
    Ok(dates)
}

fn add_months(day: NaiveDate, months: u32) -> AppResult<NaiveDate> {
    day.checked_add_months(Months::new(months))
        .ok_or_else(|| AppError::unprocessable("Date is outside the supported range"))
}

async fn create_goal(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(customer_id): Path<String>,
    Json(body): Json<GoalBody>,
) -> AppResult<impl IntoResponse> {
    load_customer(&state.pool, &user.id, &customer_id).await?;
    contribution_dates(Utc::now().date_naive(), body.goal_date)?;
    let goal = sqlx::query_as::<_, Goal>(
        "INSERT INTO planner_goals
            (id, user_id, customer_id, name, goal_eur, goal_date, created_at)
         VALUES ($1, $2, $3, $4, $5, $6, now())
         RETURNING id, customer_id, name, goal_eur, goal_date, created_at",
    )
    .bind(opaque_id())
    .bind(&user.id)
    .bind(&customer_id)
    .bind(validate_name(&body.name)?)
    .bind(parse_money(&body.goal_eur, "goal_eur", true)?)
    .bind(body.goal_date)
    .fetch_one(&state.pool)
    .await?;
    Ok((StatusCode::CREATED, Json(goal_value(&goal))))
}

async fn goal(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    Ok(Json(goal_value(
        &load_goal(&state.pool, &user.id, &customer_id, &goal_id).await?,
    )))
}

async fn update_goal(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id)): Path<(String, String)>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let target = load_goal(&state.pool, &user.id, &customer_id, &goal_id).await?;
    let object = body
        .as_object()
        .filter(|object| !object.is_empty())
        .ok_or_else(|| {
            AppError::unprocessable("Supply at least one goal field; fields cannot be null.")
        })?;
    if object
        .keys()
        .any(|key| !matches!(key.as_str(), "name" | "goal_eur" | "goal_date"))
        || object.values().any(Value::is_null)
    {
        return Err(AppError::unprocessable(
            "Supply at least one goal field; fields cannot be null.",
        ));
    }
    let name = object
        .get("name")
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| AppError::unprocessable("Invalid goal name"))
                .and_then(validate_name)
        })
        .transpose()?
        .unwrap_or(target.name);
    let amount = object
        .get("goal_eur")
        .map(|value| parse_money(value, "goal_eur", true))
        .transpose()?
        .unwrap_or(target.goal_eur);
    let date = object
        .get("goal_date")
        .map(|value| {
            value
                .as_str()
                .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
                .ok_or_else(|| AppError::unprocessable("Invalid goal_date"))
        })
        .transpose()?
        .unwrap_or(target.goal_date);
    if date != target.goal_date {
        contribution_dates(Utc::now().date_naive(), date)?;
    }
    let goal = sqlx::query_as::<_, Goal>(
        "UPDATE planner_goals SET name = $4, goal_eur = $5, goal_date = $6
         WHERE id = $1 AND user_id = $2 AND customer_id = $3
         RETURNING id, customer_id, name, goal_eur, goal_date, created_at",
    )
    .bind(&goal_id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(name)
    .bind(amount)
    .bind(date)
    .fetch_one(&state.pool)
    .await?;
    Ok(Json(goal_value(&goal)))
}

#[derive(Debug)]
struct SimulationInput {
    goal_eur: Decimal,
    starting_capital_eur: Decimal,
    goal_date: NaiveDate,
    reliability: f64,
    as_of: Option<DateTime<Utc>>,
    simulation_method: String,
    block_size: Option<usize>,
    strategy: Value,
    instrument_id: String,
}

fn parse_simulation(body: &Value, fixed_goal: Option<&Goal>) -> AppResult<SimulationInput> {
    let object = body
        .as_object()
        .ok_or_else(|| AppError::unprocessable("Simulation body must be an object"))?;
    let allowed = if fixed_goal.is_some() {
        [
            "starting_capital_eur",
            "reliability",
            "as_of",
            "simulation_method",
            "block_size",
            "strategy",
        ]
        .as_slice()
    } else {
        [
            "goal_eur",
            "starting_capital_eur",
            "goal_date",
            "reliability",
            "as_of",
            "simulation_method",
            "block_size",
            "strategy",
        ]
        .as_slice()
    };
    if object.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(AppError::unprocessable("Unexpected simulation field"));
    }
    let goal_eur = fixed_goal
        .map(|goal| Ok(goal.goal_eur))
        .unwrap_or_else(|| parse_money(&body["goal_eur"], "goal_eur", true))?;
    let starting_capital_eur = parse_money(
        body.get("starting_capital_eur")
            .ok_or_else(|| AppError::unprocessable("starting_capital_eur is required"))?,
        "starting_capital_eur",
        false,
    )?;
    let goal_date = if let Some(goal) = fixed_goal {
        goal.goal_date
    } else {
        body.get("goal_date")
            .and_then(Value::as_str)
            .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
            .ok_or_else(|| AppError::unprocessable("Invalid goal_date"))?
    };
    let reliability = body
        .get("reliability")
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite() && (0.01..=0.99).contains(value))
        .ok_or_else(|| AppError::unprocessable("Invalid reliability"))?;
    let as_of = body
        .get("as_of")
        .filter(|value| !value.is_null())
        .map(|value| {
            value
                .as_str()
                .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
                .map(|value| value.with_timezone(&Utc))
                .ok_or_else(|| AppError::unprocessable("as_of must include a timezone"))
        })
        .transpose()?;
    if as_of.is_some_and(|value| value > Utc::now()) {
        return Err(AppError::unprocessable(
            "Simulation as_of cannot be in the future.",
        ));
    }
    let simulation_method = body
        .get("simulation_method")
        .and_then(Value::as_str)
        .unwrap_or("standard")
        .to_owned();
    let block_size = body
        .get("block_size")
        .filter(|value| !value.is_null())
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok());
    if (simulation_method == "block_bootstrap") != block_size.is_some()
        || !matches!(simulation_method.as_str(), "standard" | "block_bootstrap")
        || block_size.is_some_and(|value| !(1..=60).contains(&value))
    {
        return Err(AppError::unprocessable(
            "Block size is required only for block bootstrap.",
        ));
    }
    let strategy = body
        .get("strategy")
        .and_then(Value::as_object)
        .ok_or_else(|| AppError::unprocessable("strategy is required"))?;
    if strategy.get("id").and_then(Value::as_str) != Some("buy_and_hold_single_instrument") {
        return Err(AppError::unprocessable("Unknown goal strategy"));
    }
    let parameters = strategy
        .get("parameters")
        .and_then(Value::as_object)
        .ok_or_else(|| AppError::unprocessable("strategy.parameters is required"))?;
    if parameters.len() != 1 {
        return Err(AppError::unprocessable("Invalid strategy parameters"));
    }
    let instrument_id = parameters
        .get("instrument")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::unprocessable("Instrument is required"))?
        .to_owned();
    contribution_dates(as_of.unwrap_or_else(Utc::now).date_naive(), goal_date)?;
    Ok(SimulationInput {
        goal_eur,
        starting_capital_eur,
        goal_date,
        reliability,
        as_of,
        simulation_method,
        block_size,
        strategy: Value::Object(strategy.clone()),
        instrument_id,
    })
}

#[derive(Debug)]
struct ReturnEstimates {
    mean: f64,
    variance: f64,
    count: usize,
    warnings: Vec<String>,
    returns: Vec<f64>,
}

async fn return_estimates(
    pool: &PgPool,
    instrument_id: &str,
    cutoff: NaiveDate,
) -> AppResult<ReturnEstimates> {
    let row = sqlx::query_as::<_, (Option<Value>, Option<String>)>(
        "SELECT listing, price_currency_override FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| {
        AppError::unprocessable(
            "Choose an instrument with published Yahoo history. Request Yahoo acquisition first.",
        )
    })?;
    let generation = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
           AND listing = $2
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .bind(&row.0)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| {
        AppError::unprocessable(
            "Choose an instrument with published Yahoo history. Request Yahoo acquisition first.",
        )
    })?;
    let quotes =
        sqlx::query_as::<_, (NaiveDate, Option<Decimal>, Option<Decimal>, Option<String>)>(
            "SELECT quote_date, adj_close, close, price_currency
         FROM quotes WHERE job_id = $1 AND quote_date <= $2 ORDER BY quote_date",
        )
        .bind(generation)
        .bind(cutoff)
        .fetch_all(pool)
        .await?;
    let mut currencies = std::collections::HashSet::new();
    let mut unknown = false;
    let mut filled_rows = quotes
        .iter()
        .map(|(date, adjusted, close, currency)| {
            match currency
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                Some(currency) => {
                    currencies.insert(currency.to_ascii_uppercase());
                }
                None => unknown = true,
            }
            crate::quote_fill::QuoteRow {
                date: *date,
                close: *close,
                adjusted: *adjusted,
                currency: currency.clone(),
                carried_from_date: None,
            }
        })
        .collect::<Vec<_>>();
    let override_currency = row.1.as_deref();
    let policy =
        crate::quote_fill::Policy::from_json(&crate::market_data::quote_fill_policy(pool).await?);
    crate::quote_fill::fill(
        &mut filled_rows,
        instrument_id,
        &policy,
        override_currency,
        &[
            crate::quote_fill::PriceField::Close,
            crate::quote_fill::PriceField::Adjusted,
        ],
    );
    let prices = filled_rows
        .iter()
        .filter_map(|row| {
            row.adjusted
                .or(row.close)
                .and_then(|value| value.to_string().parse::<f64>().ok())
                .filter(|value| value.is_finite() && *value > 0.0)
                .map(|value| (row.date, value))
        })
        .collect::<Vec<_>>();
    let eur = if let Some(override_currency) = override_currency {
        currencies.is_empty()
            || currencies == std::collections::HashSet::from([override_currency.to_owned()])
    } else {
        currencies == std::collections::HashSet::from(["EUR".to_owned()]) && !unknown
    };
    if !eur || override_currency.is_some_and(|value| !value.eq_ignore_ascii_case("EUR")) {
        return Err(AppError::unprocessable(
            "Only EUR-priced history can be simulated. Confirm missing price currency on the instrument page; non-EUR or mixed prices require FX conversion.",
        ));
    }
    let mut returns = Vec::new();
    for pair in prices.windows(2) {
        if adjacent_weekdays(pair[0].0, pair[1].0) {
            returns.push(pair[1].1.ln() - pair[0].1.ln());
        }
    }
    if returns.len() < MIN_RETURNS {
        return Err(AppError::unprocessable(format!(
            "At least {MIN_RETURNS} usable daily returns are required; download more EOD history."
        )));
    }
    let mean = returns.iter().sum::<f64>() / returns.len() as f64;
    let variance = returns
        .iter()
        .map(|value| (value - mean).powi(2))
        .sum::<f64>()
        / (returns.len() - 1) as f64;
    if !mean.is_finite() || !variance.is_finite() {
        return Err(AppError::unprocessable(
            "Return estimates are not numerically usable for simulation.",
        ));
    }
    Ok(ReturnEstimates {
        mean,
        variance,
        count: returns.len(),
        warnings: Vec::new(),
        returns,
    })
}

fn adjacent_weekdays(previous: NaiveDate, latest: NaiveDate) -> bool {
    let gap = (latest - previous).num_days();
    gap > 0
        && gap <= 3
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

async fn simulate_value(pool: &PgPool, input: &SimulationInput) -> AppResult<Value> {
    let cutoff = input.as_of.unwrap_or_else(Utc::now);
    let quote_fill_policy = crate::market_data::quote_fill_policy(pool).await?;
    let estimates = return_estimates(pool, &input.instrument_id, cutoff.date_naive()).await?;
    let deposits = contribution_dates(cutoff.date_naive(), input.goal_date)?;
    let mut steps = deposits
        .iter()
        .map(|date| (*date, true))
        .collect::<Vec<_>>();
    if deposits.last().is_some_and(|last| *last < input.goal_date) {
        steps.push((input.goal_date, false));
    }
    let (growth, annuity) = scenario_factors(&steps, cutoff.date_naive(), &estimates, input)?;
    let initial = input
        .starting_capital_eur
        .to_string()
        .parse::<f64>()
        .map_err(|_| AppError::unprocessable("Starting capital is too large"))?;
    let goal = input
        .goal_eur
        .to_string()
        .parse::<f64>()
        .map_err(|_| AppError::unprocessable("Goal is too large"))?;
    let mut required = growth
        .iter()
        .zip(&annuity)
        .map(|(growth, annuity)| ((goal - initial * growth) / annuity).max(0.0))
        .collect::<Vec<_>>();
    required.sort_by(f64_order);
    let rank = (input.reliability * PATHS as f64).ceil() as usize - 1;
    let monthly_value = (required[rank] * 100.0).ceil() / 100.0;
    if !monthly_value.is_finite() || monthly_value > 1_000_000_000_000.0 {
        return Err(AppError::unprocessable(
            "The required monthly amount exceeds the EUR 1 trillion calculation limit. Try a longer horizon or a smaller goal.",
        ));
    }
    let monthly = Decimal::from_str(&format!("{monthly_value:.2}"))
        .map_err(|_| AppError::unprocessable("Projection is numerically unstable"))?;
    let terminal = growth
        .iter()
        .zip(&annuity)
        .map(|(growth, annuity)| initial * growth + monthly_value * annuity)
        .collect::<Vec<_>>();
    let success = terminal.iter().filter(|value| **value >= goal).count() as f64 / PATHS as f64;
    let zero_success = growth
        .iter()
        .filter(|growth| initial * **growth >= goal)
        .count() as f64
        / PATHS as f64;
    let mut sorted_terminal = terminal.clone();
    sorted_terminal.sort_by(f64_order);
    let trajectory = trajectory(
        &steps,
        cutoff.date_naive(),
        &estimates,
        input,
        initial,
        monthly_value,
    )?;
    let warning = if input.simulation_method == "standard" {
        "Model-estimated success is not a guarantee. Historical parameters and 10,000 simulated paths are uncertain; real markets have fat tails and changing regimes."
    } else {
        "Model-estimated success is not a guarantee. Historical returns and 10,000 simulated paths are uncertain; future regimes and losses may differ from observed history."
    };
    Ok(json!({
        "monthly_contribution_eur": money(monthly),
        "target_probability": input.reliability,
        "success_probability": success,
        "zero_contribution_success_probability": zero_success,
        "simulation_paths": PATHS,
        "seed": SEED,
        "simulation_method": input.simulation_method,
        "block_size": input.block_size,
        "valuation_date": cutoff.date_naive(),
        "goal_date": input.goal_date,
        "goal_eur": money(input.goal_eur),
        "trajectory": trajectory,
        "contribution_count": deposits.len(),
        "first_contribution_date": deposits.first(),
        "last_contribution_date": deposits.last(),
        "total_contributions_eur": money(monthly * Decimal::from(deposits.len() as u64)),
        "total_invested_eur": money(input.starting_capital_eur + monthly * Decimal::from(deposits.len() as u64)),
        "terminal_percentiles_eur": {
            "p05": money_float(percentile(&sorted_terminal, 0.05)),
            "p50": money_float(percentile(&sorted_terminal, 0.50)),
            "p95": money_float(percentile(&sorted_terminal, 0.95)),
        },
        "parameter_statistics": {"instrument": {
            "instrument_id": input.instrument_id,
            "return_count": estimates.count,
            "daily_mean_log_return": estimates.mean,
            "daily_log_return_variance": estimates.variance,
            "annualized_mean_log_return": estimates.mean * 252.0,
            "annualized_volatility": (estimates.variance * 252.0).sqrt(),
            "eligible": true,
            "warnings": estimates.warnings,
        }},
        "quote_fill_policy": quote_fill_policy,
        "data_as_of": cutoff,
        "history_contract": "latest_complete_yahoo",
        "warnings": [warning,
            "Fixed payments at each monthly anniversary (clamped to month end), first payment one month from today, with fractional shares and no withdrawals.",
            "Nominal EUR: inflation, taxes and transaction fees are not modelled.",
            "Uses the latest complete Yahoo published history with quote dates bounded by as_of, not observations known at that timestamp. Later provider corrections can revise historical prices; this is not strict point-in-time data."
        ],
        "strategy": input.strategy,
    }))
}

fn scenario_factors(
    steps: &[(NaiveDate, bool)],
    today: NaiveDate,
    estimates: &ReturnEstimates,
    input: &SimulationInput,
) -> AppResult<(Vec<f64>, Vec<f64>)> {
    let mut rng = DeterministicRng::new(SEED);
    let mut growth = vec![1.0; PATHS];
    let mut annuity = vec![0.0; PATHS];
    let mut previous = today;
    for (date, deposit) in steps {
        let trading_time = 252.0 * (*date - previous).num_days() as f64 / 365.25;
        for path in 0..PATHS {
            let factor = period_factor(&mut rng, estimates, input, trading_time)?;
            growth[path] *= factor;
            annuity[path] *= factor;
            if *deposit {
                annuity[path] += 1.0;
            }
        }
        previous = *date;
    }
    Ok((growth, annuity))
}

fn trajectory(
    steps: &[(NaiveDate, bool)],
    today: NaiveDate,
    estimates: &ReturnEstimates,
    input: &SimulationInput,
    capital: f64,
    monthly: f64,
) -> AppResult<Vec<Value>> {
    let mut rng = DeterministicRng::new(SEED);
    let mut growth = vec![1.0; PATHS];
    let mut annuity = vec![0.0; PATHS];
    let mut points = vec![trajectory_point(today, &growth, &annuity, capital, monthly)];
    let mut previous = today;
    for (date, deposit) in steps {
        let trading_time = 252.0 * (*date - previous).num_days() as f64 / 365.25;
        for path in 0..PATHS {
            let factor = period_factor(&mut rng, estimates, input, trading_time)?;
            growth[path] *= factor;
            annuity[path] *= factor;
            if *deposit {
                annuity[path] += 1.0;
            }
        }
        points.push(trajectory_point(*date, &growth, &annuity, capital, monthly));
        previous = *date;
    }
    Ok(points)
}

fn period_factor(
    rng: &mut DeterministicRng,
    estimates: &ReturnEstimates,
    input: &SimulationInput,
    trading_time: f64,
) -> AppResult<f64> {
    let log_return = if input.simulation_method == "standard" {
        estimates.mean * trading_time + rng.normal() * (estimates.variance * trading_time).sqrt()
    } else {
        let block_size = input.block_size.expect("validated block size");
        if estimates.returns.len() < block_size {
            return Err(AppError::unprocessable(
                "No uninterrupted historical return run supports this block size. Choose a shorter block or download more history.",
            ));
        }
        let count = trading_time.round().max(1.0) as usize;
        let mut total = 0.0;
        let mut remaining = count;
        while remaining > 0 {
            let start = rng.index(estimates.returns.len() - block_size + 1);
            let take = remaining.min(block_size);
            total += estimates.returns[start..start + take].iter().sum::<f64>();
            remaining -= take;
        }
        total
    };
    let factor = log_return.exp();
    if !factor.is_finite() {
        return Err(AppError::unprocessable(
            "This history and horizon produce numerically unstable projections. Check price quality or choose a shorter horizon.",
        ));
    }
    Ok(factor)
}

fn trajectory_point(
    date: NaiveDate,
    growth: &[f64],
    annuity: &[f64],
    capital: f64,
    monthly: f64,
) -> Value {
    let wealth = growth
        .iter()
        .zip(annuity)
        .map(|(growth, annuity)| capital * growth + monthly * annuity)
        .collect::<Vec<_>>();
    let mean = wealth.iter().sum::<f64>() / wealth.len() as f64;
    let mut sorted = wealth;
    sorted.sort_by(f64_order);
    json!({
        "date": date,
        "mean_eur": money_float(mean),
        "p05_eur": money_float(percentile(&sorted, 0.05)),
        "p25_eur": money_float(percentile(&sorted, 0.25)),
        "p50_eur": money_float(percentile(&sorted, 0.50)),
        "p75_eur": money_float(percentile(&sorted, 0.75)),
        "p95_eur": money_float(percentile(&sorted, 0.95)),
    })
}

fn percentile(sorted: &[f64], quantile: f64) -> f64 {
    let index = ((sorted.len() - 1) as f64 * quantile).ceil() as usize;
    sorted[index]
}

fn money_float(value: f64) -> String {
    format!("{value:.2}")
}

fn f64_order(left: &f64, right: &f64) -> Ordering {
    left.partial_cmp(right).unwrap_or(Ordering::Equal)
}

struct DeterministicRng {
    state: u64,
    spare: Option<f64>,
}

impl DeterministicRng {
    fn new(seed: u64) -> Self {
        Self {
            state: seed.max(1),
            spare: None,
        }
    }

    fn uniform(&mut self) -> f64 {
        let mut value = self.state;
        value ^= value << 13;
        value ^= value >> 7;
        value ^= value << 17;
        self.state = value;
        ((value >> 11) as f64 + 1.0) / ((1_u64 << 53) as f64 + 2.0)
    }

    fn normal(&mut self) -> f64 {
        if let Some(spare) = self.spare.take() {
            return spare;
        }
        let radius = (-2.0 * self.uniform().ln()).sqrt();
        let angle = std::f64::consts::TAU * self.uniform();
        self.spare = Some(radius * angle.sin());
        radius * angle.cos()
    }

    fn index(&mut self, length: usize) -> usize {
        (self.uniform() * length as f64).floor() as usize % length
    }
}

async fn simulate_goal(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let input = parse_simulation(&body, None)?;
    Ok(Json(simulate_value(&state.pool, &input).await?))
}

async fn simulate_customer_goal(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id)): Path<(String, String)>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let goal = load_goal(&state.pool, &user.id, &customer_id, &goal_id).await?;
    let input = parse_simulation(&body, Some(&goal))?;
    let result = simulate_value(&state.pool, &input).await?;
    let now = Utc::now();
    let inputs = json!({
        "goal_eur": money(input.goal_eur),
        "starting_capital_eur": money(input.starting_capital_eur),
        "goal_date": input.goal_date,
        "reliability": input.reliability,
        "as_of": input.as_of,
        "simulation_method": input.simulation_method,
        "block_size": input.block_size,
        "strategy": input.strategy,
    });
    let strategy_definition = strategy_definition();
    let id = opaque_id();
    sqlx::query(
        "INSERT INTO planner_simulations
            (id, user_id, customer_id, goal_id, calculated_at, saved_at,
             expires_at, is_plan, inputs, strategy_definition, result)
         VALUES ($1, $2, $3, $4, $5, NULL, $6, false, $7, $8, $9)",
    )
    .bind(&id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(&goal_id)
    .bind(now)
    .bind(now + chrono::Duration::hours(24))
    .bind(&inputs)
    .bind(&strategy_definition)
    .bind(&result)
    .execute(&state.pool)
    .await?;
    Ok(Json(json!({
        "id": id,
        "customer_id": customer_id,
        "goal_id": goal_id,
        "calculated_at": now,
        "saved_at": Value::Null,
        "inputs": inputs,
        "strategy_definition": strategy_definition,
        "result": result,
        "is_plan": false,
    })))
}

fn calendar_month_difference(start: NaiveDate, end: NaiveDate) -> AppResult<f64> {
    if end < start {
        return Ok(-calendar_month_difference(end, start)?);
    }
    let mut months = (end.year() - start.year()) * 12 + end.month() as i32 - start.month() as i32;
    if add_months(start, months as u32)? > end {
        months -= 1;
    }
    let lower = add_months(start, months as u32)?;
    let upper = add_months(start, months as u32 + 1)?;
    Ok(months as f64 + (end - lower).num_days() as f64 / (upper - lower).num_days() as f64)
}

fn forecast_goal_date(
    today: NaiveDate,
    input: &SimulationInput,
    estimates: &ReturnEstimates,
    capital: f64,
    monthly: f64,
    goal: f64,
) -> AppResult<(Option<NaiveDate>, &'static str, NaiveDate)> {
    let search_end = add_months(today, MAX_MONTHS as u32)?;
    if capital >= goal {
        return Ok((Some(today), "reached", search_end));
    }
    let mut steps = BTreeMap::new();
    for month in 1..=MAX_MONTHS {
        steps.insert(add_months(today, month as u32)?, true);
    }
    steps.entry(input.goal_date).or_insert(false);
    let threshold = (input.reliability * PATHS as f64).ceil() as usize;
    let mut rng = DeterministicRng::new(SEED);
    let mut growth = vec![1.0; PATHS];
    let mut annuity = vec![0.0; PATHS];
    let mut previous = today;
    for (date, deposit) in steps {
        let trading_time = 252.0 * (date - previous).num_days() as f64 / 365.25;
        for path in 0..PATHS {
            let Ok(factor) = period_factor(&mut rng, estimates, input, trading_time) else {
                return Ok((None, "unavailable", search_end));
            };
            growth[path] *= factor;
            annuity[path] *= factor;
            if deposit {
                annuity[path] += 1.0;
            }
            let wealth = capital * growth[path] + monthly * annuity[path];
            if !wealth.is_finite() {
                return Ok((None, "unavailable", search_end));
            }
        }
        if growth
            .iter()
            .zip(&annuity)
            .filter(|(growth, annuity)| capital * **growth + monthly * **annuity >= goal)
            .count()
            >= threshold
        {
            return Ok((Some(date), "reached", search_end));
        }
        previous = date;
    }
    Ok((None, "not_reached", search_end))
}

pub(crate) async fn tracking_projection(
    pool: &PgPool,
    saved_inputs: &Value,
    saved_result: &Value,
    equity: Decimal,
) -> AppResult<Value> {
    let mut body = saved_inputs.clone();
    body["starting_capital_eur"] = json!(money(equity));
    body["as_of"] = json!(Utc::now().to_rfc3339());
    let input = parse_simulation(&body, None)?;
    let projection = simulate_value(pool, &input).await?;
    let estimates = return_estimates(
        pool,
        &input.instrument_id,
        input.as_of.unwrap_or_else(Utc::now).date_naive(),
    )
    .await?;
    let deposits = contribution_dates(
        input.as_of.unwrap_or_else(Utc::now).date_naive(),
        input.goal_date,
    )?;
    let mut steps = deposits
        .iter()
        .map(|date| (*date, true))
        .collect::<Vec<_>>();
    if deposits.last().is_some_and(|last| *last < input.goal_date) {
        steps.push((input.goal_date, false));
    }
    let (growth, annuity) = scenario_factors(
        &steps,
        input.as_of.unwrap_or_else(Utc::now).date_naive(),
        &estimates,
        &input,
    )?;
    let capital = equity
        .to_string()
        .parse::<f64>()
        .map_err(|_| AppError::unprocessable("Portfolio equity is too large"))?;
    let goal = input
        .goal_eur
        .to_string()
        .parse::<f64>()
        .map_err(|_| AppError::unprocessable("Goal is too large"))?;
    let original_monthly = saved_result
        .get("monthly_contribution_eur")
        .and_then(Value::as_str)
        .and_then(|value| Decimal::from_str(value).ok())
        .ok_or_else(|| {
            AppError::unprocessable(
                "The saved plan does not contain a supported simulation snapshot.",
            )
        })?;
    let original_monthly_float = original_monthly
        .to_string()
        .parse::<f64>()
        .map_err(|_| AppError::unprocessable("Saved contribution is too large"))?;
    let current_success = growth
        .iter()
        .zip(&annuity)
        .filter(|(growth, annuity)| capital * **growth + original_monthly_float * **annuity >= goal)
        .count() as f64
        / PATHS as f64;
    let required = projection
        .get("monthly_contribution_eur")
        .and_then(Value::as_str)
        .and_then(|value| Decimal::from_str(value).ok())
        .ok_or_else(|| {
            AppError::Internal("tracking projection omitted required contribution".into())
        })?;
    let target = input.reliability;
    let today = input.as_of.unwrap_or_else(Utc::now).date_naive();
    let (forecast_date, forecast_status, forecast_search_end_date) = forecast_goal_date(
        today,
        &input,
        &estimates,
        capital,
        original_monthly_float,
        goal,
    )?;
    let schedule_difference_months = forecast_date
        .map(|date| calendar_month_difference(input.goal_date, date))
        .transpose()?;
    Ok(json!({
        "success_probability": current_success,
        "target_probability": target,
        "target_gap_pp": (current_success - target) * 100.0,
        "on_track": current_success >= target,
        "required_monthly_contribution_eur": money(required),
        "monthly_contribution_change_eur": money(required - original_monthly),
        "required_contribution_success_probability": projection.get("success_probability"),
        "simulation_paths": projection.get("simulation_paths"),
        "seed": projection.get("seed"),
        "simulation_method": projection.get("simulation_method"),
        "block_size": projection.get("block_size"),
        "forecast_goal_date": forecast_date,
        "forecast_status": forecast_status,
        "forecast_search_end_date": forecast_search_end_date,
        "schedule_difference_months": schedule_difference_months,
        "schedule_difference_years": schedule_difference_months.map(|months| months / 12.0),
        "warnings": projection.get("warnings"),
    }))
}

fn strategy_definition() -> Value {
    json!({
        "id": "buy_and_hold_single_instrument",
        "name": "Buy and hold single instrument",
        "description": "Invest starting capital and each monthly contribution in one instrument and hold it.",
        "parameters": [{
            "key": "instrument",
            "type": "instrument",
            "label": "Instrument",
            "required": true,
            "universe": "acquired",
        }],
        "supports_calibration": true,
    })
}

async fn strategies(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
) -> AppResult<Json<Value>> {
    let latest = sqlx::query_scalar::<_, Value>(
        "SELECT summary FROM calibration_runs
         WHERE user_id = $1 AND strategy_id = 'buy_and_hold_single_instrument'
         ORDER BY calculated_at DESC, id DESC LIMIT 1",
    )
    .bind(user.id)
    .fetch_optional(&state.pool)
    .await?;
    let mut definition = strategy_definition()
        .as_object()
        .cloned()
        .unwrap_or_default();
    definition.insert("latest_calibration".into(), latest.unwrap_or(Value::Null));
    Ok(Json(json!({"items": [definition]})))
}

#[derive(Debug, Clone)]
struct CalibrationPrice {
    date: NaiveDate,
    price: f64,
}

type CalibrationPathMap = BTreeMap<usize, (Vec<f64>, Vec<f64>)>;

#[derive(Debug)]
struct CalibrationStats {
    mean: f64,
    variance: f64,
    returns: Vec<f64>,
    runs: Vec<Vec<f64>>,
}

async fn calibration_prices(pool: &PgPool, instrument: &str) -> AppResult<Vec<CalibrationPrice>> {
    let state = sqlx::query_as::<_, (Option<Value>, Option<String>)>(
        "SELECT listing, price_currency_override FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| {
        AppError::unprocessable("Request Yahoo acquisition before calculating calibration.")
    })?;
    let generation = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
           AND listing = $2
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC LIMIT 1",
    )
    .bind(instrument)
    .bind(&state.0)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| {
        AppError::unprocessable("Request Yahoo acquisition before calculating calibration.")
    })?;
    let rows = sqlx::query_as::<_, (NaiveDate, Option<Decimal>, Option<Decimal>, Option<String>)>(
        "SELECT quote_date, adj_close, close, price_currency FROM quotes
         WHERE job_id = $1 ORDER BY quote_date LIMIT 20001",
    )
    .bind(generation)
    .fetch_all(pool)
    .await?;
    if rows.len() > 20_000 {
        return Err(AppError::unprocessable(
            "Saved history exceeds the 20,000-date calculation limit; no history was silently truncated.",
        ));
    }
    let override_currency = state.1.as_deref();
    let mut prices = Vec::new();
    for (date, adjusted, close, currency) in rows {
        let currency = currency
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_ascii_uppercase);
        let eur = override_currency.is_none_or(|value| value == "EUR")
            && (currency.as_deref() == Some("EUR")
                || (currency.is_none() && override_currency == Some("EUR")));
        if !eur {
            return Err(AppError::unprocessable(
                "Only EUR-priced history can be calibrated. Explicit foreign currencies are not relabelled.",
            ));
        }
        if let Some(price) = adjusted
            .or(close)
            .and_then(|value| value.to_string().parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value > 0.0)
        {
            prices.push(CalibrationPrice { date, price });
        }
    }
    if prices.len() < MIN_RETURNS + 1 {
        return Err(AppError::unprocessable(
            "Download more EOD history before calculating calibration.",
        ));
    }
    Ok(prices)
}

fn calibration_stats(prices: &[CalibrationPrice]) -> Option<CalibrationStats> {
    let mut returns = Vec::new();
    let mut runs = Vec::new();
    let mut run = Vec::new();
    for pair in prices.windows(2) {
        if adjacent_weekdays(pair[0].date, pair[1].date) {
            let value = pair[1].price.ln() - pair[0].price.ln();
            returns.push(value);
            run.push(value);
        } else if !run.is_empty() {
            runs.push(std::mem::take(&mut run));
        }
    }
    if !run.is_empty() {
        runs.push(run);
    }
    if returns.len() < MIN_RETURNS {
        return None;
    }
    let mean = returns.iter().sum::<f64>() / returns.len() as f64;
    let variance = returns
        .iter()
        .map(|value| (value - mean).powi(2))
        .sum::<f64>()
        / (returns.len() - 1) as f64;
    (mean.is_finite() && variance.is_finite()).then_some(CalibrationStats {
        mean,
        variance,
        returns,
        runs,
    })
}

fn calibration_paths(
    origin: NaiveDate,
    months: usize,
    stats: &CalibrationStats,
    method: &str,
    block_size: Option<usize>,
    seed: u64,
) -> AppResult<CalibrationPathMap> {
    let mut rng = DeterministicRng::new(seed ^ origin.num_days_from_ce() as u64);
    let mut growth = vec![1.0; PATHS];
    let mut annuity = vec![0.0; PATHS];
    let mut previous = origin;
    let mut results = BTreeMap::new();
    for month in 1..=months {
        let date = add_months(origin, month as u32)?;
        let trading_time = 252.0 * (date - previous).num_days() as f64 / 365.25;
        for path in 0..PATHS {
            let log_return = if method == "standard" {
                stats.mean * trading_time + rng.normal() * (stats.variance * trading_time).sqrt()
            } else {
                let block_size = block_size.expect("validated block size");
                let eligible = stats
                    .runs
                    .iter()
                    .filter(|run| run.len() >= block_size)
                    .collect::<Vec<_>>();
                if eligible.is_empty() {
                    return Err(AppError::unprocessable(
                        "No uninterrupted historical return run supports this block size. Choose a shorter block or download more history.",
                    ));
                }
                let mut count = trading_time.round().max(1.0) as usize;
                let mut total = 0.0;
                while count > 0 {
                    let run = eligible[rng.index(eligible.len())];
                    let start = rng.index(run.len() - block_size + 1);
                    let take = count.min(block_size);
                    total += run[start..start + take].iter().sum::<f64>();
                    count -= take;
                }
                total
            };
            let factor = log_return.exp();
            if !factor.is_finite() {
                return Err(AppError::unprocessable(
                    "Generated forecasts are numerically unstable. Check price quality or reduce the maximum horizon.",
                ));
            }
            growth[path] *= factor;
            annuity[path] = annuity[path] * factor + 1.0;
        }
        results.insert(month, (growth.clone(), annuity.clone()));
        previous = date;
    }
    Ok(results)
}

fn realized_calibration_value(
    prices: &[CalibrationPrice],
    origin_index: usize,
    horizon: usize,
    capital: f64,
    monthly: f64,
) -> AppResult<(f64, NaiveDate)> {
    let origin = prices[origin_index].date;
    let goal_date = add_months(origin, horizon as u32)?;
    let end_index = prices
        .partition_point(|row| row.date <= goal_date)
        .saturating_sub(1);
    if end_index < origin_index || (goal_date - prices[end_index].date).num_days() > 7 {
        return Err(AppError::unprocessable("missing_outcome_price"));
    }
    let end_price = prices[end_index].price;
    let mut wealth = capital * end_price / prices[origin_index].price;
    for month in 1..=horizon {
        let due = add_months(origin, month as u32)?;
        let execution = prices.partition_point(|row| row.date < due);
        if execution > end_index {
            if (goal_date - due).num_days() > 7 {
                return Err(AppError::unprocessable("missing_execution_price"));
            }
            wealth += monthly;
        } else if (prices[execution].date - due).num_days() > 7 {
            return Err(AppError::unprocessable("missing_execution_price"));
        } else {
            wealth += monthly * end_price / prices[execution].price;
        }
    }
    Ok((wealth, prices[end_index].date))
}

async fn calibrate(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let allowed = HashSet::from([
        "mode",
        "strategy",
        "scenario_count",
        "min_horizon_months",
        "max_horizon_months",
        "lookback_months",
        "simulation_method",
        "block_size",
        "start_date",
        "end_date",
        "seed",
    ]);
    let object = body
        .as_object()
        .ok_or_else(|| AppError::unprocessable("Calibration body must be an object"))?;
    if object.keys().any(|key| !allowed.contains(key.as_str()))
        || body.get("mode").and_then(Value::as_str) != Some("random")
    {
        return Err(AppError::unprocessable("Invalid calibration settings"));
    }
    let strategy = body
        .get("strategy")
        .cloned()
        .ok_or_else(|| AppError::unprocessable("strategy is required"))?;
    if strategy.get("id").and_then(Value::as_str) != Some("buy_and_hold_single_instrument") {
        return Err(AppError::unprocessable("Unknown goal strategy"));
    }
    let instrument = strategy
        .pointer("/parameters/instrument")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::unprocessable("Instrument is required"))?;
    let scenario_count = body
        .get("scenario_count")
        .and_then(Value::as_u64)
        .unwrap_or(1_000) as usize;
    let min_horizon = body
        .get("min_horizon_months")
        .and_then(Value::as_u64)
        .unwrap_or(3) as usize;
    let max_horizon = body
        .get("max_horizon_months")
        .and_then(Value::as_u64)
        .unwrap_or(24) as usize;
    let lookback = body
        .get("lookback_months")
        .and_then(Value::as_u64)
        .unwrap_or(12) as u32;
    let seed = body
        .get("seed")
        .filter(|value| !value.is_null())
        .and_then(Value::as_u64)
        .unwrap_or_else(|| {
            (Utc::now().timestamp_subsec_nanos() as u64 ^ Uuid::new_v4().as_u128() as u64)
                & u32::MAX as u64
        });
    let method = body
        .get("simulation_method")
        .and_then(Value::as_str)
        .unwrap_or("standard");
    let block_size = body
        .get("block_size")
        .filter(|value| !value.is_null())
        .and_then(Value::as_u64)
        .map(|value| value as usize);
    if !(10..=5_000).contains(&scenario_count)
        || !(1..=120).contains(&min_horizon)
        || !(1..=120).contains(&max_horizon)
        || min_horizon > max_horizon
        || !(3..=120).contains(&lookback)
        || seed > u32::MAX as u64
        || !matches!(method, "standard" | "block_bootstrap")
        || (method == "block_bootstrap") != block_size.is_some()
        || block_size.is_some_and(|value| !(1..=60).contains(&value))
    {
        return Err(AppError::unprocessable("Invalid calibration settings"));
    }
    let start_date = match body.get("start_date").filter(|value| !value.is_null()) {
        None => None,
        Some(value) => Some(
            value
                .as_str()
                .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
                .ok_or_else(|| AppError::unprocessable("Invalid start_date"))?,
        ),
    };
    let end_date = match body.get("end_date").filter(|value| !value.is_null()) {
        None => None,
        Some(value) => Some(
            value
                .as_str()
                .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
                .ok_or_else(|| AppError::unprocessable("Invalid end_date"))?,
        ),
    };
    if start_date
        .zip(end_date)
        .is_some_and(|(start, end)| start > end)
    {
        return Err(AppError::unprocessable(
            "Forecast start date must not be after end date",
        ));
    }
    let quote_fill_policy = crate::market_data::quote_fill_policy(&state.pool).await?;
    let prices = calibration_prices(&state.pool, instrument).await?;
    let mut origins = Vec::new();
    let mut previous_month = None;
    for (index, row) in prices.iter().enumerate() {
        if row.date.weekday().number_from_monday() > 5 {
            continue;
        }
        let month = (row.date.year(), row.date.month());
        if previous_month != Some(month)
            && start_date.is_none_or(|start| row.date >= start)
            && end_date.is_none_or(|end| row.date <= end)
            && row.date >= add_months(prices[0].date, lookback)?
            && add_months(row.date, min_horizon as u32)? <= prices.last().unwrap().date
        {
            origins.push(index);
        }
        previous_month = Some(month);
    }
    if origins.is_empty() || origins.len() > 600 {
        return Err(AppError::unprocessable(
            "No historical origins fit a full training lookback and minimum forecast horizon, or the 600-origin limit was exceeded.",
        ));
    }
    let mut schedule_rng = DeterministicRng::new(seed ^ 149);
    for index in (1..origins.len()).rev() {
        let other = schedule_rng.index(index + 1);
        origins.swap(index, other);
    }
    let mut grouped = BTreeMap::<usize, Vec<(usize, usize, Decimal, Decimal, f64)>>::new();
    for number in 0..scenario_count {
        let origin_index = origins[number % origins.len()];
        let local = number / origins.len();
        let origin = prices[origin_index].date;
        let max_fit = (min_horizon..=max_horizon)
            .filter(|month| {
                add_months(origin, *month as u32)
                    .is_ok_and(|date| date <= prices.last().unwrap().date)
            })
            .max()
            .ok_or_else(|| AppError::unprocessable("No complete historical horizon"))?;
        let mut rng =
            DeterministicRng::new(seed ^ origin.num_days_from_ce() as u64 ^ local as u64 ^ 150);
        let capital_cents = 100_000 + rng.index(9_900_001) as u64;
        let capital = Decimal::from(capital_cents) / Decimal::from(100);
        let capital_cents_u64 = capital_cents;
        let goal_cents =
            capital_cents_u64 + 1 + rng.index((100_000_000 - capital_cents_u64) as usize) as u64;
        let goal = Decimal::from(goal_cents) / Decimal::from(100);
        let horizon = min_horizon + rng.index(max_fit - min_horizon + 1);
        let bucket = (origin.num_days_from_ce() as usize + local) % 10;
        let lower = (bucket as f64 / 10.0).max(0.01);
        let upper = ((bucket + 1) as f64 / 10.0).min(0.99);
        let requested = lower + rng.uniform() * (upper - lower);
        grouped
            .entry(origin_index)
            .or_default()
            .push((number, horizon, capital, goal, requested));
    }
    let periods = 2 * grouped
        .values()
        .map(|group| group.iter().map(|case| case.1).max().unwrap_or(0))
        .sum::<usize>();
    if periods > 30_000 {
        return Err(AppError::unprocessable(
            "Generated calibration exceeds 30,000 simulated monthly periods across both streams.",
        ));
    }
    let mut forecasts = Vec::new();
    let mut excluded = Vec::new();
    for (origin_index, cases) in grouped {
        let origin = prices[origin_index].date;
        let training_start = origin
            .checked_sub_months(Months::new(lookback))
            .ok_or_else(|| AppError::unprocessable("Invalid calibration lookback"))?;
        let first = prices.partition_point(|row| row.date < training_start);
        let training = &prices[first..=origin_index];
        let Some(stats) = calibration_stats(training) else {
            excluded.extend(cases.iter().map(|case| {
                json!({
                    "scenario_id": format!("{}:{}", origin, case.0 + 1),
                    "origin_date": origin, "reason": "insufficient_training"
                })
            }));
            continue;
        };
        let max_months = cases.iter().map(|case| case.1).max().unwrap_or(0);
        let solver = calibration_paths(origin, max_months, &stats, method, block_size, seed ^ 151)?;
        let prediction =
            calibration_paths(origin, max_months, &stats, method, block_size, seed ^ 152)?;
        for (number, horizon, capital, goal, requested) in cases {
            let (solver_growth, solver_annuity) = &solver[&horizon];
            let capital_float = capital.to_string().parse::<f64>().unwrap_or_default();
            let goal_float = goal.to_string().parse::<f64>().unwrap_or_default();
            let mut required = solver_growth
                .iter()
                .zip(solver_annuity)
                .map(|(growth, annuity)| ((goal_float - capital_float * growth) / annuity).max(0.0))
                .collect::<Vec<_>>();
            required.sort_by(f64_order);
            let rank = (requested * PATHS as f64).ceil() as usize - 1;
            let monthly_float = (required[rank] * 100.0).ceil() / 100.0;
            if !monthly_float.is_finite() || monthly_float > 1_000_000_000_000.0 {
                return Err(AppError::unprocessable(
                    "Generated funding exceeds the calculation limit.",
                ));
            }
            let (prediction_growth, prediction_annuity) = &prediction[&horizon];
            let predicted_probability = prediction_growth
                .iter()
                .zip(prediction_annuity)
                .filter(|(growth, annuity)| {
                    capital_float * **growth + monthly_float * **annuity >= goal_float
                })
                .count() as f64
                / PATHS as f64;
            match realized_calibration_value(&prices, origin_index, horizon, capital_float, monthly_float) {
                Ok((terminal, valuation_date)) => forecasts.push(json!({
                    "scenario_id": format!("{}:{}", origin, number + 1),
                    "origin_date": origin,
                    "goal_date": add_months(origin, horizon as u32)?,
                    "training_start_date": training.first().unwrap().date,
                    "training_end_date": training.last().unwrap().date,
                    "training_return_count": stats.returns.len(),
                    "price_basis": "adj_close",
                    "goal_eur": money(goal),
                    "starting_capital_eur": money(capital),
                    "monthly_contribution_eur": format!("{monthly_float:.2}"),
                    "horizon_months": horizon,
                    "requested_probability": requested,
                    "predicted_probability": predicted_probability,
                    "observed_success": terminal >= goal_float - 8.0 * f64::EPSILON * goal_float.abs(),
                    "terminal_value_eur": format!("{terminal:.2}"),
                    "valuation_date": valuation_date,
                })),
                Err(problem) => excluded.push(json!({
                    "scenario_id": format!("{}:{}", origin, number + 1),
                    "origin_date": origin,
                    "reason": problem.to_string(),
                })),
            }
        }
    }
    if forecasts.is_empty() {
        return Err(AppError::unprocessable(
            "No evaluable generated scenarios. Require EUR prices, at least 60 usable daily training returns, and valid historical execution prices.",
        ));
    }
    forecasts.sort_by(|left, right| {
        left["scenario_id"]
            .as_str()
            .cmp(&right["scenario_id"].as_str())
    });
    let mut bins = (0..10)
        .map(|index| {
            json!({
                "lower": index as f64 / 10.0, "upper": (index + 1) as f64 / 10.0,
                "count": 0, "mean_predicted_probability": Value::Null,
                "observed_success_rate": Value::Null,
            })
        })
        .collect::<Vec<_>>();
    let mut bin_values = vec![Vec::<(f64, f64)>::new(); 10];
    let mut brier = 0.0;
    let mut predicted_sum = 0.0;
    let mut observed_sum = 0.0;
    let mut scored_origins = HashSet::new();
    for forecast in &forecasts {
        let probability = forecast["predicted_probability"]
            .as_f64()
            .unwrap_or_default();
        let observed = f64::from(forecast["observed_success"].as_bool().unwrap_or(false));
        bin_values[((probability * 10.0) as usize).min(9)].push((probability, observed));
        brier += (probability - observed).powi(2);
        predicted_sum += probability;
        observed_sum += observed;
        if let Some(origin) = forecast["origin_date"].as_str() {
            scored_origins.insert(origin.to_owned());
        }
    }
    let count = forecasts.len() as f64;
    let mut calibration_error = 0.0;
    for (index, values) in bin_values.iter().enumerate() {
        if values.is_empty() {
            continue;
        }
        let mean = values.iter().map(|value| value.0).sum::<f64>() / values.len() as f64;
        let observed = values.iter().map(|value| value.1).sum::<f64>() / values.len() as f64;
        bins[index] = json!({
            "lower": index as f64 / 10.0, "upper": (index + 1) as f64 / 10.0,
            "count": values.len(), "mean_predicted_probability": mean,
            "observed_success_rate": observed,
        });
        calibration_error += values.len() as f64 * (mean - observed).abs() / count;
    }
    let mut skipped_counts = BTreeMap::<String, usize>::new();
    for origin in &excluded {
        if let Some(reason) = origin.get("reason").and_then(Value::as_str) {
            *skipped_counts.entry(reason.to_owned()).or_default() += 1;
        }
    }
    let mut history_hasher = Sha256::new();
    for price in &prices {
        history_hasher.update(price.date.to_string().as_bytes());
        history_hasher.update([0]);
        history_hasher.update(price.price.to_bits().to_be_bytes());
    }
    let history_fingerprint = format!("{:x}", history_hasher.finalize());
    let settings = json!({
        "mode": "random", "scenario_count": scenario_count, "lookback_months": lookback,
        "start_date": start_date, "end_date": end_date,
        "min_horizon_months": min_horizon, "max_horizon_months": max_horizon,
        "simulation_method": method, "block_size": block_size,
    });
    let id = opaque_id();
    let calculated_at = Utc::now();
    let result = json!({
        "id": id, "strategy_name": "Buy and hold single instrument", "strategy": strategy,
        "calculated_at": calculated_at, "settings": settings, "seed": seed,
        "simulation_paths": PATHS, "method": "walk_forward",
        "quote_fill_policy": quote_fill_policy,
        "data_as_of": Utc::now(),
        "history_first_date": prices.first().unwrap().date,
        "history_last_date": prices.last().unwrap().date,
        "candidate_count": scenario_count,
        "available_origin_count": origins.len(),
        "origin_count": scored_origins.len(),
        "generation": {
            "method": "independent_goals_contribution_solver", "version": 2,
            "solver_simulation_paths": PATHS, "capital_min_eur": "1000.00",
            "capital_max_eur": "100000.00", "goal_max_eur": "1000000.00",
            "goal_rule": "above_starting_capital", "probability_min": 0.01,
            "probability_max": 0.99,
        },
        "sample_count": forecasts.len(),
        "brier_score": brier / count,
        "calibration_error": calibration_error,
        "mean_predicted_probability": predicted_sum / count,
        "observed_success_rate": observed_sum / count,
        "calibration_bins": bins,
        "forecasts": forecasts,
        "excluded_origins": excluded,
        "skipped_counts": skipped_counts,
        "history_fingerprint": history_fingerprint,
        "warnings": [
            "Walk-forward fitting uses the latest complete Yahoo published history, restricted to prices dated on or before each origin. This is not strict point-in-time data.",
            "Generated scenarios use independent seeded funding-solver and prediction streams; held-out prices are read only after funding and probability are fixed.",
            "Monthly origins and overlapping horizons are correlated, not independent market observations. More scenarios improve probability-range coverage, not historical evidence.",
            "Nominal EUR; fractional shares, no FX conversion, inflation, taxes or trading fees."
        ],
    });
    let summary = json!({
        "id": id, "strategy_name": "Buy and hold single instrument",
        "strategy": result["strategy"], "calculated_at": calculated_at,
        "settings": result["settings"], "seed": seed,
        "brier_score": result["brier_score"], "calibration_error": result["calibration_error"],
        "sample_count": result["sample_count"], "origin_count": result["origin_count"],
        "generation": result["generation"],
    });
    sqlx::query(
        "INSERT INTO calibration_runs
            (id, user_id, strategy_id, calculated_at, result, summary)
         VALUES ($1, $2, 'buy_and_hold_single_instrument', $3, $4, $5)",
    )
    .bind(&id)
    .bind(&user.id)
    .bind(calculated_at)
    .bind(&result)
    .bind(summary)
    .execute(&state.pool)
    .await?;
    Ok(Json(result))
}

async fn calibrations(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    let (limit, offset) = pagination(query)?;
    let total =
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM calibration_runs WHERE user_id = $1")
            .bind(&user.id)
            .fetch_one(&state.pool)
            .await?;
    let items = sqlx::query_scalar::<_, Value>(
        "SELECT summary FROM calibration_runs WHERE user_id = $1
         ORDER BY calculated_at DESC, id DESC LIMIT $2 OFFSET $3",
    )
    .bind(&user.id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&state.pool)
    .await?;
    Ok(Json(
        json!({"items": items, "total": total, "limit": limit, "offset": offset}),
    ))
}

async fn calibration_run(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(run_id): Path<String>,
) -> AppResult<Json<Value>> {
    let result = sqlx::query_scalar::<_, Value>(
        "SELECT result FROM calibration_runs WHERE id = $1 AND user_id = $2",
    )
    .bind(run_id)
    .bind(user.id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::not_found("Calibration run not found."))?;
    Ok(Json(json!({"result": result})))
}

async fn saved_calibration(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(strategy_id): Path<String>,
) -> AppResult<Json<Value>> {
    if strategy_id != "buy_and_hold_single_instrument" {
        return Err(AppError::not_found("Strategy not found."));
    }
    let result = sqlx::query_scalar::<_, Value>(
        "SELECT result FROM calibration_runs
         WHERE user_id = $1 AND strategy_id = $2
         ORDER BY calculated_at DESC, id DESC LIMIT 1",
    )
    .bind(user.id)
    .bind(strategy_id)
    .fetch_optional(&state.pool)
    .await?;
    Ok(Json(json!({"result": result})))
}

#[derive(Debug, FromRow)]
struct Simulation {
    id: String,
    customer_id: String,
    goal_id: String,
    calculated_at: DateTime<Utc>,
    saved_at: Option<DateTime<Utc>>,
    is_plan: bool,
    inputs: Value,
    strategy_definition: Value,
    result: Value,
}

fn simulation_value(simulation: &Simulation) -> Value {
    json!({
        "id": simulation.id,
        "customer_id": simulation.customer_id,
        "goal_id": simulation.goal_id,
        "calculated_at": simulation.calculated_at,
        "saved_at": simulation.saved_at,
        "inputs": simulation.inputs,
        "strategy_definition": simulation.strategy_definition,
        "result": simulation.result,
        "is_plan": simulation.is_plan,
    })
}

fn simulation_summary(simulation: &Simulation) -> Value {
    json!({
        "id": simulation.id,
        "is_plan": simulation.is_plan,
        "goal_id": simulation.goal_id,
        "calculated_at": simulation.calculated_at,
        "saved_at": simulation.saved_at,
        "inputs": simulation.inputs,
        "strategy_name": simulation.strategy_definition.get("name"),
        "monthly_contribution_eur": simulation.result.get("monthly_contribution_eur"),
        "success_probability": simulation.result.get("success_probability"),
    })
}

async fn plans(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(customer_id): Path<String>,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    load_customer(&state.pool, &user.id, &customer_id).await?;
    list_simulations(&state.pool, &user.id, &customer_id, None, true, query)
        .await
        .map(Json)
}

async fn simulations(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id)): Path<(String, String)>,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    load_goal(&state.pool, &user.id, &customer_id, &goal_id).await?;
    list_simulations(
        &state.pool,
        &user.id,
        &customer_id,
        Some(&goal_id),
        false,
        query,
    )
    .await
    .map(Json)
}

async fn list_simulations(
    pool: &PgPool,
    user_id: &str,
    customer_id: &str,
    goal_id: Option<&str>,
    plans_only: bool,
    query: Pagination,
) -> AppResult<Value> {
    let (limit, offset) = pagination(query)?;
    let rows = sqlx::query_as::<_, Simulation>(
        "SELECT id, customer_id, goal_id, calculated_at, saved_at, is_plan,
                inputs, strategy_definition, result
         FROM planner_simulations
         WHERE user_id = $1 AND customer_id = $2 AND saved_at IS NOT NULL
           AND ($3::text IS NULL OR goal_id = $3)
           AND ($4::boolean = false OR is_plan = true)
         ORDER BY calculated_at DESC, id DESC
         LIMIT $5 OFFSET $6",
    )
    .bind(user_id)
    .bind(customer_id)
    .bind(goal_id)
    .bind(plans_only)
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await?;
    let total = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM planner_simulations
         WHERE user_id = $1 AND customer_id = $2 AND saved_at IS NOT NULL
           AND ($3::text IS NULL OR goal_id = $3)
           AND ($4::boolean = false OR is_plan = true)",
    )
    .bind(user_id)
    .bind(customer_id)
    .bind(goal_id)
    .bind(plans_only)
    .fetch_one(pool)
    .await?;
    Ok(json!({
        "items": rows.iter().map(simulation_summary).collect::<Vec<_>>(),
        "total": total, "limit": limit, "offset": offset,
    }))
}

async fn load_saved_simulation(
    pool: &PgPool,
    user_id: &str,
    customer_id: &str,
    goal_id: &str,
    simulation_id: &str,
) -> AppResult<Simulation> {
    load_goal(pool, user_id, customer_id, goal_id).await?;
    sqlx::query_as::<_, Simulation>(
        "SELECT id, customer_id, goal_id, calculated_at, saved_at, is_plan,
                inputs, strategy_definition, result
         FROM planner_simulations
         WHERE id = $1 AND user_id = $2 AND customer_id = $3 AND goal_id = $4
           AND saved_at IS NOT NULL",
    )
    .bind(simulation_id)
    .bind(user_id)
    .bind(customer_id)
    .bind(goal_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Saved simulation not found."))
}

async fn simulation(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id, simulation_id)): Path<(String, String, String)>,
) -> AppResult<Json<Value>> {
    Ok(Json(simulation_value(
        &load_saved_simulation(
            &state.pool,
            &user.id,
            &customer_id,
            &goal_id,
            &simulation_id,
        )
        .await?,
    )))
}

async fn save_simulation(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id, simulation_id)): Path<(String, String, String)>,
) -> AppResult<Json<Value>> {
    load_goal(&state.pool, &user.id, &customer_id, &goal_id).await?;
    let now = Utc::now();
    let updated = sqlx::query_as::<_, Simulation>(
        "UPDATE planner_simulations
         SET saved_at = now(), expires_at = NULL
         WHERE id = $1 AND user_id = $2 AND customer_id = $3 AND goal_id = $4
           AND saved_at IS NULL AND expires_at > now()
         RETURNING id, customer_id, goal_id, calculated_at, saved_at, is_plan,
                   inputs, strategy_definition, result",
    )
    .bind(&simulation_id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(&goal_id)
    .fetch_optional(&state.pool)
    .await?;
    if let Some(simulation) = updated {
        return Ok(Json(simulation_value(&simulation)));
    }
    let existing = sqlx::query_as::<_, Simulation>(
        "SELECT id, customer_id, goal_id, calculated_at, saved_at, is_plan,
                inputs, strategy_definition, result
         FROM planner_simulations
         WHERE id = $1 AND user_id = $2 AND customer_id = $3 AND goal_id = $4",
    )
    .bind(&simulation_id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(&goal_id)
    .fetch_optional(&state.pool)
    .await?;
    match existing {
        None => Err(AppError::not_found(
            "Simulation preview not found or expired.",
        )),
        Some(simulation) if simulation.saved_at.is_none() => Err(AppError::Http {
            status: StatusCode::GONE,
            detail: "Simulation preview expired. Calculate a new preview before saving.".into(),
        }),
        Some(simulation) => {
            let _ = now;
            Ok(Json(simulation_value(&simulation)))
        }
    }
}

async fn make_plan(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, goal_id, simulation_id)): Path<(String, String, String)>,
) -> AppResult<Json<Value>> {
    load_goal(&state.pool, &user.id, &customer_id, &goal_id).await?;
    let simulation = sqlx::query_as::<_, Simulation>(
        "UPDATE planner_simulations SET is_plan = true
         WHERE id = $1 AND user_id = $2 AND customer_id = $3 AND goal_id = $4
           AND saved_at IS NOT NULL
         RETURNING id, customer_id, goal_id, calculated_at, saved_at, is_plan,
                   inputs, strategy_definition, result",
    )
    .bind(&simulation_id)
    .bind(&user.id)
    .bind(&customer_id)
    .bind(&goal_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::not_found("Saved simulation not found."))?;
    Ok(Json(simulation_value(&simulation)))
}
