import { useEffect, useId, useState } from 'react';

const variants = { unknown: 'Unknown return variant', price: 'Price return', net_total_return: 'Net total return', gross_total_return: 'Gross total return' };
const number = (value) => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)) ? Number(value) : null;
const date = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
function validPerformance(data, portfolioId, benchmarkId) {
  return data?.portfolio_id === portfolioId && data.currency === 'EUR' && data.method === 'daily_twr'
    && data.cash_flow_timing === 'deposits_start_withdrawals_end' && typeof data.as_of === 'string' && Number.isFinite(Date.parse(data.as_of))
    && (data.start_date === null || date(data.start_date)) && (data.end_date === null || date(data.end_date))
    && Array.isArray(data.warnings) && data.warnings.every((item) => typeof item === 'string')
    && (!benchmarkId || (data.benchmark?.instrument_id === benchmarkId && typeof data.benchmark.name === 'string'
      && typeof data.benchmark.symbol === 'string' && (data.benchmark.currency === null || typeof data.benchmark.currency === 'string')
      && Object.hasOwn(variants, data.benchmark.return_variant)))
    && (data.summary === undefined || (data.summary && ['twr_percent', 'sharpe', 'sortino'].every((key) => data.summary[key] === null || number(data.summary[key]) !== null)))
    && Array.isArray(data.points) && data.points.every((point, i) => date(point.date) && (!i || point.date > data.points[i - 1].date)
      && ['sharpe', 'sortino'].every((key) => point[key] == null || number(point[key]) !== null)
      && (point.twr_percent === null || number(point.twr_percent) !== null)
      && (!benchmarkId || point.benchmark_percent === null || number(point.benchmark_percent) !== null));
}

