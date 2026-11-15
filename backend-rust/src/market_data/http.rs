use std::{collections::HashSet, sync::LazyLock};

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, patch, post},
    Json, Router,
};
use chrono::{DateTime, Datelike, NaiveDate, Utc};
use regex::Regex;
use rust_decimal::{prelude::ToPrimitive, Decimal};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sqlx::{FromRow, PgPool, Row};
use uuid::Uuid;

use crate::{
    auth::{AdminUser, CurrentUser},
    AppError, AppResult, AppState,
};

use super::{
    public_acquisition_job, request_automatic_acquisition, request_user_acquisition,
    MarketDataError,
};

const INDEX_HISTORY_START: &str = "1900-01-01";
const PRICE_CURRENCIES: [&str; 5] = ["EUR", "USD", "CHF", "GBP", "RUB"];

static ISIN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$").expect("valid ISIN regex"));
static INDEX_ID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^idx-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$").expect("valid index id regex")
});
static SYMBOL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Z0-9^][A-Z0-9._^=\-]*$").expect("valid provider symbol regex")
});
static MIC: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z0-9]{4}$").expect("valid MIC regex"));

pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/settings/market-data",
            get(get_market_data_settings).put(put_market_data_settings),
        )
        .route(
            "/settings/performance",
            get(get_performance_settings).put(put_performance_settings),
        )
        .route("/settings/performance/indices", get(performance_indices))
        .route("/indices", get(list_indices).post(create_index))
        .route("/indices/search", get(search_indices))
        .route("/indices/acquisitions", post(acquire_all_indices))
        .route(
            "/indices/{instrument_id}",
            get(index_detail).delete(remove_index),
        )
        .route("/indices/{instrument_id}/acquisitions", post(acquire_index))
        .route("/indices/{instrument_id}/quotes", get(index_quotes))
        .route("/instruments/search", get(search_instruments))
        .route("/instruments/acquired", get(acquired_instruments))
        .route("/instruments/acquired/universe", get(acquired_universe))
        .route("/instruments/budget", get(budget))
        .route(
            "/instruments/{instrument_id}",
            get(instrument_detail).delete(remove_instrument),
        )
        .route(
            "/instruments/{instrument_id}/statistics",
            get(instrument_statistics),
        )
        .route(
            "/instruments/{instrument_id}/price-currency",
            patch(set_price_currency),
        )
        .route(
            "/instruments/{instrument_id}/splits",
            get(read_splits).post(create_manual_split),
        )
        .route(
            "/instruments/{instrument_id}/splits/{split_id}",
            delete(remove_manual_split),
        )
        .route(
            "/instruments/{instrument_id}/quotes",
            get(instrument_quotes),
        )
        .route(
            "/instruments/{instrument_id}/acquisitions",
            post(acquire_instrument),
        )
}

#[derive(Debug, Deserialize)]
struct Pagination {
    limit: Option<i64>,
    offset: Option<i64>,
}

fn pagination(query: Pagination, default: i64, maximum: i64) -> AppResult<(i64, i64)> {
    let limit = query.limit.unwrap_or(default);
    let offset = query.offset.unwrap_or(0);
    if !(1..=maximum).contains(&limit) || offset < 0 {
        return Err(AppError::unprocessable("Invalid pagination parameters"));
    }
    Ok((limit, offset))
}

fn validate_isin(value: &str) -> AppResult<()> {
    if !ISIN.is_match(value) {
        return Err(AppError::unprocessable("Invalid instrument identifier"));
    }
    Ok(())
}

fn validate_index_id(value: &str) -> AppResult<()> {
    if !INDEX_ID.is_match(value) {
        return Err(AppError::unprocessable("Invalid index identifier"));
    }
    Ok(())
}

fn market_error(error: MarketDataError) -> AppError {
    match error {
        MarketDataError::Invalid { status, message } => AppError::Http {
            status: StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST),
            detail: message,
        },
        MarketDataError::Database(error) => AppError::Database(error),
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MarketDataPolicy {
    method: String,
    seed: u64,
}

async fn get_market_data_settings(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
) -> AppResult<Json<Value>> {
    Ok(Json(super::quote_fill_policy(&state.pool).await?))
}

async fn put_market_data_settings(
    State(state): State<AppState>,
    AdminUser(user): AdminUser,
    Json(body): Json<MarketDataPolicy>,
) -> AppResult<Json<Value>> {
    if !matches!(body.method.as_str(), "smooth" | "normal") || body.seed > u32::MAX.into() {
        return Err(AppError::unprocessable("Invalid market-data settings"));
    }
    let value = json!({
        "method": body.method,
        "seed": body.seed,
        "updated_at": Utc::now(),
        "updated_by": user.id,
    });
    sqlx::query(
        "INSERT INTO app_settings (key, value, updated_at, updated_by)
         VALUES ('market_data', $1, now(), $2)
         ON CONFLICT (key) DO UPDATE
         SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
    )
    .bind(value)
    .bind(user.id)
    .execute(&state.pool)
    .await?;
    Ok(Json(super::quote_fill_policy(&state.pool).await?))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProxySetting {
    instrument_id: Option<String>,
}

async fn read_performance_settings(pool: &PgPool) -> AppResult<Value> {
    let value = sqlx::query_scalar::<_, Value>(
        "SELECT value FROM app_settings WHERE key = 'portfolio_performance'",
    )
    .fetch_optional(pool)
    .await?
    .unwrap_or_else(|| json!({}));
    Ok(json!({
        "proxy": value.get("proxy").cloned().unwrap_or(Value::Null),
        "updated_at": value.get("updated_at").cloned().unwrap_or(Value::Null),
    }))
}

async fn get_performance_settings(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
) -> AppResult<Json<Value>> {
    Ok(Json(read_performance_settings(&state.pool).await?))
}

async fn acquired_proxy(pool: &PgPool, instrument_id: &str) -> AppResult<Value> {
    validate_index_id(instrument_id)?;
    let row = sqlx::query(
        "SELECT i.instrument_id, i.name, i.symbol,
                COALESCE(i.currency, p.data ->> 'currency', 'EUR') AS currency
         FROM indices i
         JOIN LATERAL (
             SELECT id FROM acquisition_jobs
             WHERE instrument_id = i.instrument_id AND provider = 'yfinance' AND published = true
             ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC
             LIMIT 1
         ) publication ON true
         JOIN instruments p ON p.job_id = publication.id
         WHERE i.instrument_id = $1 AND i.removed_at IS NULL
           AND EXISTS (SELECT 1 FROM quotes q WHERE q.job_id = publication.id)",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| {
        AppError::unprocessable("Acquire the selected index before using it as a proxy.")
    })?;
    let currency: String = row.try_get("currency")?;
    if !currency.trim().eq_ignore_ascii_case("EUR") {
        return Err(AppError::unprocessable(
            "Only EUR indices are supported; missing currency metadata defaults to EUR.",
        ));
    }
    Ok(json!({
        "instrument_id": row.try_get::<String, _>("instrument_id")?,
        "name": row.try_get::<String, _>("name")?,
        "symbol": row.try_get::<String, _>("symbol")?,
        "exchange": "YAHOO",
        "currency": "EUR",
    }))
}

async fn performance_indices(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    let (limit, offset) = pagination(query, 100, 100)?;
    let identifiers = sqlx::query_scalar::<_, String>(
        "SELECT instrument_id FROM indices
         WHERE removed_at IS NULL
         ORDER BY name, instrument_id",
    )
    .fetch_all(&state.pool)
    .await?;
    let mut items = Vec::new();
    for identifier in identifiers {
        if let Ok(item) = acquired_proxy(&state.pool, &identifier).await {
            items.push(item);
        }
    }
    let total = items.len();
    let items = items
        .into_iter()
        .skip(offset as usize)
        .take(limit as usize)
        .collect::<Vec<_>>();
    Ok(Json(json!({
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    })))
}

async fn put_performance_settings(
    State(state): State<AppState>,
    AdminUser(user): AdminUser,
    Json(body): Json<ProxySetting>,
) -> AppResult<Json<Value>> {
    let proxy = if let Some(instrument_id) = body.instrument_id.as_deref() {
        acquired_proxy(&state.pool, instrument_id).await?
    } else {
        Value::Null
    };
    let now = Utc::now();
    let value = json!({"proxy": proxy, "updated_at": now, "updated_by": user.id});
    sqlx::query(
        "INSERT INTO app_settings (key, value, updated_at, updated_by)
         VALUES ('portfolio_performance', $1, now(), $2)
         ON CONFLICT (key) DO UPDATE
         SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
    )
    .bind(value)
    .bind(user.id)
    .execute(&state.pool)
    .await?;
    Ok(Json(read_performance_settings(&state.pool).await?))
}

#[derive(Debug, FromRow)]
struct IndexRecord {
    instrument_id: String,
    symbol: String,
    name: String,
    currency: Option<String>,
    return_variant: String,
    provider: String,
    provenance: Value,
}

async fn require_index(pool: &PgPool, instrument_id: &str) -> AppResult<IndexRecord> {
    validate_index_id(instrument_id)?;
    sqlx::query_as::<_, IndexRecord>(
        "SELECT instrument_id, symbol, name, currency, return_variant,
                provider, provenance
         FROM indices
         WHERE instrument_id = $1 AND removed_at IS NULL",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Index not found in the index catalogue"))
}

