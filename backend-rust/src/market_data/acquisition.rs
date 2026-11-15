use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde_json::{json, Value};
use sqlx::{FromRow, PgPool, Postgres, Transaction};
use thiserror::Error;
use uuid::Uuid;

use crate::{jobs::queue, Config};

use super::yahoo::{history_ranges, parse_chart_page, ChartPage, YahooClient, YahooError};

#[derive(Debug, Error)]
pub enum AcquisitionError {
    #[error("acquisition is already owned by another worker")]
    Busy,
    #[error("database unavailable")]
    Database(#[from] sqlx::Error),
    #[error("{0}")]
    RetryableProvider(YahooError),
}

#[derive(Debug, FromRow)]
struct AcquisitionJob {
    id: String,
    instrument_id: String,
    status: String,
    active: bool,
    mode: Option<String>,
    instrument_kind: Option<String>,
    requested_at: DateTime<Utc>,
    listing: Option<Value>,
    requested_listing: Option<Value>,
    listing_resolution: Option<Value>,
    lookup_ticker: Option<String>,
    date_from: Option<NaiveDate>,
    date_to: Option<NaiveDate>,
    full_date_from: Option<NaiveDate>,
}

#[derive(Debug, FromRow)]
struct SavedProfile {
    data: Value,
}

pub async fn run(pool: &PgPool, config: &Config, job_id: &str) -> Result<(), AcquisitionError> {
    let existing = load_job(pool, job_id).await?;
    let Some(existing) = existing else {
        return Ok(());
    };
    if !existing.active || matches!(existing.status.as_str(), "succeeded" | "failed" | "retired") {
        return Ok(());
    }

    let owner = Uuid::new_v4().to_string();
    let lease_seconds = config.job_lease.as_secs_f64();
    let job = sqlx::query_as::<_, AcquisitionJob>(
        "UPDATE acquisition_jobs
         SET status = 'running', phase = 'resolving_listing', owner = $2,
             lease_until = now() + make_interval(secs => $3::double precision),
             started_at = COALESCE(started_at, now())
         WHERE id = $1 AND active = true
           AND (status = 'queued' OR (status = 'running' AND lease_until <= now()))
         RETURNING id, instrument_id, status, active, mode, instrument_kind,
                   requested_at, listing, requested_listing, listing_resolution,
                   lookup_ticker, date_from, date_to, full_date_from",
    )
    .bind(job_id)
    .bind(&owner)
    .bind(lease_seconds)
    .fetch_optional(pool)
    .await?;
    let Some(mut job) = job else {
        let current = load_job(pool, job_id).await?;
        if current.is_some_and(|job| job.active && job.status == "running") {
            return Err(AcquisitionError::Busy);
        }
        return Ok(());
    };

    let claimed_job_id = job.id.clone();
    let client = YahooClient::new(pool, config, &claimed_job_id, &owner)
        .map_err(AcquisitionError::RetryableProvider)?;
    let outcome = execute(pool, config, &client, &owner, &mut job).await;
    match outcome {
        Ok(()) => {
            enqueue_retention(pool, &job.id, &job.instrument_id).await?;
            Ok(())
        }
        Err(ExecutionError::Database(error)) => Err(AcquisitionError::Database(error)),
        Err(ExecutionError::Busy) => Err(AcquisitionError::Busy),
        Err(ExecutionError::Provider(error)) if error.retryable() => {
            Err(AcquisitionError::RetryableProvider(error))
        }
        Err(ExecutionError::Provider(error)) => {
            mark_failed(pool, &job.id, &owner, error.code, error.message).await?;
            enqueue_retention(pool, &job.id, &job.instrument_id).await?;
            Ok(())
        }
        Err(ExecutionError::Invalid(message)) => {
            mark_failed(pool, &job.id, &owner, "invalid_state", &message).await?;
            Ok(())
        }
    }
}

#[derive(Debug, Error)]
enum ExecutionError {
    #[error("database unavailable")]
    Database(#[from] sqlx::Error),
    #[error("{0}")]
    Provider(#[from] YahooError),
    #[error("acquisition lease lost")]
    Busy,
    #[error("{0}")]
    Invalid(String),
}

async fn execute(
    pool: &PgPool,
    config: &Config,
    client: &YahooClient<'_>,
    owner: &str,
    job: &mut AcquisitionJob,
) -> Result<(), ExecutionError> {
    let is_index = job.instrument_kind.as_deref() == Some("index");
    if job.mode.as_deref() == Some("incremental") {
        let full_start = job.full_date_from.ok_or_else(|| {
            ExecutionError::Invalid("Incremental acquisition has no full-history boundary".into())
        })?;
        checkpoint(
            pool,
            config,
            &job.id,
            owner,
            "resolving_listing",
            Some(("refresh", full_start)),
        )
        .await?;
        job.mode = Some("refresh".into());
        job.date_from = Some(full_start);
    }

    let mut listing = parse_listing(job.listing.as_ref());
    let mut resolution = job.listing_resolution.clone();
    if listing.is_none() {
        let requested = job.requested_listing.as_ref();
        let resolved = if let Some(symbol) = requested
            .and_then(|value| value.get("symbol"))
            .and_then(Value::as_str)
        {
            if requested
                .and_then(|value| value.get("exchange"))
                .and_then(Value::as_str)
                .is_none()
            {
                client.resolve_symbol(symbol).await?
            } else {
                let exchange = requested
                    .and_then(|value| value.get("exchange"))
                    .and_then(Value::as_str)
                    .expect("checked exchange");
                super::yahoo::Listing {
                    symbol: symbol.to_ascii_uppercase(),
                    exchange: exchange.to_ascii_uppercase(),
                    resolution: json!({
                        "source": "manual_listing",
                        "query": symbol,
                        "collected_at": Utc::now(),
                    }),
                }
            }
        } else {
            client
                .resolve_listing(
                    job.lookup_ticker.as_deref().unwrap_or_default(),
                    &job.instrument_id,
                )
                .await?
        };
        listing = Some((resolved.symbol, resolved.exchange));
        resolution = Some(resolved.resolution);
        let listing_json = json!({
            "symbol": listing.as_ref().expect("listing").0,
            "exchange": listing.as_ref().expect("listing").1,
        });
        let updated = sqlx::query(
            "UPDATE acquisition_jobs
             SET listing = $3, listing_resolution = $4, phase = 'tickerinfo',
                 lease_until = now() + make_interval(secs => $5::double precision)
             WHERE id = $1 AND owner = $2 AND active = true AND status = 'running'",
        )
        .bind(&job.id)
        .bind(owner)
        .bind(&listing_json)
        .bind(&resolution)
        .bind(config.job_lease.as_secs_f64())
        .execute(pool)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(ExecutionError::Busy);
        }
        job.listing = Some(listing_json);
        job.listing_resolution = resolution.clone();
    }
    let (symbol, exchange) = listing.expect("resolved listing");

    checkpoint(pool, config, &job.id, owner, "tickerinfo", None).await?;
    let profile = if let Some(saved) =
        sqlx::query_as::<_, SavedProfile>("SELECT data FROM instruments WHERE job_id = $1")
            .bind(&job.id)
            .fetch_optional(pool)
            .await?
    {
        saved.data
    } else {
        let profile = client.chart_meta(&symbol).await?;
        if is_index && profile.get("instrumentType").and_then(Value::as_str) != Some("INDEX") {
            return Err(ExecutionError::Provider(YahooError {
                code: "index_identity_mismatch",
                message: "Yahoo must identify the exact requested symbol as an INDEX. Previous saved history remains active.",
            }));
        }
        let (asset_class, index_name) = if is_index {
            (None, None)
        } else {
            latest_catalogue_metadata(pool, &job.instrument_id).await?
        };
        sqlx::query(
            "INSERT INTO instruments
                (id, instrument_id, symbol, exchange, provider, job_id,
                 collected_at, collection_date, data, asset_class, index_name, published)
             VALUES ($1, $2, $3, $4, 'yfinance', $5, now(), CURRENT_DATE,
                     $6, $7, $8, false)
             ON CONFLICT (job_id) DO NOTHING",
        )
        .bind(Uuid::new_v4().to_string())
        .bind(&job.instrument_id)
        .bind(&symbol)
        .bind(&exchange)
        .bind(&job.id)
        .bind(&profile)
        .bind(asset_class)
        .bind(index_name)
        .execute(pool)
        .await?;
        profile
    };

    checkpoint(pool, config, &job.id, owner, "quotes", None).await?;
    let start = job
        .date_from
        .ok_or_else(|| ExecutionError::Invalid("Acquisition has no start date".into()))?;
    let end = job
        .date_to
        .ok_or_else(|| ExecutionError::Invalid("Acquisition has no end date".into()))?;
    let ranges = history_ranges(start, end);
    let mut all_splits = Vec::new();
    let mut total_rows = 0_i32;
    let mut latest_date = None;

    for (offset, (lower, upper)) in ranges.into_iter().enumerate() {
        let page = if let Some(saved) = sqlx::query_scalar::<_, Value>(
            "SELECT data FROM raw_eod_pages WHERE job_id = $1 AND page_offset = $2",
        )
        .bind(&job.id)
        .bind(offset as i32)
        .fetch_optional(pool)
        .await?
        {
            parse_saved_page(saved, &symbol, &exchange, lower, upper)?
        } else {
            client
                .history(&symbol, &exchange, lower, upper, &profile)
                .await?
                .into_iter()
                .next()
                .ok_or_else(|| ExecutionError::Invalid("Yahoo returned no chart page".into()))?
        };
        if is_index
            && page.raw.get("empty_history").is_none()
            && profile.get("instrumentType").and_then(Value::as_str) != Some("INDEX")
        {
            return Err(ExecutionError::Invalid(
                "Yahoo index identity changed during acquisition".into(),
            ));
        }
        persist_page(
            pool,
            config,
            job,
            owner,
            &symbol,
            &exchange,
            offset as i32,
            lower,
            &page,
        )
        .await?;
        total_rows += i32::try_from(page.rows.len()).unwrap_or(i32::MAX);
        for row in &page.rows {
            latest_date =
                Some(latest_date.map_or(row.date, |latest: NaiveDate| latest.max(row.date)));
        }
        all_splits.extend(page.splits);
    }

    let quote_count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(DISTINCT quote_date) FROM quotes WHERE job_id = $1",
    )
    .bind(&job.id)
    .fetch_one(pool)
    .await?;
    if quote_count == 0 {
        return Err(ExecutionError::Provider(YahooError {
            code: "empty_history",
            message: "Yahoo returned no daily historical quotes for this symbol and requested dates. A searchable symbol does not guarantee downloadable history. Previous saved history remains active.",
        }));
    }

