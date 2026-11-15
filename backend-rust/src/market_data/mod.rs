pub mod acquisition;
pub mod http;
pub mod refresh;
pub mod retention;
pub mod statistics;
pub mod yahoo;

use std::sync::LazyLock;

use chrono::{NaiveDate, Utc};
use regex::Regex;
use serde_json::{json, Value};
use sqlx::{FromRow, PgPool};
use thiserror::Error;
use uuid::Uuid;

pub async fn quote_fill_policy(pool: &PgPool) -> Result<Value, sqlx::Error> {
    let value =
        sqlx::query_scalar::<_, Value>("SELECT value FROM app_settings WHERE key = 'market_data'")
            .fetch_optional(pool)
            .await?
            .unwrap_or_else(|| json!({}));
    Ok(json!({
        "version": 1,
        "method": value.get("method").and_then(Value::as_str).unwrap_or("smooth"),
        "seed": value.get("seed").and_then(Value::as_u64).unwrap_or(0),
    }))
}

#[derive(Debug, Error)]
pub enum MarketDataError {
    #[error("{message}")]
    Invalid { status: u16, message: String },
    #[error("database unavailable")]
    Database(#[from] sqlx::Error),
}

impl MarketDataError {
    pub fn invalid(status: u16, message: impl Into<String>) -> Self {
        Self::Invalid {
            status,
            message: message.into(),
        }
    }

    pub fn status(&self) -> u16 {
        match self {
            Self::Invalid { status, .. } => *status,
            Self::Database(_) => 503,
        }
    }
}

#[derive(Debug, Clone)]
pub struct AcquisitionRequest {
    pub id: String,
    pub created: bool,
}

#[derive(FromRow)]
struct EtfCatalogue {
    ticker: Option<String>,
    inception_date: Option<NaiveDate>,
}

#[derive(FromRow)]
struct IndexCatalogue {
    symbol: String,
}

#[derive(FromRow)]
struct InstrumentState {
    listing: Option<Value>,
}

#[derive(FromRow)]
struct Publication {
    id: String,
    listing: Option<Value>,
}

#[derive(FromRow)]
struct ActiveAcquisition {
    id: String,
    requested_listing: Option<Value>,
    listing: Option<Value>,
}

static TICKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Z0-9^][A-Z0-9._^=\-]*$").expect("valid catalogue ticker regex")
});

