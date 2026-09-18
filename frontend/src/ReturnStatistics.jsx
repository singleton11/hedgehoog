import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';

export function useReturnStatistics(isin, refreshKey = '') {
  const { request } = useAuth();
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState({ key: null, data: null, error: null });
  const sequence = useRef(0);
  const controller = useRef(null);
  const key = JSON.stringify([isin, refreshKey, attempt]);

  useEffect(() => {
    const current = ++sequence.current;
    const abort = new AbortController();
    controller.current = abort;
    if (!isin) return () => abort.abort();
    async function load() {
      try {
        const data = await request(`/instruments/${encodeURIComponent(isin)}/statistics`, { signal: abort.signal });
        if (abort.signal.aborted || sequence.current !== current) return;
        if (data?.instrument_id !== isin || typeof data.eligible !== 'boolean' || !Array.isArray(data.warnings)
          || !Number.isInteger(data.return_count) || !Number.isInteger(data.price_count) || !Number.isInteger(data.quote_count)) {
          throw new Error('Statistics returned an unexpected response.');
        }
        setState({ key, data, error: null });
      } catch (error) {
        if (abort.signal.aborted || sequence.current !== current) return;
        setState({ key, data: null, error: error.message || 'Statistics are unavailable.' });
      }
    }
    load();
    return () => abort.abort();
  }, [isin, key, request]);

  function retry() {
    sequence.current += 1;
    controller.current?.abort();
    setAttempt((value) => value + 1);
  }

  return {
    data: state.key === key ? state.data : null,
    error: state.key === key ? state.error : null,
    loading: Boolean(isin) && state.key !== key,
    retry,
  };
}

export const percent = (value) => typeof value === 'number' && Number.isFinite(value)
  ? new Intl.NumberFormat('en', { style: 'percent', maximumFractionDigits: 2 }).format(value) : 'Unavailable';
const variance = (value) => typeof value === 'number' && Number.isFinite(value)
  ? value.toLocaleString('en', { maximumSignificantDigits: 6, useGrouping: false }) : 'Unavailable';

export function StatisticsSummary({ statistics }) {
  const stats = statistics;
  return <div className="return-statistics">
    <dl className="facts">
      <div><dt>Daily mean simple return</dt><dd>{percent(stats.daily_mean_return)}</dd></div>
      <div><dt>Daily simple-return variance (fraction²)</dt><dd>{variance(stats.daily_return_variance)}</dd></div>
      <div><dt>Annualized volatility</dt><dd>{percent(stats.annualized_volatility)}</dd></div>
      <div><dt>Saved quotes / usable prices / returns</dt><dd>{stats.quote_count} / {stats.price_count} / {stats.return_count}</dd></div>
      <div><dt>History window</dt><dd>{stats.first_date && stats.last_date ? `${stats.first_date} – ${stats.last_date}` : 'No usable history'}</dd></div>
      <div><dt>Price basis</dt><dd>{stats.price_basis === 'adj_close' ? 'Adjusted close' : stats.price_basis === 'close' ? 'Unadjusted close' : 'Unavailable'}</dd></div>
      <div><dt>Listing price currency</dt><dd>{stats.price_currency || 'Unknown'}{stats.currency_source && ` (${stats.currency_source})`}</dd></div>
      <div><dt>Saved-data snapshot (UTC)</dt><dd>{stats.as_of || 'Unavailable'}</dd></div>
    </dl>
    <p className="muted">Mean and variance describe daily simple returns, not annual returns. Variance is in return-fraction squared, not percent.</p>
    {stats.price_basis === 'close' && <p className="muted">A consistent adjusted-close series was unavailable; unadjusted closes may distort returns around distributions or splits.</p>}
    {stats.warnings.length > 0 && <ul aria-label="Statistics warnings">{stats.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </div>;
}

export default function ReturnStatistics({ isin, refreshKey }) {
  const { data, error, loading, retry } = useReturnStatistics(isin, refreshKey);
  return <section aria-label="Return statistics">
    <h2>Return statistics</h2>
    <p className="muted">Computed from saved quotes only; viewing statistics uses no provider quota.</p>
    {loading && <p role="status">Loading return statistics…</p>}
    {error && <div><p>Return statistics unavailable: {error}</p><button className="secondary" onClick={retry}>Retry statistics</button></div>}
    {data && <>
      <StatisticsSummary statistics={data} />
      {!data.eligible && <p>Not eligible for goal planning: {data.ineligible_reason || 'EUR prices and at least 60 return observations are required.'}</p>}
    </>}
  </section>;
}
