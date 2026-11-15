use std::{collections::HashSet, sync::LazyLock, time::Duration};

use chrono::{NaiveDate, Utc};
use regex::Regex;
use reqwest::redirect::Policy;
use serde_json::Value;
use sqlx::PgPool;
use thiserror::Error;
use uuid::Uuid;

use crate::{
    jobs::{enqueue, JobKind, NewJob, Queue},
    AppError,
};

const ETF_SOURCE_URL: &str =
    "https://raw.githubusercontent.com/albertored/etfdb/refs/heads/main/json/etfs.json";
static ISIN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$").expect("valid ISIN regex"));

#[derive(Debug, Error)]
pub enum EtfImportError {
    #[error("could not download ETF catalogue")]
    Download(#[source] reqwest::Error),
    #[error("{0}")]
    Invalid(String),
    #[error("could not persist ETF catalogue")]
    Database(#[source] sqlx::Error),
    #[error("could not queue catalogue market-data refresh")]
    Queue(#[source] AppError),
}

pub async fn import_daily(pool: &PgPool) -> Result<usize, EtfImportError> {
    let import_date = Utc::now().date_naive();
    let import_id = format!("etfs:{import_date}");
    sqlx::query(
        "INSERT INTO imports (id, import_date, etfs)
         VALUES ($1, $2, false)
         ON CONFLICT (import_date) DO NOTHING",
    )
    .bind(&import_id)
    .bind(import_date)
    .execute(pool)
    .await
    .map_err(EtfImportError::Database)?;
    let complete = sqlx::query_scalar::<_, bool>("SELECT etfs FROM imports WHERE import_date = $1")
        .bind(import_date)
        .fetch_one(pool)
        .await
        .map_err(EtfImportError::Database)?;

    let count = if complete {
        tracing::info!(%import_date, "ETF import is already complete");
        0
    } else {
        let records = download().await?;
        persist(pool, import_date, &records).await?;
        records.len()
    };

    enqueue(
        pool,
        NewJob {
            id: None,
            kind: JobKind::MarketDataRefresh,
            queue: Queue::Catalogue,
            payload: serde_json::json!({ "include_catalogue": true }),
            max_attempts: 6,
            run_at: Utc::now(),
            dedupe_key: Some(format!("catalogue-market-data-refresh:{import_date}")),
        },
    )
    .await
    .map_err(EtfImportError::Queue)?;
    Ok(count)
}

async fn download() -> Result<Vec<Value>, EtfImportError> {
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(60))
        .redirect(Policy::limited(5))
        .build()
        .map_err(EtfImportError::Download)?;
    let response = client
        .get(ETF_SOURCE_URL)
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .map_err(EtfImportError::Download)?;
    let records = response
        .json::<Vec<Value>>()
        .await
        .map_err(EtfImportError::Download)?;
    validate(&records)?;
    Ok(records)
}

fn validate(records: &[Value]) -> Result<(), EtfImportError> {
    if records.is_empty() {
        return Err(EtfImportError::Invalid(
            "ETF catalogue must be a non-empty JSON array".into(),
        ));
    }
    let mut seen = HashSet::with_capacity(records.len());
    for (index, record) in records.iter().enumerate() {
        let object = record.as_object().ok_or_else(|| {
            EtfImportError::Invalid(format!("ETF catalogue record {index} must be an object"))
        })?;
        let isin = object
            .get("isin")
            .and_then(Value::as_str)
            .filter(|isin| ISIN.is_match(isin))
            .ok_or_else(|| {
                EtfImportError::Invalid(format!("ETF catalogue record {index} has an invalid ISIN"))
            })?;
        if !seen.insert(isin) {
            return Err(EtfImportError::Invalid(format!(
                "ETF catalogue contains duplicate ISIN {isin}"
            )));
        }
        for reserved in ["_id", "import_date"] {
            if object.contains_key(reserved) {
                return Err(EtfImportError::Invalid(format!(
                    "ETF catalogue record {index} contains reserved field {reserved}"
                )));
            }
        }
    }
    Ok(())
}

async fn persist(
    pool: &PgPool,
    import_date: NaiveDate,
    records: &[Value],
) -> Result<(), EtfImportError> {
    let mut transaction = pool.begin().await.map_err(EtfImportError::Database)?;
    for record in records {
        let object = record.as_object().expect("validated ETF object");
        let isin = object
            .get("isin")
            .and_then(Value::as_str)
            .expect("validated ETF ISIN");
        let inception_date = text(object.get("inception_date"))
            .and_then(|value| NaiveDate::parse_from_str(&value, "%Y-%m-%d").ok());
        sqlx::query(
            "INSERT INTO etfs
                (id, isin, import_date, name, ticker, asset_class, currency, inception_date, source)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
             ON CONFLICT (isin, import_date) DO UPDATE
             SET name = EXCLUDED.name,
                 ticker = EXCLUDED.ticker,
                 asset_class = EXCLUDED.asset_class,
                 currency = EXCLUDED.currency,
                 inception_date = EXCLUDED.inception_date,
                 source = EXCLUDED.source",
        )
        .bind(Uuid::new_v4().to_string())
        .bind(isin)
        .bind(import_date)
        .bind(text(object.get("name")))
        .bind(text(object.get("ticker")))
        .bind(text(object.get("asset_class")))
        .bind(text(object.get("currency")))
        .bind(inception_date)
        .bind(record)
        .execute(&mut *transaction)
        .await
        .map_err(EtfImportError::Database)?;
    }
    sqlx::query("UPDATE imports SET etfs = true WHERE import_date = $1")
        .bind(import_date)
        .execute(&mut *transaction)
        .await
        .map_err(EtfImportError::Database)?;
    transaction
        .commit()
        .await
        .map_err(EtfImportError::Database)?;
    tracing::info!(count = records.len(), %import_date, "ETF catalogue imported");
    Ok(())
}

fn text(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).map(ToOwned::to_owned)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::validate;

    #[test]
    fn rejects_duplicate_isins_before_writing() {
        let records = vec![
            json!({"isin": "IE00B4L5Y983"}),
            json!({"isin": "IE00B4L5Y983"}),
        ];
        assert!(validate(&records).is_err());
    }

    #[test]
    fn accepts_valid_catalogue_records() {
        let records = vec![json!({"isin": "IE00B4L5Y983", "name": "Example"})];
        validate(&records).expect("valid records");
    }
}