export function PerformanceChart({ points, benchmark = null, ratios = false }) {
  const id = useId();
  const [position, setPosition] = useState(null);
  const index = position === null ? null : Math.min(position, points.length - 1);
  const selected = index === null ? null : points[index];
  const series = ratios ? [{ key: 'sharpe', label: 'Sharpe', className: 'performance-portfolio' },
    { key: 'sortino', label: 'Sortino', className: 'performance-benchmark' }] : [{ key: 'twr_percent', label: 'Portfolio TWR', className: 'performance-portfolio' },
    ...(benchmark ? [{ key: 'benchmark_percent', label: benchmark.name, className: 'performance-benchmark' }] : [])];
  const values = points.flatMap((point) => series.map(({ key }) => number(point[key]))).filter((value) => value !== null);
  // Normalize first to keep SVG arithmetic finite even for very large decimal strings.
  const scale = values.reduce((max, value) => Math.max(max, Math.abs(value)), 1);
  const low = values.reduce((min, value) => Math.min(min, value / scale), 0);
  const high = values.reduce((max, value) => Math.max(max, value / scale), 0);
  const bottom = low === high ? -1 : low, top = low === high ? 1 : high;
  const first = Date.parse(points[0]?.date), last = Date.parse(points.at(-1)?.date);
  const x = (point) => first === last ? 390 : 90 + (Date.parse(point.date) - first) / (last - first) * 600;
  const y = (value) => 220 - (value / scale - bottom) / (top - bottom) * 190;
  const display = (value) => number(value) === null ? 'Unavailable' : `${number(value).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${ratios ? '' : '%'}`;
  function navigate(event) {
    const lastIndex = points.length - 1;
    const current = index ?? lastIndex;
    const next = { ArrowLeft: Math.max(0, current - 1), ArrowRight: Math.min(lastIndex, current + 1), Home: 0, End: lastIndex }[event.key];
    if (next !== undefined) { event.preventDefault(); setPosition(next); }
  }
  if (!points.length) return <p>No performance history yet.</p>;
  return <div className="performance-chart">
    <ul className="performance-legend" aria-label={ratios ? 'Risk ratio legend' : 'Chart legend'}>{series.map((item) => <li key={item.key} className={item.className}
      title={item.key === 'benchmark_percent' ? `${benchmark.currency || 'Unknown currency'} · ${variants[benchmark.return_variant]} · No FX conversion` : ratios ? 'Since-inception annualized log excess-return ratio' : 'Cumulative daily TWR, including fees'}>{item.label}</li>)}</ul>
    {!values.length && <p>No valid performance values are available for this period.</p>}
    <div className="performance-plot" onMouseLeave={() => setPosition(null)}>
    <div className="chart-scroll" role="region" aria-label={ratios ? 'Scrollable risk ratios chart' : 'Scrollable performance chart'} tabIndex={0}>
    <svg viewBox="0 0 720 260" role="img" tabIndex={0} aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}
      onFocus={() => setPosition(points.length - 1)} onBlur={() => setPosition(null)} onKeyDown={navigate}>
      <title id={`${id}-title`}>{ratios ? 'Sharpe and Sortino' : 'Cumulative performance (%)'}</title>
      <desc id={`${id}-description`}>Gaps indicate unavailable values. Hover or tap a point for values. With the chart focused, use Left and Right to browse dates, or Home and End.</desc>
      {[bottom, (bottom + top) / 2, top, ...(bottom < 0 && top > 0 ? [0] : [])].map((tick, i) => <g key={i}>
        <line x1="90" x2="690" y1={y(tick * scale)} y2={y(tick * scale)} className="performance-grid" />
        <text x="82" y={y(tick * scale) + 4} textAnchor="end">{(tick * scale).toLocaleString('en', { maximumFractionDigits: 2, notation: Math.abs(tick * scale) >= 100000 ? 'scientific' : 'standard' })}{ratios ? '' : '%'}</text>
      </g>)}
      <text x="90" y="248">{points[0].date}</text><text x="690" y="248" textAnchor="end">{points.at(-1).date}</text>
      {series.map(({ key, className, label }) => {
        let drawing = false;
        const path = points.map((point) => {
          const value = number(point[key]);
          if (value === null) { drawing = false; return ''; }
          const command = drawing ? 'L' : 'M'; drawing = true;
          return `${command}${x(point)},${y(value)}`;
        }).join(' ');
        return <g key={key} className={className}><path data-testid={`${key}-line`} d={path} fill="none" stroke="currentColor" strokeWidth="2" />
          {points.map((point, i) => number(point[key]) !== null && <circle key={point.date} cx={x(point)} cy={y(number(point[key]))} r={i === index ? '5' : '3'} fill="currentColor" onMouseEnter={() => setPosition(i)} onClick={() => setPosition(i)}>
            <title>{point.date}: {label} {display(point[key])}</title></circle>)}</g>;
      })}
    </svg>
    </div>
    {selected && <div className="performance-tooltip" role="status" aria-label={ratios ? 'Risk ratio values' : 'Performance values'}>
      <strong>{selected.date}</strong>{series.map(({ key, label, className }) => <span key={key} className={className}>{label}: {display(selected[key])}</span>)}
    </div>}
    </div>
  </div>;
}