async fn index_detail_value(
    pool: &PgPool,
    instrument_id: &str,
    supplied: Option<IndexRecord>,
) -> AppResult<Value> {
    let record = match supplied {
        Some(record) => record,
        None => require_index(pool, instrument_id).await?,
    };
    let publication = latest_publication(pool, instrument_id).await?;
    let generation = publication
        .as_ref()
        .map(|publication| publication.id.clone());
    let profile = if let Some(generation) = generation.as_deref() {
        sqlx::query_as::<_, (DateTime<Utc>, NaiveDate, Value)>(
            "SELECT collected_at, collection_date, data
             FROM instruments WHERE job_id = $1",
        )
        .bind(generation)
        .fetch_optional(pool)
        .await?
        .map(|row| json!({"collected_at": row.0, "collection_date": row.1, "data": row.2}))
    } else {
        None
    };
    let (count, first, last) = if let Some(generation) = generation.as_deref() {
        sqlx::query_as::<_, (i64, Option<NaiveDate>, Option<NaiveDate>)>(
            "SELECT COUNT(DISTINCT quote_date), MIN(quote_date), MAX(quote_date)
             FROM quotes WHERE job_id = $1",
        )
        .bind(generation)
        .fetch_one(pool)
        .await?
    } else {
        (0, None, None)
    };
    let provider_currency = profile
        .as_ref()
        .and_then(|profile| profile.get("data"))
        .and_then(|data| data.get("currency"))
        .and_then(Value::as_str);
    let currency = record.currency.as_deref().or(provider_currency);
    let verified = profile.is_some();
    let acquisition = latest_job(pool, instrument_id).await?;
    let mut provenance = record.provenance.as_object().cloned().unwrap_or_default();
    provenance.remove("added_by");
    provenance.insert(
        "verification_scope".into(),
        if verified {
            Value::String("yahoo_symbol_and_index_type".into())
        } else {
            Value::Null
        },
    );
    provenance.insert(
        "verified_generation".into(),
        generation.clone().map(Value::String).unwrap_or(Value::Null),
    );
    provenance.insert(
        "currency_source".into(),
        if record.currency.is_some() {
            Value::String("catalogue".into())
        } else if currency.is_some() {
            Value::String("yahoo_chart".into())
        } else {
            Value::Null
        },
    );
    Ok(json!({
        "instrument_id": record.instrument_id,
        "name": record.name,
        "symbol": record.symbol,
        "return_variant": record.return_variant,
        "provider": record.provider,
        "currency": currency,
        "verification_status": if verified { "verified" } else { "unverified" },
        "provenance": provenance,
        "generation": generation,
        "acquisition": acquisition,
        "profile": profile,
        "summary": {
            "quote_count": count,
            "first_date": first,
            "last_date": last,
            "warning": if publication.is_none() { Value::String("Request Yahoo acquisition before reading history.".into()) } else { Value::Null },
        },
        "history_request": {
            "requested_from": INDEX_HISTORY_START,
            "scope": "full_available_history",
            "warning": "Requests Yahoo history from 1900-01-01 through yesterday. This is a request boundary, not an inception date or a completeness guarantee.",
        },
    }))
}

