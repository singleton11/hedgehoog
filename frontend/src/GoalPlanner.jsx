import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import GoalProjectionChart from './GoalProjectionChart.jsx';
import { formatEuros, parseProjection } from './goalProjection.js';
export { formatEuros } from './goalProjection.js';
import { percent, StatisticsSummary } from './ReturnStatistics.jsx';
import StrategyParameters, { supportsParameter } from './StrategyParameters.jsx';
import { BUY_AND_HOLD_ID, sameStrategy, useGoalStrategies } from './goalStrategies.js';

// UTC calendar anniversaries clamp to the month's final day, rather than rolling over.
export function monthlyAnniversary(date, months) {
  const first = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + months, 1));
  const lastDay = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
  first.setUTCDate(Math.min(date.getUTCDate(), lastDay));
  return first.toISOString().slice(0, 10);
}

export function normalizeEuros(value) {
  if (!/^\d+(?:\.\d{1,2})?$/.test(value.trim())) return null;
  const [whole, fraction = ''] = value.trim().split('.');
  return `${whole.replace(/^0+(?=\d)/, '')}.${fraction.padEnd(2, '0')}`;
}

function validateInputs(inputs, now = new Date()) {
  const goal = normalizeEuros(inputs.goal);
  const capital = normalizeEuros(inputs.capital);
  if (!goal || goal === '0.00') return 'Enter a target greater than zero in EUR, using at most two decimal places.';
  if (!capital) return 'Enter starting capital of zero or more in EUR, using at most two decimal places.';
  const reliability = Number(inputs.reliability);
  if (!inputs.reliability.trim() || !Number.isFinite(reliability) || reliability < 50 || reliability > 99) {
    return 'Reliability must be between 50% and 99%, inclusive.';
  }
  const parsed = new Date(`${inputs.date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(inputs.date) || Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== inputs.date
    || inputs.date < monthlyAnniversary(now, 1) || inputs.date > monthlyAnniversary(now, 600)) {
    return 'Choose a goal date from the next monthly anniversary through 50 years from today (UTC).';
  }
  return null;
}

export function validResult(data, body, strategy) {
  if (!data || !body?.strategy || !body.strategy.parameters || typeof body.strategy.parameters !== 'object'
    || !Array.isArray(strategy?.parameters) || strategy.parameters.some((parameter) => !parameter || typeof parameter.key !== 'string')) return false;
  const amounts = [data?.monthly_contribution_eur, data?.total_contributions_eur, data?.total_invested_eur,
    data?.terminal_percentiles_eur?.p05, data?.terminal_percentiles_eur?.p50, data?.terminal_percentiles_eur?.p95];
  const probabilities = [data?.target_probability, data?.success_probability, data?.zero_contribution_success_probability];
  const projection = parseProjection(data?.trajectory, data?.goal_eur);
  const instrumentParameters = strategy.parameters.filter((parameter) => parameter.type === 'instrument' && body.strategy.parameters[parameter.key]);
  return projection && projection.points[0].date === data.valuation_date && projection.points.at(-1).date === data.goal_date
    && sameStrategy(data.strategy, body.strategy)
    && data.goal_eur === body.goal_eur && Number.isInteger(data.simulation_paths) && data.simulation_paths > 0
    && data.goal_date === body.goal_date && data.target_probability === body.reliability
    && data.parameter_statistics && typeof data.parameter_statistics === 'object' && !Array.isArray(data.parameter_statistics)
    && Object.keys(data.parameter_statistics).length === instrumentParameters.length
    && instrumentParameters.every(({ key }) => data.parameter_statistics[key]?.instrument_id === body.strategy.parameters[key]
      && Array.isArray(data.parameter_statistics[key]?.warnings))
    && Array.isArray(data.warnings)
    && amounts.every((value) => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value))
    && probabilities.every((value) => typeof value === 'number' && value >= 0 && value <= 1)
    && Number.isInteger(data.contribution_count) && data.contribution_count > 0;
}

export function validSimulationRecord(data, goal, { saved = false, id } = {}) {
  return Boolean(goal) && typeof data?.id === 'string' && /^[a-zA-Z0-9_-]+$/.test(data.id) && (!id || data.id === id)
    && data.customer_id === goal.customer_id && data.goal_id === goal.id
    && typeof data.calculated_at === 'string' && Number.isFinite(Date.parse(data.calculated_at))
    && (saved ? typeof data.saved_at === 'string' && Number.isFinite(Date.parse(data.saved_at)) : data.saved_at === null)
    // Saved reports describe their original goal settings, not today's editable goal.
    && (saved || (data.inputs?.goal_eur === goal.goal_eur && data.inputs?.goal_date === goal.goal_date))
    && typeof data.inputs?.starting_capital_eur === 'string' && /^\d+\.\d{2}$/.test(data.inputs.starting_capital_eur)
    && data.strategy_definition?.id === data.inputs?.strategy?.id && typeof data.strategy_definition?.name === 'string'
    && validResult(data.result, data.inputs, data.strategy_definition);
}

export function SimulationResult({ result, strategy }) {
  return <section aria-label="Goal simulation result" className="goal-result">
    <h3>Estimated monthly contribution</h3>
    <div role="status">
      <p className="monthly-contribution">{formatEuros(result.monthly_contribution_eur)} <span>per month</span></p>
      <p><strong>{percent(result.success_probability)} estimated success</strong> · {percent(result.target_probability)} target</p>
    </div>
    {/^0+(?:\.0+)?$/.test(result.monthly_contribution_eur) && <p>No monthly contribution needed.</p>}
    <dl className="facts">
      <div><dt>Schedule</dt><dd>{result.contribution_count} payments</dd></div>
      <div><dt>Total contributions</dt><dd>{formatEuros(result.total_contributions_eur)}</dd></div>
      <div><dt>Total invested</dt><dd>{formatEuros(result.total_invested_eur)}</dd></div>
    </dl>
    <GoalProjectionChart trajectory={result.trajectory} goal={result.goal_eur} pathCount={result.simulation_paths} />
    <h3>Projected final value</h3>
    <dl className="facts terminal-values">
      <div><dt>Downside · 5th percentile</dt><dd>{formatEuros(result.terminal_percentiles_eur.p05)}</dd></div>
      <div><dt>Median · 50th percentile</dt><dd>{formatEuros(result.terminal_percentiles_eur.p50)}</dd></div>
      <div><dt>Upside · 95th percentile</dt><dd>{formatEuros(result.terminal_percentiles_eur.p95)}</dd></div>
    </dl>
    <details className="disclosure"><summary>Calculation details</summary>
      <div className="disclosure-content">
        <p><strong>Strategy:</strong> {strategy.name}</p>
        <dl className="facts">
          <div><dt>First / last contribution (UTC)</dt><dd>{result.first_contribution_date} / {result.last_contribution_date}</dd></div>
          <div><dt>Valuation / goal date (UTC)</dt><dd>{result.valuation_date} / {result.goal_date}</dd></div>
          <div><dt>Success without contributions</dt><dd>{percent(result.zero_contribution_success_probability)}</dd></div>
        </dl>
        {Object.entries(result.parameter_statistics).map(([key, statistics]) => <div key={key}>
          <h4>{strategy.parameters.find((parameter) => parameter.key === key)?.label} · {statistics.instrument_id}</h4>
          <StatisticsSummary statistics={statistics} />
        </div>)}
      </div>
    </details>
  </section>;
}

export default function GoalPlanner({ isin, goal, apiBase, reportHref }) {
  const { request } = useAuth();
  const { strategies, instruments, loading, error: loadError, retry } = useGoalStrategies();
  // Legacy links prefill the first strategy. Ordinary planner state is a
  // strategy selection, never an instrument pretending to be a strategy.
  const [strategyId, setStrategyId] = useState(isin ? BUY_AND_HOLD_ID : null);
  const [parameters, setParameters] = useState(() => isin ? { instrument: isin } : {});
  const strategy = strategies.find((item) => item.id === strategyId) || (strategyId === null ? strategies[0] : null);
  const selection = strategy ? { id: strategy.id, parameters } : null;
  const [readiness, setReadiness] = useState({});
  const [inputs, setInputs] = useState(() => ({ goal: goal?.goal_eur || '100000', capital: '0', date: goal?.goal_date || monthlyAnniversary(new Date(), 120), reliability: '75' }));
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const saveLock = useRef(false);
  const sequence = useRef(0);
  const controller = useRef(null);
  const signature = JSON.stringify([selection, inputs]);
  const eligible = strategy && !loading && !loadError && strategy.parameters.every((parameter) => {
    const state = readiness[`${strategy.id}:${parameter.key}`];
    return supportsParameter(parameter) && state?.value === (parameters[parameter.key] || '') && state.ready;
  });
  const now = new Date();

  useEffect(() => () => { sequence.current += 1; controller.current?.abort(); }, [request]);

  const invalidate = useCallback(() => {
    sequence.current += 1;
    controller.current?.abort();
    setResult(null);
    setBusy(false);
    setSaving(false);
    saveLock.current = false;
    setSaveError(null);
    setError(null);
  }, []);
  const onReadiness = useCallback((key, value, ready) => {
    setReadiness((previous) => ({ ...previous, [key]: { value, ready } }));
  }, []);

  function change(field, value) {
    if (goal && (field === 'goal' || field === 'date')) return;
    invalidate();
    setInputs((previous) => ({ ...previous, [field]: value }));
  }

  function changeStrategy(id) {
    invalidate();
    setStrategyId(id);
    setParameters({});
    setReadiness({});
  }

  function changeParameter(key, value) {
    invalidate();
    setParameters((previous) => ({ ...previous, [key]: value }));
  }

  async function simulate(event) {
    event.preventDefault();
    if (busy || saveLock.current || !eligible) return;
    invalidate();
    const validationError = validateInputs(inputs);
    if (validationError) { setError(validationError); return; }
    const current = ++sequence.current;
    const abort = new AbortController();
    controller.current = abort;
    const body = { strategy: selection, goal_eur: normalizeEuros(inputs.goal), starting_capital_eur: normalizeEuros(inputs.capital),
      goal_date: inputs.date, reliability: Number(inputs.reliability) / 100 };
    setBusy(true);
    try {
      const payload = goal ? { strategy: body.strategy, starting_capital_eur: body.starting_capital_eur, reliability: body.reliability } : body;
      const response = await request(goal ? `${apiBase}/simulate` : '/goals/simulate', { method: 'POST', body: payload, signal: abort.signal });
      if (abort.signal.aborted || sequence.current !== current) return;
      const data = goal ? response?.result : response;
      if (!validResult(data, body, strategy) || (goal && (!validSimulationRecord(response, goal)
        || response.inputs.starting_capital_eur !== body.starting_capital_eur))) throw new Error('Simulation returned an unexpected response.');
      setResult({ signature, data, preview: goal ? response : null });
    } catch (error) {
      if (abort.signal.aborted || sequence.current !== current) return;
      setError(error.message || 'Simulation failed. Please try again.');
    } finally {
      if (!abort.signal.aborted && sequence.current === current) setBusy(false);
    }
  }

  async function saveSimulation() {
    if (!result?.preview || result.saved || result.signature !== signature || saveLock.current) return;
    saveLock.current = true;
    setSaving(true);
    setSaveError(null);
    const current = ++sequence.current;
    const abort = new AbortController();
    controller.current = abort;
    try {
      const saved = await request(`${apiBase}/simulations/${encodeURIComponent(result.preview.id)}/save`, { method: 'POST', signal: abort.signal });
      if (abort.signal.aborted || sequence.current !== current) return;
      if (!validSimulationRecord(saved, goal, { saved: true, id: result.preview.id })
        || saved.inputs.goal_eur !== result.preview.inputs.goal_eur || saved.inputs.goal_date !== result.preview.inputs.goal_date) {
        throw new Error('Unexpected save response. Check saved simulations or retry saving this result.');
      }
      window.location.hash = reportHref(saved.id);
      setResult((previous) => ({ ...previous, saved: true }));
    } catch (error) {
      if (!abort.signal.aborted && sequence.current === current) setSaveError(error.message || 'Could not save simulation. Please retry.');
    } finally {
      if (!abort.signal.aborted && sequence.current === current) { saveLock.current = false; setSaving(false); }
    }
  }

  return <article aria-label="Goal planner" className="goal-planner">
    <div className="page-heading"><p className="eyebrow">Make room for your future</p>
      <h2 className="instrument-name">{goal ? 'New simulation' : 'Goal planner'}</h2>
      <p className="muted">{goal ? `${goal.name} · Calculate first, then save the simulations you want to keep.` : 'Choose a strategy. Turn a future goal into a monthly plan.'}</p>
    </div>
    <fieldset className="planner-controls" disabled={saving || result?.saved}>
    <section aria-label="Strategy configuration" className="goal-strategy">
      {loading && <p role="status">Loading strategies and acquired instruments…</p>}
      {loadError && <>
        <p role="alert">Could not load planner data: {loadError}</p>
        <button type="button" className="secondary" onClick={() => { invalidate(); setReadiness({}); retry(); }}>Retry planner data</button>
      </>}
      {!loading && !loadError && <>
        <label htmlFor="goal-strategy">Strategy</label>
        <select id="goal-strategy" value={strategy?.id || ''} onChange={(event) => changeStrategy(event.target.value)}>
          {!strategy && <option value="">Choose a strategy</option>}
          {strategies.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        {strategy && <>
          <p className="muted strategy-description">{strategy.description}</p>
          <StrategyParameters strategy={strategy} parameters={parameters} instruments={instruments}
            onChange={changeParameter} onReadiness={onReadiness} onInvalidate={invalidate} />
        </>}
      </>}
    </section>
    <form aria-label="Goal planner inputs" className="goal-form" noValidate onSubmit={simulate}>
      <div className="goal-fields">
        <div><label htmlFor="goal-eur">Target (EUR)</label><input id="goal-eur" inputMode="decimal" value={inputs.goal} required maxLength={100} readOnly={Boolean(goal)}
          onChange={(event) => change('goal', event.target.value)} /></div>
        <div><label htmlFor="starting-capital-eur">Starting capital (EUR)</label><input id="starting-capital-eur" inputMode="decimal" value={inputs.capital} required maxLength={100}
          onChange={(event) => change('capital', event.target.value)} /></div>
        <div><label htmlFor="goal-date">Goal date (UTC)</label><input id="goal-date" type="date" value={inputs.date} required readOnly={Boolean(goal)}
          min={monthlyAnniversary(now, 1)} max={monthlyAnniversary(now, 600)} onChange={(event) => change('date', event.target.value)} /></div>
        <div><label htmlFor="goal-reliability">Reliability target (%)</label><input id="goal-reliability" type="number" min="50" max="99" step="any" value={inputs.reliability} required
          onChange={(event) => change('reliability', event.target.value)} /></div>
      </div>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={!eligible || busy}>{busy ? 'Simulating…' : 'Calculate monthly contribution'}</button>
      {busy && <p role="status" className="sr-only">Simulating…</p>}
    </form>
    </fieldset>
    {result?.signature === signature && <>
      {goal && <section className="simulation-save" aria-label="Save calculated simulation">
        <p>{result.saved ? 'Simulation saved.' : 'Not saved yet. Save this exact result to this goal, or change the settings to try another simulation.'}</p>
        {!result.saved && <button type="button" disabled={saving} onClick={saveSimulation}>{saving ? 'Saving…' : 'Save simulation'}</button>}
        {saveError && <p role="alert">{saveError}</p>}
        <p className="muted">Unsaved results expire after 24 hours.</p>
      </section>}
      <SimulationResult result={result.data} strategy={result.preview?.strategy_definition || strategy} />
    </>}
  </article>;
}
