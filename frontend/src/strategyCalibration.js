import { sameStrategy } from './strategySelection.js';

const UINT32_MAX = 4294967295;
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const probability = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const validSeed = (value) => count(value) && value <= UINT32_MAX;
const close = (left, right) => Math.abs(left - right) < 1e-8;
const validDate = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && !value.startsWith('0000') && Number.isFinite(Date.parse(`${value}T00:00:00Z`))
  && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
const utcTimestamp = (value) => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)
  && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value))
  && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);

export const DEFAULT_CALIBRATION_INPUTS = {
  scenarioCount: '1000', minHorizon: '3', maxHorizon: '24', lookback: '12', start: '', end: '', seed: '',
};

export function savedCalibrationInputs(result) {
  const settings = result.settings;
  return { scenarioCount: String(settings.scenario_count), minHorizon: String(settings.min_horizon_months),
    maxHorizon: String(settings.max_horizon_months), lookback: String(settings.lookback_months),
    start: settings.start_date || '', end: settings.end_date || '', seed: String(result.seed) };
}

function months(value, label, minimum) {
  const text = value.trim();
  if (!/^\d+$/.test(text) || Number(text) < minimum || Number(text) > 120) {
    throw new Error(`${label} must be a whole number from ${minimum} to 120 months.`);
  }
  return Number(text);
}

export function calibrationRequest(inputs, strategy) {
  if (inputs.mode !== undefined && inputs.mode !== 'random') throw new Error('Only generated scenarios are supported.');
  const text = inputs.scenarioCount.trim();
  if (!/^\d+$/.test(text) || Number(text) < 10 || Number(text) > 5000) {
    throw new Error('Number of scenarios must be a whole number from 10 to 5000.');
  }
  const body = {
    strategy, mode: 'random', scenario_count: Number(text),
    min_horizon_months: months(inputs.minHorizon, 'Minimum horizon', 1),
    max_horizon_months: months(inputs.maxHorizon, 'Maximum horizon', 1),
    lookback_months: months(inputs.lookback, 'Training lookback', 3),
    start_date: inputs.start || null, end_date: inputs.end || null, seed: null,
  };
  if (body.min_horizon_months > body.max_horizon_months) throw new Error('Minimum horizon must be on or below the maximum horizon.');
  if ((body.start_date && !validDate(body.start_date)) || (body.end_date && !validDate(body.end_date))) {
    throw new Error('Choose valid forecast-origin dates or leave them blank.');
  }
  if (body.start_date && body.end_date && body.start_date > body.end_date) {
    throw new Error('Forecast-origin start date must be on or before the end date.');
  }
  const seed = inputs.seed.trim();
  if (seed && (!/^\d+$/.test(seed) || !validSeed(Number(seed)))) {
    throw new Error('Seed must be a whole number from 0 to 4294967295, or blank for server generation.');
  }
  body.seed = seed ? Number(seed) : null;
  return body;
}

function validSettings(settings) {
  return object(settings) && Object.keys(settings).length === 7 && settings.mode === 'random'
    && count(settings.scenario_count) && settings.scenario_count >= 10 && settings.scenario_count <= 5000
    && count(settings.min_horizon_months) && settings.min_horizon_months >= 1
    && count(settings.max_horizon_months) && settings.max_horizon_months >= settings.min_horizon_months && settings.max_horizon_months <= 120
    && count(settings.lookback_months) && settings.lookback_months >= 3 && settings.lookback_months <= 120
    && (settings.start_date === null || validDate(settings.start_date))
    && (settings.end_date === null || validDate(settings.end_date))
    && (!settings.start_date || !settings.end_date || settings.start_date <= settings.end_date);
}

