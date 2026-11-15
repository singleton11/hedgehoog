use chrono::Utc;
use serde::Serialize;
use serde_json::json;
use sqlx::PgPool;
use uuid::Uuid;

use crate::Config;

use super::MarketDataError;

const RETENTION_LOCK: i64 = 4_054_675_583_516_866_899;

#[derive(Debug, Default, Serialize)]
pub struct RetentionResult {
    pub examined: usize,
    pub pruned: usize,
    pub skipped: usize,
}

pub async fn cleanup(
    pool: &PgPool,
    config: &Config,
    instrument_id: Option<&str>,
) -> Result<RetentionResult, MarketDataError> {
    if !config.market_data_retention_enabled {
        return Ok(RetentionResult::default());
    }

    let mut transaction = pool.begin().await?;
    let locked = sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_xact_lock($1)")
        .bind(RETENTION_LOCK)
        .fetch_one(&mut *transaction)
        .await?;
    if !locked {
        return Ok(RetentionResult {
            skipped: 1,
            ..RetentionResult::default()
        });
    }

    let candidates = sqlx::query_scalar::<_, String>(
        "WITH latest AS (
             SELECT DISTINCT ON (instrument_id) instrument_id, id
             FROM acquisition_jobs
             WHERE provider = 'yfinance' AND published = true
             ORDER BY instrument_id, requested_at DESC, published_at DESC NULLS LAST, id DESC
         )
         SELECT jobs.id
         FROM acquisition_jobs jobs
         LEFT JOIN latest ON latest.instrument_id = jobs.instrument_id
         WHERE jobs.active = false
           AND jobs.status IN ('succeeded', 'failed', 'retired')
           AND jobs.retention_state IS DISTINCT FROM 'pruned'
           AND ($1::text IS NULL OR jobs.instrument_id = $1)
           AND jobs.id IS DISTINCT FROM latest.id
           AND NOT EXISTS (
               SELECT 1 FROM acquisition_jobs active
               WHERE active.active = true AND active.base_generation = jobs.id
           )
           AND NOT EXISTS (
               SELECT 1 FROM market_data_reader_leases readers
               WHERE readers.generation_id = jobs.id AND readers.expires_at > now()
           )
         ORDER BY jobs.finished_at NULLS FIRST, jobs.requested_at, jobs.id
         LIMIT $2
         FOR UPDATE OF jobs SKIP LOCKED",
    )
    .bind(instrument_id)
    .bind(config.market_data_retention_batch_size)
    .fetch_all(&mut *transaction)
    .await?;

    let mut result = RetentionResult {
        examined: candidates.len(),
        ..RetentionResult::default()
    };
    for generation_id in candidates {
        let still_protected = sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS (
                 SELECT 1 FROM acquisition_jobs active
                 WHERE active.active = true AND active.base_generation = $1
             ) OR EXISTS (
                 SELECT 1 FROM market_data_reader_leases readers
                 WHERE readers.generation_id = $1 AND readers.expires_at > now()
             )",
        )
        .bind(&generation_id)
        .fetch_one(&mut *transaction)
        .await?;
        if still_protected {
            result.skipped += 1;
            continue;
        }

        let instrument = sqlx::query_scalar::<_, String>(
            "SELECT instrument_id FROM acquisition_jobs WHERE id = $1",
        )
        .bind(&generation_id)
        .fetch_one(&mut *transaction)
        .await?;
        let mut deleted = serde_json::Map::new();
        for (table, key) in [
            ("raw_eod_pages", "raw_eod_pages"),
            ("raw_split_pages", "raw_split_pages"),
            ("quotes", "quotes"),
            ("instruments", "instruments"),
            ("split_snapshots", "split_snapshots"),
        ] {
            let sql = format!("DELETE FROM {table} WHERE job_id = $1");
            let count = sqlx::query(&sql)
                .bind(&generation_id)
                .execute(&mut *transaction)
                .await?
                .rows_affected();
            deleted.insert(key.into(), json!(count));
        }
        sqlx::query(
            "UPDATE acquisition_jobs
             SET retention_state = 'pruned', pruned_at = now()
             WHERE id = $1",
        )
        .bind(&generation_id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "INSERT INTO market_data_retention
                (id, instrument_id, generation_id, provider, pruned_at, evidence)
             VALUES ($1, $2, $3, 'yfinance', $4, $5)",
        )
        .bind(Uuid::new_v4().to_string())
        .bind(instrument)
        .bind(&generation_id)
        .bind(Utc::now())
        .bind(json!({ "deleted": deleted }))
        .execute(&mut *transaction)
        .await?;
        result.pruned += 1;
    }

    transaction.commit().await?;
    tracing::info!(?result, "market-data retention completed");
    Ok(result)
}
