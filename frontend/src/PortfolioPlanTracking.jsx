import { useEffect, useState } from 'react';
import { formatEuros } from './goalProjection.js';

const probability = (value) => Number.isFinite(value) && value >= 0 && value <= 1;
const money = (value) => typeof value === 'string' && /^-?\d+\.\d{2}$/.test(value);
const percent = (value) => `${(value * 100).toFixed(1)}%`;
function timing(data) {
  if (data.forecast_status === 'not_reached') return 'Target probability not reached within 50 years';
  if (data.forecast_status === 'unavailable') return 'Forecast unavailable';
  const months = data.schedule_difference_months;
  if (months === 0) return 'On schedule';
  return `${Math.abs(months).toFixed(1)} months (${Math.abs(data.schedule_difference_years).toFixed(1)} years) ${months < 0 ? 'ahead' : 'late'}`;
}
function valid(data, planId) {
  return data?.plan_id === planId && typeof data.on_track === 'boolean'
    && ['success_probability', 'original_probability', 'target_probability'].every((key) => probability(data[key]))
    && ['probability_change_pp', 'target_gap_pp'].every((key) => Number.isFinite(data[key]))
    && ['equity_eur', 'monthly_contribution_eur', 'required_monthly_contribution_eur', 'monthly_contribution_change_eur', 'goal_eur'].every((key) => money(data[key]))
    && typeof data.strategy_name === 'string' && typeof data.goal_date === 'string'
    && ['reached', 'not_reached', 'unavailable'].includes(data.forecast_status)
    && typeof data.forecast_search_end_date === 'string'
    && (data.forecast_status === 'reached'
      ? typeof data.forecast_goal_date === 'string' && Number.isFinite(data.schedule_difference_months) && Number.isFinite(data.schedule_difference_years)
      : data.forecast_goal_date === null && data.schedule_difference_months === null && data.schedule_difference_years === null)
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
  const ahead = data?.on_track && BigInt(data.required_monthly_contribution_eur.replace('.', '')) < BigInt(data.monthly_contribution_eur.replace('.', ''));
  return <section aria-label="Plan progress">
    <h3>Plan progress</h3>
    {!planId ? <p className="muted">Assign a saved plan to see whether this portfolio is on track.</p>
      : !current ? <p role="status">Calculating plan progress…</p>
        : state.error ? <><p role="alert">{state.error}</p><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry plan progress</button></>
          : <>
            <dl className="facts">
              <div><dt>Plan status</dt><dd className={data.on_track ? 'return-positive' : 'return-negative'}>{ahead ? 'Ahead of plan' : data.on_track ? 'On track' : 'Below target probability'}</dd></div>
              <div><dt>Probability at planned payment</dt><dd>{percent(data.success_probability)}</dd></div>
              <div><dt>Target probability</dt><dd>{percent(data.target_probability)}</dd></div>
            </dl>
            <dl className="facts">
              <div><dt title="First checkpoint reaching the target probability at the original payment, using refreshed estimates and the planned strategy; not a guarantee.">Estimated goal date</dt><dd>{data.forecast_goal_date ?? 'Not available'}</dd></div>
              <div><dt>Time ahead / late</dt><dd>{timing(data)}</dd></div>
            </dl>
            <dl className="facts">
              <div><dt>Planned monthly investment</dt><dd>{formatEuros(data.monthly_contribution_eur)}</dd></div>
              <div><dt>Monthly investment needed to reach target</dt><dd>{formatEuros(data.required_monthly_contribution_eur)}</dd></div>
            </dl>
          </>}
  </section>;
}
