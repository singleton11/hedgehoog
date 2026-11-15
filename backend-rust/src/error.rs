use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde::Serialize;
use thiserror::Error;

pub type AppResult<T> = Result<T, AppError>;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("{0}")]
    Config(String),
    #[error("{detail}")]
    Http { status: StatusCode, detail: String },
    #[error("database unavailable")]
    Database(#[from] sqlx::Error),
    #[error("database migration failed")]
    Migration(#[from] sqlx::migrate::MigrateError),
    #[error("internal server error")]
    Internal(String),
}

#[derive(Serialize)]
struct ErrorBody<'a> {
    detail: &'a str,
}

impl AppError {
    pub fn bad_request(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::BAD_REQUEST,
            detail: detail.into(),
        }
    }

    pub fn unauthorized(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::UNAUTHORIZED,
            detail: detail.into(),
        }
    }

    pub fn forbidden(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::FORBIDDEN,
            detail: detail.into(),
        }
    }

    pub fn not_found(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::NOT_FOUND,
            detail: detail.into(),
        }
    }

    pub fn conflict(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::CONFLICT,
            detail: detail.into(),
        }
    }

    pub fn unprocessable(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            detail: detail.into(),
        }
    }

    pub fn service_unavailable(detail: impl Into<String>) -> Self {
        Self::Http {
            status: StatusCode::SERVICE_UNAVAILABLE,
            detail: detail.into(),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, detail) = match self {
            Self::Config(detail) => (StatusCode::INTERNAL_SERVER_ERROR, detail),
            Self::Http { status, detail } => (status, detail),
            Self::Database(error) => {
                tracing::error!(error = %error, "database request failed");
                (
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Database unavailable".to_owned(),
                )
            }
            Self::Migration(error) => {
                tracing::error!(error = %error, "database migration failed");
                (
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Database unavailable".to_owned(),
                )
            }
            Self::Internal(error) => {
                tracing::error!(error = %error, "internal request failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Internal server error".to_owned(),
                )
            }
        };
        (status, Json(ErrorBody { detail: &detail })).into_response()
    }
}
