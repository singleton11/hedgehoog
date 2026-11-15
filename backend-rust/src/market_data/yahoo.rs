use std::{str::FromStr, time::Duration};

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use chrono_tz::Tz;
use reqwest::StatusCode;
use rust_decimal::Decimal;
use serde_json::{json, Map, Value};
use sqlx::PgPool;
use thiserror::Error;
use tokio::time::sleep;

use crate::Config;

const SEARCH_URL: &str = "https://query2.finance.yahoo.com/v1/finance/search";
const CHART_URL: &str = "https://query2.finance.yahoo.com/v8/finance/chart";
const HISTORY_CHUNK_DAYS: i64 = 99 * 365;

#[derive(Debug, Error)]
#[error("{message}")]
pub struct YahooError {
    pub code: &'static str,
    pub message: &'static str,
}

impl YahooError {
    fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    fn invalid_request() -> Self {
        Self::new(
            "invalid_request",
            "The Yahoo Finance request parameters are invalid.",
        )
    }

    fn malformed() -> Self {
        Self::new(
            "malformed_response",
            "Yahoo Finance returned an unreadable or unsupported response.",
        )
    }

    pub fn retryable(&self) -> bool {
        self.code == "database_error"
    }
}

pub struct YahooClient<'a> {
    client: reqwest::Client,
    pool: &'a PgPool,
    config: &'a Config,
    job_id: &'a str,
    owner: &'a str,
}

#[derive(Debug, Clone)]
pub struct Listing {
    pub symbol: String,
    pub exchange: String,
    pub resolution: Value,
}

#[derive(Debug, Clone)]
pub struct ChartPage {
    pub raw: Value,
    pub rows: Vec<QuoteRow>,
    pub splits: Vec<Value>,
}

#[derive(Debug, Clone)]
pub struct QuoteRow {
    pub date: NaiveDate,
    pub data: Value,
    pub open: Option<Decimal>,
    pub high: Option<Decimal>,
    pub low: Option<Decimal>,
    pub close: Option<Decimal>,
    pub volume: Option<Decimal>,
    pub adj_close: Option<Decimal>,
    pub price_currency: Option<String>,
}

impl<'a> YahooClient<'a> {
    pub fn new(
        pool: &'a PgPool,
        config: &'a Config,
        job_id: &'a str,
        owner: &'a str,
    ) -> Result<Self, YahooError> {
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(config.market_data_timeout)
            .redirect(reqwest::redirect::Policy::limited(5))
            .user_agent("Mozilla/5.0 Hedgehoog/0.1")
            .build()
            .map_err(|_| {
                YahooError::new(
                    "transport_error",
                    "Yahoo Finance could not be reached. No automatic retry was made.",
                )
            })?;
        Ok(Self {
            client,
            pool,
            config,
            job_id,
            owner,
        })
    }