async fn list_indices(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<Pagination>,
) -> AppResult<Json<Value>> {
    let (limit, offset) = pagination(query, 20, 100)?;
    let records = sqlx::query_as::<_, IndexRecord>(
        "SELECT instrument_id, symbol, name, currency, return_variant,
                provider, provenance
         FROM indices
         WHERE removed_at IS NULL
         ORDER BY name, instrument_id
         LIMIT $1 OFFSET $2",
    )
    .bind(limit)
    .bind(offset)
    .fetch_all(&state.pool)
    .await?;
    let total =
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM indices WHERE removed_at IS NULL")
            .fetch_one(&state.pool)
            .await?;
    let mut items = Vec::with_capacity(records.len());
    for record in records {
        let instrument_id = record.instrument_id.clone();
        items.push(index_detail_value(&state.pool, &instrument_id, Some(record)).await?);
    }
    Ok(Json(
        json!({"items": items, "total": total, "limit": limit, "offset": offset}),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct IndexRequest {
    symbol: String,
    name: String,
    currency: Option<String>,
    #[serde(default = "unknown_return_variant")]
    return_variant: String,
}

fn unknown_return_variant() -> String {
    "unknown".into()
}

async fn create_index(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Json(mut body): Json<IndexRequest>,
) -> AppResult<impl IntoResponse> {
    body.symbol = body.symbol.trim().to_ascii_uppercase();
    body.name = body.name.trim().to_owned();
    body.currency = body
        .currency
        .map(|currency| currency.trim().to_ascii_uppercase());
    if body.symbol.is_empty()
        || body.symbol.len() > 64
        || !SYMBOL.is_match(&body.symbol)
        || body.name.is_empty()
        || body.name.len() > 200
        || body.currency.as_ref().is_some_and(|currency| {
            currency.len() != 3 || !currency.chars().all(|c| c.is_ascii_uppercase())
        })
        || !matches!(
            body.return_variant.as_str(),
            "unknown" | "price" | "net_total_return" | "gross_total_return"
        )
    {
        return Err(AppError::unprocessable("Invalid index catalogue entry"));
    }
    let existing =
        sqlx::query_scalar::<_, String>("SELECT instrument_id FROM indices WHERE symbol = $1")
            .bind(&body.symbol)
            .fetch_optional(&state.pool)
            .await?;
    let instrument_id = if let Some(instrument_id) = existing {
        sqlx::query(
            "UPDATE indices SET removed_at = NULL, removed_by = NULL WHERE instrument_id = $1",
        )
        .bind(&instrument_id)
        .execute(&state.pool)
        .await?;
        instrument_id
    } else {
        let instrument_id = format!("idx-{}", Uuid::new_v4());
        sqlx::query(
            "INSERT INTO indices
                (id, instrument_id, symbol, name, currency, return_variant,
                 verification_status, provider, provenance, created_at)
             VALUES ($1, $1, $2, $3, $4, $5, 'unverified', 'yfinance', $6, now())",
        )
        .bind(&instrument_id)
        .bind(&body.symbol)
        .bind(&body.name)
        .bind(&body.currency)
        .bind(&body.return_variant)
        .bind(json!({"source": "manual", "metadata_verified": false, "added_by": user.id}))
        .execute(&state.pool)
        .await?;
        instrument_id
    };
    let item = index_detail_value(&state.pool, &instrument_id, None).await?;
    Ok((StatusCode::CREATED, Json(item)))
}

#[derive(Debug, Deserialize)]
struct SearchQuery {
    q: String,
    limit: Option<usize>,
}

async fn search_indices(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<SearchQuery>,
) -> AppResult<Json<Value>> {
    let term = query.q.trim();
    let limit = query.limit.unwrap_or(15);
    if term.len() < 2 || term.len() > 100 || !(1..=20).contains(&limit) {
        return Err(AppError::unprocessable(
            "Enter at least two characters to search indices.",
        ));
    }
    let client = reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(state.config.market_data_timeout)
        .user_agent("Mozilla/5.0 Hedgehoog/0.1")
        .build()
        .map_err(|error| AppError::Internal(error.to_string()))?;
    let payload = client
        .get("https://query2.finance.yahoo.com/v1/finance/search")
        .query(&[
            ("q", term),
            ("quotesCount", "50"),
            ("newsCount", "0"),
            ("listsCount", "0"),
            ("enableFuzzyQuery", "false"),
        ])
        .send()
        .await
        .map_err(|_| {
            AppError::service_unavailable(
                "Index search is unavailable. Please retry or use manual entry.",
            )
        })?
        .json::<Value>()
        .await
        .map_err(|_| {
            AppError::service_unavailable(
                "Index search is unavailable. Please retry or use manual entry.",
            )
        })?;
    let mut items = Vec::new();
    let mut seen = HashSet::new();
    for row in payload
        .get("quotes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if row.get("quoteType").and_then(Value::as_str) != Some("INDEX") {
            continue;
        }
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let Some(name) = row
            .get("longname")
            .or_else(|| row.get("shortname"))
            .and_then(Value::as_str)
            .map(str::trim)
        else {
            continue;
        };
        if symbol.len() > 64
            || !SYMBOL.is_match(symbol)
            || name.is_empty()
            || name.len() > 200
            || !seen.insert(symbol.to_owned())
        {
            continue;
        }
        let currency = row
            .get("currency")
            .and_then(Value::as_str)
            .filter(|currency| {
                currency.len() == 3 && currency.chars().all(|c| c.is_ascii_uppercase())
            });
        items.push(json!({
            "symbol": symbol,
            "name": name,
            "currency": currency,
            "return_variant": "unknown",
        }));
        if items.len() >= limit {
            break;
        }
    }
    Ok(Json(json!({"items": items})))
}

async fn index_detail(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        index_detail_value(&state.pool, &instrument_id, None).await?,
    ))
}

