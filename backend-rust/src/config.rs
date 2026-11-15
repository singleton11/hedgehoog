use std::{env, net::SocketAddr, str::FromStr, time::Duration};

use crate::error::{AppError, AppResult};

#[derive(Clone)]
pub struct Config {
    pub database_url: String,
    pub bind_addr: SocketAddr,
    pub environment: Environment,
    pub jwt_secret: String,
    pub jwt_expire_minutes: i64,
    pub cookie_secure: bool,
    pub allow_insecure_http: bool,
    pub admin_login: String,
    pub admin_password: String,
    pub allowed_origins: Vec<String>,
    pub database_max_connections: u32,
    pub job_poll_interval: Duration,
    pub job_lease: Duration,
    pub market_data_retention_enabled: bool,
    pub market_data_retention_batch_size: i64,
    pub market_data_max_calls_per_job: i32,
    pub market_data_timeout: Duration,
    pub yahoo_request_interval: Duration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Environment {
    Development,
    Production,
}

impl Config {
    pub fn from_env() -> AppResult<Self> {
        dotenvy::dotenv().ok();

        let environment = match value("ENVIRONMENT", "development").as_str() {
            "development" => Environment::Development,
            "production" => Environment::Production,
            value => {
                return Err(AppError::Config(format!(
                    "ENVIRONMENT must be development or production, got {value:?}"
                )))
            }
        };
        let jwt_secret = required("JWT_SECRET")?;
        if jwt_secret.len() < 32 {
            return Err(AppError::Config(
                "JWT_SECRET must contain at least 32 characters".into(),
            ));
        }
        let admin_password = value("ADMIN_PASSWORD", "admin");
        if admin_password.is_empty() {
            return Err(AppError::Config("ADMIN_PASSWORD cannot be empty".into()));
        }

        let cookie_secure = boolean("COOKIE_SECURE", false)?;
        let allow_insecure_http = boolean("ALLOW_INSECURE_HTTP", false)?;
        if environment == Environment::Production {
            if admin_password == "admin" || admin_password.len() < 12 {
                return Err(AppError::Config(
                    "set a strong ADMIN_PASSWORD (at least 12 characters) in production".into(),
                ));
            }
            if !cookie_secure && !allow_insecure_http {
                return Err(AppError::Config(
                    "COOKIE_SECURE must be true in production unless ALLOW_INSECURE_HTTP is true"
                        .into(),
                ));
            }
        }

        Ok(Self {
            database_url: value(
                "DATABASE_URL",
                "postgresql://hedgehoog:hedgehoog-local@127.0.0.1:5432/hedgehoog",
            ),
            bind_addr: value("BIND_ADDR", "0.0.0.0:8000")
                .parse()
                .map_err(|error| AppError::Config(format!("invalid BIND_ADDR: {error}")))?,
            environment,
            jwt_secret,
            jwt_expire_minutes: number("JWT_EXPIRE_MINUTES", 43_200_i64, 1, 43_200)?,
            cookie_secure,
            allow_insecure_http,
            admin_login: value("ADMIN_LOGIN", "admin"),
            admin_password,
            allowed_origins: csv(
                "ALLOWED_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
            ),
            database_max_connections: number("DATABASE_MAX_CONNECTIONS", 20_u32, 1, 200)?,
            job_poll_interval: Duration::from_millis(number(
                "JOB_POLL_INTERVAL_MS",
                500_u64,
                50,
                60_000,
            )?),
            job_lease: Duration::from_secs(number("JOB_LEASE_SECONDS", 120_u64, 30, 3600)?),
            market_data_retention_enabled: boolean("MARKET_DATA_RETENTION_ENABLED", true)?,
            market_data_retention_batch_size: number(
                "MARKET_DATA_RETENTION_BATCH_SIZE",
                1_000_i64,
                1,
                10_000,
            )?,
            market_data_max_calls_per_job: number(
                "MARKET_DATA_MAX_CALLS_PER_JOB",
                40_i32,
                1,
                200,
            )?,
            market_data_timeout: Duration::from_secs(number(
                "MARKET_DATA_TIMEOUT_SECONDS",
                15_u64,
                1,
                60,
            )?),
            yahoo_request_interval: Duration::from_secs_f64(float_number(
                "YAHOO_REQUEST_INTERVAL_SECONDS",
                0.05,
                0.0,
                60.0,
            )?),
        })
    }
}

fn required(name: &str) -> AppResult<String> {
    env::var(name).map_err(|_| AppError::Config(format!("{name} is required")))
}

fn value(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn boolean(name: &str, default: bool) -> AppResult<bool> {
    match env::var(name) {
        Ok(value) => bool::from_str(&value)
            .map_err(|_| AppError::Config(format!("{name} must be true or false"))),
        Err(_) => Ok(default),
    }
}

fn number<T>(name: &str, default: T, minimum: T, maximum: T) -> AppResult<T>
where
    T: FromStr + PartialOrd + Copy + std::fmt::Display,
    <T as FromStr>::Err: std::fmt::Display,
{
    let parsed = match env::var(name) {
        Ok(value) => value
            .parse()
            .map_err(|error| AppError::Config(format!("invalid {name}: {error}")))?,
        Err(_) => default,
    };
    if parsed < minimum || parsed > maximum {
        return Err(AppError::Config(format!(
            "{name} must be between {minimum} and {maximum}"
        )));
    }
    Ok(parsed)
}

fn float_number(name: &str, default: f64, minimum: f64, maximum: f64) -> AppResult<f64> {
    let parsed = match env::var(name) {
        Ok(value) => value
            .parse::<f64>()
            .map_err(|error| AppError::Config(format!("invalid {name}: {error}")))?,
        Err(_) => default,
    };
    if !parsed.is_finite() || parsed < minimum || parsed > maximum {
        return Err(AppError::Config(format!(
            "{name} must be finite and between {minimum} and {maximum}"
        )));
    }
    Ok(parsed)
}

fn csv(name: &str, default: &str) -> Vec<String> {
    value(name, default)
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}
