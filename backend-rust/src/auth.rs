use std::{
    collections::HashSet,
    sync::LazyLock,
    time::{SystemTime, UNIX_EPOCH},
};

use argon2::{
    password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
    Argon2,
};
use axum::{
    extract::{FromRequestParts, State},
    http::{
        header::{COOKIE, SET_COOKIE},
        request::Parts,
        HeaderMap, HeaderValue, StatusCode,
    },
    response::{IntoResponse, Response},
    Json,
};
use chrono::Utc;
use cookie::{time::Duration as CookieDuration, Cookie, SameSite};
use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use rand_core::OsRng;
use serde::{Deserialize, Serialize};
use sqlx::FromRow;
use uuid::Uuid;

use crate::{config::Environment, error::AppError, AppResult, AppState, Config};

pub const COOKIE_NAME: &str = "hedgehoog_session";
const JWT_ISSUER: &str = "hedgehoog";
const JWT_AUDIENCE: &str = "hedgehoog-web";

static DUMMY_HASH: LazyLock<String> =
    LazyLock::new(|| hash_password("not-a-real-user-password").expect("dummy password hash"));

#[derive(Clone, Debug, FromRow)]
pub struct User {
    pub id: String,
    pub login: String,
    pub password_hash: String,
    pub role: String,
    pub disabled: bool,
}

#[derive(Debug, Deserialize)]
pub struct LoginRequest {
    pub login: String,
    pub password: String,
}

#[derive(Debug, Serialize)]
pub struct UserResponse {
    pub id: String,
    pub login: String,
    pub role: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct Claims {
    sub: String,
    iat: usize,
    exp: usize,
    iss: String,
    aud: String,
}

pub struct CurrentUser(pub User);

pub struct AdminUser(pub User);

impl FromRequestParts<AppState> for CurrentUser {
    type Rejection = AppError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let token = session_cookie(&parts.headers)
            .ok_or_else(|| AppError::unauthorized("Authentication required"))?;
        let user_id = decode_token(&token, &state.config)
            .map_err(|_| AppError::unauthorized("Authentication required"))?;
        Uuid::parse_str(&user_id).map_err(|_| AppError::unauthorized("Authentication required"))?;

        let user = sqlx::query_as::<_, User>(
            "SELECT id, login, password_hash, role, disabled FROM users WHERE id = $1",
        )
        .bind(user_id)
        .fetch_optional(&state.pool)
        .await?
        .filter(|user| !user.disabled)
        .ok_or_else(|| AppError::unauthorized("Authentication required"))?;
        Ok(Self(user))
    }
}

impl FromRequestParts<AppState> for AdminUser {
    type Rejection = AppError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let CurrentUser(user) = CurrentUser::from_request_parts(parts, state).await?;
        if user.role != "admin" {
            return Err(AppError::forbidden("Administrator access required"));
        }
        Ok(Self(user))
    }
}

