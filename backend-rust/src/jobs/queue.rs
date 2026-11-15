use std::{fmt, str::FromStr, time::Duration};

use chrono::{DateTime, Utc};
use serde_json::Value;
use sqlx::{FromRow, PgPool};
use uuid::Uuid;

use crate::{AppError, AppResult};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Queue {
    Catalogue,
    MarketData,
}

impl Queue {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Catalogue => "catalogue",
            Self::MarketData => "marketdata",
        }
    }
}

impl fmt::Display for Queue {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for Queue {
    type Err = AppError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "catalogue" => Ok(Self::Catalogue),
            "marketdata" => Ok(Self::MarketData),
            value => Err(AppError::Config(format!("unknown queue {value:?}"))),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JobKind {
    EtfImport,
    MarketDataRefresh,
    MarketDataRetention,
    MarketDataAcquisition,
    MarketDataStatistics,
}

impl JobKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::EtfImport => "etf_import",
            Self::MarketDataRefresh => "market_data_refresh",
            Self::MarketDataRetention => "market_data_retention",
            Self::MarketDataAcquisition => "market_data_acquisition",
            Self::MarketDataStatistics => "market_data_statistics",
        }
    }
}

impl fmt::Display for JobKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for JobKind {
    type Err = AppError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "etf_import" => Ok(Self::EtfImport),
            "market_data_refresh" => Ok(Self::MarketDataRefresh),
            "market_data_retention" => Ok(Self::MarketDataRetention),
            "market_data_acquisition" => Ok(Self::MarketDataAcquisition),
            "market_data_statistics" => Ok(Self::MarketDataStatistics),
            value => Err(AppError::Internal(format!("unknown job kind {value:?}"))),
        }
    }
}

#[derive(Debug, Clone)]
pub struct NewJob {
    pub id: Option<String>,
    pub kind: JobKind,
    pub queue: Queue,
    pub payload: Value,
    pub max_attempts: i32,
    pub run_at: DateTime<Utc>,
    pub dedupe_key: Option<String>,
}

impl NewJob {
    pub fn immediate(kind: JobKind, queue: Queue, payload: Value, max_attempts: i32) -> Self {
        Self {
            id: None,
            kind,
            queue,
            payload,
            max_attempts,
            run_at: Utc::now(),
            dedupe_key: None,
        }
    }
}

#[derive(Debug, Clone, FromRow)]
pub struct JobRecord {
    pub id: String,
    pub kind: String,
    pub queue: String,
    pub payload: Value,
    pub status: String,
    pub run_at: DateTime<Utc>,
    pub requested_at: DateTime<Utc>,
    pub started_at: Option<DateTime<Utc>>,
    pub finished_at: Option<DateTime<Utc>>,
    pub attempts: i32,
    pub max_attempts: i32,
    pub locked_by: Option<String>,
    pub locked_at: Option<DateTime<Utc>>,
    pub lease_until: Option<DateTime<Utc>>,
    pub last_error: Option<String>,
    pub public_error: Option<String>,
    pub dedupe_key: Option<String>,
}

impl JobRecord {
    pub fn parsed_kind(&self) -> AppResult<JobKind> {
        self.kind.parse()
    }
}

pub async fn enqueue(pool: &PgPool, job: NewJob) -> AppResult<String> {
    if job.max_attempts < 1 {
        return Err(AppError::Internal("max_attempts must be positive".into()));
    }
    let id = job.id.unwrap_or_else(|| Uuid::new_v4().to_string());
    let result = sqlx::query(
        "INSERT INTO job_queue
            (id, kind, queue, payload, status, run_at, requested_at, max_attempts, dedupe_key)
         VALUES ($1, $2, $3, $4, 'queued', $5, now(), $6, $7)
         ON CONFLICT DO NOTHING",
    )
    .bind(&id)
    .bind(job.kind.as_str())
    .bind(job.queue.as_str())
    .bind(job.payload)
    .bind(job.run_at)
    .bind(job.max_attempts)
    .bind(&job.dedupe_key)
    .execute(pool)
    .await?;

    if result.rows_affected() == 1 {
        return Ok(id);
    }
    if let Some(dedupe_key) = job.dedupe_key {
        return sqlx::query_scalar::<_, String>("SELECT id FROM job_queue WHERE dedupe_key = $1")
            .bind(dedupe_key)
            .fetch_one(pool)
            .await
            .map_err(Into::into);
    }
    let exists =
        sqlx::query_scalar::<_, bool>("SELECT EXISTS(SELECT 1 FROM job_queue WHERE id = $1)")
            .bind(&id)
            .fetch_one(pool)
            .await?;
    if exists {
        Ok(id)
    } else {
        Err(AppError::Internal("job insert did not persist".into()))
    }
}

