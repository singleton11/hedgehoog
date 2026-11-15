use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Transaction};

use super::{http::calculate_statistics, quote_fill_policy};

type StatisticsRow = (NaiveDate, Option<Decimal>, Option<Decimal>, Option<String>);

pub fn current(
    snapshot: Option<&Value>,
    generation: &str,
    override_currency: Option<&str>,
    policy: &Value,
) -> bool {
    snapshot.is_some_and(|snapshot| {
        snapshot.get("generation").and_then(Value::as_str) == Some(generation)
            && snapshot.get("override_currency") == Some(&json!(override_currency))
            && snapshot.get("quote_fill_policy") == Some(policy)
    })
}

pub async fn calculate(
    pool: &PgPool,
    instrument_id: &str,
    generation: &str,
    override_currency: Option<&str>,
) -> Result<Value, sqlx::Error> {
    let rows = sqlx::query_as::<_, StatisticsRow>(
        "SELECT quote_date, adj_close, close, price_currency
         FROM quotes WHERE job_id = $1 ORDER BY quote_date",
    )
    .bind(generation)
    .fetch_all(pool)
    .await?;
    let policy = quote_fill_policy(pool).await?;
    let mut snapshot = calculate_statistics(&rows, override_currency, instrument_id, &policy);
    snapshot["generation"] = json!(generation);
    snapshot["override_currency"] = json!(override_currency);
    Ok(snapshot)
}

pub async fn save(
    transaction: &mut Transaction<'_, Postgres>,
    instrument_id: &str,
    snapshot: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO instrument_statistics (instrument_id, snapshot)
         VALUES ($1, $2)
         ON CONFLICT (instrument_id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
    )
    .bind(instrument_id)
    .bind(snapshot)
    .execute(&mut **transaction)
    .await?;
    Ok(())
}

// Existing publications have no snapshot. Rebuild them off the request path,
// and recheck publication/settings after calculation to avoid saving stale work.
pub async fn reconcile(pool: &PgPool) -> Result<usize, sqlx::Error> {
    let rows = sqlx::query_as::<_, (String, String, Option<String>, Option<Value>)>(
        "WITH publications AS (
             SELECT DISTINCT ON (instrument_id) instrument_id, id
             FROM acquisition_jobs
             WHERE provider = 'yfinance' AND published = true
               AND instrument_id NOT LIKE 'idx-%'
             ORDER BY instrument_id, requested_at DESC, published_at DESC NULLS LAST, id DESC
         )
         SELECT p.instrument_id, p.id, state.price_currency_override, stats.snapshot
         FROM publications p
         LEFT JOIN instrument_state state ON state.instrument_id = p.instrument_id
         LEFT JOIN instrument_statistics stats ON stats.instrument_id = p.instrument_id",
    )
    .fetch_all(pool)
    .await?;
    let policy = quote_fill_policy(pool).await?;
    let mut refreshed = 0;
    for (instrument_id, generation, override_currency, saved) in rows {
        if refreshed >= 100 {
            break;
        }
        if current(
            saved.as_ref(),
            &generation,
            override_currency.as_deref(),
            &policy,
        ) {
            continue;
        }
        let snapshot = calculate(
            pool,
            &instrument_id,
            &generation,
            override_currency.as_deref(),
        )
        .await?;
        let mut transaction = pool.begin().await?;
        let still_current = sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS (
                 SELECT 1 FROM acquisition_jobs jobs
                 WHERE jobs.id = $1 AND jobs.published = true
                   AND jobs.id = (
                     SELECT id FROM acquisition_jobs
                     WHERE instrument_id = $2 AND provider = 'yfinance' AND published = true
                     ORDER BY requested_at DESC, published_at DESC NULLS LAST, id DESC LIMIT 1
                   )
                   AND (SELECT price_currency_override FROM instrument_state
                        WHERE instrument_id = $2) IS NOT DISTINCT FROM $3
             )",
        )
        .bind(&generation)
        .bind(&instrument_id)
        .bind(&override_currency)
        .fetch_one(&mut *transaction)
        .await?;
        if still_current
            && quote_fill_policy_in_transaction(&mut transaction).await?
                == snapshot["quote_fill_policy"]
        {
            save(&mut transaction, &instrument_id, &snapshot).await?;
            refreshed += 1;
        }
        transaction.commit().await?;
    }
    Ok(refreshed)
}

async fn quote_fill_policy_in_transaction(
    transaction: &mut Transaction<'_, Postgres>,
) -> Result<Value, sqlx::Error> {
    let value =
        sqlx::query_scalar::<_, Value>("SELECT value FROM app_settings WHERE key = 'market_data'")
            .fetch_optional(&mut **transaction)
            .await?
            .unwrap_or_else(|| json!({}));
    Ok(json!({
        "version": 1,
        "method": value.get("method").and_then(Value::as_str).unwrap_or("smooth"),
        "seed": value.get("seed").and_then(Value::as_u64).unwrap_or(0),
    }))
}