function validSelection(selection, definition) {
  if (!object(selection) || typeof selection.id !== 'string' || !selection.id || !object(selection.parameters)) return false;
  if (!Object.values(selection.parameters).every((value) => typeof value === 'string' && value.length > 0)) return false;
  if (!definition) return true;
  return selection.id === definition.id
    && Object.keys(selection.parameters).every((key) => definition.parameters.some((parameter) => parameter.key === key))
    && definition.parameters.every((parameter) => {
      const value = selection.parameters[parameter.key];
      return value === undefined ? !parameter.required
        : parameter.type !== 'instrument' || /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(value);
    });
}

// The catalogue carries compact summaries only, never the audit arrays.
export function validCalibrationSummary(data, definition) {
  return object(data) && validSelection(data.strategy, definition) && validSettings(data.settings)
    && utcTimestamp(data.calculated_at) && validSeed(data.seed)
    && probability(data.brier_score) && probability(data.calibration_error)
    && count(data.sample_count) && data.sample_count > 0 && data.sample_count <= data.settings.scenario_count
    && count(data.origin_count) && data.origin_count > 0 && data.origin_count <= data.sample_count;
}

export function validCalibrationRunSummary(data) {
  return validCalibrationSummary(data) && typeof data.id === 'string'
    && /^(?:[a-f0-9]{24}|legacy-[a-f0-9]{64})$/.test(data.id)
    && typeof data.strategy_name === 'string' && data.strategy_name.trim().length > 0;
}

export function validCalibrationHistory(data, limit, offset) {
  return object(data) && data.limit === limit && data.offset === offset && count(data.total)
    && Array.isArray(data.items) && data.items.length === Math.min(limit, Math.max(0, data.total - offset))
    && data.items.every(validCalibrationRunSummary) && new Set(data.items.map((item) => item.id)).size === data.items.length;
}

