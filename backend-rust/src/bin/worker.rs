use std::str::FromStr;

use clap::Parser;
use hedgehoog::{
    db,
    jobs::{queue::Queue, worker},
    telemetry, Config,
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

#[derive(Debug, Parser)]
struct Arguments {
    #[arg(long, env = "JOB_QUEUE", default_value = "catalogue")]
    queue: String,
    #[arg(long, env = "WORKER_CONCURRENCY", default_value_t = 1)]
    concurrency: usize,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    telemetry::init("hedgehoog-worker");
    let arguments = Arguments::parse();
    let config = Config::from_env()?;
    let pool = db::connect(&config).await?;
    db::migrate(&pool).await?;

    let queue = Queue::from_str(&arguments.queue)?;
    let host = hostname::get()
        .ok()
        .and_then(|value| value.into_string().ok())
        .unwrap_or_else(|| "unknown-host".into());
    let worker_id = format!("{host}:{}:{}", std::process::id(), Uuid::new_v4());
    let shutdown = CancellationToken::new();
    let signal = shutdown.clone();
    tokio::spawn(async move {
        shutdown_signal().await;
        signal.cancel();
    });

    worker::run(
        pool,
        worker::WorkerConfig {
            queue,
            concurrency: arguments.concurrency,
            poll_interval: config.job_poll_interval,
            lease: config.job_lease,
            worker_id,
            app_config: config.clone(),
        },
        shutdown,
    )
    .await?;
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("could not install Ctrl+C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("could not install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {}
        _ = terminate => {}
    }
}
