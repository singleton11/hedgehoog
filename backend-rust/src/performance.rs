use std::collections::{BTreeMap, BTreeSet, HashMap};

use axum::{
    extract::{Path, Query, State},
    routing::get,
    Json, Router,
};
use chrono::{Duration, NaiveDate, Utc};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::{PgPool, Row};

use crate::{
    auth::CurrentUser,
    portfolio::{self, LedgerEvent},
    AppError, AppResult, AppState,
};

pub fn router() -> Router<AppState> {
    Router::new().route(
        "/customers/{customer_id}/portfolios/{portfolio_id}/performance",
        get(portfolio_performance),
    )
}

#[derive(Debug, Deserialize)]
struct PerformanceQuery {
    benchmark_id: Option<String>,
}

#[derive(Debug, Default)]
struct SavedMarks {
    values: BTreeMap<NaiveDate, Decimal>,
}

impl SavedMarks {
    fn at(&self, day: NaiveDate) -> Option<Decimal> {
        self.values
            .range(..=day)
            .next_back()
            .map(|(_, value)| *value)
    }
}

async fn marks(
    pool: &PgPool,
    instrument_id: &str,
    require_eur: bool,
    carry_trailing: bool,
) -> AppResult<SavedMarks> {
    let state = sqlx::query_as::<_, (Option<Value>, Option<String>)>(
        "SELECT listing, price_currency_override FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?;
    let Some((listing, override_currency)) = state else {
        return Ok(SavedMarks::default());
    };
    let generation = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
           AND listing = $2
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC LIMIT 1",
    )
    .bind(instrument_id)
    .bind(listing)
    .fetch_optional(pool)
    .await?;
    let Some(generation) = generation else {
        return Ok(SavedMarks::default());
    };
    let rows = sqlx::query(
        "SELECT quote_date, close, price_currency FROM quotes
         WHERE job_id = $1 AND quote_date <= CURRENT_DATE ORDER BY quote_date",
    )
    .bind(generation)
    .fetch_all(pool)
    .await?;
    let mut rows = rows
        .into_iter()
        .map(|row| {
            Ok(crate::quote_fill::QuoteRow {
                date: row.try_get("quote_date")?,
                close: row.try_get("close")?,
                adjusted: None,
                currency: row.try_get("price_currency")?,
                carried_from_date: None,
            })
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()?;
    let policy =
        crate::quote_fill::Policy::from_json(&crate::market_data::quote_fill_policy(pool).await?);
    crate::quote_fill::fill(
        &mut rows,
        instrument_id,
        &policy,
        override_currency.as_deref(),
        &[crate::quote_fill::PriceField::Close],
    );
    if carry_trailing {
        crate::quote_fill::carry_trailing_smooth(&mut rows, &policy);
    }
    let mut values = BTreeMap::new();
    for row in rows {
        let currency = row
            .currency
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_ascii_uppercase);
        let supported = !require_eur
            || (override_currency
                .as_deref()
                .is_none_or(|value| value == "EUR")
                && (currency.as_deref() == Some("EUR")
                    || (currency.is_none() && override_currency.as_deref() == Some("EUR"))));
        if supported {
            if let Some(close) = row.close.filter(|value| *value > Decimal::ZERO) {
                values.insert(row.date, close);
            }
        }
    }
    Ok(SavedMarks { values })
}

fn percent(growth: Option<Decimal>) -> Option<String> {
    growth.map(|value| {
        let result = (value - Decimal::ONE) * Decimal::from(100);
        let rendered = format!("{result:.8}");
        if result == Decimal::ZERO {
            "0.00000000".into()
        } else {
            rendered
        }
    })
}

#[derive(Default)]
struct RiskRatios {
    returns: Vec<f64>,
    valid: bool,
}

impl RiskRatios {
    fn new() -> Self {
        Self {
            returns: Vec::new(),
            valid: true,
        }
    }

    fn add(&mut self, factor: Option<Decimal>, target: Option<Decimal>) {
        let pair = factor
            .zip(target)
            .and_then(|(factor, target)| {
                Some((
                    factor.to_string().parse::<f64>().ok()?,
                    target.to_string().parse::<f64>().ok()?,
                ))
            })
            .filter(|(factor, target)| *factor > 0.0 && *target > 0.0);
        if let Some((factor, target)) = pair {
            self.returns.push(factor.ln() - target.ln());
        } else {
            self.valid = false;
        }
    }

    fn values(&self) -> (Option<String>, Option<String>) {
        if !self.valid || self.returns.len() < 2 {
            return (None, None);
        }
        let mean = self.returns.iter().sum::<f64>() / self.returns.len() as f64;
        let variance = self
            .returns
            .iter()
            .map(|value| (value - mean).powi(2))
            .sum::<f64>()
            / (self.returns.len() - 1) as f64;
        let downside = self
            .returns
            .iter()
            .map(|value| value.min(0.0).powi(2))
            .sum::<f64>()
            / self.returns.len() as f64;
        let numerator = mean * 365.0_f64.sqrt();
        (
            (variance > 0.0).then(|| format!("{:.8}", numerator / variance.sqrt())),
            (downside > 0.0).then(|| format!("{:.8}", numerator / downside.sqrt())),
        )
    }
}

async fn benchmark(pool: &PgPool, benchmark_id: &str) -> AppResult<(Value, SavedMarks)> {
    let row = sqlx::query(
        "SELECT instrument_id, name, symbol, currency, return_variant
         FROM indices WHERE instrument_id = $1 AND removed_at IS NULL",
    )
    .bind(benchmark_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Index not found"))?;
    let details = json!({
        "instrument_id": row.get::<String, _>("instrument_id"),
        "name": row.get::<String, _>("name"),
        "symbol": row.get::<String, _>("symbol"),
        "currency": row.try_get::<Option<String>, _>("currency")?,
        "return_variant": row.get::<String, _>("return_variant"),
    });
    Ok((details, marks(pool, benchmark_id, false, false).await?))
}

async fn portfolio_performance(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((customer_id, portfolio_id)): Path<(String, String)>,
    Query(query): Query<PerformanceQuery>,
) -> AppResult<Json<Value>> {
    let portfolio =
        portfolio::require_portfolio(&state.pool, &user.id, &customer_id, &portfolio_id).await?;
    let mut events = portfolio::load_events(&state.pool, &portfolio_id).await?;
    let today = Utc::now().date_naive();
    events.retain(|event| event.removed_at.is_none() && event.transaction_date <= today);
    events.sort_by_key(|event| (event.transaction_date, event.sequence));
    let settings = sqlx::query_scalar::<_, Value>(
        "SELECT value FROM app_settings WHERE key = 'portfolio_performance'",
    )
    .fetch_optional(&state.pool)
    .await?
    .unwrap_or_else(|| json!({"proxy": Value::Null}));
    let proxy = settings.get("proxy").cloned().unwrap_or(Value::Null);
    let proxy_id = proxy.get("instrument_id").and_then(Value::as_str);
    let proxy_marks = if let Some(identifier) = proxy_id {
        Some(marks(&state.pool, identifier, true, false).await?)
    } else {
        None
    };
    let (benchmark, benchmark_marks) = if let Some(identifier) = query.benchmark_id.as_deref() {
        let (details, marks) = benchmark(&state.pool, identifier).await?;
        (details, Some(marks))
    } else {
        (Value::Null, None)
    };
    let mut warnings = vec![
        "Historical portfolio returns, benchmarks and risk ratios use the latest complete Yahoo published history, restricted to quote dates on or before each valuation date. This is not strict point-in-time data: later provider corrections and corporate-action adjustments may revise historical prices and introduce look-ahead bias.".to_owned(),
    ];
    if proxy_id.is_none() {
        warnings.push(
            "Sharpe and Sortino unavailable: select a saved EUR price-return proxy in Settings."
                .into(),
        );
    }
    let policy = crate::market_data::quote_fill_policy(&state.pool).await?;
    let mut result = json!({
        "portfolio_id": portfolio.id,
        "currency": "EUR",
        "method": "daily_twr",
        "cash_flow_timing": "deposits_start_withdrawals_end",
        "as_of": Utc::now(),
        "start_date": Value::Null,
        "end_date": Value::Null,
        "points": [],
        "benchmark": benchmark,
        "warnings": warnings,
        "quote_fill_policy": policy,
        "risk_proxy": proxy,
        "ratio_method": "since_inception_calendar_daily_log_excess_365",
        "summary": {"twr_percent": Value::Null, "sharpe": Value::Null, "sortino": Value::Null},
    });
    if events.is_empty() {
        return Ok(Json(result));
    }
    let first = events[0].transaction_date;
    let baseline = first.pred_opt().unwrap_or(first);
    let mut instrument_marks = HashMap::new();
    let mut dates = BTreeSet::from([first, today]);
    for event in &events {
        dates.insert(event.transaction_date);
        if let Some(identifier) = &event.instrument_id {
            if !instrument_marks.contains_key(identifier) {
                let history = marks(&state.pool, identifier, true, true).await?;
                dates.extend(history.values.keys().copied().filter(|day| *day >= first));
                instrument_marks.insert(identifier.clone(), history);
            }
        }
    }
    if let Some(history) = &benchmark_marks {
        dates.extend(history.values.keys().copied().filter(|day| *day >= first));
    }
    if let Some(history) = &proxy_marks {
        dates.extend(history.values.keys().copied().filter(|day| *day >= first));
    }
    result["start_date"] = json!(baseline);
    result["end_date"] = json!(today);
    let benchmark_base = benchmark_marks
        .as_ref()
        .and_then(|history| history.at(baseline));
    let mut points = Vec::new();
    if baseline < first {
        points.push(json!({
            "date": baseline,
            "twr_percent": "0.00000000",
            "benchmark_percent": benchmark_base.map(|_| "0.00000000"),
            "sharpe": Value::Null,
            "sortino": Value::Null,
        }));
    }
    let mut cash = Decimal::ZERO;
    let mut holdings = HashMap::<String, Decimal>::new();
    let mut previous_equity = Decimal::ZERO;
    let mut growth = Some(Decimal::ONE);
    let mut event_index = 0_usize;
    let mut ratios = RiskRatios::new();
    let mut ratio_values = Vec::new();
    for day in dates {
        let mut deposits = Decimal::ZERO;
        let mut withdrawals = Decimal::ZERO;
        while event_index < events.len() && events[event_index].transaction_date == day {
            apply_event(
                &events[event_index],
                &mut cash,
                &mut holdings,
                &mut deposits,
                &mut withdrawals,
            );
            event_index += 1;
        }
        let mut equity = cash;
        let mut missing = Vec::new();
        for (identifier, quantity) in &holdings {
            match instrument_marks
                .get(identifier)
                .and_then(|history| history.at(day))
            {
                Some(close) => equity += *quantity * close,
                None => missing.push(identifier.clone()),
            }
        }
        if !missing.is_empty() {
            if growth.is_some() {
                result["warnings"].as_array_mut().unwrap().push(json!(format!(
                    "TWR unavailable from {day}: missing, invalid or unconfirmed EUR saved close for {}. The cumulative chain cannot resume until this history is repaired; no partial portfolio return is shown.",
                    missing.join(", ")
                )));
            }
            growth = None;
        }
        let denominator = previous_equity + deposits;
        let factor = if growth.is_some() && denominator > Decimal::ZERO {
            let factor = (equity + withdrawals) / denominator;
            growth = growth.map(|value| value * factor);
            Some(factor)
        } else if growth.is_some() && equity + withdrawals == Decimal::ZERO {
            None
        } else {
            growth = None;
            None
        };
        let prior_day = day.checked_sub_signed(Duration::days(1)).unwrap_or(day);
        let target = proxy_marks.as_ref().and_then(|history| {
            history
                .at(prior_day)
                .zip(history.at(day))
                .map(|(opening, closing)| closing / opening)
        });
        if denominator > Decimal::ZERO || growth.is_none() {
            ratios.add(factor, target);
        }
        previous_equity = equity;
        let benchmark_growth = benchmark_base.and_then(|base| {
            benchmark_marks
                .as_ref()
                .and_then(|history| history.at(day))
                .map(|close| close / base)
        });
        let (sharpe, sortino) = ratios.values();
        ratio_values.push((sharpe.clone(), sortino.clone()));
        points.push(json!({
            "date": day,
            "twr_percent": percent(growth),
            "benchmark_percent": percent(benchmark_growth),
            "sharpe": sharpe,
            "sortino": sortino,
        }));
    }
    let final_twr = points
        .last()
        .and_then(|point| point.get("twr_percent"))
        .cloned()
        .unwrap_or(Value::Null);
    let average = |index: usize| -> Option<String> {
        let values = ratio_values
            .iter()
            .filter_map(|pair| {
                if index == 0 {
                    pair.0.as_ref()
                } else {
                    pair.1.as_ref()
                }
            })
            .filter_map(|value| value.parse::<Decimal>().ok())
            .collect::<Vec<_>>();
        (!values.is_empty()).then(|| {
            format!(
                "{:.8}",
                values.iter().copied().sum::<Decimal>() / Decimal::from(values.len() as u64)
            )
        })
    };
    result["points"] = Value::Array(points);
    result["summary"] = json!({
        "twr_percent": final_twr,
        "sharpe": average(0),
        "sortino": average(1),
    });
    Ok(Json(result))
}

fn apply_event(
    event: &LedgerEvent,
    cash: &mut Decimal,
    holdings: &mut HashMap<String, Decimal>,
    deposits: &mut Decimal,
    withdrawals: &mut Decimal,
) {
    match event.transaction_type.as_str() {
        "deposit" => {
            let amount = event.amount_eur.unwrap_or_default();
            *deposits += amount;
            *cash += amount;
        }
        "withdrawal" => {
            let amount = event.amount_eur.unwrap_or_default();
            *withdrawals += amount;
            *cash -= amount;
        }
        "buy" | "sell" => {
            let quantity = event.quantity.unwrap_or_default();
            let gross = (quantity * event.price_eur.unwrap_or_default())
                .round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero);
            let commission = event.commission_eur.unwrap_or_default();
            let direction = if event.transaction_type == "buy" {
                Decimal::ONE
            } else {
                -Decimal::ONE
            };
            *cash -= direction * gross + commission;
            if let Some(identifier) = &event.instrument_id {
                *holdings.entry(identifier.clone()).or_default() += direction * quantity;
                if holdings.get(identifier) == Some(&Decimal::ZERO) {
                    holdings.remove(identifier);
                }
            }
        }
        _ => {}
    }
}
