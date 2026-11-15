use chrono::{Datelike, NaiveDate};
use rust_decimal::{prelude::ToPrimitive, Decimal};
use serde_json::Value;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone)]
pub struct QuoteRow {
    pub date: NaiveDate,
    pub close: Option<Decimal>,
    pub adjusted: Option<Decimal>,
    pub currency: Option<String>,
    pub carried_from_date: Option<NaiveDate>,
}

#[derive(Debug, Clone, Copy)]
pub enum PriceField {
    Close,
    Adjusted,
}

#[derive(Debug, Clone)]
pub struct Policy {
    pub method: String,
    pub seed: u64,
}

impl Policy {
    pub fn from_json(value: &Value) -> Self {
        Self {
            method: value
                .get("method")
                .and_then(Value::as_str)
                .unwrap_or("smooth")
                .to_owned(),
            seed: value.get("seed").and_then(Value::as_u64).unwrap_or(0),
        }
    }
}

fn value(row: &QuoteRow, field: PriceField) -> Option<Decimal> {
    match field {
        PriceField::Close => row.close,
        PriceField::Adjusted => row.adjusted,
    }
}

fn set_value(row: &mut QuoteRow, field: PriceField, value: Decimal) {
    match field {
        PriceField::Close => row.close = Some(value),
        PriceField::Adjusted => row.adjusted = Some(value),
    }
}

fn positive(value: Option<Decimal>) -> Option<Decimal> {
    value.filter(|value| *value > Decimal::ZERO)
}

fn currency(row: &QuoteRow, override_currency: Option<&str>) -> Option<String> {
    row.currency
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase)
        .or_else(|| override_currency.map(str::to_ascii_uppercase))
}

pub fn adjacent(previous: NaiveDate, latest: NaiveDate) -> bool {
    let gap = (latest - previous).num_days();
    gap > 0
        && gap <= 3
        && (1..=gap)
            .filter(|step| {
                (previous + chrono::Duration::days(*step))
                    .weekday()
                    .number_from_monday()
                    <= 5
            })
            .count()
            == 1
}

pub fn fill(
    rows: &mut [QuoteRow],
    instrument_id: &str,
    policy: &Policy,
    override_currency: Option<&str>,
    fields: &[PriceField],
) {
    for field in fields {
        fill_field(rows, instrument_id, policy, override_currency, *field);
    }
}

fn fill_field(
    rows: &mut [QuoteRow],
    instrument_id: &str,
    policy: &Policy,
    override_currency: Option<&str>,
    field: PriceField,
) {
    let raw = rows.iter().map(|row| value(row, field)).collect::<Vec<_>>();
    let currencies = rows
        .iter()
        .map(|row| currency(row, override_currency))
        .collect::<Vec<_>>();
    let mut count = 0_usize;
    let mut mean = 0.0_f64;
    let mut m2 = 0.0_f64;

    for index in 0..rows.len() {
        if index > 0 && currencies[index] != currencies[index - 1] {
            count = 0;
            mean = 0.0;
            m2 = 0.0;
        }
        if index > 0
            && currencies[index] == currencies[index - 1]
            && adjacent(rows[index - 1].date, rows[index].date)
        {
            if let (Some(previous), Some(current)) =
                (positive(raw[index - 1]), positive(raw[index]))
            {
                let observed = current
                    .to_f64()
                    .zip(previous.to_f64())
                    .map(|(current, previous)| current.ln() - previous.ln())
                    .unwrap_or(f64::NAN);
                if observed.is_finite() {
                    count += 1;
                    let delta = observed - mean;
                    mean += delta / count as f64;
                    m2 += delta * (observed - mean);
                }
            }
        }

        if raw[index].is_some()
            || rows[index].date.weekday().number_from_monday() > 5
            || !matches!(currencies[index].as_deref(), None | Some("EUR"))
            || override_currency.is_some_and(|value| !value.eq_ignore_ascii_case("EUR"))
        {
            continue;
        }

        let replacement = if policy.method == "smooth" {
            smooth_value(rows, &raw, &currencies, index, field)
        } else if policy.method == "normal"
            && count >= 2
            && index > 0
            && currencies[index] == currencies[index - 1]
            && adjacent(rows[index - 1].date, rows[index].date)
        {
            positive(value(&rows[index - 1], field)).and_then(|anchor| {
                let variance = m2 / (count - 1) as f64;
                let draw = deterministic_normal(
                    policy.seed,
                    instrument_id,
                    rows[index].date,
                    field,
                    mean,
                    variance.max(0.0).sqrt(),
                );
                let anchor = anchor.to_f64()?;
                (anchor.ln() + draw)
                    .exp()
                    .to_string()
                    .parse::<Decimal>()
                    .ok()
                    .filter(|value| *value > Decimal::ZERO)
            })
        } else {
            None
        };
        if let Some(replacement) = replacement {
            set_value(&mut rows[index], field, replacement);
        }
    }
}

