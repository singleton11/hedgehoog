import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import InstrumentSearch from './InstrumentSearch.jsx';
import GoalProjectionChart from './GoalProjectionChart.jsx';
import { formatEuros, parseProjection } from './goalProjection.js';
export { formatEuros } from './goalProjection.js';
import { displayText } from './instruments.js';
import { percent, StatisticsSummary, useReturnStatistics } from './ReturnStatistics.jsx';

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

function validResult(data, body) {
  const amounts = [data?.monthly_contribution_eur, data?.total_contributions_eur, data?.total_invested_eur,
    data?.terminal_percentiles_eur?.p05, data?.terminal_percentiles_eur?.p50, data?.terminal_percentiles_eur?.p95];
  const probabilities = [data?.target_probability, data?.success_probability, data?.zero_contribution_success_probability];
  const projection = parseProjection(data?.trajectory, data?.goal_eur);
  return projection && projection.points[0].date === data.valuation_date && projection.points.at(-1).date === data.goal_date
    && data.goal_eur === body.goal_eur && Number.isInteger(data.simulation_paths) && data.simulation_paths > 0
    && data?.statistics?.instrument_id === body.instrument_id && data.goal_date === body.goal_date
    && Array.isArray(data.statistics.warnings) && Array.isArray(data.warnings)
    && amounts.every((value) => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value))
    && probabilities.every((value) => typeof value === 'number' && value >= 0 && value <= 1)
    && Number.isInteger(data.contribution_count) && data.contribution_count > 0;
}

function Assumptions() {
  return <details className="disclosure"><summary>Model assumptions and limitations</summary>
    <div className="disclosure-content">
      <ul>
        <li>All amounts are nominal euros. The instrument must trade in EUR; underlying foreign-exchange exposure may still exist.</li>
        <li>Starting capital is invested today. Fixed monthly payments are additional to starting capital, on calendar anniversaries starting one month from today (UTC), clamped to the last day of shorter months. Deposits occur at the end of each period; fractional shares are assumed.</li>
        <li>Independent, normally distributed log returns are fitted to saved history, using 252 trading days per year. Historical simple-return statistics are descriptive, not forecasts.</li>
        <li>Distributions are reinvested only insofar as adjusted history includes them. A consistent adjusted-close series is preferred; otherwise unadjusted closes are used with a caveat.</li>
        <li>No taxes, transaction fees or inflation are included.</li>
        <li>Monte Carlo sampling and estimated historical parameters are uncertain. Markets can differ materially from this model: model-estimated success is not a guarantee or investment advice.</li>
      </ul>
    </div>
  </details>;
}

