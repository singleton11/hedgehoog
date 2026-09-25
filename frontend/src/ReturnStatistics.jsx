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

export function formatReturn(value) {
  const direction = typeof value === 'number' && Number.isFinite(value) ? Math.sign(value) : 0;
  return {
    className: direction > 0 ? 'return-positive' : direction < 0 ? 'return-negative' : '',
    text: `${direction > 0 ? '+' : ''}${percent(value === 0 ? 0 : value)}`,
  };
}

export const priceBasisLabel = (basis) => ({ split_adjusted_close: 'Split-adjusted close',
  adj_close: 'Adjusted close', close: 'Unadjusted close' }[basis] || 'Unavailable');

export function historicalReturnTitle(statistics) {
  const basis = statistics.historical_year_price_basis === 'split_adjusted_close'
    ? 'Split-adjusted closes; cash distributions are not reinvested'
    : statistics.historical_year_price_basis === 'adj_close'
      ? 'Adjusted closes' : 'Unadjusted closes; splits and distributions may affect the return';
  return percent(statistics.historical_year_return) !== 'Unavailable'
    ? `${statistics.historical_year_start_date} – ${statistics.historical_year_end_date}. ${basis}.`
    : 'Requires comparable, positive endpoint prices covering a full year, with a start quote no more than 7 days before the anniversary.';
}

export function StatisticsSummary({ statistics, variant = 'planner' }) {
  const stats = statistics;
  const historicalReturn = formatReturn(stats.historical_year_return);
  const annualReturn = variant === 'instrument'
    ? formatReturn(stats.annualized_mean_log_return) : { text: percent(stats.annualized_mean_log_return) };
  const warnings = variant === 'instrument'
    ? stats.warnings.filter((warning) => !/^\d+ interval\(s\) spanning missing weekdays excluded from daily returns\./.test(warning))
    : stats.warnings;
  return <div className="return-statistics">
    <dl className="facts">
      {variant === 'instrument' ? <div>
        <dt title="Actual trailing-12-month price change ending at the latest saved quote">Historical annual return</dt>
        <dd className={historicalReturn.className} title={historicalReturnTitle(stats)}>{historicalReturn.text}</dd>
      </div> : <>
        <div><dt title="Average daily log return">Daily return</dt><dd>{percent(stats.daily_mean_log_return)}</dd></div>
        <div><dt title="Square root of daily log-return variance">Daily volatility</dt><dd>{percent(stats.daily_volatility)}</dd></div>
      </>}
      <div><dt title="Average daily log return × 252 trading days">Annual return</dt><dd className={annualReturn.className}>{annualReturn.text}</dd></div>
      <div><dt title="Daily volatility × √252 trading days">Annual volatility</dt><dd>{percent(stats.annualized_volatility)}</dd></div>
      <div><dt>Saved quotes / usable prices / returns</dt><dd>{stats.quote_count} / {stats.price_count} / {stats.return_count}</dd></div>
      <div><dt>History window</dt><dd>{stats.first_date && stats.last_date ? `${stats.first_date} – ${stats.last_date}` : 'No usable history'}</dd></div>
      <div><dt>Price basis</dt><dd>{priceBasisLabel(stats.price_basis)}</dd></div>
      <div><dt>Listing price currency</dt><dd>{stats.price_currency || 'Unknown'}{stats.currency_source && ` (${stats.currency_source})`}</dd></div>
      <div><dt>Saved-data snapshot (UTC)</dt><dd>{stats.as_of || 'Unavailable'}</dd></div>
    </dl>
    {stats.price_basis === 'close' && <p className="muted">Unadjusted prices: splits and distributions may affect returns.</p>}
    {warnings.length > 0 && <ul aria-label="Statistics warnings">{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </div>;
}

export default function ReturnStatistics({ isin, refreshKey }) {
  const { data, error, loading, retry } = useReturnStatistics(isin, refreshKey);
  return <section aria-label="Return statistics">
    <h2>Return statistics</h2>
    {loading && <p role="status">Loading return statistics…</p>}
    {error && <div><p>Return statistics unavailable: {error}</p><button className="secondary" onClick={retry}>Retry statistics</button></div>}
    {data && <>
      <StatisticsSummary statistics={data} variant="instrument" />
      {!data.eligible && <p>Not eligible for goal planning: {data.ineligible_reason || 'EUR prices and at least 60 return observations are required.'}</p>}
    </>}
  </section>;
}
