use axum::{
    extract::{Request, State},
    http::{
        header::{CACHE_CONTROL, HOST, ORIGIN},
        HeaderValue, Method, StatusCode,
    },
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use tower_http::{
    catch_panic::CatchPanicLayer,
    request_id::{MakeRequestUuid, PropagateRequestIdLayer, SetRequestIdLayer},
    trace::TraceLayer,
};

use crate::{
    auth::{self, AdminUser, CurrentUser},
    broker_import, jobs, market_data, performance, planner, portfolio, recommendations, AppResult,
    AppState,
};

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/auth/login", post(auth::login))
        .route("/auth/me", get(auth::me))
        .route("/auth/logout", post(auth::logout))
        .route("/", get(root))
        .route("/admin", get(admin))
        .nest("/jobs", jobs::http::router())
        .merge(market_data::http::router())
        .merge(planner::router())
        .merge(portfolio::router())
        .merge(broker_import::router())
        .merge(performance::router())
        .merge(recommendations::router())
        .layer(PropagateRequestIdLayer::x_request_id())
        .layer(SetRequestIdLayer::x_request_id(MakeRequestUuid))
        .layer(TraceLayer::new_for_http())
        .layer(CatchPanicLayer::new())
        .layer(middleware::from_fn_with_state(
            state.clone(),
            security_headers_and_origin_check,
        ))
        .with_state(state)
}

async fn health(State(state): State<AppState>) -> AppResult<Json<Value>> {
    crate::db::ping(&state.pool).await?;
    Ok(Json(json!({ "status": "ok" })))
}

async fn root(CurrentUser(_user): CurrentUser) -> Json<Value> {
    Json(json!({ "message": "Hello, World!" }))
}

async fn admin(AdminUser(_user): AdminUser) -> Json<Value> {
    Json(json!({ "message": "Administrator access granted" }))
}

async fn security_headers_and_origin_check(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Response {
    if !matches!(
        *request.method(),
        Method::GET | Method::HEAD | Method::OPTIONS
    ) {
        let headers = request.headers();
        let cross_site = headers
            .get("sec-fetch-site")
            .and_then(|value| value.to_str().ok())
            == Some("cross-site");
        let origin_allowed = headers
            .get(ORIGIN)
            .and_then(|value| value.to_str().ok())
            .map(|origin| {
                same_origin(headers)
                    .as_deref()
                    .is_some_and(|expected| origin == expected)
                    || state
                        .config
                        .allowed_origins
                        .iter()
                        .any(|allowed| allowed == origin)
            })
            .unwrap_or(true);
        if cross_site || !origin_allowed {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({ "detail": "Origin not allowed" })),
            )
                .into_response();
        }
    }

    let mut response = next.run(request).await;
    response
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response.headers_mut().insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    response
}

fn same_origin(headers: &axum::http::HeaderMap) -> Option<String> {
    let host = headers.get(HOST)?.to_str().ok()?;
    let scheme = headers
        .get("x-forwarded-proto")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .map(str::trim)
        .filter(|value| matches!(*value, "http" | "https"))
        .unwrap_or("http");
    Some(format!("{scheme}://{host}"))
}