async fn remove_index(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(instrument_id): Path<String>,
) -> AppResult<Json<Value>> {
    validate_index_id(&instrument_id)?;
    let result = sqlx::query(
        "UPDATE indices SET removed_at = now(), removed_by = $2
         WHERE instrument_id = $1 AND removed_at IS NULL",
    )
    .bind(&instrument_id)
    .bind(user.id)
    .execute(&state.pool)
    .await?;
    if result.rows_affected() == 0 {
        let exists = sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS(SELECT 1 FROM indices WHERE instrument_id = $1)",
        )
        .bind(&instrument_id)
        .fetch_one(&state.pool)
        .await?;
        if !exists {
            return Err(AppError::not_found(
                "Index not found in the index catalogue",
            ));
        }
    }
    Ok(Json(
        json!({"instrument_id": instrument_id, "removed": true}),
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Confirmed {
    confirmed: bool,
}

fn require_confirmation(body: &Confirmed) -> AppResult<()> {
    if !body.confirmed {
        return Err(AppError::unprocessable(
            "Explicit acquisition authorization is required",
        ));
    }
    Ok(())
}

async fn acquire_all_indices(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Json(body): Json<Confirmed>,
) -> AppResult<impl IntoResponse> {
    require_confirmation(&body)?;
    let identifiers = sqlx::query_scalar::<_, String>(
        "SELECT instrument_id FROM indices WHERE removed_at IS NULL ORDER BY instrument_id",
    )
    .fetch_all(&state.pool)
    .await?;
    let mut jobs = Vec::new();
    for instrument_id in identifiers {
        let request = request_automatic_acquisition(&state.pool, &instrument_id, &user.id, false)
            .await
            .map_err(market_error)?;
        if let Some(job) = public_acquisition_job(&state.pool, &request.id).await? {
            jobs.push(job);
        }
    }
    Ok((StatusCode::ACCEPTED, Json(json!({"jobs": jobs}))))
}

async fn acquire_index(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(instrument_id): Path<String>,
    Json(body): Json<Confirmed>,
) -> AppResult<impl IntoResponse> {
    require_confirmation(&body)?;
    require_index(&state.pool, &instrument_id).await?;
    let request = request_automatic_acquisition(&state.pool, &instrument_id, &user.id, false)
        .await
        .map_err(market_error)?;
    let job = public_acquisition_job(&state.pool, &request.id)
        .await?
        .unwrap_or(Value::Null);
    Ok((StatusCode::ACCEPTED, Json(json!({"job": job}))))
}

#[derive(Debug, FromRow)]
struct LatestPublication {
    id: String,
    listing: Option<Value>,
}

async fn latest_publication(
    pool: &PgPool,
    instrument_id: &str,
) -> AppResult<Option<LatestPublication>> {
    Ok(sqlx::query_as::<_, LatestPublication>(
        "SELECT id, listing
         FROM acquisition_jobs
         WHERE instrument_id = $1 AND provider = 'yfinance' AND published = true
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?)
}

async fn latest_job(pool: &PgPool, instrument_id: &str) -> AppResult<Value> {
    let job_id = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1
         ORDER BY requested_at DESC, id DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?;
    match job_id {
        Some(job_id) => Ok(public_acquisition_job(pool, &job_id)
            .await?
            .unwrap_or(Value::Null)),
        None => Ok(Value::Null),
    }
}

async fn search_instruments(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<SearchQuery>,
) -> AppResult<Json<Value>> {
    let term = query.q.trim();
    let limit = query.limit.unwrap_or(15);
    if term.len() < 2 || term.len() > 100 || !(1..=50).contains(&limit) {
        return Err(AppError::unprocessable(
            "Enter at least two non-whitespace characters",
        ));
    }
    let pattern = format!("%{}%", term.replace('%', "\\%").replace('_', "\\_"));
    let rows = sqlx::query(
        "WITH latest AS (
             SELECT DISTINCT ON (isin) isin, name, ticker, asset_class, currency
             FROM etfs
             ORDER BY isin, import_date DESC
         )
         SELECT isin, name, ticker, asset_class, currency
         FROM latest
         WHERE isin ILIKE $1 ESCAPE '\\'
            OR COALESCE(ticker, '') ILIKE $1 ESCAPE '\\'
            OR name ILIKE $1 ESCAPE '\\'
         ORDER BY name, isin
         LIMIT $2",
    )
    .bind(pattern)
    .bind(limit as i64)
    .fetch_all(&state.pool)
    .await?;
    let items = rows
        .into_iter()
        .map(|row| {
            let isin: String = row.get("isin");
            json!({
                "isin": isin,
                "instrument_id": isin,
                "name": row.get::<String, _>("name"),
                "ticker": row.get::<Option<String>, _>("ticker"),
                "asset_class": row.get::<Option<String>, _>("asset_class"),
                "currency": row.get::<Option<String>, _>("currency"),
            })
        })
        .collect::<Vec<_>>();
    Ok(Json(json!({"items": items})))
}

#[derive(Debug, Deserialize)]
struct AcquiredQuery {
    limit: Option<i64>,
    offset: Option<i64>,
    min_history_years: Option<String>,
    #[serde(default)]
    hide_negative_returns: bool,
}

async fn acquired_rows(pool: &PgPool) -> AppResult<Vec<Value>> {
    let rows = sqlx::query(
        "WITH publications AS (
             SELECT DISTINCT ON (instrument_id)
                    id, instrument_id, listing, history_summary, requested_at, published_at
             FROM acquisition_jobs
             WHERE provider = 'yfinance' AND published = true
               AND instrument_id NOT LIKE 'idx-%'
             ORDER BY instrument_id, requested_at DESC, published_at DESC NULLS LAST, id DESC
         ), catalogues AS (
             SELECT DISTINCT ON (isin) isin, name, ticker
             FROM etfs ORDER BY isin, import_date DESC
         )
         SELECT p.id AS generation, p.instrument_id, p.listing, p.history_summary,
                profile.collected_at, profile.asset_class, profile.index_name,
                c.name, c.ticker
         FROM publications p
         LEFT JOIN instruments profile ON profile.job_id = p.id
         LEFT JOIN catalogues c ON c.isin = p.instrument_id
         ORDER BY profile.collected_at DESC NULLS LAST, p.instrument_id",
    )
    .fetch_all(pool)
    .await?;
    let policy = super::quote_fill_policy(pool).await?;
    let mut items = Vec::new();
    for row in rows {
        let generation: String = row.get("generation");
        let collected_at = row.get::<Option<DateTime<Utc>>, _>("collected_at");
        if collected_at.is_none() {
            return Err(AppError::conflict(
                "The latest Yahoo profile is unavailable. Request Yahoo history again.",
            ));
        }
        let instrument_id: String = row.get("instrument_id");
        let summary = row
            .get::<Option<Value>, _>("history_summary")
            .unwrap_or_else(|| json!({}));
        let statistic_rows =
            sqlx::query_as::<_, (NaiveDate, Option<Decimal>, Option<Decimal>, Option<String>)>(
                "SELECT quote_date, adj_close, close, price_currency
             FROM quotes WHERE job_id = $1 ORDER BY quote_date",
            )
            .bind(&generation)
            .fetch_all(pool)
            .await?;
        let override_currency = sqlx::query_scalar::<_, Option<String>>(
            "SELECT price_currency_override FROM instrument_state WHERE instrument_id = $1",
        )
        .bind(&instrument_id)
        .fetch_optional(pool)
        .await?
        .flatten();
        let statistics = calculate_statistics(
            &statistic_rows,
            override_currency.as_deref(),
            &instrument_id,
            &policy,
        );
        let mut item = Map::new();
        item.insert("instrument_id".into(), json!(instrument_id));
        item.insert("isin".into(), json!(instrument_id));
        item.insert("name".into(), json!(row.get::<Option<String>, _>("name")));
        item.insert(
            "ticker".into(),
            json!(row.get::<Option<String>, _>("ticker")),
        );
        item.insert(
            "listing".into(),
            row.get::<Option<Value>, _>("listing")
                .unwrap_or(Value::Null),
        );
        item.insert("generation".into(), json!(generation));
        item.insert("collected_at".into(), json!(collected_at));
        item.insert(
            "asset_class".into(),
            json!(row.get::<Option<String>, _>("asset_class")),
        );
        item.insert(
            "index".into(),
            json!(row.get::<Option<String>, _>("index_name")),
        );
        for key in ["quote_count", "first_date", "last_date"] {
            item.insert(key.into(), summary.get(key).cloned().unwrap_or(Value::Null));
        }
        for key in [
            "annualized_mean_log_return",
            "annualized_volatility",
            "historical_year_return",
            "historical_year_start_date",
            "historical_year_end_date",
            "historical_year_price_basis",
            "historical_year_carried_from_date",
            "historical_year_volatility",
            "historical_year_volatility_start_date",
            "historical_year_volatility_end_date",
            "historical_year_volatility_price_basis",
            "historical_year_volatility_return_count",
        ] {
            item.insert(
                key.into(),
                statistics.get(key).cloned().unwrap_or(Value::Null),
            );
        }
        items.push(Value::Object(item));
    }
    Ok(items)
}

async fn acquired_instruments(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<AcquiredQuery>,
) -> AppResult<Json<Value>> {
    let (limit, offset) = pagination(
        Pagination {
            limit: query.limit,
            offset: query.offset,
        },
        20,
        100,
    )?;
    let years = match query.min_history_years.as_deref() {
        None => None,
        Some("5") => Some(5),
        Some("15") => Some(15),
        Some("30") => Some(30),
        _ => return Err(AppError::unprocessable("Invalid history filter")),
    };
    let mut items = acquired_rows(&state.pool).await?;
    if let Some(years) = years {
        items.retain(|item| {
            let Some(start) = item
                .get("first_date")
                .and_then(Value::as_str)
                .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
            else {
                return false;
            };
            let Some(end) = item
                .get("last_date")
                .and_then(Value::as_str)
                .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
            else {
                return false;
            };
            anniversary(start, years).is_some_and(|boundary| end >= boundary)
        });
    }
    if query.hide_negative_returns {
        items.retain(|item| {
            ["historical_year_return", "annualized_mean_log_return"]
                .into_iter()
                .all(|key| {
                    item.get(key)
                        .and_then(Value::as_f64)
                        .is_some_and(|value| value >= 0.0)
                })
        });
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

fn anniversary(start: NaiveDate, years: i32) -> Option<NaiveDate> {
    start
        .with_year(start.year() + years)
        .or_else(|| NaiveDate::from_ymd_opt(start.year() + years, 2, 28))
}

async fn acquired_universe(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
) -> AppResult<Json<Value>> {
    let items = acquired_rows(&state.pool)
        .await?
        .into_iter()
        .map(|item| {
            json!({
                "instrument_id": item.get("instrument_id"),
                "name": item.get("name"),
                "ticker": item.get("ticker"),
                "collected_at": item.get("collected_at"),
            })
        })
        .collect::<Vec<_>>();
    Ok(Json(json!({"total": items.len(), "items": items})))
}

async fn budget(CurrentUser(_user): CurrentUser) -> Json<Value> {
    Json(json!({
        "provider": "yfinance",
        "metered": false,
        "month": Utc::now().format("%Y-%m").to_string(),
        "limit": Value::Null,
        "used": 0,
        "remaining": Value::Null,
    }))
}

#[derive(Debug, FromRow)]
struct CatalogueRecord {
    isin: String,
    import_date: NaiveDate,
    name: String,
    ticker: Option<String>,
    asset_class: Option<String>,
    currency: Option<String>,
    inception_date: Option<NaiveDate>,
    source: Value,
}

async fn require_etf(pool: &PgPool, instrument_id: &str) -> AppResult<CatalogueRecord> {
    validate_isin(instrument_id)?;
    sqlx::query_as::<_, CatalogueRecord>(
        "SELECT isin, import_date, name, ticker, asset_class, currency, inception_date, source
         FROM etfs WHERE isin = $1 ORDER BY import_date DESC LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::not_found("Instrument not found in the ETF catalogue"))
}

#[derive(Debug, Deserialize)]
struct DetailQuery {
    as_of: Option<DateTime<Utc>>,
}

async fn instrument_detail(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Query(query): Query<DetailQuery>,
) -> AppResult<Json<Value>> {
    let catalogue = require_etf(&state.pool, &instrument_id).await?;
    let state_row = sqlx::query_as::<_, (Option<Value>, Option<Value>, Option<String>)>(
        "SELECT listing, listing_resolution, price_currency_override
         FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(&instrument_id)
    .fetch_optional(&state.pool)
    .await?
    .unwrap_or((None, None, None));
    let publication = latest_publication(&state.pool, &instrument_id).await?;
    if let (Some(publication), Some(listing)) = (&publication, &state_row.0) {
        if publication.listing.as_ref() != Some(listing) {
            return Err(AppError::conflict(
                "The latest Yahoo listing changed. Refresh instrument details before reading history.",
            ));
        }
    }
    let generation = publication
        .as_ref()
        .map(|publication| publication.id.clone());
    let profile = if let Some(generation) = generation.as_deref() {
        sqlx::query(
            "SELECT collected_at, collection_date, data, asset_class, index_name
             FROM instruments WHERE job_id = $1",
        )
        .bind(generation)
        .fetch_optional(&state.pool)
        .await?
        .map(|row| {
            json!({
                "collected_at": row.get::<DateTime<Utc>, _>("collected_at"),
                "collection_date": row.get::<NaiveDate, _>("collection_date"),
                "data": row.get::<Value, _>("data"),
                "asset_class": row.get::<Option<String>, _>("asset_class"),
                "index": row.get::<Option<String>, _>("index_name"),
            })
        })
    } else {
        None
    };
    let cutoff = query.as_of.unwrap_or_else(Utc::now).date_naive();
    let (count, first, last) = if let Some(generation) = generation.as_deref() {
        sqlx::query_as::<_, (i64, Option<NaiveDate>, Option<NaiveDate>)>(
            "SELECT COUNT(DISTINCT quote_date), MIN(quote_date), MAX(quote_date)
             FROM quotes WHERE job_id = $1 AND quote_date <= $2",
        )
        .bind(generation)
        .bind(cutoff)
        .fetch_one(&state.pool)
        .await?
    } else {
        (0, None, None)
    };
    let history_status = if generation.is_none() {
        "not_requested"
    } else if count == 0 {
        "empty"
    } else if catalogue
        .inception_date
        .zip(first)
        .is_none_or(|(inception, first)| (first - inception).num_days() > 7)
    {
        "partial"
    } else {
        "available"
    };
    let warning = match (generation.is_some(), count, catalogue.inception_date, first) {
        (false, _, _, _) => Some(
            "No published Yahoo history is available. Request Yahoo acquisition before reading history."
                .to_owned(),
        ),
        (true, 0, _, _) => None,
        (true, _, Some(inception), Some(first)) if (first - inception).num_days() > 7 => Some(
            format!("Incomplete since-inception history: requested from {inception}, but the earliest stored quote is {first}. Provider or listing coverage may start after fund inception; a completed request does not certify complete historical coverage."),
        ),
        _ => None,
    };
    let mut catalogue_json = catalogue.source.as_object().cloned().unwrap_or_default();
    catalogue_json.insert("isin".into(), json!(catalogue.isin));
    catalogue_json.insert("import_date".into(), json!(catalogue.import_date));
    catalogue_json.insert("name".into(), json!(catalogue.name));
    catalogue_json.insert("ticker".into(), json!(catalogue.ticker));
    catalogue_json.insert("asset_class".into(), json!(catalogue.asset_class));
    catalogue_json.insert("currency".into(), json!(catalogue.currency));
    catalogue_json.insert("inception_date".into(), json!(catalogue.inception_date));
    Ok(Json(json!({
        "instrument_id": instrument_id,
        "generation": generation,
        "catalogue": catalogue_json,
        "listing": state_row.0,
        "listing_resolution": state_row.1,
        "price_currency_override": state_row.2,
        "profile": profile,
        "acquisition": latest_job(&state.pool, &instrument_id).await?,
        "summary": {
            "quote_count": count,
            "first_date": first,
            "last_date": last,
            "requested_from": catalogue.inception_date,
            "history_status": history_status,
            "warning": warning,
        },
        "provider": "yfinance",
        "price_basis": "provider_split_adjusted",
        "provider_configured": true,
        "budget": Value::Null,
    })))
}

async fn remove_instrument(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
) -> AppResult<StatusCode> {
    require_etf(&state.pool, &instrument_id).await?;
    let mut transaction = state.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(&instrument_id)
        .execute(&mut *transaction)
        .await?;
    let active = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS(
             SELECT 1 FROM acquisition_jobs WHERE instrument_id = $1 AND active = true
         )",
    )
    .bind(&instrument_id)
    .fetch_one(&mut *transaction)
    .await?;
    if active {
        return Err(AppError::conflict(
            "This ETF has an active acquisition or removal. Wait for it to finish and retry.",
        ));
    }
    sqlx::query(
        "DELETE FROM job_queue
         WHERE id IN (SELECT id FROM acquisition_jobs WHERE instrument_id = $1)",
    )
    .bind(&instrument_id)
    .execute(&mut *transaction)
    .await?;
    for statement in [
        "DELETE FROM manual_splits WHERE instrument_id = $1",
        "DELETE FROM instrument_statistics WHERE instrument_id = $1",
        "DELETE FROM instrument_state WHERE instrument_id = $1",
        "DELETE FROM market_data_reader_leases WHERE instrument_id = $1",
        "DELETE FROM market_data_retention WHERE instrument_id = $1",
        "DELETE FROM acquisition_jobs WHERE instrument_id = $1",
    ] {
        sqlx::query(statement)
            .bind(&instrument_id)
            .execute(&mut *transaction)
            .await?;
    }
    transaction.commit().await?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
struct GenerationQuery {
    generation: Option<String>,
}

async fn instrument_statistics(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Query(query): Query<GenerationQuery>,
) -> AppResult<Json<Value>> {
    let catalogue = require_etf(&state.pool, &instrument_id).await?;
    let publication =
        selected_publication(&state.pool, &instrument_id, query.generation.as_deref()).await?;
    let state_row = sqlx::query_as::<_, (Option<Value>, Option<String>)>(
        "SELECT listing, price_currency_override FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(&instrument_id)
    .fetch_optional(&state.pool)
    .await?
    .unwrap_or((None, None));
    let rows = if let Some(publication) = publication.as_ref() {
        sqlx::query_as::<_, (NaiveDate, Option<Decimal>, Option<Decimal>, Option<String>)>(
            "SELECT quote_date, adj_close, close, price_currency
             FROM quotes WHERE job_id = $1 ORDER BY quote_date",
        )
        .bind(&publication.id)
        .fetch_all(&state.pool)
        .await?
    } else {
        Vec::new()
    };
    let policy = super::quote_fill_policy(&state.pool).await?;
    let statistics = calculate_statistics(&rows, state_row.1.as_deref(), &instrument_id, &policy);
    let currency = statistics.get("price_currency").and_then(Value::as_str);
    let reason = if rows.is_empty() {
        Some("Request Yahoo acquisition for this instrument first; no readable published Yahoo history is available at these quote dates.")
    } else if currency != Some("EUR") {
        Some("Only EUR-priced history can be simulated. Confirm missing price currency on the instrument page; non-EUR or mixed prices require FX conversion.")
    } else if statistics
        .get("return_count")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        < 60
    {
        Some("At least 60 usable daily returns are required; download more EOD history.")
    } else {
        None
    };
    let mut output = statistics.as_object().cloned().unwrap_or_default();
    output.insert("instrument_id".into(), json!(instrument_id));
    output.insert("name".into(), json!(catalogue.name));
    output.insert("listing".into(), state_row.0.unwrap_or(Value::Null));
    output.insert(
        "generation".into(),
        publication
            .map(|publication| json!(publication.id))
            .unwrap_or(Value::Null),
    );
    output.insert("eligible".into(), json!(reason.is_none()));
    output.insert("ineligible_reason".into(), json!(reason));
    Ok(Json(Value::Object(output)))
}

type StatisticsRow = (NaiveDate, Option<Decimal>, Option<Decimal>, Option<String>);

#[derive(Debug)]
struct DailyStatistics {
    basis: &'static str,
    prices: Vec<(NaiveDate, f64)>,
    returns: Vec<f64>,
    invalid_prices: usize,
    skipped_gaps: usize,
    weekend_quotes: usize,
    mean: Option<f64>,
    variance: Option<f64>,
    warnings: Vec<String>,
}

fn price(row: &crate::quote_fill::QuoteRow, basis: &str) -> Option<f64> {
    let value = if basis == "adj_close" {
        row.adjusted
    } else {
        row.close
    }?;
    value
        .to_f64()
        .filter(|value| value.is_finite() && *value > 0.0)
}

fn daily_statistics(rows: &[crate::quote_fill::QuoteRow]) -> DailyStatistics {
    let weekday_rows = rows
        .iter()
        .filter(|row| row.date.weekday().number_from_monday() <= 5)
        .collect::<Vec<_>>();
    let weekend_quotes = rows.len().saturating_sub(weekday_rows.len());
    let basis = if !weekday_rows.is_empty()
        && weekday_rows
            .iter()
            .all(|row| price(row, "adj_close").is_some())
    {
        "adj_close"
    } else {
        "close"
    };
    let mut prices = Vec::new();
    let mut returns = Vec::new();
    let mut invalid_prices = 0;
    let mut skipped_gaps = 0;
    let mut previous: Option<(NaiveDate, f64)> = None;
    for row in weekday_rows {
        let Some(current_price) = price(row, basis) else {
            invalid_prices += 1;
            previous = None;
            continue;
        };
        prices.push((row.date, current_price));
        if let Some((previous_date, previous_price)) = previous {
            let gap = (row.date - previous_date).num_days();
            let weekdays = (1..=gap.min(7))
                .filter(|days| {
                    (previous_date + chrono::Duration::days(*days))
                        .weekday()
                        .number_from_monday()
                        <= 5
                })
                .count();
            if gap > 7 || weekdays != 1 {
                skipped_gaps += 1;
            } else {
                let log_return = current_price.ln() - previous_price.ln();
                if log_return.is_finite() {
                    returns.push(log_return);
                }
            }
        }
        previous = Some((row.date, current_price));
    }
    let mean = (!returns.is_empty()).then(|| returns.iter().sum::<f64>() / returns.len() as f64);
    let variance = (returns.len() >= 2).then(|| {
        let mean = mean.expect("mean for nonempty returns");
        returns
            .iter()
            .map(|value| (value - mean).powi(2))
            .sum::<f64>()
            / (returns.len() - 1) as f64
    });
    let mut warnings = Vec::new();
    if weekend_quotes > 0 {
        warnings.push(format!(
            "{weekend_quotes} weekend quote(s) excluded from weekday return estimation."
        ));
    }
    if basis == "close" && !rows.is_empty() {
        warnings.push("Adjusted closes are incomplete; all returns use unadjusted closes. Splits and distributions can distort these estimates.".into());
    }
    if invalid_prices > 0 {
        warnings.push(format!(
            "{invalid_prices} non-positive, missing or non-finite price(s) excluded; returns do not bridge these rows."
        ));
    }
    if skipped_gaps > 0 {
        warnings.push(format!(
            "{skipped_gaps} interval(s) spanning missing weekdays excluded from daily returns. Exchange holidays may also be excluded; no exchange calendar is assumed."
        ));
    }
    if returns.len() < 252 {
        warnings.push(
            "Less than one trading year of usable returns; estimates may be particularly unreliable."
                .into(),
        );
    }
    if returns
        .iter()
        .any(|value| *value > 1.5_f64.ln() || *value < 0.5_f64.ln())
    {
        warnings.push("History includes daily moves greater than 50%; check corporate actions and data quality before relying on a projection.".into());
    }
    DailyStatistics {
        basis,
        prices,
        returns,
        invalid_prices,
        skipped_gaps,
        weekend_quotes,
        mean,
        variance,
        warnings,
    }
}

fn historical_window(
    rows: &[crate::quote_fill::QuoteRow],
) -> Option<&[crate::quote_fill::QuoteRow]> {
    let end = rows.last()?;
    let anniversary = end
        .date
        .with_year(end.date.year() - 1)
        .or_else(|| NaiveDate::from_ymd_opt(end.date.year() - 1, end.date.month(), 28))?;
    let start = rows[..rows.len().saturating_sub(1)]
        .iter()
        .rposition(|row| row.date <= anniversary)?;
    if (anniversary - rows[start].date).num_days() > 7 {
        return None;
    }
    let currencies = rows[start..]
        .iter()
        .filter_map(|row| row.currency.as_deref())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase)
        .collect::<HashSet<_>>();
    (currencies.len() <= 1).then_some(&rows[start..])
}

fn calculate_statistics(
    rows: &[StatisticsRow],
    override_currency: Option<&str>,
    instrument_id: &str,
    policy_value: &Value,
) -> Value {
    let mut currencies = HashSet::new();
    let mut unknown_currency = false;
    let mut filled_rows = rows
        .iter()
        .map(|(date, adjusted, close, currency)| {
            if let Some(currency) = currency.as_deref().filter(|value| !value.trim().is_empty()) {
                currencies.insert(currency.trim().to_ascii_uppercase());
            } else {
                unknown_currency = true;
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
    let raw_rows = filled_rows.clone();
    let policy = crate::quote_fill::Policy::from_json(policy_value);
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
    let filled_quote_count = raw_rows
        .iter()
        .zip(&filled_rows)
        .filter(|(raw, filled)| {
            (raw.close.is_none() && filled.close.is_some())
                || (raw.adjusted.is_none() && filled.adjusted.is_some())
        })
        .count();
    let daily = daily_statistics(&filled_rows);
    let currency = if let Some(override_currency) = override_currency {
        (currencies.is_empty() || currencies == HashSet::from([override_currency.to_owned()]))
            .then_some(override_currency)
    } else if currencies.len() == 1 && !unknown_currency {
        currencies.iter().next().map(String::as_str)
    } else {
        None
    };

    let mut historical_rows = filled_rows.clone();
    let carry_currency_supported = override_currency.is_none_or(|value| value == "EUR")
        && historical_rows.iter().all(|row| {
            row.currency
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_ascii_uppercase)
                .or_else(|| override_currency.map(str::to_owned))
                .as_deref()
                .is_none_or(|value| value == "EUR")
        });
    if carry_currency_supported {
        crate::quote_fill::carry_trailing_smooth(&mut historical_rows, &policy);
    }
    let mut historical_return = None;
    let mut historical_start = None;
    let mut historical_end = None;
    let mut historical_basis = None;
    let mut historical_carried_from = None;
    if let Some(window) = historical_window(&historical_rows) {
        let start = &window[0];
        let end = &window[window.len() - 1];
        let basis = if price(start, "adj_close").is_some() && price(end, "adj_close").is_some() {
            "adj_close"
        } else {
            "close"
        };
        if let (Some(start_price), Some(end_price)) = (price(start, basis), price(end, basis)) {
            let value = end_price / start_price - 1.0;
            if value.is_finite() {
                historical_return = Some(value);
                historical_start = Some(start.date);
                historical_end = Some(end.date);
                historical_basis = Some(basis);
                historical_carried_from = end.carried_from_date;
            }
        }
    }

    let mut historical_volatility = None;
    let mut volatility_start = None;
    let mut volatility_end = None;
    let mut volatility_basis = None;
    let mut volatility_return_count = None;
    if let Some(window) = historical_window(&filled_rows) {
        let statistics = daily_statistics(window);
        if let Some(variance) = statistics.variance {
            historical_volatility = Some((variance * 252.0).sqrt());
            volatility_start = window.first().map(|row| row.date);
            volatility_end = window.last().map(|row| row.date);
            volatility_basis = Some(statistics.basis);
            volatility_return_count = Some(statistics.returns.len());
        }
    }
    let annualized_volatility = daily.variance.map(|value| (value * 252.0).sqrt());
    let mut warnings = daily.warnings.clone();
    if let (Some(source), Some(end)) = (historical_carried_from, historical_end) {
        warnings.push(format!(
            "Historical annual return uses a stale last valid close from {source} through {end}; trailing missing closes count as zero price returns."
        ));
    }
    json!({
        "price_basis": daily.basis,
        "quote_count": rows.len(),
        "price_count": daily.prices.len(),
        "return_count": daily.returns.len(),
        "first_date": daily.prices.first().map(|row| row.0),
        "last_date": daily.prices.last().map(|row| row.0),
        "invalid_price_count": daily.invalid_prices,
        "filled_quote_count": filled_quote_count,
        "quote_fill_policy": policy_value,
        "skipped_gap_count": daily.skipped_gaps,
        "weekend_quote_count": daily.weekend_quotes,
        "daily_mean_log_return": daily.mean,
        "daily_log_return_variance": daily.variance,
        "daily_volatility": daily.variance.map(f64::sqrt),
        "annualized_mean_log_return": daily.mean.map(|value| value * 252.0),
        "annualized_volatility": annualized_volatility,
        "historical_year_return": historical_return,
        "historical_year_start_date": historical_start,
        "historical_year_end_date": historical_end,
        "historical_year_price_basis": historical_basis,
        "historical_year_carried_from_date": historical_carried_from,
        "historical_year_volatility": historical_volatility,
        "historical_year_volatility_start_date": volatility_start,
        "historical_year_volatility_end_date": volatility_end,
        "historical_year_volatility_price_basis": volatility_basis,
        "historical_year_volatility_return_count": volatility_return_count,
        "price_currency": currency,
        "currency_source": if override_currency.is_some() && currency.is_some() { "manual" } else if currency.is_some() { "provider" } else { "unknown" },
        "computed_at": Utc::now(),
        "warnings": warnings,
    })
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CurrencyRequest {
    price_currency: String,
}

async fn set_price_currency(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Json(mut body): Json<CurrencyRequest>,
) -> AppResult<Json<Value>> {
    require_etf(&state.pool, &instrument_id).await?;
    body.price_currency = body.price_currency.trim().to_ascii_uppercase();
    if !PRICE_CURRENCIES.contains(&body.price_currency.as_str()) {
        return Err(AppError::unprocessable("Unsupported price currency"));
    }
    sqlx::query(
        "INSERT INTO instrument_state (instrument_id, price_currency_override)
         VALUES ($1, $2)
         ON CONFLICT (instrument_id) DO UPDATE
         SET price_currency_override = EXCLUDED.price_currency_override",
    )
    .bind(&instrument_id)
    .bind(&body.price_currency)
    .execute(&state.pool)
    .await?;
    Ok(Json(
        json!({"price_currency_override": body.price_currency}),
    ))
}

async fn read_splits(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Query(query): Query<DetailQuery>,
) -> AppResult<Json<Value>> {
    require_etf(&state.pool, &instrument_id).await?;
    let cutoff = query.as_of.unwrap_or_else(Utc::now);
    let listing = sqlx::query_scalar::<_, Option<Value>>(
        "SELECT listing FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(&instrument_id)
    .fetch_optional(&state.pool)
    .await?
    .flatten();
    let publication = latest_publication(&state.pool, &instrument_id).await?;
    let provider = if let Some(publication) = publication.as_ref() {
        sqlx::query_scalar::<_, Value>("SELECT events FROM split_snapshots WHERE job_id = $1")
            .bind(&publication.id)
            .fetch_optional(&state.pool)
            .await?
            .and_then(|events| events.as_array().cloned())
            .unwrap_or_default()
            .into_iter()
            .filter(|event| {
                event
                    .get("date")
                    .and_then(Value::as_str)
                    .is_some_and(|date| date <= cutoff.date_naive().to_string().as_str())
            })
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let manual = sqlx::query(
        "SELECT id, split_date, factor, note, created_at
         FROM manual_splits
         WHERE instrument_id = $1 AND created_at <= $2
           AND (removed_at IS NULL OR removed_at > $2)
         ORDER BY split_date, id",
    )
    .bind(&instrument_id)
    .bind(cutoff)
    .fetch_all(&state.pool)
    .await?
    .into_iter()
    .map(|row| {
        json!({
            "id": row.get::<String, _>("id"),
            "date": row.get::<NaiveDate, _>("split_date"),
            "factor": row.get::<Decimal, _>("factor").to_string(),
            "note": row.get::<String, _>("note"),
            "created_at": row.get::<DateTime<Utc>, _>("created_at"),
        })
    })
    .collect::<Vec<_>>();
    Ok(Json(json!({
        "instrument_id": instrument_id,
        "listing": listing,
        "supported": false,
        "manual": manual,
        "provider": provider,
        "error": Value::Null,
    })))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManualSplitRequest {
    id: Uuid,
    symbol: String,
    exchange: String,
    date: String,
    factor: String,
    #[serde(default)]
    note: String,
}

async fn create_manual_split(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Json(body): Json<ManualSplitRequest>,
) -> AppResult<(StatusCode, Json<Value>)> {
    let catalogue = require_etf(&state.pool, &instrument_id).await?;
    let symbol = body.symbol.trim().to_ascii_uppercase();
    let exchange = body.exchange.trim().to_ascii_uppercase();
    if !SYMBOL.is_match(&symbol)
        || symbol.len() > 64
        || !MIC.is_match(&exchange)
        || body.note.len() > 500
    {
        return Err(AppError::unprocessable("Invalid manual split"));
    }
    let date = NaiveDate::parse_from_str(&body.date, "%Y-%m-%d")
        .map_err(|_| AppError::unprocessable("Invalid split date"))?;
    if date > Utc::now().date_naive() {
        return Err(AppError::unprocessable(
            "The effective date cannot be in the future",
        ));
    }
    if catalogue
        .inception_date
        .is_some_and(|inception| date < inception)
    {
        return Err(AppError::unprocessable(
            "The split date cannot be before the ETF inception date",
        ));
    }
    let factor = body
        .factor
        .parse::<Decimal>()
        .map_err(|_| AppError::unprocessable("Invalid split factor"))?;
    if factor == Decimal::ONE
        || factor < Decimal::new(1, 9)
        || factor > Decimal::from(1_000_000_000_u64)
    {
        return Err(AppError::unprocessable("Invalid split factor"));
    }
    let listing = sqlx::query_scalar::<_, Option<Value>>(
        "SELECT listing FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(&instrument_id)
    .fetch_optional(&state.pool)
    .await?
    .flatten();
    if listing.as_ref() != Some(&json!({"symbol": symbol, "exchange": exchange}))
        || !sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS(SELECT 1 FROM quotes WHERE instrument_id = $1)",
        )
        .bind(&instrument_id)
        .fetch_one(&state.pool)
        .await?
    {
        return Err(AppError::conflict(
            "Save quotes for this listing before adding a manual split. Refresh instrument details.",
        ));
    }
    let _ = body.id;
    Err(AppError::conflict(
        "Yahoo prices are already split-adjusted. Manual split multiplication is not supported for this price basis.",
    ))
}

async fn remove_manual_split(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path((instrument_id, split_id)): Path<(String, Uuid)>,
) -> AppResult<StatusCode> {
    require_etf(&state.pool, &instrument_id).await?;
    let result = sqlx::query(
        "UPDATE manual_splits
         SET active = false, removed_at = now(), removed_by = $3
         WHERE id = $1 AND instrument_id = $2 AND active = true",
    )
    .bind(split_id.to_string())
    .bind(&instrument_id)
    .bind(user.id)
    .execute(&state.pool)
    .await?;
    if result.rows_affected() == 0 {
        return Err(AppError::not_found("Manual split not found"));
    }
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Debug, Deserialize)]
struct QuotesQuery {
    limit: Option<i64>,
    offset: Option<i64>,
    as_of: Option<DateTime<Utc>>,
    generation: Option<String>,
}

async fn instrument_quotes(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Query(query): Query<QuotesQuery>,
) -> AppResult<Json<Value>> {
    require_etf(&state.pool, &instrument_id).await?;
    quotes_value(&state.pool, &instrument_id, query)
        .await
        .map(Json)
}

async fn index_quotes(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Path(instrument_id): Path<String>,
    Query(query): Query<QuotesQuery>,
) -> AppResult<Json<Value>> {
    require_index(&state.pool, &instrument_id).await?;
    quotes_value(&state.pool, &instrument_id, query)
        .await
        .map(Json)
}

async fn selected_publication(
    pool: &PgPool,
    instrument_id: &str,
    generation: Option<&str>,
) -> AppResult<Option<LatestPublication>> {
    let publication = latest_publication(pool, instrument_id).await?;
    if let Some(generation) = generation {
        if generation == "legacy"
            || publication
                .as_ref()
                .is_none_or(|publication| publication.id != generation)
        {
            return Err(AppError::conflict(
                "This history generation is unavailable or superseded. Refresh instrument details and retry using the latest Yahoo history.",
            ));
        }
    }
    Ok(publication)
}

async fn quotes_value(pool: &PgPool, instrument_id: &str, query: QuotesQuery) -> AppResult<Value> {
    let (limit, offset) = pagination(
        Pagination {
            limit: query.limit,
            offset: query.offset,
        },
        1000,
        1000,
    )?;
    let as_of = query.as_of.unwrap_or_else(Utc::now);
    let publication =
        selected_publication(pool, instrument_id, query.generation.as_deref()).await?;
    let Some(publication) = publication else {
        return Ok(json!({
            "items": [], "total": 0, "limit": limit, "offset": offset,
            "as_of": as_of, "generation": Value::Null,
            "warning": "No published Yahoo history is available. Request Yahoo acquisition first.",
        }));
    };
    let state_listing = sqlx::query_scalar::<_, Option<Value>>(
        "SELECT listing FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .flatten();
    if publication.listing != state_listing {
        return Err(AppError::conflict(
            "The latest Yahoo listing changed. Refresh instrument details before reading history.",
        ));
    }
    let total = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM quotes WHERE job_id = $1 AND quote_date <= $2",
    )
    .bind(&publication.id)
    .bind(as_of.date_naive())
    .fetch_one(pool)
    .await?;
    let rows = sqlx::query(
        "SELECT quote_date, collected_at, collection_date, data, quality_flags,
                open, high, low, close, volume, adj_close
         FROM quotes
         WHERE job_id = $1 AND quote_date <= $2
         ORDER BY quote_date
         LIMIT $3 OFFSET $4",
    )
    .bind(&publication.id)
    .bind(as_of.date_naive())
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await?;
    let mut items = Vec::with_capacity(rows.len());
    for row in rows {
        let date = row.get::<NaiveDate, _>("quote_date");
        let mut item = row
            .get::<Value, _>("data")
            .as_object()
            .cloned()
            .unwrap_or_default();
        item.insert(
            "provider_date".into(),
            item.get("date").cloned().unwrap_or_else(|| json!(date)),
        );
        item.insert("date".into(), json!(date));
        item.insert(
            "collected_at".into(),
            json!(row.get::<DateTime<Utc>, _>("collected_at")),
        );
        item.insert(
            "collection_date".into(),
            json!(row.get::<NaiveDate, _>("collection_date")),
        );
        item.insert(
            "quality_flags".into(),
            json!(row.get::<Vec<String>, _>("quality_flags")),
        );
        for field in ["open", "high", "low", "close", "volume", "adj_close"] {
            if let Some(value) = row.get::<Option<Decimal>, _>(field) {
                item.insert(field.into(), Value::String(value.to_string()));
            } else if item.get(field).is_some_and(Value::is_number) {
                let value = item.get(field).cloned().unwrap_or(Value::Null);
                item.insert(field.into(), Value::String(value.to_string()));
            }
        }
        items.push(Value::Object(item));
    }
    Ok(json!({
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "as_of": as_of,
        "generation": publication.id,
    }))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AcquisitionBody {
    symbol: Option<String>,
    exchange: Option<String>,
    confirmed: bool,
    #[serde(default = "update_mode")]
    mode: String,
}

fn update_mode() -> String {
    "update".into()
}

async fn acquire_instrument(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,
    Path(instrument_id): Path<String>,
    Json(mut body): Json<AcquisitionBody>,
) -> AppResult<impl IntoResponse> {
    require_etf(&state.pool, &instrument_id).await?;
    if !body.confirmed {
        return Err(AppError::unprocessable(
            "Explicit acquisition authorization is required",
        ));
    }
    if !matches!(body.mode.as_str(), "update" | "backfill") {
        return Err(AppError::unprocessable("Invalid acquisition mode"));
    }
    body.symbol = body.symbol.map(|value| value.trim().to_ascii_uppercase());
    body.exchange = body.exchange.map(|value| value.trim().to_ascii_uppercase());
    if body.exchange.is_some() && body.symbol.is_none() {
        return Err(AppError::unprocessable(
            "Supply a symbol with a manual exchange override",
        ));
    }
    if body
        .symbol
        .as_ref()
        .is_some_and(|symbol| symbol.len() > 64 || !SYMBOL.is_match(symbol))
        || body
            .exchange
            .as_ref()
            .is_some_and(|exchange| !MIC.is_match(exchange))
    {
        return Err(AppError::unprocessable("Invalid listing override"));
    }
    let request = request_user_acquisition(
        &state.pool,
        &instrument_id,
        &user.id,
        body.symbol.as_deref(),
        body.exchange.as_deref(),
    )
    .await
    .map_err(market_error)?;
    let job = public_acquisition_job(&state.pool, &request.id)
        .await?
        .unwrap_or(Value::Null);
    Ok((StatusCode::ACCEPTED, Json(json!({"job": job}))))
}