    checkpoint(pool, config, &job.id, owner, "splits", None).await?;
    persist_splits(pool, job, &symbol, &exchange, start, end, all_splits).await?;
    publish(
        pool,
        config,
        job,
        owner,
        &symbol,
        &exchange,
        &profile,
        resolution,
        latest_date,
        total_rows,
    )
    .await?;
    Ok(())
}

fn parse_saved_page(
    saved: Value,
    symbol: &str,
    exchange: &str,
    lower: NaiveDate,
    upper: NaiveDate,
) -> Result<ChartPage, ExecutionError> {
    let raw = saved.get("raw").cloned().unwrap_or(saved);
    if raw.get("empty_history").is_some() {
        return Ok(ChartPage {
            raw,
            rows: Vec::new(),
            splits: Vec::new(),
        });
    }
    parse_chart_page(raw, symbol, exchange, lower, upper).map_err(Into::into)
}

#[allow(clippy::too_many_arguments)]
async fn persist_page(
    pool: &PgPool,
    config: &Config,
    job: &AcquisitionJob,
    owner: &str,
    symbol: &str,
    exchange: &str,
    offset: i32,
    lower: NaiveDate,
    page: &ChartPage,
) -> Result<(), ExecutionError> {
    let data = json!({
        "pagination": {
            "limit": page.rows.len(),
            "offset": offset,
            "count": page.rows.len(),
            "total": page.rows.len(),
        },
        "data": page.rows.iter().map(|row| &row.data).collect::<Vec<_>>(),
        "raw": page.raw,
        "provider": "yfinance",
        "price_basis": "provider_split_adjusted",
    });
    let mut transaction = pool.begin().await?;
    sqlx::query(
        "INSERT INTO raw_eod_pages
            (id, instrument_id, symbol, exchange, provider, job_id,
             collected_at, collection_date, page_offset, data)
         VALUES ($1, $2, $3, $4, 'yfinance', $5, now(), CURRENT_DATE, $6, $7)
         ON CONFLICT (job_id, page_offset) DO NOTHING",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&job.instrument_id)
    .bind(symbol)
    .bind(exchange)
    .bind(&job.id)
    .bind(offset)
    .bind(&data)
    .execute(&mut *transaction)
    .await?;
    for row in &page.rows {
        sqlx::query(
            "INSERT INTO quotes
                (id, instrument_id, symbol, exchange, provider, job_id,
                 collected_at, collection_date, quote_date, data,
                 open, high, low, close, volume, adj_close, price_currency)
             VALUES ($1, $2, $3, $4, 'yfinance', $5, now(), CURRENT_DATE, $6, $7,
                     $8, $9, $10, $11, $12, $13, $14)
             ON CONFLICT (job_id, quote_date) DO NOTHING",
        )
        .bind(Uuid::new_v4().to_string())
        .bind(&job.instrument_id)
        .bind(symbol)
        .bind(exchange)
        .bind(&job.id)
        .bind(row.date)
        .bind(&row.data)
        .bind(row.open)
        .bind(row.high)
        .bind(row.low)
        .bind(row.close)
        .bind(row.volume)
        .bind(row.adj_close)
        .bind(&row.price_currency)
        .execute(&mut *transaction)
        .await?;
    }
    let updated = sqlx::query(
        "UPDATE acquisition_jobs
         SET pages = GREATEST(pages, $3 + 1),
             rows = (SELECT COUNT(*) FROM quotes WHERE job_id = $1),
             next_offset = $3 + 1,
             last_page_date = GREATEST(last_page_date, $4),
             lease_until = now() + make_interval(secs => $5::double precision)
         WHERE id = $1 AND owner = $2 AND active = true AND status = 'running'",
    )
    .bind(&job.id)
    .bind(owner)
    .bind(offset)
    .bind(page.rows.iter().map(|row| row.date).max().unwrap_or(lower))
    .bind(config.job_lease.as_secs_f64())
    .execute(&mut *transaction)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(ExecutionError::Busy);
    }
    transaction.commit().await?;
    Ok(())
}

async fn persist_splits(
    pool: &PgPool,
    job: &AcquisitionJob,
    symbol: &str,
    exchange: &str,
    start: NaiveDate,
    end: NaiveDate,
    splits: Vec<Value>,
) -> Result<(), ExecutionError> {
    let page = json!({
        "pagination": {"limit": splits.len(), "offset": 0, "count": splits.len(), "total": splits.len()},
        "data": splits,
        "provider": "yfinance",
    });
    let mut transaction = pool.begin().await?;
    sqlx::query(
        "INSERT INTO raw_split_pages
            (id, instrument_id, symbol, exchange, provider, job_id,
             collected_at, collection_date, page_offset, data)
         VALUES ($1, $2, $3, $4, 'yfinance', $5, now(), CURRENT_DATE, 0, $6)
         ON CONFLICT (job_id, page_offset) DO NOTHING",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&job.instrument_id)
    .bind(symbol)
    .bind(exchange)
    .bind(&job.id)
    .bind(&page)
    .execute(&mut *transaction)
    .await?;
    sqlx::query(
        "INSERT INTO split_snapshots
            (id, instrument_id, symbol, exchange, job_id, provider,
             requested_at, collected_at, date_from, date_to, events)
         VALUES ($1, $2, $3, $4, $5, 'yfinance', $6, now(), $7, $8, $9)
         ON CONFLICT (job_id) DO NOTHING",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&job.instrument_id)
    .bind(symbol)
    .bind(exchange)
    .bind(&job.id)
    .bind(job.requested_at)
    .bind(start)
    .bind(end)
    .bind(page.get("data").cloned().unwrap_or_else(|| json!([])))
    .execute(&mut *transaction)
    .await?;
    sqlx::query(
        "UPDATE acquisition_jobs
         SET splits_complete = true, next_split_offset = $2,
             expected_split_total = $2
         WHERE id = $1",
    )
    .bind(&job.id)
    .bind(
        page.get("data")
            .and_then(Value::as_array)
            .map_or(0_i32, |items| {
                i32::try_from(items.len()).unwrap_or(i32::MAX)
            }),
    )
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn publish(
    pool: &PgPool,
    config: &Config,
    job: &AcquisitionJob,
    owner: &str,
    symbol: &str,
    exchange: &str,
    profile: &Value,
    resolution: Option<Value>,
    latest_date: Option<NaiveDate>,
    total_rows: i32,
) -> Result<(), ExecutionError> {
    checkpoint(pool, config, &job.id, owner, "publishing", None).await?;
    let summary = sqlx::query_as::<_, (i64, Option<NaiveDate>, Option<NaiveDate>)>(
        "SELECT COUNT(DISTINCT quote_date), MIN(quote_date), MAX(quote_date)
         FROM quotes WHERE job_id = $1",
    )
    .bind(&job.id)
    .fetch_one(pool)
    .await?;
    let listing = json!({"symbol": symbol, "exchange": exchange});
    let published_at = Utc::now();
    let history_summary = json!({
        "version": 1,
        "instrument_id": job.instrument_id,
        "listing": listing,
        "generation": job.id,
        "observations": summary.0,
        "quote_count": summary.0,
        "first_date": summary.1,
        "last_date": summary.2,
    });
    let warning = if job.instrument_kind.as_deref() == Some("index") {
        Some("Yahoo history was requested from 1900-01-01; this boundary is not index inception or a guarantee of complete coverage.")
    } else if total_rows == 0 {
        Some("The provider returned no quotes for this range. No dates were marked as received.")
    } else {
        None
    };

    let statistics = if job.instrument_kind.as_deref() == Some("index") {
        None
    } else {
        let override_currency = sqlx::query_scalar::<_, Option<String>>(
            "SELECT price_currency_override FROM instrument_state WHERE instrument_id = $1",
        )
        .bind(&job.instrument_id)
        .fetch_optional(pool)
        .await?
        .flatten();
        Some(
            super::statistics::calculate(
                pool,
                &job.instrument_id,
                &job.id,
                override_currency.as_deref(),
            )
            .await?,
        )
    };
    let mut transaction = pool.begin().await?;
    sqlx::query(
        "INSERT INTO market_data_publications
            (id, instrument_id, symbol, exchange, provider, price_basis,
             job_id, requested_at, published_at)
         VALUES ($1, $2, $3, $4, 'yfinance', 'provider_split_adjusted',
                 $1, $5, $6)
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(&job.id)
    .bind(&job.instrument_id)
    .bind(symbol)
    .bind(exchange)
    .bind(job.requested_at)
    .bind(published_at)
    .execute(&mut *transaction)
    .await?;
    sqlx::query("UPDATE instruments SET published = true WHERE job_id = $1")
        .bind(&job.id)
        .execute(&mut *transaction)
        .await?;
    sqlx::query(
        "INSERT INTO instrument_state
            (instrument_id, listing, listing_resolution, listing_job_id, last_complete_quote_date)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (instrument_id) DO UPDATE
         SET listing = EXCLUDED.listing,
             listing_resolution = COALESCE(EXCLUDED.listing_resolution, instrument_state.listing_resolution),
             listing_job_id = EXCLUDED.listing_job_id,
             last_complete_quote_date = GREATEST(instrument_state.last_complete_quote_date, EXCLUDED.last_complete_quote_date)",
    )
    .bind(&job.instrument_id)
    .bind(&listing)
    .bind(resolution)
    .bind(&job.id)
    .bind(latest_date)
    .execute(&mut *transaction)
    .await?;
    if job.instrument_kind.as_deref() == Some("index") {
        let currency = profile.get("currency").and_then(Value::as_str);
        sqlx::query(
            "UPDATE indices
             SET verification_status = 'verified', currency = COALESCE(currency, $2)
             WHERE instrument_id = $1 AND removed_at IS NULL",
        )
        .bind(&job.instrument_id)
        .bind(currency)
        .execute(&mut *transaction)
        .await?;
    }
    let updated = sqlx::query(
        "UPDATE acquisition_jobs
         SET status = 'succeeded', active = false, phase = 'complete',
             finished_at = now(), published = true, published_at = $3,
             history_summary = $4, warning = $5, error = NULL, error_code = NULL,
             download_complete = true, splits_complete = true,
             lease_until = NULL
         WHERE id = $1 AND owner = $2 AND active = true AND status = 'running'",
    )
    .bind(&job.id)
    .bind(owner)
    .bind(published_at)
    .bind(history_summary)
    .bind(warning)
    .execute(&mut *transaction)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(ExecutionError::Busy);
    }
    if let Some(statistics) = statistics {
        super::statistics::save(&mut transaction, &job.instrument_id, &statistics).await?;
    }
    transaction.commit().await?;
    Ok(())
}

async fn checkpoint(
    pool: &PgPool,
    config: &Config,
    job_id: &str,
    owner: &str,
    phase: &str,
    full_refresh: Option<(&str, NaiveDate)>,
) -> Result<(), ExecutionError> {
    let result = if let Some((mode, date_from)) = full_refresh {
        sqlx::query(
            "UPDATE acquisition_jobs
             SET phase = $3, mode = $4, date_from = $5, splits_date_from = $5,
                 incremental_checked = true,
                 full_refresh_reason = 'Rust worker materializes a full immutable generation',
                 lease_until = now() + make_interval(secs => $6::double precision)
             WHERE id = $1 AND owner = $2 AND active = true AND status = 'running'",
        )
        .bind(job_id)
        .bind(owner)
        .bind(phase)
        .bind(mode)
        .bind(date_from)
        .bind(config.job_lease.as_secs_f64())
        .execute(pool)
        .await?
    } else {
        sqlx::query(
            "UPDATE acquisition_jobs
             SET phase = $3,
                 lease_until = now() + make_interval(secs => $4::double precision)
             WHERE id = $1 AND owner = $2 AND active = true AND status = 'running'",
        )
        .bind(job_id)
        .bind(owner)
        .bind(phase)
        .bind(config.job_lease.as_secs_f64())
        .execute(pool)
        .await?
    };
    if result.rows_affected() != 1 {
        return Err(ExecutionError::Busy);
    }
    Ok(())
}

async fn mark_failed(
    pool: &PgPool,
    job_id: &str,
    owner: &str,
    code: &str,
    message: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE acquisition_jobs
         SET status = 'failed', active = false, phase = 'failed', finished_at = now(),
             lease_until = NULL, error = $3, error_code = $4
         WHERE id = $1 AND owner = $2 AND active = true",
    )
    .bind(job_id)
    .bind(owner)
    .bind(message)
    .bind(code)
    .execute(pool)
    .await?;
    Ok(())
}

async fn enqueue_retention(
    pool: &PgPool,
    job_id: &str,
    instrument_id: &str,
) -> Result<(), sqlx::Error> {
    let retention_id = Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO job_queue
            (id, kind, queue, payload, status, run_at, requested_at,
             max_attempts, dedupe_key)
         VALUES ($1, 'market_data_retention', 'catalogue', $2, 'queued', now(), now(),
                 13, $3)
         ON CONFLICT (dedupe_key) DO NOTHING",
    )
    .bind(retention_id)
    .bind(json!({"instrument_id": instrument_id}))
    .bind(format!("retention-after:{job_id}"))
    .execute(pool)
    .await?;
    Ok(())
}

async fn load_job(pool: &PgPool, job_id: &str) -> Result<Option<AcquisitionJob>, sqlx::Error> {
    sqlx::query_as::<_, AcquisitionJob>(
        "SELECT id, instrument_id, status, active, mode, instrument_kind,
                requested_at, listing, requested_listing, listing_resolution,
                lookup_ticker, date_from, date_to, full_date_from
         FROM acquisition_jobs WHERE id = $1",
    )
    .bind(job_id)
    .fetch_optional(pool)
    .await
}

fn parse_listing(value: Option<&Value>) -> Option<(String, String)> {
    Some((
        value?.get("symbol")?.as_str()?.to_owned(),
        value?.get("exchange")?.as_str()?.to_owned(),
    ))
}

async fn latest_catalogue_metadata(
    pool: &PgPool,
    instrument_id: &str,
) -> Result<(Option<String>, Option<String>), sqlx::Error> {
    sqlx::query_as::<_, (Option<String>, Option<String>)>(
        "SELECT asset_class, source ->> 'index'
         FROM etfs
         WHERE isin = $1
         ORDER BY import_date DESC
         LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(pool)
    .await
    .map(|value| value.unwrap_or((None, None)))
}

#[allow(dead_code)]
async fn _lock_job<'a>(
    transaction: &mut Transaction<'a, Postgres>,
    job_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT 1 FROM acquisition_jobs WHERE id = $1 FOR UPDATE")
        .bind(job_id)
        .execute(&mut **transaction)
        .await?;
    Ok(())
}

#[allow(dead_code)]
fn _decimal(value: Option<Decimal>) -> Option<String> {
    value.map(|value| value.to_string())
}

#[allow(dead_code)]
fn _queue_kind() -> queue::JobKind {
    queue::JobKind::MarketDataAcquisition
}