    pub async fn resolve_listing(&self, ticker: &str, isin: &str) -> Result<Listing, YahooError> {
        let ticker = valid_symbol(ticker)?;
        let mut queries = vec![ticker.clone()];
        if valid_isin(isin) {
            queries.push(isin.to_ascii_uppercase());
        }
        for query in queries {
            let payload = self.search(&query).await?;
            let quotes = payload
                .get("quotes")
                .and_then(Value::as_array)
                .ok_or_else(YahooError::malformed)?;
            let mut accepted = Vec::new();
            for quote in quotes {
                let Some(object) = quote.as_object() else {
                    continue;
                };
                let Some(symbol) = object.get("symbol").and_then(Value::as_str) else {
                    continue;
                };
                if !matches!(
                    object.get("quoteType").and_then(Value::as_str),
                    Some("ETF" | "EQUITY" | "INDEX" | "MUTUALFUND")
                ) {
                    continue;
                }
                if object
                    .get("isin")
                    .and_then(Value::as_str)
                    .is_some_and(|value| !isin.is_empty() && !value.eq_ignore_ascii_case(isin))
                {
                    continue;
                }
                let upper = symbol.to_ascii_uppercase();
                let exact = upper == ticker;
                let root = ticker_root(&upper) == ticker_root(&ticker);
                let isin_match = query != ticker
                    && object
                        .get("isin")
                        .and_then(Value::as_str)
                        .is_none_or(|value| value.eq_ignore_ascii_case(&query));
                if exact || root || isin_match {
                    accepted.push((upper, object));
                }
            }
            accepted.sort_by(|left, right| left.0.cmp(&right.0));
            accepted.dedup_by(|left, right| {
                left.0 == right.0
                    && left.1.get("exchange").and_then(Value::as_str)
                        == right.1.get("exchange").and_then(Value::as_str)
            });
            if accepted.len() > 1 {
                return Err(YahooError::new(
                    "listing_not_found",
                    "No unambiguous Yahoo listing was found. Set the symbol and MIC manually.",
                ));
            }
            if let Some((symbol, selected)) = accepted.into_iter().next() {
                let exchange_name = selected
                    .get("exchange")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        YahooError::new(
                            "unknown_exchange",
                            "The Yahoo exchange is not mapped. Set the symbol and MIC manually.",
                        )
                    })?;
                let exchange = exchange_mic(exchange_name).ok_or_else(|| {
                    YahooError::new(
                        "unknown_exchange",
                        "The Yahoo exchange is not mapped. Set the symbol and MIC manually.",
                    )
                })?;
                return Ok(Listing {
                    symbol,
                    exchange: exchange.into(),
                    resolution: json!({
                        "source": "yahoo_search",
                        "query": query,
                        "name": selected.get("longname").or_else(|| selected.get("shortname")),
                        "data": Value::Object(selected.clone()),
                        "raw": payload,
                        "collected_at": Utc::now(),
                    }),
                });
            }
        }
        Err(YahooError::new(
            "listing_not_found",
            "No unambiguous Yahoo listing was found. Set the symbol and MIC manually.",
        ))
    }

    pub async fn resolve_symbol(&self, symbol: &str) -> Result<Listing, YahooError> {
        let symbol = valid_symbol(symbol)?;
        let meta = self.chart_meta(&symbol).await?;
        let exchange_name = meta
            .get("exchangeName")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                YahooError::new(
                    "unknown_exchange",
                    "The Yahoo exchange is not mapped. Set the symbol and MIC manually.",
                )
            })?;
        let exchange = exchange_mic(exchange_name).ok_or_else(|| {
            YahooError::new(
                "unknown_exchange",
                "The Yahoo exchange is not mapped. Set the symbol and MIC manually.",
            )
        })?;
        Ok(Listing {
            symbol: symbol.clone(),
            exchange: exchange.into(),
            resolution: json!({
                "source": "yahoo_chart_metadata",
                "query": symbol,
                "name": meta.get("longName").or_else(|| meta.get("shortName")),
                "exchangeName": exchange_name,
                "data": meta,
                "collected_at": Utc::now(),
            }),
        })
    }

    pub async fn chart_meta(&self, symbol: &str) -> Result<Value, YahooError> {
        let symbol = valid_symbol(symbol)?;
        let url = format!("{CHART_URL}/{symbol}");
        let (status, payload) = self
            .request_value(&url, &[("range", "1d".into()), ("interval", "1d".into())])
            .await?;
        if !status.is_success() {
            return Err(status_error(status));
        }
        let result = chart_result(&payload, &symbol)?;
        let meta = result
            .get("meta")
            .and_then(Value::as_object)
            .ok_or_else(YahooError::malformed)?;
        timezone(meta)?;
        let mut profile = meta.clone();
        profile.insert("symbol".into(), Value::String(symbol));
        profile.insert(
            "name".into(),
            meta.get("longName")
                .or_else(|| meta.get("shortName"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        profile.insert(
            "profile_source".into(),
            Value::String("chart_metadata".into()),
        );
        profile.insert(
            "price_basis".into(),
            Value::String("provider_split_adjusted".into()),
        );
        Ok(Value::Object(profile))
    }

    pub async fn history(
        &self,
        symbol: &str,
        exchange: &str,
        start: NaiveDate,
        end: NaiveDate,
        meta: &Value,
    ) -> Result<Vec<ChartPage>, YahooError> {
        if start > end {
            return Err(YahooError::invalid_request());
        }
        let symbol = valid_symbol(symbol)?;
        let meta_object = meta.as_object().ok_or_else(YahooError::malformed)?;
        let tz = timezone(meta_object)?;
        let first_trade = meta_object.get("firstTradeDate").and_then(Value::as_i64);
        let mut pages = Vec::new();
        for (lower, upper) in history_ranges(start, end) {
            let period1 = local_midnight(lower, tz)?;
            let period2 = local_midnight(
                upper.succ_opt().ok_or_else(YahooError::invalid_request)?,
                tz,
            )?;
            let url = format!("{CHART_URL}/{symbol}");
            let params = [
                ("period1", period1.to_string()),
                ("period2", period2.to_string()),
                ("interval", "1d".into()),
                ("events", "div,splits".into()),
                ("includeAdjustedClose", "true".into()),
            ];
            let (status, payload) = self.request_value(&url, &params).await?;
            if status == StatusCode::BAD_REQUEST
                && first_trade.is_some_and(|first| period2 <= first)
                && no_history_response(&payload, period1, period2)
            {
                pages.push(ChartPage {
                    raw: json!({
                        "empty_history": {
                            "metadata": meta,
                            "request": {"period1": period1, "period2": period2},
                            "response": payload,
                        }
                    }),
                    rows: Vec::new(),
                    splits: Vec::new(),
                });
                continue;
            }
            if !status.is_success() {
                return Err(status_error(status));
            }
            pages.push(parse_chart_page(payload, &symbol, exchange, lower, upper)?);
        }
        Ok(pages)
    }

    async fn search(&self, query: &str) -> Result<Value, YahooError> {
        let (status, payload) = self
            .request_value(
                SEARCH_URL,
                &[
                    ("q", query.to_owned()),
                    ("quotesCount", "50".into()),
                    ("newsCount", "0".into()),
                    ("listsCount", "0".into()),
                    ("enableFuzzyQuery", "false".into()),
                ],
            )
            .await?;
        if !status.is_success() {
            return Err(status_error(status));
        }
        if !payload.get("quotes").is_some_and(Value::is_array) {
            return Err(YahooError::malformed());
        }
        Ok(payload)
    }

    async fn request_value(
        &self,
        url: &str,
        params: &[(&str, String)],
    ) -> Result<(StatusCode, Value), YahooError> {
        self.reserve_request().await?;
        let response = self
            .client
            .get(url)
            .query(
                &params
                    .iter()
                    .map(|(key, value)| (*key, value.as_str()))
                    .collect::<Vec<_>>(),
            )
            .send()
            .await
            .map_err(|_| {
                YahooError::new(
                    "transport_error",
                    "Yahoo Finance could not be reached. No automatic retry was made.",
                )
            })?;
        let status = response.status();
        if matches!(
            status,
            StatusCode::UNAUTHORIZED
                | StatusCode::FORBIDDEN
                | StatusCode::TOO_MANY_REQUESTS
                | StatusCode::UNAVAILABLE_FOR_LEGAL_REASONS
        ) {
            return Err(status_error(status));
        }
        let payload = response
            .json::<Value>()
            .await
            .map_err(|_| YahooError::malformed())?;
        Ok((status, payload))
    }

    async fn reserve_request(&self) -> Result<(), YahooError> {
        let lease_seconds = self.config.job_lease.as_secs_f64();
        let calls = sqlx::query_scalar::<_, i32>(
            "UPDATE acquisition_jobs
             SET calls = calls + 1,
                 lease_until = now() + make_interval(secs => $4::double precision)
             WHERE id = $1 AND owner = $2 AND active = true AND status = 'running'
               AND calls < $3
             RETURNING calls",
        )
        .bind(self.job_id)
        .bind(self.owner)
        .bind(self.config.market_data_max_calls_per_job)
        .bind(lease_seconds)
        .fetch_optional(self.pool)
        .await
        .map_err(|_| {
            YahooError::new(
                "database_error",
                "Database unavailable while reserving a Yahoo request.",
            )
        })?;
        if calls.is_none() {
            return Err(YahooError::new(
                "job_request_limit",
                "This job reached its request safety limit. Its unfinished range was not marked complete.",
            ));
        }

        let mut transaction = self.pool.begin().await.map_err(|_| {
            YahooError::new(
                "database_error",
                "Database unavailable while pacing a Yahoo request.",
            )
        })?;
        sqlx::query(
            "INSERT INTO provider_request_pacing (provider, next_request_at)
             VALUES ('yfinance', now())
             ON CONFLICT (provider) DO NOTHING",
        )
        .execute(&mut *transaction)
        .await
        .map_err(|_| {
            YahooError::new(
                "database_error",
                "Database unavailable while pacing a Yahoo request.",
            )
        })?;
        let next = sqlx::query_scalar::<_, DateTime<Utc>>(
            "SELECT next_request_at
             FROM provider_request_pacing
             WHERE provider = 'yfinance'
             FOR UPDATE",
        )
        .fetch_one(&mut *transaction)
        .await
        .map_err(|_| {
            YahooError::new(
                "database_error",
                "Database unavailable while pacing a Yahoo request.",
            )
        })?;
        let now = Utc::now();
        let slot = next.max(now);
        sqlx::query(
            "UPDATE provider_request_pacing
             SET next_request_at = $1 + make_interval(secs => $2::double precision)
             WHERE provider = 'yfinance'",
        )
        .bind(slot)
        .bind(self.config.yahoo_request_interval.as_secs_f64())
        .execute(&mut *transaction)
        .await
        .map_err(|_| {
            YahooError::new(
                "database_error",
                "Database unavailable while pacing a Yahoo request.",
            )
        })?;
        transaction.commit().await.map_err(|_| {
            YahooError::new(
                "database_error",
                "Database unavailable while pacing a Yahoo request.",
            )
        })?;
        if slot > now {
            sleep(
                (slot - now)
                    .to_std()
                    .unwrap_or_else(|_| Duration::from_secs(0)),
            )
            .await;
        }
        Ok(())
    }
}

pub(crate) fn parse_chart_page(
    payload: Value,
    symbol: &str,
    exchange: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<ChartPage, YahooError> {
    let result = chart_result(&payload, symbol)?;
    let meta = result
        .get("meta")
        .and_then(Value::as_object)
        .ok_or_else(YahooError::malformed)?;
    let zone = timezone(meta)?;
    let currency = meta
        .get("currency")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let timestamps = result
        .get("timestamp")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let indicators = result.get("indicators").and_then(Value::as_object);
    let quotes = indicators
        .and_then(|value| value.get("quote"))
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .and_then(Value::as_object);
    let adjusted = indicators
        .and_then(|value| value.get("adjclose"))
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .and_then(Value::as_object);

    let mut rows = Vec::new();
    for (index, timestamp) in timestamps.iter().enumerate() {
        let Some(timestamp) = timestamp.as_i64() else {
            continue;
        };
        let Some(instant) = DateTime::from_timestamp(timestamp, 0) else {
            continue;
        };
        let date = instant.with_timezone(&zone).date_naive();
        if date < start || date > end {
            continue;
        }
        let open = array_value(quotes, "open", index);
        let high = array_value(quotes, "high", index);
        let low = array_value(quotes, "low", index);
        let close = array_value(quotes, "close", index);
        let volume = array_value(quotes, "volume", index);
        let adj_close = array_value(adjusted, "adjclose", index);
        rows.push(QuoteRow {
            date,
            data: json!({
                "open": open,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "adj_close": adj_close,
                "symbol": symbol,
                "exchange": exchange,
                "date": date,
                "price_currency": currency,
                "price_basis": "provider_split_adjusted",
                "yahoo_timestamp": timestamp,
            }),
            open: decimal(&open),
            high: decimal(&high),
            low: decimal(&low),
            close: decimal(&close),
            volume: decimal(&volume),
            adj_close: decimal(&adj_close),
            price_currency: currency.clone(),
        });
    }

    let mut splits = Vec::new();
    if let Some(events) = result
        .get("events")
        .and_then(Value::as_object)
        .and_then(|events| events.get("splits"))
        .and_then(Value::as_object)
    {
        for event in events.values() {
            let Some(object) = event.as_object() else {
                continue;
            };
            let Some(timestamp) = object.get("date").and_then(Value::as_i64) else {
                continue;
            };
            let Some(instant) = DateTime::from_timestamp(timestamp, 0) else {
                continue;
            };
            let date = instant.with_timezone(&zone).date_naive();
            if date < start || date > end {
                continue;
            }
            let factor = split_factor(object);
            splits.push(json!({
                "date": date,
                "symbol": symbol,
                "split_factor": factor.map(|value| value.to_string()),
                "yahoo": event,
            }));
        }
    }

    Ok(ChartPage {
        raw: payload,
        rows,
        splits,
    })
}

fn chart_result<'a>(
    payload: &'a Value,
    symbol: &str,
) -> Result<&'a Map<String, Value>, YahooError> {
    let chart = payload
        .get("chart")
        .and_then(Value::as_object)
        .ok_or_else(YahooError::malformed)?;
    if let Some(error) = chart.get("error").filter(|value| !value.is_null()) {
        if error.get("code").and_then(Value::as_str) == Some("Not Found") {
            return Err(YahooError::new(
                "invalid_symbol",
                "Yahoo Finance did not recognize the requested symbol.",
            ));
        }
        return Err(YahooError::malformed());
    }
    let result = chart
        .get("result")
        .and_then(Value::as_array)
        .filter(|values| values.len() == 1)
        .and_then(|values| values.first())
        .and_then(Value::as_object)
        .ok_or_else(YahooError::malformed)?;
    let returned = result
        .get("meta")
        .and_then(Value::as_object)
        .and_then(|meta| meta.get("symbol"))
        .and_then(Value::as_str)
        .ok_or_else(YahooError::malformed)?;
    if !returned.eq_ignore_ascii_case(symbol) {
        return Err(YahooError::new(
            "identity_mismatch",
            "Yahoo Finance returned a different instrument.",
        ));
    }
    Ok(result)
}

fn no_history_response(payload: &Value, period1: i64, period2: i64) -> bool {
    let expected = format!("Data doesn't exist for startDate = {period1}, endDate = {period2}");
    payload.get("chart").is_some_and(|chart| {
        chart.get("result").is_some_and(Value::is_null)
            && chart
                .get("error")
                .and_then(|error| error.get("code"))
                .and_then(Value::as_str)
                == Some("Bad Request")
            && chart
                .get("error")
                .and_then(|error| error.get("description"))
                .and_then(Value::as_str)
                == Some(expected.as_str())
    })
}

fn status_error(status: StatusCode) -> YahooError {
    match status {
        StatusCode::UNAUTHORIZED => YahooError::new(
            "authentication_error",
            "Yahoo Finance denied access. No automatic retry was made.",
        ),
        StatusCode::FORBIDDEN | StatusCode::UNAVAILABLE_FOR_LEGAL_REASONS => YahooError::new(
            "plan_restricted",
            "Yahoo Finance denied access. No automatic retry was made.",
        ),
        StatusCode::TOO_MANY_REQUESTS => YahooError::new(
            "quota_exceeded",
            "Yahoo Finance rate limited the session. No automatic retry was made.",
        ),
        _ => YahooError::new(
            "http_error",
            "Yahoo Finance returned an unsuccessful response. No automatic retry was made.",
        ),
    }
}

fn timezone(meta: &Map<String, Value>) -> Result<Tz, YahooError> {
    meta.get("exchangeTimezoneName")
        .and_then(Value::as_str)
        .and_then(|value| Tz::from_str(value).ok())
        .ok_or_else(YahooError::malformed)
}

fn local_midnight(date: NaiveDate, zone: Tz) -> Result<i64, YahooError> {
    zone.from_local_datetime(
        &date
            .and_hms_opt(0, 0, 0)
            .ok_or_else(YahooError::invalid_request)?,
    )
    .earliest()
    .map(|value| value.timestamp())
    .ok_or_else(YahooError::invalid_request)
}

pub(crate) fn history_ranges(mut start: NaiveDate, end: NaiveDate) -> Vec<(NaiveDate, NaiveDate)> {
    let mut ranges = Vec::new();
    while (end - start).num_days() >= HISTORY_CHUNK_DAYS {
        let boundary = start + chrono::Duration::days(HISTORY_CHUNK_DAYS);
        ranges.push((start, boundary - chrono::Duration::days(1)));
        start = boundary;
    }
    ranges.push((start, end));
    ranges
}

fn array_value(container: Option<&Map<String, Value>>, key: &str, index: usize) -> Value {
    container
        .and_then(|value| value.get(key))
        .and_then(Value::as_array)
        .and_then(|values| values.get(index))
        .cloned()
        .unwrap_or(Value::Null)
}

fn decimal(value: &Value) -> Option<Decimal> {
    match value {
        Value::Number(number) => Decimal::from_str(&number.to_string()).ok(),
        Value::String(value) => Decimal::from_str(value).ok(),
        _ => None,
    }
}

fn split_factor(event: &Map<String, Value>) -> Option<Decimal> {
    let numerator = decimal(event.get("numerator")?)?;
    let denominator = decimal(event.get("denominator")?)?;
    (denominator != Decimal::ZERO)
        .then(|| numerator / denominator)
        .filter(|factor| *factor > Decimal::ZERO)
}

fn valid_symbol(value: &str) -> Result<String, YahooError> {
    let value = value.trim().to_ascii_uppercase();
    if value.is_empty() || value.len() > 100 || value.chars().any(char::is_whitespace) {
        return Err(YahooError::invalid_request());
    }
    Ok(value)
}

fn valid_isin(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 12
        && bytes[0..2].iter().all(u8::is_ascii_uppercase)
        && bytes[2..11]
            .iter()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
        && bytes[11].is_ascii_digit()
}

fn ticker_root(symbol: &str) -> &str {
    let Some((root, suffix)) = symbol.rsplit_once('.') else {
        return symbol;
    };
    if matches!(
        suffix,
        "DE" | "F"
            | "L"
            | "AS"
            | "PA"
            | "BR"
            | "LS"
            | "MI"
            | "MC"
            | "SW"
            | "ST"
            | "CO"
            | "HE"
            | "OL"
            | "VI"
            | "IR"
    ) {
        root
    } else {
        symbol
    }
}

fn exchange_mic(value: &str) -> Option<&'static str> {
    match value {
        "NMS" | "NGM" | "NCM" | "NAS" => Some("XNAS"),
        "NYQ" => Some("XNYS"),
        "PCX" => Some("ARCX"),
        "ASE" => Some("XASE"),
        "BTS" => Some("BATS"),
        "GER" => Some("XETR"),
        "FRA" => Some("XFRA"),
        "LSE" => Some("XLON"),
        "AMS" => Some("XAMS"),
        "PAR" => Some("XPAR"),
        "BRU" => Some("XBRU"),
        "LIS" => Some("XLIS"),
        "MIL" => Some("XMIL"),
        "MCE" => Some("XMAD"),
        "EBS" => Some("XSWX"),
        "STO" => Some("XSTO"),
        "CPH" => Some("XCSE"),
        "HEL" => Some("XHEL"),
        "OSL" => Some("XOSL"),
        "VIE" => Some("XWBO"),
        "DUB" => Some("XDUB"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use chrono::NaiveDate;
    use serde_json::json;

    use super::{history_ranges, no_history_response, ticker_root};

    #[test]
    fn history_ranges_do_not_overlap() {
        let start = NaiveDate::from_ymd_opt(1900, 1, 1).unwrap();
        let end = NaiveDate::from_ymd_opt(2026, 1, 1).unwrap();
        let ranges = history_ranges(start, end);
        assert!(ranges.len() > 1);
        for pair in ranges.windows(2) {
            assert_eq!(pair[0].1.succ_opt(), Some(pair[1].0));
        }
        assert_eq!(ranges.first().unwrap().0, start);
        assert_eq!(ranges.last().unwrap().1, end);
    }

    #[test]
    fn validates_exact_no_history_error() {
        let payload = json!({
            "chart": {
                "result": null,
                "error": {
                    "code": "Bad Request",
                    "description": "Data doesn't exist for startDate = 1, endDate = 2"
                }
            }
        });
        assert!(no_history_response(&payload, 1, 2));
        assert!(!no_history_response(&payload, 1, 3));
    }

    #[test]
    fn only_known_listing_suffixes_are_removed() {
        assert_eq!(ticker_root("SPYI.DE"), "SPYI");
        assert_eq!(ticker_root("BRK.B"), "BRK.B");
    }
}
