use std::env;

use chrono::{Duration, Utc};
use hedgehoog::{
    db,
    market_data::{
        acquisition, refresh::refresh_stale, request_automatic_acquisition, retention::cleanup,
    },
    Config,
};
use serde_json::json;
use uuid::Uuid;

#[tokio::test]
async fn acquisition_enqueue_refresh_and_retention_are_postgres_native() {
    let Ok(database_url) = env::var("TEST_DATABASE_URL") else {
        eprintln!("TEST_DATABASE_URL is not set; skipping PostgreSQL market-data test");
        return;
    };
    env::set_var("DATABASE_URL", &database_url);
    env::set_var(
        "JWT_SECRET",
        "postgres-market-data-secret-with-at-least-32-characters",
    );
    let config = Config::from_env().expect("test configuration");
    let pool = db::connect(&config).await.expect("connect PostgreSQL");
    db::migrate(&pool).await.expect("run migrations");
    sqlx::query(
        "TRUNCATE
             market_data_retention, market_data_reader_leases,
             raw_eod_pages, raw_split_pages, split_snapshots, quotes, instruments,
             market_data_publications, job_queue, acquisition_jobs,
             instrument_state, instrument_statistics, etfs, imports, app_settings
         CASCADE",
    )
    .execute(&pool)
    .await
    .expect("clear market data");

    let today = Utc::now().date_naive();
    sqlx::query(
        "INSERT INTO etfs
            (id, isin, import_date, name, ticker, asset_class, currency, inception_date, source)
         VALUES ($1, 'IE00B4L5Y983', $2, 'Example ETF', 'EXAMPLE', 'Equity', 'EUR',
                 '2010-01-01', $3)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(today)
    .bind(json!({"isin": "IE00B4L5Y983"}))
    .execute(&pool)
    .await
    .expect("insert catalogue ETF");

    let first = request_automatic_acquisition(&pool, "IE00B4L5Y983", "test:refresh", true)
        .await
        .expect("request acquisition");
    assert!(first.created);
    let duplicate = request_automatic_acquisition(&pool, "IE00B4L5Y983", "test:refresh", true)
        .await
        .expect("deduplicate acquisition");
    assert!(!duplicate.created);
    assert_eq!(first.id, duplicate.id);
    let queued = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM job_queue
         WHERE id = $1 AND kind = 'market_data_acquisition' AND queue = 'marketdata'",
    )
    .bind(&first.id)
    .fetch_one(&pool)
    .await
    .expect("count queued acquisition");
    assert_eq!(queued, 1);

    let listing = json!({"symbol": "EXAMPLE.DE", "exchange": "XETR"});
    sqlx::query("UPDATE acquisition_jobs SET listing = $2 WHERE id = $1")
        .bind(&first.id)
        .bind(&listing)
        .execute(&pool)
        .await
        .expect("set resolved listing");
    let profile = json!({
        "symbol": "EXAMPLE.DE",
        "instrumentType": "ETF",
        "exchangeName": "GER",
        "exchangeTimezoneName": "Europe/Berlin",
        "currency": "EUR",
        "firstTradeDate": today.and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp(),
    });
    sqlx::query(
        "INSERT INTO instruments
            (id, instrument_id, symbol, exchange, provider, job_id, collected_at,
             collection_date, data, published)
         VALUES ($1, 'IE00B4L5Y983', 'EXAMPLE.DE', 'XETR', 'yfinance', $2,
                 now(), $3, $4, false)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&first.id)
    .bind(today)
    .bind(&profile)
    .execute(&pool)
    .await
    .expect("insert resumable profile");
    let quote_timestamp = today.and_hms_opt(12, 0, 0).unwrap().and_utc().timestamp();
    let chart = json!({
        "chart": {
            "result": [{
                "meta": profile,
                "timestamp": [quote_timestamp],
                "indicators": {
                    "quote": [{
                        "open": [99.0], "high": [101.0], "low": [98.0],
                        "close": [100.0], "volume": [1000]
                    }],
                    "adjclose": [{"adjclose": [100.0]}]
                },
                "events": {"splits": {}}
            }],
            "error": null
        }
    });
    sqlx::query(
        "INSERT INTO raw_eod_pages
            (id, instrument_id, symbol, exchange, provider, job_id, collected_at,
             collection_date, page_offset, data)
         VALUES ($1, 'IE00B4L5Y983', 'EXAMPLE.DE', 'XETR', 'yfinance', $2,
                 now(), $3, 0, $4)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&first.id)
    .bind(today)
    .bind(json!({"raw": chart}))
    .execute(&pool)
    .await
    .expect("insert resumable raw page");

    acquisition::run(&pool, &config, &first.id)
        .await
        .expect("complete acquisition from durable checkpoints");
    let acquisition_status = sqlx::query_as::<_, (String, bool, bool)>(
        "SELECT status, active, published FROM acquisition_jobs WHERE id = $1",
    )
    .bind(&first.id)
    .fetch_one(&pool)
    .await
    .expect("read completed acquisition");
    assert_eq!(acquisition_status, ("succeeded".into(), false, true));
    let snapshot = sqlx::query_scalar::<_, serde_json::Value>(
        "SELECT snapshot FROM instrument_statistics WHERE instrument_id = 'IE00B4L5Y983'",
    )
    .fetch_one(&pool)
    .await
    .expect("published statistics snapshot");
    assert_eq!(snapshot["generation"], first.id);
    assert_eq!(snapshot["quote_count"], 1);
    assert_eq!(snapshot["quote_fill_policy"]["method"], "smooth");
    hedgehoog::jobs::scheduler::enqueue_due(&pool, Utc::now())
        .await
        .expect("check current statistics without a currency override");
    let statistics_jobs = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM job_queue WHERE kind = 'market_data_statistics'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(statistics_jobs, 0);
    acquisition::run(&pool, &config, &first.id)
        .await
        .expect("completed acquisition is idempotent");
    let unchanged = sqlx::query_scalar::<_, serde_json::Value>(
        "SELECT snapshot FROM instrument_statistics WHERE instrument_id = 'IE00B4L5Y983'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(unchanged, snapshot);
    assert_eq!(
        hedgehoog::market_data::statistics::reconcile(&pool)
            .await
            .unwrap(),
        0
    );
    sqlx::query("UPDATE instrument_statistics SET snapshot = jsonb_set(snapshot, '{generation}', '\"outdated\"') WHERE instrument_id = 'IE00B4L5Y983'")
        .execute(&pool).await.unwrap();
    assert_eq!(
        hedgehoog::market_data::statistics::reconcile(&pool)
            .await
            .unwrap(),
        1
    );
    let repaired = sqlx::query_scalar::<_, serde_json::Value>(
        "SELECT snapshot FROM instrument_statistics WHERE instrument_id = 'IE00B4L5Y983'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(repaired["generation"], first.id);

    let counts = refresh_stale(&pool, true, Utc::now())
        .await
        .expect("refresh inventory");
    assert_eq!(counts.fresh, 1);
    assert_eq!(counts.requested, 4); // Curated indices have no publication yet.

    let old_id = Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO acquisition_jobs
            (id, instrument_id, status, active, phase, mode, provider, price_basis,
             instrument_kind, history_scope, requested_at, started_at, finished_at,
             listing, date_from, date_to, published, published_at)
         VALUES ($1, 'IE00B4L5Y983', 'succeeded', false, 'complete', 'refresh',
                 'yfinance', 'provider_split_adjusted', 'etf', 'since_inception',
                 $2, $2, $2, $3, '2010-01-01', $4, true, $2)",
    )
    .bind(&old_id)
    .bind(Utc::now() - Duration::days(2))
    .bind(&listing)
    .bind(today - Duration::days(2))
    .execute(&pool)
    .await
    .expect("insert superseded acquisition");
    sqlx::query(
        "INSERT INTO raw_eod_pages
            (id, instrument_id, symbol, exchange, provider, job_id, collected_at,
             collection_date, page_offset, data)
         VALUES ($1, 'IE00B4L5Y983', 'EXAMPLE.DE', 'XETR', 'yfinance', $2,
                 now(), $3, 0, '{}'::jsonb)",
    )
    .bind(Uuid::new_v4().to_string())
    .bind(&old_id)
    .bind(today - Duration::days(2))
    .execute(&pool)
    .await
    .expect("insert superseded payload");

    let result = cleanup(&pool, &config, Some("IE00B4L5Y983"))
        .await
        .expect("prune superseded generation");
    assert_eq!(result.pruned, 1);
    let old_payloads =
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM raw_eod_pages WHERE job_id = $1")
            .bind(&old_id)
            .fetch_one(&pool)
            .await
            .expect("count old payloads");
    assert_eq!(old_payloads, 0);
    let latest_quotes =
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM quotes WHERE job_id = $1")
            .bind(&first.id)
            .fetch_one(&pool)
            .await
            .expect("count latest quotes");
    assert_eq!(latest_quotes, 1);
}