pub async fn request_automatic_acquisition(
    pool: &PgPool,
    instrument_id: &str,
    requested_by: &str,
    incremental: bool,
) -> Result<AcquisitionRequest, MarketDataError> {
    let is_index = instrument_id.starts_with("idx-");
    let (inception, ticker, index_listing) = if is_index {
        let index = sqlx::query_as::<_, IndexCatalogue>(
            "SELECT symbol FROM indices
             WHERE instrument_id = $1 AND removed_at IS NULL",
        )
        .bind(instrument_id)
        .fetch_optional(pool)
        .await?
        .ok_or_else(|| MarketDataError::invalid(404, "Index not found"))?;
        (
            NaiveDate::from_ymd_opt(1900, 1, 1).expect("valid history boundary"),
            index.symbol.clone(),
            Some(json!({ "symbol": index.symbol, "exchange": "YAHOO" })),
        )
    } else {
        let etf = sqlx::query_as::<_, EtfCatalogue>(
            "SELECT ticker, inception_date
             FROM etfs
             WHERE isin = $1
             ORDER BY import_date DESC
             LIMIT 1",
        )
        .bind(instrument_id)
        .fetch_optional(pool)
        .await?
        .ok_or_else(|| MarketDataError::invalid(404, "Instrument not found"))?;
        let inception = etf.inception_date.ok_or_else(|| {
            MarketDataError::invalid(
                422,
                "This instrument has no valid inception date for a history request",
            )
        })?;
        if inception > Utc::now().date_naive() {
            return Err(MarketDataError::invalid(
                422,
                "The instrument inception date is in the future",
            ));
        }
        let ticker = etf.ticker.unwrap_or_default().trim().to_ascii_uppercase();
        (inception, ticker, None)
    };

    if !is_index && (ticker.len() > 64 || !TICKER.is_match(&ticker)) {
        return Err(MarketDataError::invalid(
            422,
            "This instrument has no valid catalogue ticker. Supply a manual Yahoo symbol.",
        ));
    }

    let state = sqlx::query_as::<_, InstrumentState>(
        "SELECT listing FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .unwrap_or(InstrumentState { listing: None });

    let publication = sqlx::query_as::<_, Publication>(
        "SELECT id, listing
         FROM acquisition_jobs
         WHERE instrument_id = $1
           AND provider = 'yfinance'
           AND published = true
         ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?;

    let listing = if is_index {
        index_listing
    } else if publication.is_some() {
        state.listing
    } else {
        None
    };
    if let (Some(publication), Some(listing)) = (&publication, &listing) {
        if publication.listing.as_ref() != Some(listing) {
            return Err(MarketDataError::invalid(
                409,
                "The latest Yahoo listing changed. Refresh instrument details before requesting history.",
            ));
        }
    }

    if let Some(existing) = sqlx::query_scalar::<_, String>(
        "SELECT id FROM acquisition_jobs
         WHERE instrument_id = $1 AND active = true
         ORDER BY requested_at DESC, id DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    {
        return Ok(AcquisitionRequest {
            id: existing,
            created: false,
        });
    }

    let today = Utc::now().date_naive();
    let mut start = inception;
    let mut mode = "refresh";
    let mut base_generation = None;
    if incremental {
        if let (Some(publication), Some(listing)) = (&publication, &listing) {
            let last_date = sqlx::query_scalar::<_, Option<NaiveDate>>(
                "SELECT MAX(quote_date)
                 FROM quotes
                 WHERE instrument_id = $1
                   AND symbol = $2
                   AND exchange = $3
                   AND provider = 'yfinance'
                   AND job_id = $4
                   AND quote_date <= $5",
            )
            .bind(instrument_id)
            .bind(
                listing
                    .get("symbol")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
            .bind(
                listing
                    .get("exchange")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
            .bind(&publication.id)
            .bind(today)
            .fetch_one(pool)
            .await?;
            if let Some(last_date) = last_date {
                start = last_date;
                mode = "incremental";
                base_generation = Some(publication.id.clone());
            }
        }
    }

    let id = Uuid::new_v4().to_string();
    let mut transaction = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(instrument_id)
        .execute(&mut *transaction)
        .await?;
    let inserted = sqlx::query(
        "INSERT INTO acquisition_jobs (
             id, instrument_id, status, active, phase, mode, provider, price_basis,
             instrument_kind, history_scope, requested_by, requested_at,
             listing, requested_listing, lookup_ticker, date_from, date_to,
             splits_date_from, splits_date_to, full_date_from, base_generation,
             published, pages, rows, flagged_rows, unindexed_rows, calls,
             next_offset, next_split_offset
         )
         VALUES (
             $1, $2, 'queued', true, 'queued', $3, 'yfinance',
             'provider_split_adjusted', $4, $5, $6, now(),
             $7, NULL, $8, $9, $10, $11, $12, $13, $14,
             false, 0, 0, 0, 0, 0, 0, 0
         )
         ON CONFLICT (instrument_id) WHERE active DO NOTHING",
    )
    .bind(&id)
    .bind(instrument_id)
    .bind(mode)
    .bind(if is_index { "index" } else { "etf" })
    .bind(if is_index {
        "full_available_history"
    } else {
        "since_inception"
    })
    .bind(requested_by)
    .bind(&listing)
    .bind(&ticker)
    .bind(start)
    .bind(today)
    .bind(start)
    .bind(today.max(inception))
    .bind((mode == "incremental").then_some(inception))
    .bind(&base_generation)
    .execute(&mut *transaction)
    .await?;

    if inserted.rows_affected() == 0 {
        let existing = sqlx::query_scalar::<_, String>(
            "SELECT id FROM acquisition_jobs
             WHERE instrument_id = $1 AND active = true
             ORDER BY requested_at DESC, id DESC
             LIMIT 1",
        )
        .bind(instrument_id)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or_else(|| MarketDataError::invalid(409, "Acquisition state changed; please retry"))?;
        transaction.commit().await?;
        return Ok(AcquisitionRequest {
            id: existing,
            created: false,
        });
    }

    sqlx::query(
        "INSERT INTO job_queue
            (id, kind, queue, payload, status, run_at, requested_at, max_attempts)
         VALUES ($1, 'market_data_acquisition', 'marketdata', $2, 'queued', now(), now(), 13)",
    )
    .bind(&id)
    .bind(json!({ "job_id": id }))
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;

    Ok(AcquisitionRequest { id, created: true })
}

pub async fn request_user_acquisition(
    pool: &PgPool,
    instrument_id: &str,
    requested_by: &str,
    symbol: Option<&str>,
    exchange: Option<&str>,
) -> Result<AcquisitionRequest, MarketDataError> {
    let Some(symbol) = symbol else {
        return request_automatic_acquisition(pool, instrument_id, requested_by, false).await;
    };
    if instrument_id.starts_with("idx-") {
        return request_automatic_acquisition(pool, instrument_id, requested_by, false).await;
    }

    let etf = sqlx::query_as::<_, EtfCatalogue>(
        "SELECT ticker, inception_date
         FROM etfs
         WHERE isin = $1
         ORDER BY import_date DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| MarketDataError::invalid(404, "Instrument not found"))?;
    let inception = etf.inception_date.ok_or_else(|| {
        MarketDataError::invalid(
            422,
            "This instrument has no valid inception date for a history request",
        )
    })?;
    if inception > Utc::now().date_naive() {
        return Err(MarketDataError::invalid(
            422,
            "The instrument inception date is in the future",
        ));
    }

    let symbol = symbol.trim().to_ascii_uppercase();
    if symbol.len() > 64 || !TICKER.is_match(&symbol) {
        return Err(MarketDataError::invalid(422, "Invalid Yahoo symbol"));
    }
    let exchange = exchange.map(|value| value.trim().to_ascii_uppercase());
    if exchange.as_ref().is_some_and(|value| {
        value.len() != 4
            || !value
                .chars()
                .all(|character| character.is_ascii_alphanumeric())
    }) {
        return Err(MarketDataError::invalid(422, "Invalid exchange MIC"));
    }
    let requested_listing = json!({
        "symbol": symbol,
        "exchange": exchange,
    });
    let requested_listing = if exchange.is_some() {
        requested_listing
    } else {
        json!({ "symbol": symbol })
    };

    if let Some(existing) = sqlx::query_as::<_, ActiveAcquisition>(
        "SELECT id, requested_listing, listing
         FROM acquisition_jobs
         WHERE instrument_id = $1 AND active = true
         ORDER BY requested_at DESC, id DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    {
        let existing_request = existing.requested_listing.or(existing.listing);
        if existing_request.as_ref() != Some(&requested_listing) {
            return Err(MarketDataError::invalid(
                409,
                "Another listing is already being collected for this instrument",
            ));
        }
        return Ok(AcquisitionRequest {
            id: existing.id,
            created: false,
        });
    }

    let state = sqlx::query_as::<_, InstrumentState>(
        "SELECT listing FROM instrument_state WHERE instrument_id = $1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await?
    .unwrap_or(InstrumentState { listing: None });
    let has_quotes = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS(
             SELECT 1 FROM quotes
             WHERE instrument_id = $1 AND provider = 'yfinance'
         )",
    )
    .bind(instrument_id)
    .fetch_one(pool)
    .await?;
    if has_quotes {
        if let Some(saved) = state.listing.as_ref() {
            let same = if let Some(exchange) = exchange.as_deref() {
                saved.get("symbol").and_then(Value::as_str) == Some(symbol.as_str())
                    && saved.get("exchange").and_then(Value::as_str) == Some(exchange)
            } else {
                saved.get("symbol").and_then(Value::as_str) == Some(symbol.as_str())
            };
            if !same {
                return Err(MarketDataError::invalid(
                    409,
                    "A listing with stored quotes cannot be changed or mixed with another listing",
                ));
            }
        }
    }

    let listing = exchange
        .as_ref()
        .map(|exchange| json!({ "symbol": symbol, "exchange": exchange }));
    let today = Utc::now().date_naive();
    let id = Uuid::new_v4().to_string();
    let mut transaction = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(instrument_id)
        .execute(&mut *transaction)
        .await?;
    let inserted = sqlx::query(
        "INSERT INTO acquisition_jobs (
             id, instrument_id, status, active, phase, mode, provider, price_basis,
             instrument_kind, history_scope, requested_by, requested_at,
             listing, requested_listing, lookup_ticker, date_from, date_to,
             splits_date_from, splits_date_to, published, pages, rows,
             flagged_rows, unindexed_rows, calls, next_offset, next_split_offset
         )
         VALUES (
             $1, $2, 'queued', true, 'queued', 'refresh', 'yfinance',
             'provider_split_adjusted', 'etf', 'since_inception', $3, now(),
             $4, $5, $6, $7, $8, $7, $8,
             false, 0, 0, 0, 0, 0, 0, 0
         )
         ON CONFLICT (instrument_id) WHERE active DO NOTHING",
    )
    .bind(&id)
    .bind(instrument_id)
    .bind(requested_by)
    .bind(&listing)
    .bind(&requested_listing)
    .bind(&symbol)
    .bind(inception)
    .bind(today)
    .execute(&mut *transaction)
    .await?;
    if inserted.rows_affected() == 0 {
        let existing = sqlx::query_scalar::<_, String>(
            "SELECT id FROM acquisition_jobs WHERE instrument_id = $1 AND active = true",
        )
        .bind(instrument_id)
        .fetch_one(&mut *transaction)
        .await?;
        transaction.commit().await?;
        return Ok(AcquisitionRequest {
            id: existing,
            created: false,
        });
    }
    sqlx::query(
        "INSERT INTO job_queue
            (id, kind, queue, payload, status, run_at, requested_at, max_attempts)
         VALUES ($1, 'market_data_acquisition', 'marketdata', $2, 'queued', now(), now(), 13)",
    )
    .bind(&id)
    .bind(json!({ "job_id": id }))
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok(AcquisitionRequest { id, created: true })
}

pub async fn public_acquisition_job(
    pool: &PgPool,
    job_id: &str,
) -> Result<Option<Value>, sqlx::Error> {
    let row = sqlx::query_as::<
        _,
        (
            String,
            String,
            String,
            Option<String>,
            Option<String>,
            chrono::DateTime<Utc>,
            Option<chrono::DateTime<Utc>>,
            Option<chrono::DateTime<Utc>>,
            i32,
            i32,
            i32,
            i32,
            i32,
            Option<String>,
            Option<String>,
        ),
    >(
        "SELECT id, instrument_id, status, phase, mode, requested_at,
                started_at, finished_at, pages, rows, flagged_rows,
                unindexed_rows, calls, error, warning
         FROM acquisition_jobs WHERE id = $1",
    )
    .bind(job_id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|row| {
        json!({
            "id": row.0,
            "instrument_id": row.1,
            "status": row.2,
            "phase": row.3,
            "mode": row.4,
            "requested_at": row.5,
            "started_at": row.6,
            "finished_at": row.7,
            "pages": row.8,
            "rows": row.9,
            "flagged_rows": row.10,
            "unindexed_rows": row.11,
            "calls": row.12,
            "error": row.13,
            "warning": row.14,
        })
    }))
}
