use axum::{
    extract::{Query, State},
    routing::get,
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;

use crate::{auth::CurrentUser, AppError, AppResult, AppState};

const VISIBLE_JOBS: &str = "
    SELECT id,
           'market_data_acquisition'::text AS job_type,
           status,
           instrument_id,
           requested_at,
           started_at,
           finished_at
    FROM acquisition_jobs
    WHERE status IN ('queued', 'running', 'succeeded', 'failed')
    UNION ALL
    SELECT id,
           kind AS job_type,
           status,
           NULL::text AS instrument_id,
           requested_at,
           started_at,
           finished_at
    FROM job_queue
    WHERE kind IN ('etf_import', 'market_data_refresh')
      AND status IN ('queued', 'running', 'retrying', 'succeeded', 'failed')
";

#[derive(Debug, Deserialize)]
struct JobsQuery {
    status: Option<String>,
    job_type: Option<String>,
    limit: Option<i64>,
    offset: Option<i64>,
}

#[derive(Debug, Serialize, FromRow)]
struct JobItem {
    id: String,
    job_type: String,
    status: String,
    instrument_id: Option<String>,
    requested_at: DateTime<Utc>,
    started_at: Option<DateTime<Utc>>,
    finished_at: Option<DateTime<Utc>>,
    #[sqlx(skip)]
    error: Option<String>,
}

#[derive(Debug, Default, Serialize)]
struct JobCounts {
    total: i64,
    queued: i64,
    running: i64,
    retrying: i64,
    succeeded: i64,
    failed: i64,
}

#[derive(Debug, Serialize)]
struct JobTypeOption {
    value: &'static str,
    label: &'static str,
}

#[derive(Debug, Serialize)]
struct JobsResponse {
    items: Vec<JobItem>,
    total: i64,
    counts: JobCounts,
    job_types: Vec<JobTypeOption>,
}

pub fn router() -> Router<AppState> {
    Router::new().route("/", get(list_jobs))
}

async fn list_jobs(
    State(state): State<AppState>,
    CurrentUser(_user): CurrentUser,
    Query(query): Query<JobsQuery>,
) -> AppResult<Json<JobsResponse>> {
    let status = normalized_filter(query.status);
    let job_type = normalized_filter(query.job_type);
    validate_filter(
        status.as_deref(),
        &["queued", "running", "retrying", "succeeded", "failed"],
        "status",
    )?;
    validate_filter(
        job_type.as_deref(),
        &[
            "market_data_acquisition",
            "etf_import",
            "market_data_refresh",
        ],
        "job_type",
    )?;
    let limit = query.limit.unwrap_or(50);
    let offset = query.offset.unwrap_or(0);
    if !(1..=200).contains(&limit) {
        return Err(AppError::bad_request("limit must be between 1 and 200"));
    }
    if offset < 0 {
        return Err(AppError::bad_request("offset must be non-negative"));
    }

    let count_sql = format!(
        "WITH visible AS ({VISIBLE_JOBS})
         SELECT status, COUNT(*)::bigint AS count
         FROM visible
         GROUP BY status"
    );
    let rows = sqlx::query_as::<_, (String, i64)>(&count_sql)
        .fetch_all(&state.pool)
        .await?;
    let mut counts = JobCounts::default();
    for (status, count) in rows {
        counts.total += count;
        match status.as_str() {
            "queued" => counts.queued = count,
            "running" => counts.running = count,
            "retrying" => counts.retrying = count,
            "succeeded" => counts.succeeded = count,
            "failed" => counts.failed = count,
            _ => {}
        }
    }

    let filtered_sql = format!(
        "WITH visible AS ({VISIBLE_JOBS})
         SELECT COUNT(*)::bigint
         FROM visible
         WHERE ($1::text IS NULL OR status = $1)
           AND ($2::text IS NULL OR job_type = $2)"
    );
    let total = sqlx::query_scalar::<_, i64>(&filtered_sql)
        .bind(status.as_deref())
        .bind(job_type.as_deref())
        .fetch_one(&state.pool)
        .await?;

    let page_sql = format!(
        "WITH visible AS ({VISIBLE_JOBS})
         SELECT id, job_type, status, instrument_id, requested_at, started_at, finished_at
         FROM visible
         WHERE ($1::text IS NULL OR status = $1)
           AND ($2::text IS NULL OR job_type = $2)
         ORDER BY requested_at DESC, id DESC, job_type DESC
         LIMIT $3 OFFSET $4"
    );
    let mut items = sqlx::query_as::<_, JobItem>(&page_sql)
        .bind(status.as_deref())
        .bind(job_type.as_deref())
        .bind(limit)
        .bind(offset)
        .fetch_all(&state.pool)
        .await?;
    for item in &mut items {
        item.error = match item.status.as_str() {
            "failed" => Some("Job failed. Check the worker or service configuration.".into()),
            "retrying" => Some("Job interrupted; an automatic retry is pending.".into()),
            _ => None,
        };
    }

    Ok(Json(JobsResponse {
        items,
        total,
        counts,
        job_types: vec![
            JobTypeOption {
                value: "market_data_acquisition",
                label: "Market data acquisition",
            },
            JobTypeOption {
                value: "etf_import",
                label: "ETF catalogue import",
            },
            JobTypeOption {
                value: "market_data_refresh",
                label: "Market data refresh",
            },
        ],
    }))
}

fn normalized_filter(value: Option<String>) -> Option<String> {
    value.filter(|value| !value.is_empty())
}

fn validate_filter(value: Option<&str>, allowed: &[&str], name: &str) -> AppResult<()> {
    if value.is_some_and(|value| !allowed.contains(&value)) {
        return Err(AppError::bad_request(format!("invalid {name}")));
    }
    Ok(())
}
