import { useEffect, useState } from 'react';
import { formatEuros } from './goalProjection.js';

const probability = (value) => Number.isFinite(value) && value >= 0 && value <= 1;
const money = (value) => typeof value === 'string' && /^-?\d+\.\d{2}$/.test(value);
const percent = (value) => `${(value * 100).toFixed(1)}%`;
const signed = (value) => `${value >= 0 ? '+' : ''}${value.toFixed(1)} pp`;
function valid(data, planId) {
  return data?.plan_id === planId && typeof data.on_track === 'boolean'
    && ['success_probability', 'original_probability', 'target_probability'].every((key) => probability(data[key]))
    && ['probability_change_pp', 'target_gap_pp'].every((key) => Number.isFinite(data[key]))
    && ['equity_eur', 'monthly_contribution_eur', 'required_monthly_contribution_eur', 'monthly_contribution_change_eur', 'goal_eur'].every((key) => money(data[key]))
    && typeof data.strategy_name === 'string' && typeof data.goal_date === 'string'
    && Array.isArray(data.warnings) && data.warnings.every((value) => typeof value === 'string');
}
export default function PortfolioPlanTracking({ request, portfolioPath, planId, revision }) {
  const [state, setState] = useState(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    if (!planId) return () => abort.abort();
    request(`${portfolioPath}/plan-tracking`, { signal: abort.signal }).then((data) => {
      if (!valid(data, planId)) throw new Error('Unexpected plan tracking response.');
      if (!abort.signal.aborted) setState({ request, portfolioPath, planId, revision, attempt, data });
    }).catch((error) => {
      if (!abort.signal.aborted) setState({ request, portfolioPath, planId, revision, attempt, error: error.message || 'Unable to calculate plan progress.' });
    });
    return () => abort.abort();
  }, [request, portfolioPath, planId, revision, attempt]);
  const current = state?.request === request && state.portfolioPath === portfolioPath && state.planId === planId && state.revision === revision && state.attempt === attempt;
  const data = current ? state.data : null;
  return <section className="planner-create" aria-label="Plan progress">
    <h3>Plan progress</h3>
    {!planId ? <p className="muted">Assign a saved plan to see whether this portfolio is on track.</p>
      : !current ? <p role="status">Calculating plan progress…</p>
        : state.error ? <><p role="alert">{state.error}</p><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry plan progress</button></>
          : <>
            <h4 className={data.on_track ? 'return-positive' : 'return-negative'}>{data.on_track ? 'On track' : 'Below target probability'}</h4>
            <p>{data.strategy_name} · Goal: {formatEuros(data.goal_eur)} by {data.goal_date}</p>
            <dl>
              <dt>Probability at the planned {formatEuros(data.monthly_contribution_eur)}/month</dt><dd>{percent(data.success_probability)}</dd>
              <dt>Change from saved plan ({percent(data.original_probability)})</dt><dd>{signed(data.probability_change_pp)}</dd>
              <dt>Target probability</dt><dd>{percent(data.target_probability)} ({signed(data.target_gap_pp)} gap)</dd>
              <dt>Monthly investment needed to reach target</dt><dd>{formatEuros(data.required_monthly_contribution_eur)}</dd>
              <dt>Monthly change from plan</dt><dd>{formatEuros(data.monthly_contribution_change_eur)}</dd>
            </dl>
            <p className="muted">From today's portfolio value of {formatEuros(data.equity_eur)} (cash + holdings), using refreshed historical estimates and the saved strategy, simulation path count and seed. Assumes all capital follows that strategy from now on, with payments starting one month from today. Uses latest saved prices, not live quotes. Model estimates are not guarantees.</p>
            <details><summary>Model and valuation warnings</summary><ul>{data.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>
          </>}
  </section>;
}
