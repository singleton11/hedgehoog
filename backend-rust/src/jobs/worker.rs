use std::{sync::Arc, time::Duration};

use sqlx::PgPool;
use tokio::time::{interval, sleep};
use tokio_util::sync::CancellationToken;

use crate::{
    jobs::queue::{self, JobKind, JobRecord, Queue},
    AppError, AppResult, Config,
};

#[derive(Clone)]
pub struct WorkerConfig {
    pub queue: Queue,
    pub concurrency: usize,
    pub poll_interval: Duration,
    pub lease: Duration,
    pub worker_id: String,
    pub app_config: Config,
}

#[derive(Debug)]
enum HandlerError {
    Retryable(String),
    Permanent(String),
}

pub async fn run(pool: PgPool, config: WorkerConfig, shutdown: CancellationToken) -> AppResult<()> {
    if config.concurrency == 0 {
        return Err(AppError::Config(
            "worker concurrency must be positive".into(),
        ));
    }
    let config = Arc::new(config);
    let mut handles = Vec::with_capacity(config.concurrency);
    for slot in 0..config.concurrency {
        let pool = pool.clone();
        let config = Arc::clone(&config);
        let shutdown = shutdown.child_token();
        handles.push(tokio::spawn(async move {
            worker_loop(pool, config, slot, shutdown).await
        }));
    }

    for handle in handles {
        handle
            .await
            .map_err(|error| AppError::Internal(format!("worker task failed: {error}")))??;
    }
    Ok(())
}

async fn worker_loop(
    pool: PgPool,
    config: Arc<WorkerConfig>,
    slot: usize,
    shutdown: CancellationToken,
) -> AppResult<()> {
    let worker_id = format!("{}:{slot}", config.worker_id);
    tracing::info!(queue = %config.queue, worker_id, "worker started");
    while !shutdown.is_cancelled() {
        let Some(job) = queue::claim(&pool, config.queue, &worker_id, config.lease).await? else {
            tokio::select! {
                _ = shutdown.cancelled() => break,
                _ = sleep(config.poll_interval) => continue,
            }
        };
        run_claimed_job(
            &pool,
            &config.app_config,
            &job,
            &worker_id,
            config.lease,
            &shutdown,
        )
        .await?;
    }
    tracing::info!(queue = %config.queue, worker_id, "worker stopped");
    Ok(())
}

async fn run_claimed_job(
    pool: &PgPool,
    app_config: &Config,
    job: &JobRecord,
    worker_id: &str,
    lease: Duration,
    shutdown: &CancellationToken,
) -> AppResult<()> {
    tracing::info!(
        job_id = %job.id,
        kind = %job.kind,
        attempt = job.attempts,
        max_attempts = job.max_attempts,
        "job started"
    );

    let handler = execute(pool, app_config, job);
    tokio::pin!(handler);
    let heartbeat_period = lease.div_f64(3.0).max(Duration::from_secs(5));
    let mut heartbeat = interval(heartbeat_period);
    heartbeat.tick().await;

    let outcome = loop {
        tokio::select! {
            result = &mut handler => break Some(result),
            _ = heartbeat.tick() => {
                if !queue::heartbeat(pool, &job.id, worker_id, lease).await? {
                    tracing::warn!(job_id = %job.id, "job lease was lost");
                    break None;
                }
            }
            _ = shutdown.cancelled() => {
                tracing::info!(job_id = %job.id, "shutdown leaves job for lease recovery");
                break None;
            }
        }
    };

    match outcome {
        Some(Ok(())) => {
            if !queue::complete(pool, &job.id, worker_id).await? {
                tracing::warn!(job_id = %job.id, "job completion lost its lease");
            }
        }
        Some(Err(HandlerError::Retryable(error))) => {
            tracing::warn!(job_id = %job.id, error, "job will retry");
            queue::fail(pool, job, worker_id, &error, true).await?;
        }
        Some(Err(HandlerError::Permanent(error))) => {
            tracing::error!(job_id = %job.id, error, "job failed");
            queue::fail(pool, job, worker_id, &error, false).await?;
        }
        None => {}
    }
    Ok(())
}

async fn execute(pool: &PgPool, config: &Config, job: &JobRecord) -> Result<(), HandlerError> {
    match job
        .parsed_kind()
        .map_err(|error| HandlerError::Permanent(error.to_string()))?
    {
        JobKind::EtfImport => crate::etfs::import_daily(pool)
            .await
            .map(|_| ())
            .map_err(|error| HandlerError::Retryable(error.to_string())),
        JobKind::MarketDataRefresh => {
            let include_catalogue = job
                .payload
                .get("include_catalogue")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            crate::market_data::refresh::refresh_now(pool, include_catalogue)
                .await
                .map(|_| ())
                .map_err(|error| HandlerError::Retryable(error.to_string()))
        }
        JobKind::MarketDataRetention => {
            let instrument_id = job
                .payload
                .get("instrument_id")
                .and_then(serde_json::Value::as_str);
            crate::market_data::retention::cleanup(pool, config, instrument_id)
                .await
                .map(|_| ())
                .map_err(|error| HandlerError::Retryable(error.to_string()))
        }
        JobKind::MarketDataAcquisition => {
            let job_id = job
                .payload
                .get("job_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or(&job.id);
            crate::market_data::acquisition::run(pool, config, job_id)
                .await
                .map_err(|error| match error {
                    crate::market_data::acquisition::AcquisitionError::Busy
                    | crate::market_data::acquisition::AcquisitionError::Database(_)
                    | crate::market_data::acquisition::AcquisitionError::RetryableProvider(_) => {
                        HandlerError::Retryable(error.to_string())
                    }
                })
        }
    }
}

#[allow(dead_code)]
fn _retryable(message: impl Into<String>) -> HandlerError {
    HandlerError::Retryable(message.into())
}
