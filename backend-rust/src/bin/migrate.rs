use hedgehoog::{db, telemetry, Config};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    telemetry::init("hedgehoog-migrate");
    let config = Config::from_env()?;
    let pool = db::connect(&config).await?;
    db::migrate(&pool).await?;
    tracing::info!("database migrations completed");
    Ok(())
}
