use std::collections::BTreeSet;

use chrono::{DateTime, NaiveTime, Utc};
use serde::Serialize;
use sqlx::{FromRow, PgPool};

use super::{request_automatic_acquisition, MarketDataError};

#[derive(Debug, Default, Serialize)]
pub struct RefreshCounts {
    pub requested: usize,
    pub fresh: usize,
    pub skipped: usize,
    pub errors: usize,
}

#[derive(FromRow)]
struct LatestSuccess {
    requested_at: DateTime<Utc>,
    date_to: chrono::NaiveDate,
}

pub async fn refresh_stale(
    pool: &PgPool,
    include_catalogue: bool,
    now: DateTime<Utc>,
) -> Result<RefreshCounts, MarketDataError> {
    let mut identifiers = BTreeSet::new();
    for identifier in sqlx::query_scalar::<_, String>(
        "SELECT DISTINCT instrument_id
         FROM instruments
         WHERE instrument_id NOT LIKE 'idx-%'
           AND (provider <> 'yfinance' OR published = true)",
    )
    .fetch_all(pool)
    .await?
    {
        identifiers.insert(identifier);
    }
    if include_catalogue {
        for identifier in sqlx::query_scalar::<_, String>("SELECT DISTINCT isin FROM etfs")
            .fetch_all(pool)
            .await?
        {
            identifiers.insert(identifier);
        }
    }
    for identifier in sqlx::query_scalar::<_, String>(
        "SELECT instrument_id FROM indices WHERE removed_at IS NULL",
    )
    .fetch_all(pool)
    .await?
    {
        identifiers.insert(identifier);
    }

    let midnight = now.date_naive().and_time(NaiveTime::MIN).and_utc();
    let scheduled = now
        .date_naive()
        .and_hms_opt(2, 0, 0)
        .expect("valid schedule")
        .and_utc();
    let fresh_since = if now >= scheduled {
        scheduled
    } else {
        midnight
    };
    let today = now.date_naive();
    let mut counts = RefreshCounts::default();

    for instrument_id in identifiers {
        let latest = sqlx::query_as::<_, LatestSuccess>(
            "SELECT requested_at, date_to
             FROM acquisition_jobs
             WHERE instrument_id = $1
               AND status = 'succeeded'
               AND provider = 'yfinance'
               AND published = true
             ORDER BY finished_at DESC NULLS LAST, requested_at DESC, id DESC
             LIMIT 1",
        )
        .bind(&instrument_id)
        .fetch_optional(pool)
        .await?;
        if latest.is_some_and(|latest| {
            latest.requested_at >= fresh_since
                && latest.requested_at <= now
                && latest.date_to >= today
        }) {
            counts.fresh += 1;
            continue;
        }

        let publication_today = sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS (
                 SELECT 1
                 FROM acquisition_jobs jobs
                 JOIN instrument_state state ON state.instrument_id = jobs.instrument_id
                 JOIN quotes ON quotes.job_id = jobs.id
                 WHERE jobs.instrument_id = $1
                   AND jobs.provider = 'yfinance'
                   AND jobs.published = true
                   AND jobs.requested_at BETWEEN $2 AND $3
                   AND quotes.quote_date = $4
                   AND jobs.listing = state.listing
             )",
        )
        .bind(&instrument_id)
        .bind(fresh_since)
        .bind(now)
        .bind(today)
        .fetch_one(pool)
        .await?;
        if publication_today {
            counts.fresh += 1;
            continue;
        }

        match request_automatic_acquisition(
            pool,
            &instrument_id,
            "system:market-data-refresh",
            true,
        )
        .await
        {
            Ok(_) => counts.requested += 1,
            Err(error) if error.status() == 503 => {
                counts.errors += 1;
                return Err(error);
            }
            Err(error) => {
                counts.skipped += 1;
                tracing::warn!(%instrument_id, error = %error, "automatic refresh skipped");
            }
        }
    }
    tracing::info!(?counts, "automatic market-data refresh completed");
    Ok(counts)
}

pub async fn refresh_now(
    pool: &PgPool,
    include_catalogue: bool,
) -> Result<RefreshCounts, MarketDataError> {
    refresh_stale(pool, include_catalogue, Utc::now()).await
}
