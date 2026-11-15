use sqlx::PgPool;

use crate::{auth::LoginRateLimiter, Config};

#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub config: Config,
    pub login_rate_limiter: LoginRateLimiter,
}

impl AppState {
    pub fn new(pool: PgPool, config: Config) -> Self {
        Self {
            pool,
            config,
            login_rate_limiter: LoginRateLimiter::default(),
        }
    }
}
