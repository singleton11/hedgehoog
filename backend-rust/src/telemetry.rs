use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

pub fn init(service: &str) {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("hedgehoog=info,tower_http=info"));
    let format = tracing_subscriber::fmt::layer()
        .with_target(false)
        .with_thread_ids(false)
        .with_span_events(tracing_subscriber::fmt::format::FmtSpan::CLOSE);
    tracing_subscriber::registry()
        .with(filter)
        .with(format)
        .init();
    tracing::info!(service, "telemetry initialized");
}
