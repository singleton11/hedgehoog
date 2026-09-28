import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import CalibrationChart from './CalibrationChart.jsx';
import { formatEuros } from './goalProjection.js';
import { useGoalStrategies } from './goalStrategies.js';
import { percent, priceBasisLabel } from './ReturnStatistics.jsx';
import StrategyParameters, { supportsParameter } from './StrategyParameters.jsx';
import { calibrationRequest, DEFAULT_CALIBRATION_INPUTS, savedCalibrationInputs, validCalibrationHistory, validCalibrationResult, validCalibrationRunSummary } from './strategyCalibration.js';

const PAGE_SIZE = 20;
const calibrationError = (value) => `${(value * 100).toFixed(1)} pp`;
const runRoute = (id) => `#/calibration/${encodeURIComponent(id)}`;
function CalculatedTime({ value }) {
  return <time dateTime={value}>{new Date(value).toLocaleString('en-GB', { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short' })} UTC</time>;
}
function RunParameters({ selection }) {
  return Object.entries(selection.parameters).map(([key, value], index) => <span key={key}>
    {index > 0 && '; '}{key.replaceAll('_', ' ')}: {value}
  </span>);
}

function CalibrationHistory({ request }) {
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState(null);
  useEffect(() => {
    const abort = new AbortController();
    async function load() {
      try {
        const data = await request(`/goals/calibrations?limit=${PAGE_SIZE}&offset=${offset}`, { signal: abort.signal });
        if (abort.signal.aborted) return;
        if (!validCalibrationHistory(data, PAGE_SIZE, offset)) throw new Error('The backend returned unexpected calibration history.');
        setState({ request, offset, attempt, data });
      } catch (error) {
        if (!abort.signal.aborted) setState({ request, offset, attempt, error: error.message || 'History is unavailable.' });
      }
    }
    load();
    return () => abort.abort();
  }, [request, offset, attempt]);
  const current = state?.request === request && state.offset === offset && state.attempt === attempt;
  const data = current ? state.data : null;
  return <>
    <div className="section-heading"><h2 className="instrument-name">Calibration</h2>
      <div className="calibration-actions"><a className="button-link" href="#/calibration/new">New run</a></div></div>
    <p className="muted">Your saved calibration runs, newest first. Each run keeps its parameters, scores and report.</p>
    {!current ? <p role="status">Loading calibration history…</p> : state.error ? <>
      <p role="alert">Could not load calibration history: {state.error}</p>
      <button type="button" className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry history</button>
    </> : data.total === 0 ? <p className="empty-state">No calibration runs yet. Choose New run to calculate and save your first report.</p> : <>
      <div className="calibration-table-scroll" role="region" aria-label="Calibration history" tabIndex={0}>
        <table className="calibration-table calibration-history"><caption>Calibration runs</caption>
          <thead><tr>{['Run date', 'Strategy', 'Parameters', 'Brier score', 'Calibration error', 'Scored scenarios'].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
          <tbody>{data.items.map((run) => <tr key={run.id}>
            <th scope="row"><a href={runRoute(run.id)}><CalculatedTime value={run.calculated_at} /></a></th>
            <td>{run.strategy_name}</td><td><RunParameters selection={run.strategy} /></td>
            <td>{run.brier_score.toFixed(4)}</td><td>{calibrationError(run.calibration_error)}</td><td>{run.sample_count.toLocaleString('en')}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <nav aria-label="Calibration history pagination">
        <button type="button" className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
        <span aria-live="polite"> {data.items.length ? offset + 1 : 0}–{offset + data.items.length} of {data.total.toLocaleString('en')} </span>
        <button type="button" className="secondary" disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
      </nav>
    </>}
  </>;
}

function AuditTable({ caption, rows, headings, renderRow }) {
  const [page, setPage] = useState(0);
  const start = page * 50;
  return <section aria-label={caption}>
    <div className="calibration-table-scroll" tabIndex={0} role="region" aria-label={`${caption} table`}>
      <table className="calibration-table calibration-audit"><caption>{caption}</caption>
        <thead><tr>{headings.map((heading) => <th scope="col" key={heading}>{heading}</th>)}</tr></thead>
        <tbody>{rows.slice(start, start + 50).map(renderRow)}</tbody>
      </table>
    </div>
    <nav aria-label={`${caption} pagination`}>
      <button type="button" className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
      <span aria-live="polite"> {start + 1}–{Math.min(start + 50, rows.length)} of {rows.length.toLocaleString('en')} </span>
      <button type="button" className="secondary" disabled={start + 50 >= rows.length} onClick={() => setPage(page + 1)}>Next</button>
    </nav>
  </section>;
}

function CalibrationResult({ result }) {
  const settings = result.settings;
  return <section className="calibration-result" aria-label="Calibration report">
    <div className="section-heading"><h3>{result.strategy_name}</h3><a className="button-link" href={`#/calibration/new?from=${encodeURIComponent(result.id)}`}>Run again</a></div>
    <p><RunParameters selection={result.strategy} /> · <CalculatedTime value={result.calculated_at} /></p>
    <p className="muted">{settings.scenario_count.toLocaleString('en')} scenarios · {settings.min_horizon_months}–{settings.max_horizon_months} month horizons · {settings.lookback_months} month lookback · Seed {result.seed}<br />
      Origins: {settings.start_date || 'All available'} – {settings.end_date || 'All available'}
    </p>
    <dl className="facts calibration-metrics">
      <div><dt>Brier score</dt><dd>{result.brier_score.toFixed(4)}</dd></div>
      <div><dt>Calibration error</dt><dd>{calibrationError(result.calibration_error)}</dd></div>
      <div><dt>Scored scenarios</dt><dd>{result.sample_count.toLocaleString('en')}</dd></div>
      <div><dt>Historical origins</dt><dd>{result.origin_count.toLocaleString('en')}</dd></div>
    </dl>
    <CalibrationChart bins={result.calibration_bins} countLabel="scenarios" />
    <details className="disclosure"><summary>Calculation audit</summary><div className="disclosure-content">
      <dl className="facts">
        <div><dt>Run ID</dt><dd>{result.id}</dd></div>
        <div><dt>Strategy ID</dt><dd>{result.strategy.id}</dd></div>
        <div><dt>Mean predicted probability</dt><dd>{percent(result.mean_predicted_probability)}</dd></div>
        <div><dt>Observed success rate</dt><dd>{percent(result.observed_success_rate)}</dd></div>
        <div><dt>Monte Carlo paths per scenario</dt><dd>{result.simulation_paths.toLocaleString('en')}</dd></div>
        <div><dt>Available monthly origins</dt><dd>{result.available_origin_count.toLocaleString('en')}</dd></div>
        <div><dt>Saved history</dt><dd>{result.history_first_date} – {result.history_last_date}</dd></div>
        <div><dt>Data snapshot (UTC)</dt><dd>{result.data_as_of}</dd></div>
        <div><dt>Target-generation paths per scenario</dt><dd>{result.generation.target_simulation_paths.toLocaleString('en')}</dd></div>
        <div><dt>History fingerprint</dt><dd>{result.history_fingerprint}</dd></div>
      </dl>
      {Object.keys(result.skipped_counts).length > 0 && <ul aria-label="Skipped scenario counts">
        {Object.entries(result.skipped_counts).map(([reason, count]) => <li key={reason}>{reason}: {count}</li>)}
      </ul>}
      <AuditTable caption="Per-scenario audit" rows={result.forecasts}
        headings={['Scenario ID', 'Target (EUR)', 'Starting capital (EUR)', 'Monthly contribution (EUR)', 'Horizon (months)', 'Requested probability',
          'Forecast origin', 'Goal date', 'Training start', 'Training end', 'Training returns', 'Price basis', 'Actual predicted probability', 'Observed success', 'Terminal value (EUR)', 'Valuation date']}
        renderRow={(forecast) => <tr key={forecast.scenario_id}>
          <th scope="row">{forecast.scenario_id}</th><td>{formatEuros(forecast.goal_eur)}</td><td>{formatEuros(forecast.starting_capital_eur)}</td>
          <td>{formatEuros(forecast.monthly_contribution_eur)}</td><td>{forecast.horizon_months}</td><td>{percent(forecast.requested_probability)}</td>
          <td>{forecast.origin_date}</td><td>{forecast.goal_date}</td><td>{forecast.training_start_date}</td><td>{forecast.training_end_date}</td>
          <td>{forecast.training_return_count}</td><td>{priceBasisLabel(forecast.price_basis)}</td>
          <td>{percent(forecast.predicted_probability)}</td><td>{forecast.observed_success ? 'Yes' : 'No'}</td>
          <td>{formatEuros(forecast.terminal_value_eur)}</td><td>{forecast.valuation_date}</td>
        </tr>} />
      {result.excluded_origins.length > 0 && <AuditTable caption="Excluded scenarios" rows={result.excluded_origins}
        headings={['Scenario ID', 'Forecast origin', 'Reason']}
        renderRow={(origin) => <tr key={origin.scenario_id}><th scope="row">{origin.scenario_id}</th><td>{origin.origin_date}</td><td>{origin.reason}</td></tr>} />}
    </div></details>
  </section>;
}

function useSavedRun(request, id) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState(null);
  useEffect(() => {
    if (!id) return;
    const abort = new AbortController();
    async function load() {
      try {
        const data = await request(`/goals/calibrations/${encodeURIComponent(id)}`, { signal: abort.signal });
        if (abort.signal.aborted) return;
        if (data?.result?.id !== id || !validCalibrationRunSummary(data.result) || !validCalibrationResult(data.result)) {
          throw new Error('The backend returned an unexpected saved run.');
        }
        setState({ request, id, attempt, result: data.result });
      } catch (error) {
        if (!abort.signal.aborted) setState({ request, id, attempt, error: error.message || 'Saved run is unavailable.' });
      }
    }
    load();
    return () => abort.abort();
  }, [request, id, attempt]);
  const current = state?.request === request && state.id === id && state.attempt === attempt;
  return { result: current ? state.result : null, error: current ? state.error : null,
    loading: Boolean(id) && !current, retry: () => setAttempt((value) => value + 1) };
}
function RunLoadError({ error, retry }) {
  return <><p role="alert">Could not load calibration run: {error}</p>
    <button type="button" className="secondary" onClick={retry}>Retry run</button></>;
}
function SavedRun({ runId, request }) {
  const { result, loading, error, retry } = useSavedRun(request, runId);
  return <><h2 className="instrument-name">Calibration report</h2>
    {loading ? <p role="status">Loading calibration run…</p> : error ? <RunLoadError error={error} retry={retry} /> : result && <CalibrationResult result={result} />}
  </>;
}

function CalibrationForm({ strategies, instruments, request, source, initialStrategyId }) {
  const [strategyId, setStrategyId] = useState(source?.strategy.id || initialStrategyId || strategies.find((item) => item.supports_calibration)?.id || strategies[0]?.id || '');
  const [parameters, setParameters] = useState(source?.strategy.parameters || {});
  const [readiness, setReadiness] = useState({});
  const [inputs, setInputs] = useState(source ? savedCalibrationInputs(source) : DEFAULT_CALIBRATION_INPUTS);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const controller = useRef(null);
  const strategy = strategies.find((item) => item.id === strategyId);
  const eligible = strategy?.supports_calibration === true && strategy.parameters.every((parameter) => {
    const state = readiness[`${strategy.id}:${parameter.key}`];
    return supportsParameter(parameter) && state?.value === (parameters[parameter.key] || '') && state.ready;
  });
  useEffect(() => () => { sequence.current += 1; controller.current?.abort(); }, []);
  const invalidate = useCallback(() => {
    sequence.current += 1;
    controller.current?.abort();
    setBusy(false);
    setError(null);
  }, []);
  const onReadiness = useCallback((key, value, ready) => {
    setReadiness((previous) => ({ ...previous, [key]: { value, ready } }));
  }, []);
  function change(field, value) {
    invalidate();
    setInputs((previous) => ({ ...previous, [field]: value }));
  }
  function changeParameter(key, value) {
    invalidate();
    setParameters((previous) => ({ ...previous, [key]: value }));
  }
  function generateSeed() {
    invalidate();
    try {
      const seed = String(crypto.getRandomValues(new Uint32Array(1))[0]);
      setInputs((previous) => ({ ...previous, seed }));
    } catch { setError('Could not generate a seed. Enter one or leave it blank.'); }
  }
  async function calculate(event) {
    event.preventDefault();
    if (busy || !eligible) return;
    invalidate();
    let body;
    try { body = calibrationRequest(inputs, { id: strategy.id, parameters }); }
    catch (error) { setError(error.message); return; }
    const current = ++sequence.current;
    const abort = new AbortController();
    controller.current = abort;
    setBusy(true);
    try {
      const data = await request('/goals/calibrate', { method: 'POST', body, signal: abort.signal });
      if (abort.signal.aborted || sequence.current !== current) return;
      if (!validCalibrationRunSummary(data) || !validCalibrationResult(data, body, strategy)) throw new Error('Calibration returned an unexpected response. Check history before trying again.');
      window.location.hash = runRoute(data.id);
    } catch (error) {
      if (!abort.signal.aborted && sequence.current === current) setError(error.message || 'Calibration failed. Please try again.');
    } finally {
      if (!abort.signal.aborted && sequence.current === current) setBusy(false);
    }
  }
  return <>
    {source ? <p className="muted">Settings copied from a <a href={runRoute(source.id)}>saved run</a>. You can adjust them below; the original report will not change.</p>
      : <p className="muted">Choose a strategy and parameters. Each successful calculation is saved as a separate run.</p>}
    <form className="goal-form calibration-form" aria-label="Calibration inputs" noValidate onSubmit={calculate}>
      <fieldset disabled={busy}>
        <div><label htmlFor="calibration-strategy">Strategy</label><select id="calibration-strategy" value={strategy ? strategyId : ''}
          onChange={(event) => { invalidate(); setStrategyId(event.target.value); setParameters({}); setReadiness({}); }}>
          {!strategy && <option value="">Choose a strategy</option>}
          {strategies.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></div>
        {!strategy ? <p role="alert">This strategy is no longer available. Choose another strategy to start a new run.</p>
          : <>
            {strategy.supports_calibration !== true && <p role="status">Calibration is not available for this strategy.</p>}
            <StrategyParameters strategy={strategy} parameters={parameters} instruments={instruments} requireStatistics={false} compact
              onChange={changeParameter} onReadiness={onReadiness} onInvalidate={invalidate} />
          </>}
        <div className="goal-fields">
          <div><label htmlFor="calibration-count">Number of scenarios</label>
            <input id="calibration-count" type="number" min="10" max="5000" step="1" required value={inputs.scenarioCount} onChange={(event) => change('scenarioCount', event.target.value)} />
          </div>
          <div><label htmlFor="calibration-seed">Seed (optional)</label>
            <div className="calibration-seed"><input id="calibration-seed" inputMode="numeric" value={inputs.seed} maxLength={100}
              placeholder="Server generated" onChange={(event) => change('seed', event.target.value)} />
              <button type="button" className="secondary" onClick={generateSeed}>Generate seed</button></div>
          </div>
        </div>
        <details className="disclosure"><summary>Settings</summary><div className="disclosure-content goal-fields">
          {[['minHorizon', 'Minimum horizon (months)', 1], ['maxHorizon', 'Maximum horizon (months)', 1], ['lookback', 'Training lookback (months)', 3]].map(([key, label, min]) => <div key={key}>
            <label htmlFor={`calibration-${key}`}>{label}</label><input id={`calibration-${key}`} type="number" min={min} max="120" step="1" value={inputs[key]} required onChange={(event) => change(key, event.target.value)} />
          </div>)}
          {['start', 'end'].map((key) => <div key={key}>
            <label htmlFor={`calibration-${key}`}>Forecast-origin {key} date (optional)</label><input id={`calibration-${key}`} type="date" value={inputs[key]} onChange={(event) => change(key, event.target.value)} />
          </div>)}
        </div></details>
      </fieldset>
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">Calculating… If you leave this page, the run may still finish and appear in history.</p>}
      <button type="submit" disabled={!eligible || busy}>{busy ? 'Calculating…' : 'Run calibration'}</button>
    </form>
  </>;
}
function NewRun({ request, sourceId, initialStrategyId }) {
  const { strategies, instruments, loading, error, retry } = useGoalStrategies();
  const saved = useSavedRun(request, sourceId);
  return <><h2 className="instrument-name">New calibration run</h2>
    {loading || saved.loading ? <p role="status">Loading calibration settings…</p> : saved.error ? <RunLoadError error={saved.error} retry={saved.retry} />
      : error ? <><p role="alert">Could not load strategies: {error}</p><button type="button" className="secondary" onClick={retry}>Retry strategies</button></>
        : <CalibrationForm strategies={strategies} instruments={instruments} request={request} source={saved.result} initialStrategyId={initialStrategyId} />}
  </>;
}
function CalibrationPage({ newRun, runId, sourceId, strategyId, request }) {
  return <article className="strategy-calibration" aria-label="Calibration">
    {(newRun || runId) && <nav aria-label="Calibration navigation"><a href="#/calibration">← Calibration history</a></nav>}
    {newRun ? <NewRun request={request} sourceId={sourceId} initialStrategyId={strategyId} />
      : runId ? <SavedRun runId={runId} request={request} /> : <CalibrationHistory request={request} />}
  </article>;
}
export default function StrategyCalibration({ newRun = false, runId = null, sourceId = null, strategyId = null }) {
  const { request, user, status } = useAuth();
  // A stable request function must not let reports or pending replies cross users.
  return <CalibrationPage key={JSON.stringify([newRun, runId, sourceId, strategyId, user?.id, status])}
    newRun={newRun} runId={runId} sourceId={sourceId} strategyId={strategyId} request={request} />;
}