pub async fn seed_admin(state: &AppState) -> AppResult<()> {
    let existing = sqlx::query_as::<_, User>(
        "SELECT id, login, password_hash, role, disabled FROM users WHERE login = $1",
    )
    .bind(&state.config.admin_login)
    .fetch_optional(&state.pool)
    .await?;

    if let Some(user) = existing {
        if state.config.environment == Environment::Production
            && verify_password("admin", &user.password_hash)
        {
            return Err(AppError::Config(
                "the stored admin account still uses the development password".into(),
            ));
        }
        return Ok(());
    }

    let password = state.config.admin_password.clone();
    let password_hash = tokio::task::spawn_blocking(move || hash_password(&password))
        .await
        .map_err(|error| AppError::Internal(format!("password task failed: {error}")))??;
    sqlx::query(
        "INSERT INTO users (id, login, password_hash, role, disabled, created_at)
         VALUES ($1, $2, $3, 'admin', false, now())
         ON CONFLICT (login) DO NOTHING",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&state.config.admin_login)
    .bind(password_hash)
    .execute(&state.pool)
    .await?;
    Ok(())
}

pub async fn login(
    State(state): State<AppState>,
    Json(credentials): Json<LoginRequest>,
) -> AppResult<Response> {
    if credentials.login.is_empty() || credentials.login.len() > 128 {
        return Err(AppError::bad_request("Invalid login or password"));
    }
    if credentials.password.is_empty() || credentials.password.len() > 1024 {
        return Err(AppError::bad_request("Invalid login or password"));
    }

    let user = sqlx::query_as::<_, User>(
        "SELECT id, login, password_hash, role, disabled FROM users WHERE login = $1",
    )
    .bind(&credentials.login)
    .fetch_optional(&state.pool)
    .await?;
    let expected_hash = user
        .as_ref()
        .map(|user| user.password_hash.clone())
        .unwrap_or_else(|| DUMMY_HASH.clone());
    let password = credentials.password;
    let valid = tokio::task::spawn_blocking(move || verify_password(&password, &expected_hash))
        .await
        .map_err(|error| AppError::Internal(format!("password task failed: {error}")))?;

    let user = user
        .filter(|user| valid && !user.disabled)
        .ok_or_else(|| AppError::unauthorized("Invalid login or password"))?;
    let token = create_token(&user.id, &state.config)?;
    let mut response = Json(public_user(user)).into_response();
    response.headers_mut().insert(
        SET_COOKIE,
        HeaderValue::from_str(&session_cookie_header(&token, &state.config))
            .map_err(|error| AppError::Internal(format!("invalid session cookie: {error}")))?,
    );
    Ok(response)
}

pub async fn me(CurrentUser(user): CurrentUser) -> Json<UserResponse> {
    Json(public_user(user))
}

pub async fn logout(State(state): State<AppState>) -> AppResult<Response> {
    let cookie = Cookie::build((COOKIE_NAME, ""))
        .path("/")
        .http_only(true)
        .secure(state.config.cookie_secure)
        .same_site(SameSite::Strict)
        .max_age(CookieDuration::ZERO)
        .build();
    let mut response = StatusCode::NO_CONTENT.into_response();
    response.headers_mut().insert(
        SET_COOKIE,
        HeaderValue::from_str(&cookie.to_string())
            .map_err(|error| AppError::Internal(format!("invalid logout cookie: {error}")))?,
    );
    Ok(response)
}

pub fn public_user(user: User) -> UserResponse {
    UserResponse {
        id: user.id,
        login: user.login,
        role: user.role,
    }
}

fn session_cookie(headers: &HeaderMap) -> Option<String> {
    let raw = headers.get(COOKIE)?.to_str().ok()?;
    raw.split(';').find_map(|item| {
        let cookie = Cookie::parse(item.trim()).ok()?;
        (cookie.name() == COOKIE_NAME).then(|| cookie.value().to_owned())
    })
}

fn session_cookie_header(token: &str, config: &Config) -> String {
    Cookie::build((COOKIE_NAME, token.to_owned()))
        .path("/")
        .http_only(true)
        .secure(config.cookie_secure)
        .same_site(SameSite::Strict)
        .max_age(CookieDuration::minutes(config.jwt_expire_minutes))
        .build()
        .to_string()
}

fn create_token(user_id: &str, config: &Config) -> AppResult<String> {
    let iat = unix_time()?;
    let exp = iat
        .checked_add((config.jwt_expire_minutes * 60) as usize)
        .ok_or_else(|| AppError::Internal("token expiry overflow".into()))?;
    let claims = Claims {
        sub: user_id.to_owned(),
        iat,
        exp,
        iss: JWT_ISSUER.to_owned(),
        aud: JWT_AUDIENCE.to_owned(),
    };
    encode(
        &Header::new(Algorithm::HS256),
        &claims,
        &EncodingKey::from_secret(config.jwt_secret.as_bytes()),
    )
    .map_err(|error| AppError::Internal(format!("could not create session token: {error}")))
}

fn decode_token(token: &str, config: &Config) -> Result<String, jsonwebtoken::errors::Error> {
    let mut validation = Validation::new(Algorithm::HS256);
    validation.set_issuer(&[JWT_ISSUER]);
    validation.set_audience(&[JWT_AUDIENCE]);
    validation.required_spec_claims = HashSet::from([
        "sub".to_owned(),
        "exp".to_owned(),
        "iat".to_owned(),
        "iss".to_owned(),
        "aud".to_owned(),
    ]);
    decode::<Claims>(
        token,
        &DecodingKey::from_secret(config.jwt_secret.as_bytes()),
        &validation,
    )
    .map(|token| token.claims.sub)
}

fn unix_time() -> AppResult<usize> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as usize)
        .map_err(|error| AppError::Internal(format!("system clock before epoch: {error}")))
}

fn hash_password(password: &str) -> AppResult<String> {
    let salt = SaltString::generate(&mut OsRng);
    Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map(|hash| hash.to_string())
        .map_err(|error| AppError::Internal(format!("could not hash password: {error}")))
}

fn verify_password(password: &str, encoded: &str) -> bool {
    PasswordHash::new(encoded).ok().is_some_and(|hash| {
        Argon2::default()
            .verify_password(password.as_bytes(), &hash)
            .is_ok()
    })
}

#[allow(dead_code)]
fn _utc_now() -> chrono::DateTime<Utc> {
    Utc::now()
}
