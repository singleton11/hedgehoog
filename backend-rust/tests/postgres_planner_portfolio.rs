use std::env;

use axum::{
    body::Body,
    http::{header::COOKIE, Request, StatusCode},
};
use chrono::{Datelike, Duration, Months, Utc, Weekday};
use hedgehoog::{auth, db, http, AppState, Config};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;
use uuid::Uuid;

#[tokio::test]
async fn planner_portfolio_import_performance_and_recommendations_use_postgres() {
    let Ok(database_url) = env::var("TEST_DATABASE_URL") else {
        eprintln!("TEST_DATABASE_URL is not set; skipping planner/portfolio PostgreSQL test");
        return;
    };
    env::set_var("DATABASE_URL", &database_url);
    env::set_var(
        "JWT_SECRET",
        "postgres-planner-secret-with-at-least-32-characters",
    );
    env::set_var("ADMIN_LOGIN", "admin");
    env::set_var("ADMIN_PASSWORD", "admin");
    let config = Config::from_env().expect("test configuration");
    let pool = db::connect(&config).await.expect("connect PostgreSQL");
    db::migrate(&pool).await.expect("run migrations");
    sqlx::query(
        "TRUNCATE
             portfolio_recommendation_receipts, portfolio_transactions, portfolios,
             planner_simulations, planner_goals, planner_customers, calibration_runs,
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
    let state = AppState::new(pool.clone(), config.clone());
    auth::seed_admin(&state).await.expect("seed admin");
    seed_history(&pool).await;
    let app = http::router(state);
    let cookie = login(&app).await;
    let policy = request_json(
        &app,
        "PUT",
        "/settings/market-data",
        &cookie,
        Some(json!({"method": "normal", "seed": 11})),
    )
    .await;
    assert_eq!(policy.0, StatusCode::OK, "{}", policy.1);
    let statistics = request_json(
        &app,
        "GET",
        "/instruments/IE00B4L5Y983/statistics",
        &cookie,
        None,
    )
    .await;
    assert_eq!(statistics.0, StatusCode::OK, "{}", statistics.1);
    assert_eq!(
        statistics.1["quote_fill_policy"],
        json!({"version": 1, "method": "normal", "seed": 11})
    );
    assert!(statistics.1["annualized_mean_log_return"].is_number());
    assert_eq!(statistics.1["price_basis"], "adj_close");
    assert_eq!(statistics.1["skipped_gap_count"], 0);
    assert_eq!(statistics.1["weekend_quote_count"], 0);
    assert!(statistics.1["warnings"].as_array().is_some_and(|warnings| {
        warnings.iter().any(|warning| {
            warning
                .as_str()
                .is_some_and(|text| text.contains("Less than one trading year"))
        })
    }));
    let acquired = request_json(
        &app,
        "GET",
        "/instruments/acquired?limit=20&offset=0",
        &cookie,
        None,
    )
    .await;
    assert_eq!(acquired.0, StatusCode::OK, "{}", acquired.1);
    assert!(acquired.1["items"][0]["annualized_mean_log_return"].is_number());

    let customer = request_json(
        &app,
        "POST",
        "/customers",
        &cookie,
        Some(json!({"name": "Example customer"})),
    )
    .await;
    assert_eq!(customer.0, StatusCode::CREATED, "{}", customer.1);
    let customer_id = customer.1["id"].as_str().unwrap();
    let goal_date = Utc::now()
        .date_naive()
        .checked_add_months(Months::new(12))
        .unwrap();
    let goal = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/goals"),
        &cookie,
        Some(json!({
            "name": "Future reserve",
            "goal_eur": "5000.00",
            "goal_date": goal_date,
        })),
    )
    .await;
    assert_eq!(goal.0, StatusCode::CREATED, "{}", goal.1);
    let goal_id = goal.1["id"].as_str().unwrap();

    let simulated = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/goals/{goal_id}/simulate"),
        &cookie,
        Some(json!({
            "starting_capital_eur": "1000.00",
            "reliability": 0.8,
            "simulation_method": "standard",
            "strategy": {
                "id": "buy_and_hold_single_instrument",
                "parameters": {"instrument": "IE00B4L5Y983"}
            }
        })),
    )
    .await;
    assert_eq!(simulated.0, StatusCode::OK, "{}", simulated.1);
    assert_eq!(simulated.1["result"]["simulation_paths"], 10_000);
    assert_eq!(
        simulated.1["result"]["quote_fill_policy"],
        json!({"version": 1, "method": "normal", "seed": 11})
    );
    let calibration = request_json(
        &app,
        "POST",
        "/goals/calibrate",
        &cookie,
        Some(json!({
            "mode": "random",
            "strategy": {
                "id": "buy_and_hold_single_instrument",
                "parameters": {"instrument": "IE00B4L5Y983"}
            },
            "scenario_count": 10,
            "min_horizon_months": 1,
            "max_horizon_months": 2,
            "lookback_months": 3,
            "simulation_method": "standard",
            "block_size": null,
            "start_date": null,
            "end_date": null,
            "seed": 42
        })),
    )
    .await;
    assert_eq!(calibration.0, StatusCode::OK, "{}", calibration.1);
    assert!(calibration.1["sample_count"].as_u64().unwrap() > 0);
    assert!(calibration.1["brier_score"].is_number());
    assert_eq!(
        calibration.1["quote_fill_policy"],
        json!({"version": 1, "method": "normal", "seed": 11})
    );
    assert!(calibration.1["history_fingerprint"]
        .as_str()
        .is_some_and(|value| !value.is_empty()));
    assert_eq!(
        calibration.1["candidate_count"].as_u64().unwrap(),
        calibration.1["sample_count"].as_u64().unwrap()
            + calibration.1["skipped_counts"]
                .as_object()
                .unwrap()
                .values()
                .map(|value| value.as_u64().unwrap())
                .sum::<u64>()
    );
    let simulation_id = simulated.1["id"].as_str().unwrap();
    let saved = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/goals/{goal_id}/simulations/{simulation_id}/save"),
        &cookie,
        None,
    )
    .await;
    assert_eq!(saved.0, StatusCode::OK, "{}", saved.1);
    let plan = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/goals/{goal_id}/simulations/{simulation_id}/plan"),
        &cookie,
        None,
    )
    .await;
    assert_eq!(plan.0, StatusCode::OK, "{}", plan.1);
    assert_eq!(plan.1["is_plan"], true);

    let portfolio = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/portfolios"),
        &cookie,
        Some(json!({"name": "Goal portfolio", "plan_id": simulation_id})),
    )
    .await;
    assert_eq!(portfolio.0, StatusCode::CREATED, "{}", portfolio.1);
    let portfolio_id = portfolio.1["id"].as_str().unwrap();
    let deposit = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/portfolios/{portfolio_id}/transactions"),
        &cookie,
        Some(json!({
            "client_id": Uuid::new_v4(),
            "date": Utc::now().date_naive(),
            "type": "deposit",
            "amount_eur": "1000.00",
            "currency": "EUR"
        })),
    )
    .await;
    assert_eq!(deposit.0, StatusCode::CREATED, "{}", deposit.1);

    let performance = request_json(
        &app,
        "GET",
        &format!("/customers/{customer_id}/portfolios/{portfolio_id}/performance"),
        &cookie,
        None,
    )
    .await;
    assert_eq!(performance.0, StatusCode::OK, "{}", performance.1);
    assert_eq!(performance.1["method"], "daily_twr");
    assert_eq!(
        performance.1["quote_fill_policy"],
        json!({"version": 1, "method": "normal", "seed": 11})
    );

    let recommendation = request_json(
        &app,
        "GET",
        &format!("/customers/{customer_id}/portfolios/{portfolio_id}/recommendations"),
        &cookie,
        None,
    )
    .await;
    assert_eq!(recommendation.0, StatusCode::OK, "{}", recommendation.1);
    assert_eq!(recommendation.1["status"], "ready");
    let snapshot_token = recommendation.1["snapshot_token"].as_str().unwrap();
    let action_ids = recommendation.1["actions"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|action| action["eligible"] == true)
        .map(|action| action["id"].clone())
        .collect::<Vec<_>>();
    let preview = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/portfolios/{portfolio_id}/recommendations/preview"),
        &cookie,
        Some(json!({
            "snapshot_token": snapshot_token,
            "action_ids": action_ids,
            "date": Utc::now().date_naive(),
            "overrides": {}
        })),
    )
    .await;
    assert_eq!(preview.0, StatusCode::OK, "{}", preview.1);
    let fulfill = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/portfolios/{portfolio_id}/recommendations/fulfill"),
        &cookie,
        Some(json!({
            "snapshot_token": snapshot_token,
            "action_ids": action_ids,
            "date": Utc::now().date_naive(),
            "overrides": {},
            "operation_id": Uuid::new_v4(),
            "preview_token": preview.1["preview_token"]
        })),
    )
    .await;
    assert_eq!(fulfill.0, StatusCode::OK, "{}", fulfill.1);
    assert!(!fulfill.1["event_ids"].as_array().unwrap().is_empty());
    let tracking = request_json(
        &app,
        "GET",
        &format!("/customers/{customer_id}/portfolios/{portfolio_id}/plan-tracking"),
        &cookie,
        None,
    )
    .await;
    assert_eq!(tracking.0, StatusCode::OK, "{}", tracking.1);
    assert!(matches!(
        tracking.1["forecast_status"].as_str(),
        Some("reached" | "not_reached" | "unavailable")
    ));
    assert!(tracking.1["forecast_search_end_date"].is_string());
    assert!(tracking.1["warnings"]
        .as_array()
        .is_some_and(|warnings| warnings.iter().all(serde_json::Value::is_string)));

    let import_portfolio = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/portfolios"),
        &cookie,
        Some(json!({"name": "Imported"})),
    )
    .await;
    let import_portfolio_id = import_portfolio.1["id"].as_str().unwrap();
    let today = Utc::now().date_naive();
    let csv = format!(
        "Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id\n{},{},{},,,Deposit,,EUR,100,EUR,100,\n",
        today.format("%d-%m-%Y"),
        "12:00",
        today.format("%d-%m-%Y")
    );
    let import_preview = request_json(
        &app,
        "POST",
        &format!(
            "/customers/{customer_id}/portfolios/{import_portfolio_id}/imports/degiro/preview"
        ),
        &cookie,
        Some(json!({"csv": csv})),
    )
    .await;
    assert_eq!(import_preview.0, StatusCode::OK, "{}", import_preview.1);
    assert_eq!(import_preview.1["can_import"], true);
    let imported = request_json(
        &app,
        "POST",
        &format!("/customers/{customer_id}/portfolios/{import_portfolio_id}/imports/degiro"),
        &cookie,
        Some(json!({
            "csv": csv,
            "preview_token": import_preview.1["preview_token"]
        })),
    )
    .await;
    assert_eq!(imported.0, StatusCode::OK, "{}", imported.1);
    assert_eq!(imported.1["imported_count"], 1);
}