pub async fn claim(
    pool: &PgPool,
    queue: Queue,
    worker_id: &str,
    lease: Duration,
) -> AppResult<Option<JobRecord>> {
    let mut transaction = pool.begin().await?;
    sqlx::query(
        "UPDATE job_queue
         SET status = 'failed',
             finished_at = now(),
             locked_by = NULL,
             locked_at = NULL,
             lease_until = NULL,
             public_error = 'Job failed. Check the worker or service configuration.'
         WHERE status = 'running'
           AND lease_until <= now()
           AND attempts >= max_attempts",
    )
    .execute(&mut *transaction)
    .await?;

    let lease_seconds = i64::try_from(lease.as_secs())
        .map_err(|_| AppError::Internal("job lease is too large".into()))?;
    let claimed = sqlx::query_as::<_, JobRecord>(
        "WITH candidate AS (
             SELECT id
             FROM job_queue
             WHERE queue = $1
               AND attempts < max_attempts
               AND (
                    (status IN ('queued', 'retrying') AND run_at <= now())
                    OR
                    (status = 'running' AND lease_until <= now())
               )
             ORDER BY run_at, requested_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
         )
         UPDATE job_queue AS jobs
         SET status = 'running',
             attempts = jobs.attempts + 1,
             started_at = COALESCE(jobs.started_at, now()),
             finished_at = NULL,
             locked_by = $2,
             locked_at = now(),
             lease_until = now() + make_interval(secs => $3::double precision),
             public_error = NULL
         FROM candidate
         WHERE jobs.id = candidate.id
         RETURNING jobs.id, jobs.kind, jobs.queue, jobs.payload, jobs.status,
                   jobs.run_at, jobs.requested_at, jobs.started_at, jobs.finished_at,
                   jobs.attempts, jobs.max_attempts, jobs.locked_by, jobs.locked_at,
                   jobs.lease_until, jobs.last_error, jobs.public_error, jobs.dedupe_key",
    )
    .bind(queue.as_str())
    .bind(worker_id)
    .bind(lease_seconds as f64)
    .fetch_optional(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok(claimed)
}

pub async fn heartbeat(
    pool: &PgPool,
    job_id: &str,
    worker_id: &str,
    lease: Duration,
) -> AppResult<bool> {
    let lease_seconds = lease.as_secs_f64();
    let result = sqlx::query(
        "UPDATE job_queue
         SET lease_until = now() + make_interval(secs => $3::double precision)
         WHERE id = $1 AND status = 'running' AND locked_by = $2",
    )
    .bind(job_id)
    .bind(worker_id)
    .bind(lease_seconds)
    .execute(pool)
    .await?;
    Ok(result.rows_affected() == 1)
}

pub async fn complete(pool: &PgPool, job_id: &str, worker_id: &str) -> AppResult<bool> {
    let result = sqlx::query(
        "UPDATE job_queue
         SET status = 'succeeded',
             finished_at = now(),
             locked_by = NULL,
             locked_at = NULL,
             lease_until = NULL,
             last_error = NULL,
             public_error = NULL
         WHERE id = $1 AND status = 'running' AND locked_by = $2",
    )
    .bind(job_id)
    .bind(worker_id)
    .execute(pool)
    .await?;
    Ok(result.rows_affected() == 1)
}

pub async fn fail(
    pool: &PgPool,
    job: &JobRecord,
    worker_id: &str,
    error: &str,
    retryable: bool,
) -> AppResult<bool> {
    let retry = retryable && job.attempts < job.max_attempts;
    let delay_seconds = retry_delay(job.attempts).as_secs_f64();
    let status = if retry { "retrying" } else { "failed" };
    let public_error = if retry {
        "Job interrupted; an automatic retry is pending."
    } else {
        "Job failed. Check the worker or service configuration."
    };
    let result = sqlx::query(
        "UPDATE job_queue
         SET status = $3,
             run_at = CASE WHEN $4 THEN now() + make_interval(secs => $5::double precision) ELSE run_at END,
             finished_at = CASE WHEN $4 THEN NULL ELSE now() END,
             locked_by = NULL,
             locked_at = NULL,
             lease_until = NULL,
             last_error = $6,
             public_error = $7
         WHERE id = $1 AND status = 'running' AND locked_by = $2",
    )
    .bind(&job.id)
    .bind(worker_id)
    .bind(status)
    .bind(retry)
    .bind(delay_seconds)
    .bind(error)
    .bind(public_error)
    .execute(pool)
    .await?;
    let updated = result.rows_affected() == 1;
    if updated && !retry && job.kind == JobKind::MarketDataAcquisition.as_str() {
        sqlx::query(
            "UPDATE acquisition_jobs
             SET status = 'failed', active = false, phase = 'failed',
                 finished_at = now(), error = $2
             WHERE id = $1 AND active = true",
        )
        .bind(&job.id)
        .bind("Acquisition worker failed. Request data again to retry.")
        .execute(pool)
        .await?;
    }
    Ok(updated)
}

fn retry_delay(attempt: i32) -> Duration {
    let exponent = u32::try_from(attempt.saturating_sub(1)).unwrap_or(0).min(5);
    Duration::from_secs((30_u64.saturating_mul(2_u64.pow(exponent))).min(600))
}
