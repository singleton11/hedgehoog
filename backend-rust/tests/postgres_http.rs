use std::env;

use axum::{
    body::Body,
    http::{header::COOKIE, Request, StatusCode},
};
use hedgehoog::{auth, db, http, AppState, Config};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;
use uuid::Uuid;

#[tokio::test]
async fn authenticated_catalogue_settings_and_acquisition_routes_use_postgres() {
    let Ok(database_url) = env::var("TEST_DATABASE_URL") else {
        eprintln!("TEST_DATABASE_URL is not set; skipping PostgreSQL HTTP test");
        return;
    };
    env::set_var("DATABASE_URL", &database_url);
    env::set_var(
        "JWT_SECRET",
        "postgres-http-secret-with-at-least-32-characters",
    );
    env::set_var("ADMIN_LOGIN", "admin");
    env::set_var("ADMIN_PASSWORD", "admin");
    let config = Config::from_env().expect("test configuration");
    let pool = db::connect(&config).await.expect("connect PostgreSQL");
    db::migrate(&pool).await.expect("run migrations");
    sqlx::query(
        "TRUNCATE
             market_data_retention, market_data_reader_leases,
             raw_eod_pages, raw_split_pages, split_snapshots, quotes, instruments,
             market_data_publications, job_queue, acquisition_jobs,
             instrument_state, instrument_statistics, manual_splits,
             etfs, imports, app_settings, users
         CASCADE",
    )
    .execute(&pool)
    .await
    .expect("clear application tables");
    let state = AppState {
        pool: pool.clone(),
        config: config.clone(),
    };
    auth::seed_admin(&state).await.expect("seed admin");
    let app = http::router(state);

    let login = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"login":"admin","password":"admin"}"#))
                .unwrap(),
        )
        .await
        .expect("login response");
    assert_eq!(login.status(), StatusCode::OK);
    let cookie = login
        .headers()
        .get("set-cookie")
        .expect("session cookie")
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();

    sqlx::query(
        "INSERT INTO etfs
            (id, isin, import_date, name, ticker, asset_class, currency, inception_date, source)
         VALUES ($1, 'IE00B4L5Y983', CURRENT_DATE, 'Example ETF', 'EXAMPLE',
                 'Equity', 'EUR', '2010-01-01', $2)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(json!({"isin": "IE00B4L5Y983", "name": "Example ETF"}))
    .execute(&pool)
    .await
    .expect("insert catalogue ETF");

    let policy = get_json(&app, "/settings/market-data", &cookie).await;
    assert_eq!(policy, json!({"version": 1, "method": "smooth", "seed": 0}));

    let update = request_json(
        &app,
        "PUT",
        "/settings/market-data",
        &cookie,
        Some(json!({"method": "normal", "seed": 7})),
    )
    .await;
    assert_eq!(update.0, StatusCode::OK);
    assert_eq!(update.1["method"], "normal");
    assert_eq!(update.1["seed"], 7);

    let indices = get_json(&app, "/indices", &cookie).await;
    assert_eq!(indices["total"], 4);
    assert_eq!(indices["items"].as_array().unwrap().len(), 4);

    let search = get_json(&app, "/instruments/search?q=example", &cookie).await;
    assert_eq!(search["items"][0]["instrument_id"], "IE00B4L5Y983");

    let empty_quotes = get_json(&app, "/instruments/IE00B4L5Y983/quotes", &cookie).await;
    assert_eq!(empty_quotes["total"], 0);
    assert_eq!(empty_quotes["generation"], Value::Null);

    let acquisition = request_json(
        &app,
        "POST",
        "/instruments/IE00B4L5Y983/acquisitions",
        &cookie,
        Some(json!({
            "symbol": "EXAMPLE.DE",
            "exchange": "XETR",
            "confirmed": true,
            "mode": "update"
        })),
    )
    .await;
    assert_eq!(acquisition.0, StatusCode::ACCEPTED);
    assert_eq!(acquisition.1["job"]["status"], "queued");
    let queued = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM job_queue WHERE kind = 'market_data_acquisition'",
    )
    .fetch_one(&pool)
    .await
    .expect("count queued acquisition");
    assert_eq!(queued, 1);
}

async fn get_json(app: &axum::Router, uri: &str, cookie: &str) -> Value {
    let (status, body) = request_json(app, "GET", uri, cookie, None).await;
    assert_eq!(status, StatusCode::OK, "unexpected response: {body}");
    body
}

async fn request_json(
    app: &axum::Router,
    method: &str,
    uri: &str,
    cookie: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(COOKIE, cookie);
    let request_body = if let Some(body) = body {
        builder = builder.header("content-type", "application/json");
        Body::from(body.to_string())
    } else {
        Body::empty()
    };
    let response = app
        .clone()
        .oneshot(builder.body(request_body).unwrap())
        .await
        .expect("HTTP response");
    let status = response.status();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("read response body")
        .to_bytes();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("JSON response")
    };
    (status, body)
}