// Validate saved experiments as strictly as new calculations, including their
// metrics against both bins and the per-scenario audit. No missing metric is zero.
export function validCalibrationResult(data, body = null, definition = null) {
  if (!validCalibrationSummary(data, definition)) return false;
  if (body) {
    const settings = Object.entries(body).filter(([key]) => key !== 'strategy' && key !== 'seed');
    if (body.mode !== 'random' || !sameStrategy(data.strategy, body.strategy)
      || Object.keys(data.settings).length !== settings.length
      || !settings.every(([key, value]) => data.settings[key] === value)
      || (body.seed !== null && data.seed !== body.seed)) return false;
  }
  const settings = data.settings;
  if (data.method !== 'walk_forward' || data.simulation_paths !== 10000 || !utcTimestamp(data.data_as_of)
    || !validDate(data.history_first_date) || !validDate(data.history_last_date) || data.history_first_date > data.history_last_date
    || data.candidate_count !== settings.scenario_count
    || ![data.mean_predicted_probability, data.observed_success_rate].every(probability)
    || !object(data.skipped_counts) || !Object.values(data.skipped_counts).every(count)
    || !Array.isArray(data.warnings) || !data.warnings.every((warning) => typeof warning === 'string')
    || !Array.isArray(data.calibration_bins) || data.calibration_bins.length !== 10) return false;

  const binsValid = data.calibration_bins.every((bin, index) => object(bin)
    && probability(bin.lower) && probability(bin.upper)
    && close(bin.lower, index / 10) && close(bin.upper, (index + 1) / 10)
    && count(bin.count) && (bin.count === 0
      ? bin.mean_predicted_probability === null && bin.observed_success_rate === null
      : probability(bin.mean_predicted_probability) && probability(bin.observed_success_rate)
        && bin.mean_predicted_probability >= bin.lower - 1e-9 && bin.mean_predicted_probability <= bin.upper + 1e-9));
  if (!binsValid || data.calibration_bins.reduce((sum, bin) => sum + bin.count, 0) !== data.sample_count) return false;
  const weighted = (metric) => data.calibration_bins.reduce((sum, bin) => sum + (bin.count ? bin.count * metric(bin) : 0), 0) / data.sample_count;
  if (!close(data.calibration_error, weighted((bin) => Math.abs(bin.mean_predicted_probability - bin.observed_success_rate)))
    || !close(data.mean_predicted_probability, weighted((bin) => bin.mean_predicted_probability))
    || !close(data.observed_success_rate, weighted((bin) => bin.observed_success_rate))) return false;

  const money = (value) => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value));
  const scenarioId = (value) => typeof value === 'string' && value.trim().length > 0;
  const auditValid = Array.isArray(data.forecasts) && data.forecasts.length === data.sample_count
    && data.forecasts.every((forecast) => object(forecast)
      && [forecast.origin_date, forecast.goal_date, forecast.training_start_date, forecast.training_end_date, forecast.valuation_date].every(validDate)
      && count(forecast.training_return_count) && forecast.training_return_count > 0
      && ['split_adjusted_close', 'adj_close', 'close'].includes(forecast.price_basis) && probability(forecast.predicted_probability)
      && typeof forecast.observed_success === 'boolean' && money(forecast.terminal_value_eur)
      && scenarioId(forecast.scenario_id) && money(forecast.goal_eur) && Number(forecast.goal_eur) > 0
      && money(forecast.starting_capital_eur) && Number(forecast.starting_capital_eur) >= 1000 && Number(forecast.starting_capital_eur) <= 100000
      && money(forecast.monthly_contribution_eur) && Number(forecast.monthly_contribution_eur) <= 1000
      && count(forecast.horizon_months) && forecast.horizon_months >= settings.min_horizon_months && forecast.horizon_months <= settings.max_horizon_months
      && probability(forecast.requested_probability) && forecast.requested_probability >= 0.01 && forecast.requested_probability <= 0.99)
    && Array.isArray(data.excluded_origins) && data.excluded_origins.every((origin) => object(origin)
      && scenarioId(origin.scenario_id) && validDate(origin.origin_date) && typeof origin.reason === 'string' && origin.reason.length > 0);
  if (!auditValid) return false;

  const auditBins = Array.from({ length: 10 }, () => ({ count: 0, predicted: 0, observed: 0 }));
  let brier = 0;
  for (const forecast of data.forecasts) {
    const p = forecast.predicted_probability, y = Number(forecast.observed_success);
    const bin = auditBins[Math.min(9, Math.floor(p * 10))];
    bin.count += 1; bin.predicted += p; bin.observed += y;
    brier += (p - y) ** 2;
  }
  if (!close(data.brier_score, brier / data.sample_count)
    || !data.calibration_bins.every((bin, index) => bin.count === auditBins[index].count && (!bin.count
      || close(bin.mean_predicted_probability, auditBins[index].predicted / bin.count)
        && close(bin.observed_success_rate, auditBins[index].observed / bin.count)))) return false;

  const expectedGeneration = {
    method: 'training_quantiles_independent_streams', target_simulation_paths: 10000,
    capital_min_eur: '1000.00', capital_max_eur: '100000.00', monthly_min_eur: '0.00', monthly_max_eur: '1000.00',
    probability_min: 0.01, probability_max: 0.99,
  };
  const skipped = Object.values(data.skipped_counts).reduce((sum, value) => sum + value, 0);
  if (!object(data.generation) || !Object.entries(expectedGeneration).every(([key, value]) => data.generation[key] === value)
    || typeof data.history_fingerprint !== 'string' || !data.history_fingerprint.trim()
    || data.candidate_count !== data.sample_count + skipped || data.excluded_origins.length !== skipped
    || !count(data.available_origin_count) || data.available_origin_count < data.origin_count
    || data.origin_count !== new Set(data.forecasts.map((forecast) => forecast.origin_date)).size) return false;
  const ids = [...data.forecasts, ...data.excluded_origins].map((item) => item.scenario_id);
  const excludedCounts = new Map();
  for (const origin of data.excluded_origins) excludedCounts.set(origin.reason, (excludedCounts.get(origin.reason) || 0) + 1);
  return new Set(ids).size === data.candidate_count
    && [...excludedCounts].every(([reason, value]) => data.skipped_counts[reason] === value);
}