fn smooth_value(
    rows: &[QuoteRow],
    raw: &[Option<Decimal>],
    currencies: &[Option<String>],
    index: usize,
    _field: PriceField,
) -> Option<Decimal> {
    let mut left = None;
    for candidate in (0..index).rev() {
        if currencies[candidate] != currencies[index]
            || rows[candidate].date.weekday().number_from_monday() > 5
            || raw[candidate].is_some_and(|value| value <= Decimal::ZERO)
        {
            break;
        }
        if positive(raw[candidate]).is_some() {
            left = Some(candidate);
            break;
        }
    }
    let mut right = None;
    for candidate in index + 1..rows.len() {
        if currencies[candidate] != currencies[index]
            || rows[candidate].date.weekday().number_from_monday() > 5
            || raw[candidate].is_some_and(|value| value <= Decimal::ZERO)
        {
            break;
        }
        if positive(raw[candidate]).is_some() {
            right = Some(candidate);
            break;
        }
    }
    let (left, right) = left.zip(right)?;
    let start = positive(raw[left])?;
    let end = positive(raw[right])?;
    let weight = Decimal::from((index - left) as u64) / Decimal::from((right - left) as u64);
    Some(start * (Decimal::ONE - weight) + end * weight)
}

fn deterministic_normal(
    seed: u64,
    instrument_id: &str,
    date: NaiveDate,
    field: PriceField,
    mean: f64,
    deviation: f64,
) -> f64 {
    let field = match field {
        PriceField::Close => "close",
        PriceField::Adjusted => "adj_close",
    };
    let identity = serde_json::json!([1, seed, instrument_id, date.to_string(), field]).to_string();
    let digest = Sha256::digest(identity.as_bytes());
    PythonRandom::from_seed_bytes(&digest).normalvariate(mean, deviation)
}

struct PythonRandom {
    state: [u32; 624],
    index: usize,
}

impl PythonRandom {
    fn from_seed_bytes(seed: &[u8]) -> Self {
        let key = seed
            .rchunks(4)
            .map(|chunk| {
                let mut bytes = [0_u8; 4];
                bytes[4 - chunk.len()..].copy_from_slice(chunk);
                u32::from_be_bytes(bytes)
            })
            .collect::<Vec<_>>();
        let mut random = Self {
            state: [0; 624],
            index: 624,
        };
        random.state[0] = 19_650_218;
        for index in 1..624 {
            random.state[index] = 1_812_433_253_u32
                .wrapping_mul(random.state[index - 1] ^ (random.state[index - 1] >> 30))
                .wrapping_add(index as u32);
        }
        let mut state_index = 1_usize;
        let mut key_index = 0_usize;
        for _ in 0..624.max(key.len()) {
            random.state[state_index] = (random.state[state_index]
                ^ (random.state[state_index - 1] ^ (random.state[state_index - 1] >> 30))
                    .wrapping_mul(1_664_525))
            .wrapping_add(key[key_index])
            .wrapping_add(key_index as u32);
            state_index += 1;
            key_index += 1;
            if state_index >= 624 {
                random.state[0] = random.state[623];
                state_index = 1;
            }
            if key_index >= key.len() {
                key_index = 0;
            }
        }
        for _ in 0..623 {
            random.state[state_index] = (random.state[state_index]
                ^ (random.state[state_index - 1] ^ (random.state[state_index - 1] >> 30))
                    .wrapping_mul(1_566_083_941))
            .wrapping_sub(state_index as u32);
            state_index += 1;
            if state_index >= 624 {
                random.state[0] = random.state[623];
                state_index = 1;
            }
        }
        random.state[0] = 0x8000_0000;
        random
    }

    fn next_u32(&mut self) -> u32 {
        if self.index >= 624 {
            for index in 0..624 {
                let value = (self.state[index] & 0x8000_0000)
                    | (self.state[(index + 1) % 624] & 0x7fff_ffff);
                self.state[index] = self.state[(index + 397) % 624]
                    ^ (value >> 1)
                    ^ if value & 1 == 0 { 0 } else { 0x9908_b0df };
            }
            self.index = 0;
        }
        let mut value = self.state[self.index];
        self.index += 1;
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c_5680;
        value ^= (value << 15) & 0xefc6_0000;
        value ^= value >> 18;
        value
    }

