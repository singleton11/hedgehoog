use std::{env, time::Duration};

use chrono::{TimeZone, Utc};
use hedgehoog::{
    db,
    jobs::{
        enqueue,
        queue::{self, JobKind, NewJob, Queue},
        scheduler,
    },
    Config,
};
use serde_json::json;

#[tokio::test]
async fn queue_claims_retries_recovers_leases_and_deduplicates_schedules() {
    let Ok(database_url) = env::var("TEST_DATABASE_URL") else {
        eprintln!("TEST_DATABASE_URL is not set; skipping PostgreSQL queue integration test");
        return;
    };
    env::set_var("DATABASE_URL", &database_url);
    env::set_var(
        "JWT_SECRET",
        "postgres-queue-test-secret-with-at-least-32-characters",
    );
    let config = Config::from_env().expect("test configuration");
    let pool = db::connect(&config).await.expect("connect PostgreSQL");
    db::migrate(&pool).await.expect("run migrations");
    sqlx::query("TRUNCATE job_queue")
        .execute(&pool)
        .await
        .expect("clear queue");

    let deduped = NewJob {
        id: None,
        kind: JobKind::EtfImport,
        queue: Queue::Catalogue,
        payload: json!({}),
        max_attempts: 3,
        run_at: Utc::now(),
        dedupe_key: Some("integration:dedupe".into()),
    };
    let first = enqueue(&pool, deduped.clone())
        .await
        .expect("first enqueue");
    let second = enqueue(&pool, deduped).await.expect("deduplicated enqueue");
    assert_eq!(first, second);
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM job_queue WHERE dedupe_key = 'integration:dedupe'",
    )
    .fetch_one(&pool)
    .await
    .expect("count dedupe rows");
    assert_eq!(count, 1);

    let claimed = queue::claim(&pool, Queue::Catalogue, "worker-a", Duration::from_secs(60))
        .await
        .expect("claim job")
        .expect("queued job");
    assert_eq!(claimed.id, first);
    assert_eq!(claimed.attempts, 1);
    assert!(
        queue::claim(&pool, Queue::Catalogue, "worker-b", Duration::from_secs(60))
            .await
            .expect("second claim")
            .is_none()
    );
    assert!(
        !queue::heartbeat(&pool, &claimed.id, "wrong-worker", Duration::from_secs(60))
            .await
            .expect("mismatched heartbeat")
    );

    assert!(queue::fail(&pool, &claimed, "worker-a", "temporary", true)
        .await
        .expect("schedule retry"));
    let status =
        sqlx::query_as::<_, (String, i32)>("SELECT status, attempts FROM job_queue WHERE id = $1")
            .bind(&claimed.id)
            .fetch_one(&pool)
            .await
            .expect("retry status");
    assert_eq!(status, ("retrying".into(), 1));

    sqlx::query("UPDATE job_queue SET run_at = now() WHERE id = $1")
        .bind(&claimed.id)
        .execute(&pool)
        .await
        .expect("make retry due");
    let retried = queue::claim(&pool, Queue::Catalogue, "worker-b", Duration::from_secs(60))
        .await
        .expect("claim retry")
        .expect("retry job");
    assert_eq!(retried.attempts, 2);
    assert!(queue::complete(&pool, &retried.id, "worker-b")
        .await
        .expect("complete retry"));

    let lease_job_id = enqueue(
        &pool,
        NewJob::immediate(
            JobKind::MarketDataAcquisition,
            Queue::MarketData,
            json!({"job_id": "acquisition-1"}),
            2,
        ),
    )
    .await
    .expect("enqueue lease job");
    let lease_job = queue::claim(
        &pool,
        Queue::MarketData,
        "worker-a",
        Duration::from_secs(60),
    )
    .await
    .expect("claim lease job")
    .expect("lease job");
    assert_eq!(lease_job.id, lease_job_id);
    sqlx::query("UPDATE job_queue SET lease_until = now() - interval '1 second' WHERE id = $1")
        .bind(&lease_job.id)
        .execute(&pool)
        .await
        .expect("expire lease");
    let recovered = queue::claim(
        &pool,
        Queue::MarketData,
        "worker-b",
        Duration::from_secs(60),
    )
    .await
    .expect("recover lease")
    .expect("recovered job");
    assert_eq!(recovered.attempts, 2);
    sqlx::query("UPDATE job_queue SET lease_until = now() - interval '1 second' WHERE id = $1")
        .bind(&recovered.id)
        .execute(&pool)
        .await
        .expect("expire final lease");
    assert!(queue::claim(
        &pool,
        Queue::MarketData,
        "worker-c",
        Duration::from_secs(60)
    )
    .await
    .expect("exhausted claim")
    .is_none());
    let exhausted = sqlx::query_scalar::<_, String>("SELECT status FROM job_queue WHERE id = $1")
        .bind(&recovered.id)
        .fetch_one(&pool)
        .await
        .expect("exhausted status");
    assert_eq!(exhausted, "failed");

    sqlx::query("TRUNCATE job_queue")
        .execute(&pool)
        .await
        .expect("clear schedules");
    let after_all_schedules = Utc
        .with_ymd_and_hms(2026, 11, 15, 4, 0, 0)
        .single()
        .expect("test date");
    scheduler::enqueue_due(&pool, after_all_schedules)
        .await
        .expect("enqueue schedules");
    scheduler::enqueue_due(&pool, after_all_schedules)
        .await
        .expect("dedupe schedules");
    let scheduled = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM job_queue")
        .fetch_one(&pool)
        .await
        .expect("count schedules");
    assert_eq!(scheduled, 3);
}