export default function PortfolioPerformance({ request, portfolioPath, portfolioId, revision, onSummary }) {
  const id = useId();
  const [selection, setSelection] = useState({ request, portfolioPath, portfolioId, value: '' });
  const same = selection.request === request && selection.portfolioPath === portfolioPath && selection.portfolioId === portfolioId;
  const benchmarkId = same ? selection.value : '';
  if (!same) setSelection({ request, portfolioPath, portfolioId, value: '' });
  const [attempt, setAttempt] = useState(0), [indicesAttempt, setIndicesAttempt] = useState(0);
  const [result, setResult] = useState(null), [indices, setIndices] = useState(null);
  const key = JSON.stringify([portfolioPath, portfolioId, revision, benchmarkId, attempt]);
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const data = await request(`${portfolioPath}/performance${benchmarkId ? `?benchmark_id=${encodeURIComponent(benchmarkId)}` : ''}`, { signal: controller.signal });
        if (!validPerformance(data, portfolioId, benchmarkId)) throw new Error('Unexpected performance data. Please retry.');
        if (!controller.signal.aborted) setResult({ request, key, data });
      } catch (error) {
        if (!controller.signal.aborted) setResult({ request, key, error: error.message || 'Unable to load performance.' });
      }
    }
    load();
    return () => controller.abort();
  }, [request, portfolioPath, portfolioId, revision, benchmarkId, attempt, key]);
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const items = [], seen = new Set();
        let offset = 0, total;
        do {
          const page = await request(`/indices?limit=100&offset=${offset}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          if (!Array.isArray(page?.items) || !Number.isInteger(page.total) || page.total < 0 || page.offset !== offset || page.limit !== 100
            || (total !== undefined && total !== page.total) || page.items.length > 100 || page.items.length > Math.max(0, page.total - offset)
            || (offset < page.total && !page.items.length)) throw new Error('Could not load the complete index list. Please retry.');
          total = page.total;
          for (const item of page.items) {
            if (typeof item?.instrument_id !== 'string' || !item.instrument_id || typeof item.name !== 'string' || typeof item.symbol !== 'string'
              || !Number.isInteger(item.summary?.quote_count) || item.summary.quote_count < 0 || seen.has(item.instrument_id)) throw new Error('Unexpected or duplicate index data. Please retry.');
            seen.add(item.instrument_id); items.push(item);
          }
          offset += page.items.length;
        } while (offset < total);
        if (!controller.signal.aborted) setIndices({ request, portfolioPath, indicesAttempt, items });
      } catch (error) {
        if (!controller.signal.aborted) setIndices({ request, portfolioPath, indicesAttempt, error: error.message || 'Unable to load indices.' });
      }
    }
    load();
    return () => controller.abort();
  }, [request, portfolioPath, indicesAttempt]);
  const current = result?.request === request && result.key === key ? result : null;
  const choices = indices?.request === request && indices.portfolioPath === portfolioPath && indices.indicesAttempt === indicesAttempt ? indices : null;
  const data = current?.data;
  const benchmark = benchmarkId ? data?.benchmark : null;
  useEffect(() => {
    onSummary?.({ request, portfolioPath, revision, summary: data?.summary });
  }, [onSummary, request, portfolioPath, revision, data]);
  return <section className="portfolio-performance" aria-label="Portfolio performance">
    <h2>Performance</h2>
    <label htmlFor={`${id}-benchmark`}>Index benchmark</label>
    <select id={`${id}-benchmark`} value={benchmarkId} onChange={(event) => setSelection({ request, portfolioPath, portfolioId, value: event.target.value })}>
      <option value="">No benchmark</option>
      {benchmarkId && !choices?.items?.some((item) => item.instrument_id === benchmarkId) && <option value={benchmarkId}>Selected index (unavailable)</option>}
      {choices?.items?.filter((item) => item.active !== false).map((item) => <option key={item.instrument_id} value={item.instrument_id} disabled={item.summary.quote_count === 0}>
        {item.name} ({item.symbol}){item.summary.quote_count === 0 ? ' — download required' : ''}</option>)}
    </select>
    {choices?.items && (!choices.items.length || choices.items.some((item) => item.summary.quote_count === 0)) && <a className="performance-indices-link" href="#/indices">Manage indices</a>}
    {!choices && <p role="status">Loading indices…</p>}
    {choices?.error && <div><p role="alert">{choices.error}</p><button type="button" onClick={() => setIndicesAttempt((value) => value + 1)}>Retry indices</button></div>}
    {!current && <p role="status">Loading performance…</p>}
    {current?.error && <div><p role="alert">{current.error}</p><button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry performance</button></div>}
    {data && <>
      {data.warnings.map((warning, i) => <p key={i} role="status">{warning}</p>)}
      <PerformanceChart key={key} points={data.points} benchmark={benchmark} />
      <h3>Sharpe and Sortino</h3>
      <p title="Annualized daily log excess returns (365 calendar days). Both ratios use saved EUR proxy price returns, without distribution reinvestment. Sharpe uses sample standard deviation; Sortino uses downside deviation over all funded days. Fewer than two observations or zero deviation is unavailable.">Since inception · Proxy: {data.risk_proxy ? `${data.risk_proxy.name} (${data.risk_proxy.symbol})` : 'Not configured'}. <a href="#/settings">Settings</a></p>
      <PerformanceChart key={`${key}-ratios`} points={data.points} ratios />
    </>}
  </section>;
}