    fn random(&mut self) -> f64 {
        let first = (self.next_u32() >> 5) as u64;
        let second = (self.next_u32() >> 6) as u64;
        (first * 67_108_864 + second) as f64 / 9_007_199_254_740_992.0
    }

    fn normalvariate(&mut self, mean: f64, deviation: f64) -> f64 {
        const MAGIC: f64 = 1.715_527_769_921_413_5;
        loop {
            let first = self.random();
            let second = 1.0 - self.random();
            let value = MAGIC * (first - 0.5) / second;
            if value * value / 4.0 <= -second.ln() {
                return mean + value * deviation;
            }
        }
    }
}

pub fn carry_trailing_smooth(rows: &mut [QuoteRow], policy: &Policy) {
    if policy.method != "smooth" || rows.is_empty() {
        return;
    }
    let mut start = rows.len();
    while start > 0
        && rows[start - 1].close.is_none()
        && rows[start - 1].date.weekday().number_from_monday() <= 5
    {
        start -= 1;
    }
    if start == 0 || start == rows.len() {
        return;
    }
    let source_close = positive(rows[start - 1].close);
    let source_adjusted = positive(rows[start - 1].adjusted);
    if source_close.is_none() && source_adjusted.is_none() {
        return;
    }
    let source_date = rows[start - 1].date;
    for row in &mut rows[start..] {
        if row.close.is_none() {
            row.close = source_close;
        }
        if row.adjusted.is_none() {
            row.adjusted = source_adjusted;
        }
        row.carried_from_date = Some(source_date);
    }
}

#[cfg(test)]
mod tests {
    use std::str::FromStr;

    use super::*;

    fn row(date: &str, close: Option<&str>) -> QuoteRow {
        QuoteRow {
            date: NaiveDate::parse_from_str(date, "%Y-%m-%d").unwrap(),
            close: close.map(|value| Decimal::from_str(value).unwrap()),
            adjusted: None,
            currency: Some("EUR".into()),
            carried_from_date: None,
        }
    }

    #[test]
    fn smooths_internal_missing_weekdays_without_inserting_dates() {
        let mut rows = vec![
            row("2026-09-01", Some("100")),
            row("2026-09-02", None),
            row("2026-09-03", None),
            row("2026-09-04", Some("160")),
        ];
        fill(
            &mut rows,
            "IE00B4L5Y983",
            &Policy {
                method: "smooth".into(),
                seed: 0,
            },
            None,
            &[PriceField::Close],
        );
        assert_eq!(rows[1].close, Some(Decimal::from(120)));
        assert_eq!(rows[2].close, Some(Decimal::from(140)));
        assert_eq!(rows.len(), 4);
    }

    #[test]
    fn normal_stream_matches_python_random_contract() {
        let date = NaiveDate::parse_from_str("2026-09-04", "%Y-%m-%d").unwrap();
        let draw = deterministic_normal(7, "IE00B4L5Y983", date, PriceField::Close, 0.01, 0.02);
        assert!((draw - 0.005_610_078_539_491_870_6).abs() < 1e-15);
    }

    #[test]
    fn normal_fills_are_reproducible_and_seeded() {
        let source = vec![
            row("2026-09-01", Some("100")),
            row("2026-09-02", Some("110")),
            row("2026-09-03", Some("99")),
            row("2026-09-04", None),
        ];
        let apply = |seed| {
            let mut rows = source.clone();
            fill(
                &mut rows,
                "IE00B4L5Y983",
                &Policy {
                    method: "normal".into(),
                    seed,
                },
                None,
                &[PriceField::Close],
            );
            rows[3].close
        };
        assert_eq!(apply(7), apply(7));
        assert_ne!(apply(7), apply(8));
        assert!(apply(7).is_some_and(|value| value > Decimal::ZERO));
    }

    #[test]
    fn smooth_policy_carries_only_trailing_missing_weekdays() {
        let mut rows = vec![
            row("2026-09-01", Some("100")),
            row("2026-09-02", None),
            row("2026-09-03", None),
        ];
        carry_trailing_smooth(
            &mut rows,
            &Policy {
                method: "smooth".into(),
                seed: 0,
            },
        );
        assert_eq!(rows[2].close, Some(Decimal::from(100)));
        assert_eq!(rows[2].carried_from_date, Some(rows[0].date));
    }
}
