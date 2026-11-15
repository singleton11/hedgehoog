use chrono::{DateTime, NaiveTime, Utc};
use serde_json::json;
use sqlx::{pool::PoolConnection, PgPool, Postgres};
use tokio::time::{sleep, Duration};
use tokio_util::sync::CancellationToken;

use crate::{
    jobs::queue::{enqueue, JobKind, NewJob, Queue},
    AppResult,
};

const SCHEDULER_LOCK: i64 = 3_181_474_646_647_783_787;

pub async fn run(pool: PgPool, shutdown: CancellationToken) -> AppResult<()> {
    let mut lock_connection = acquire_lock(&pool).await?;
    tracing::info!("scheduler lock acquired");

    while !shutdown.is_cancelled() {
        enqueue_due(&pool, Utc::now()).await?;
        tokio::select! {
            _ = shutdown.cancelled() => break,
            _ = sleep(Duration::from_secs(30)) => {}
        }
    }

    sqlx::query("SELECT pg_advisory_unlock($1)")
        .bind(SCHEDULER_LOCK)
        .execute(&mut *lock_connection)
        .await?;
    tracing::info!("scheduler stopped");
    Ok(())
}

async fn acquire_lock(pool: &PgPool) -> AppResult<PoolConnection<Postgres>> {
    loop {
        let mut connection = pool.acquire().await?;
        let acquired = sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_lock($1)")
            .bind(SCHEDULER_LOCK)
            .fetch_one(&mut *connection)
            .await?;
        if acquired {
            return Ok(connection);
        }
        tracing::warn!("another scheduler owns the PostgreSQL advisory lock");
        sleep(Duration::from_secs(30)).await;
    }
}

pub async fn enqueue_due(pool: &PgPool, now: DateTime<Utc>) -> AppResult<()> {
    let date = now.date_naive();
    let schedules = [
        (
            NaiveTime::from_hms_opt(0, 0, 0).expect("valid time"),
            JobKind::EtfImport,
            6,
            "daily-etf-import",
        ),
        (
            NaiveTime::from_hms_opt(2, 0, 0).expect("valid time"),
            JobKind::MarketDataRefresh,
            6,
            "daily-market-data-refresh",
        ),
        (
            NaiveTime::from_hms_opt(3, 0, 0).expect("valid time"),
            JobKind::MarketDataRetention,
            13,
            "daily-market-data-retention",
        ),
    ];

    for (time, kind, max_attempts, name) in schedules {
        if now.time() < time {
            continue;
        }
        let dedupe_key = format!("{name}:{date}");
        enqueue(
            pool,
            NewJob {
                id: None,
                kind,
                queue: Queue::Catalogue,
                payload: json!({}),
                max_attempts,
                run_at: now,
                dedupe_key: Some(dedupe_key),
            },
        )
        .await?;
    }
    Ok(())
}
