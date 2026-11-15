use std::time::Duration;

use sqlx::{postgres::PgPoolOptions, PgPool};

use crate::{AppResult, Config};

pub async fn connect(config: &Config) -> AppResult<PgPool> {
    let pool = PgPoolOptions::new()
        .max_connections(config.database_max_connections)
        .acquire_timeout(Duration::from_secs(5))
        .connect(&config.database_url)
        .await?;
    Ok(pool)
}

pub async fn migrate(pool: &PgPool) -> AppResult<()> {
    sqlx::migrate!("./migrations").run(pool).await?;
    Ok(())
}

pub async fn ping(pool: &PgPool) -> AppResult<()> {
    sqlx::query("SELECT 1").execute(pool).await?;
    Ok(())
}