async fn seed_history(pool: &sqlx::PgPool) {
    let today = Utc::now().date_naive();
    let listing = json!({"symbol": "EXAMPLE.DE", "exchange": "XETR"});
    let job = Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO etfs
            (id, isin, import_date, name, ticker, asset_class, currency, inception_date, source)
         VALUES ($1, 'IE00B4L5Y983', $2, 'Example ETF', 'EXAMPLE', 'Equity', 'EUR',
                 '2010-01-01', $3)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(today)
    .bind(json!({"isin": "IE00B4L5Y983"}))
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO acquisition_jobs
            (id, instrument_id, status, active, phase, mode, provider, price_basis,
             instrument_kind, history_scope, requested_at, finished_at, listing,
             date_from, date_to, published, published_at)
         VALUES ($1, 'IE00B4L5Y983', 'succeeded', false, 'complete', 'refresh',
                 'yfinance', 'provider_split_adjusted', 'etf', 'since_inception',
                 now(), now(), $2, $3, $4, true, now())",
    )
    .bind(&job)
    .bind(&listing)
    .bind(today - Duration::days(180))
    .bind(today)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO instrument_state
            (instrument_id, listing, listing_job_id, last_complete_quote_date, price_currency_override)
         VALUES ('IE00B4L5Y983', $1, $2, $3, 'EUR')",
    )
    .bind(&listing)
    .bind(&job)
    .bind(today)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO instruments
            (id, instrument_id, symbol, exchange, provider, job_id, collected_at,
             collection_date, data, asset_class, published)
         VALUES ($1, 'IE00B4L5Y983', 'EXAMPLE.DE', 'XETR', 'yfinance', $2,
                 now(), $3, $4, 'ETF', true)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&job)
    .bind(today)
    .bind(json!({"longName": "Example ETF", "currency": "EUR"}))
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO market_data_publications
            (id, instrument_id, symbol, exchange, provider, price_basis, job_id,
             requested_at, published_at)
         VALUES ($1, 'IE00B4L5Y983', 'EXAMPLE.DE', 'XETR', 'yfinance',
                 'provider_split_adjusted', $2, now(), now())",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&job)
    .execute(pool)
    .await
    .unwrap();
    let mut day = today - Duration::days(180);
    let mut index = 0_i64;
    while day <= today {
        if !matches!(day.weekday(), Weekday::Sat | Weekday::Sun) {
            let price = 80.0 + index as f64 * 0.2;
            sqlx::query(
                "INSERT INTO quotes
                    (id, instrument_id, symbol, exchange, provider, job_id,
                     collected_at, collection_date, quote_date, data, close, adj_close,
                     price_currency)
                 VALUES ($1, 'IE00B4L5Y983', 'EXAMPLE.DE', 'XETR', 'yfinance', $2,
                         now(), $3, $4, '{}'::jsonb, $5, $5, 'EUR')",
            )
            .bind(Uuid::new_v4().to_string())
            .bind(&job)
            .bind(today)
            .bind(day)
            .bind(price)
            .execute(pool)
            .await
            .unwrap();
            index += 1;
        }
        day += Duration::days(1);
    }
}

async fn login(app: &axum::Router) -> String {
    let response = app
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
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    response
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned()
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
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, body)
}