function SimulationResult({ result }) {
  return <section aria-label="Goal simulation result" className="goal-result">
    <h3>Estimated monthly contribution</h3>
    <div role="status">
      <p className="monthly-contribution">{formatEuros(result.monthly_contribution_eur)} <span>per month</span></p>
      <p><strong>{percent(result.success_probability)} model-estimated success</strong> against a {percent(result.target_probability)} reliability target. Not a guarantee.</p>
    </div>
    <p>These payments are additional to your starting capital. {result.monthly_contribution_eur && /^0+(?:\.0+)?$/.test(result.monthly_contribution_eur)
      && 'The model estimates no additional monthly contribution is needed.'}</p>
    <dl className="facts">
      <div><dt>Monthly contributions</dt><dd>{result.contribution_count} payments</dd></div>
      <div><dt>First / last contribution (UTC)</dt><dd>{result.first_contribution_date} / {result.last_contribution_date}</dd></div>
      <div><dt>Valuation / goal date (UTC)</dt><dd>{result.valuation_date} / {result.goal_date}</dd></div>
      <div><dt>Total monthly contributions</dt><dd>{formatEuros(result.total_contributions_eur)}</dd></div>
      <div><dt>Total invested, including starting capital</dt><dd>{formatEuros(result.total_invested_eur)}</dd></div>
      <div><dt>Success with no monthly contributions</dt><dd>{percent(result.zero_contribution_success_probability)}</dd></div>
    </dl>
    <GoalProjectionChart trajectory={result.trajectory} goal={result.goal_eur} pathCount={result.simulation_paths} />
    <h3>Modelled terminal values</h3>
    <dl className="facts terminal-values">
      <div><dt>Downside · 5th percentile</dt><dd>{formatEuros(result.terminal_percentiles_eur.p05)}</dd></div>
      <div><dt>Median · 50th percentile</dt><dd>{formatEuros(result.terminal_percentiles_eur.p50)}</dd></div>
      <div><dt>Upside · 95th percentile</dt><dd>{formatEuros(result.terminal_percentiles_eur.p95)}</dd></div>
    </dl>
    <p className="muted">Percentiles are not minimum or maximum outcomes. {result.simulation_paths?.toLocaleString('en')} simulated paths · seed {result.seed}.</p>
    {result.warnings.length > 0 && <ul aria-label="Simulation warnings">{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
    <details className="disclosure"><summary>Statistics used for this simulation</summary>
      <div className="disclosure-content"><StatisticsSummary statistics={result.statistics} /></div>
    </details>
  </section>;
}

function SelectedGoal({ isin }) {
  const { request } = useAuth();
  const { data: statistics, error: statisticsError, loading, retry } = useReturnStatistics(isin);
  const [inputs, setInputs] = useState(() => ({ goal: '100000', capital: '10000', date: monthlyAnniversary(new Date(), 120), reliability: '90' }));
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const controller = useRef(null);
  const signature = JSON.stringify([isin, inputs]);
  const eligible = statistics?.eligible && statistics.price_currency === 'EUR' && statistics.return_count >= 60;
  const now = new Date();

  useEffect(() => () => { sequence.current += 1; controller.current?.abort(); }, [isin, request]);

  function invalidate() {
    sequence.current += 1;
    controller.current?.abort();
    setResult(null);
    setBusy(false);
    setError(null);
  }

  function change(field, value) {
    invalidate();
    setInputs((previous) => ({ ...previous, [field]: value }));
  }

  async function simulate(event) {
    event.preventDefault();
    if (busy || !eligible || loading || statisticsError) return;
    invalidate();
    const validationError = validateInputs(inputs);
    if (validationError) { setError(validationError); return; }
    const current = ++sequence.current;
    const abort = new AbortController();
    controller.current = abort;
    const body = { instrument_id: isin, goal_eur: normalizeEuros(inputs.goal), starting_capital_eur: normalizeEuros(inputs.capital),
      goal_date: inputs.date, reliability: Number(inputs.reliability) / 100 };
    setBusy(true);
    try {
      const data = await request('/goals/simulate', { method: 'POST', body, signal: abort.signal });
      if (abort.signal.aborted || sequence.current !== current) return;
      if (!validResult(data, body)) throw new Error('Simulation returned an unexpected response.');
      setResult({ signature, data });
    } catch (error) {
      if (abort.signal.aborted || sequence.current !== current) return;
      setError(error.message || 'Simulation failed. Please try again.');
    } finally {
      if (!abort.signal.aborted && sequence.current === current) setBusy(false);
    }
  }

  return <>
    <section aria-label="Selected goal instrument">
      <p className="eyebrow">{isin}</p>
      <h3>{displayText(statistics?.name) || 'Selected ETF'}</h3>
      {statistics?.listing && <p>{statistics.listing.symbol} / {statistics.listing.exchange}</p>}
      <p><a href={`#/instruments/${encodeURIComponent(isin)}`}>Instrument details and data</a></p>
      {loading && <p role="status">Checking saved data and EUR eligibility…</p>}
      {statisticsError && <p role="alert">Could not load statistics: {statisticsError}</p>}
      {(statistics || statisticsError) && <button className="secondary" onClick={() => { invalidate(); retry(); }}>{statisticsError ? 'Retry statistics' : 'Refresh statistics'}</button>}
      {statistics && <>
        {eligible ? <p role="status">Eligible for EUR goal planning · {statistics.return_count} daily return observations.</p>
          : <div className="empty-state" role="status">
            <p><strong>Not eligible for goal planning.</strong> {statistics.ineligible_reason || 'EUR prices and at least 60 return observations are required.'}</p>
            <p>Open instrument details to explicitly download history or set the correct listing price currency when it is missing. At least 60 return observations and EUR-traded prices are required; fund currency alone is not sufficient. A non-EUR listing is not converted to EUR by this planner.</p>
          </div>}
        <details className="disclosure"><summary>Saved return statistics</summary><div className="disclosure-content"><StatisticsSummary statistics={statistics} /></div></details>
      </>}
    </section>
    <form aria-label="Goal planner inputs" className="goal-form" noValidate onSubmit={simulate} aria-describedby="goal-schedule">
      <div className="goal-fields">
        <div><label htmlFor="goal-eur">Target (EUR)</label><input id="goal-eur" inputMode="decimal" value={inputs.goal} required maxLength={100}
          onChange={(event) => change('goal', event.target.value)} /></div>
        <div><label htmlFor="starting-capital-eur">Starting capital (EUR)</label><input id="starting-capital-eur" inputMode="decimal" value={inputs.capital} required maxLength={100}
          onChange={(event) => change('capital', event.target.value)} /></div>
        <div><label htmlFor="goal-date">Goal date (UTC)</label><input id="goal-date" type="date" value={inputs.date} required
          min={monthlyAnniversary(now, 1)} max={monthlyAnniversary(now, 600)} onChange={(event) => change('date', event.target.value)} /></div>
        <div><label htmlFor="goal-reliability">Reliability target (%)</label><input id="goal-reliability" type="number" min="50" max="99" step="any" value={inputs.reliability} required
          onChange={(event) => change('reliability', event.target.value)} /></div>
      </div>
      <p id="goal-schedule" className="muted">Solve a fixed monthly payment above starting capital. Goal dates: {monthlyAnniversary(now, 1)} through {monthlyAnniversary(now, 600)} (UTC); the backend validates today's UTC date and payment schedule. Reliability must be 50–99%.</p>
      <p className="muted">Reliability is a model-estimated chance of meeting the target, not a promise. All amounts are nominal EUR.</p>
      {error && <p role="alert">{error} You can edit the inputs and try again.</p>}
      <button type="submit" disabled={!eligible || loading || busy}>{busy ? 'Simulating…' : 'Calculate monthly contribution'}</button>
      {busy && <p role="status">Running the model with saved data… Editing any input cancels this result.</p>}
    </form>
    {result?.signature === signature && <SimulationResult result={result.data} />}
  </>;
}

export default function GoalPlanner({ isin, onSelect }) {
  return <article aria-label="Goal planner" className="goal-planner">
    <h2 className="instrument-name">Goal planner</h2>
    <p>Estimate a fixed monthly contribution for one EUR-traded ETF using its saved history.</p>
    <p className="muted">Searching, viewing statistics and simulating make no provider calls. Simulations run only when you choose Calculate.</p>
    <InstrumentSearch key={`search-${isin || 'home'}`} onSelect={onSelect} />
    {isin ? <SelectedGoal key={isin} isin={isin} /> : <p className="empty-state">Select an ETF to check its saved data and eligibility.</p>}
    <Assumptions />
  </article>;
}
