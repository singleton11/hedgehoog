import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PortfolioPerformance, { PerformanceChart } from './PortfolioPerformance.jsx';

const path = '/customers/c/portfolios/p';
const index = (id = 'idx-one', extra = {}) => ({ instrument_id: id, name: id, symbol: '^TEST', currency: 'USD', return_variant: 'unknown', summary: { quote_count: 10 }, ...extra });
const points = [
  { date: '2026-09-20', twr_percent: '0.00', benchmark_percent: '0.00' },
  { date: '2026-09-21', twr_percent: '-2.50', benchmark_percent: '1.20' },
  { date: '2026-09-22', twr_percent: '10.00', benchmark_percent: '4.00' },
];
const performance = (benchmarkId = '', extra = {}) => ({ portfolio_id: 'p', currency: 'EUR', method: 'daily_twr', cash_flow_timing: 'deposits_start_withdrawals_end', as_of: '2026-09-22T12:00:00Z', start_date: points[0].date, end_date: points.at(-1).date, points, benchmark: benchmarkId ? index(benchmarkId) : null, warnings: [], ...extra });
function server(items = [index()]) {
  return vi.fn(async (url) => {
    if (url.startsWith('/indices?')) {
      const offset = Number(new URL(url, 'http://local').searchParams.get('offset'));
      return { items: items.slice(offset, offset + 100), total: items.length, offset, limit: 100 };
    }
    return performance(new URL(url, 'http://local').searchParams.get('benchmark_id'));
  });
}
function deferred() { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; }
const props = { portfolioPath: path, portfolioId: 'p', revision: 0 };
const flush = async () => act(async () => {});
const choose = (value) => fireEvent.change(screen.getByLabelText('Index benchmark'), { target: { value } });
afterEach(cleanup);

describe('Portfolio performance', () => {
  it('defaults to TWR only; select and clear reads saved data without writes or acquisition', async () => {
    const request = server([index(), index('empty', { summary: { quote_count: 0 } })]);
    render(<PortfolioPerformance {...props} request={request} />); await flush();
    expect(screen.getByLabelText('Index benchmark')).toHaveValue('');
    expect(screen.getByRole('option', { name: /empty.*download required/ })).toBeDisabled();
    expect(screen.getByRole('link', { name: 'Indices page' })).toHaveAttribute('href', '#/indices');
    expect(screen.queryByTestId('benchmark_percent-line')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Scrollable performance chart' })).toHaveAttribute('tabindex', '0');
    expect(screen.getByText('Calculation and data notes').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByRole('slider')).toHaveAttribute('aria-valuetext', '2026-09-22; Portfolio TWR: 10.00%');
    choose('idx-one'); await flush();
    expect(request).toHaveBeenCalledWith(`${path}/performance?benchmark_id=idx-one`, expect.anything());
    expect(screen.getByTestId('benchmark_percent-line')).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: 'Chart legend' })).getByText('idx-one')).toBeInTheDocument();
    expect(screen.getByText(/Unknown return variant/)).toBeInTheDocument();
    choose(''); await flush();
    expect(screen.queryByTestId('benchmark_percent-line')).not.toBeInTheDocument();
    expect(request.mock.calls.every(([url, options]) => !options.method && (url.startsWith('/indices?') || url.startsWith(`${path}/performance`)))).toBe(true);
  });

  it('ignores malformed benchmark values when no benchmark is selected', async () => {
    const good = server();
    const request = vi.fn((url, options) => url.startsWith('/indices?') ? good(url, options) : Promise.resolve(performance('', {
      benchmark: { name: 'UNSELECTED' }, points: points.map((point) => ({ ...point, benchmark_percent: 'Infinity' })),
    })));
    render(<PortfolioPerformance {...props} request={request} />); await flush();
    expect(screen.getByRole('img')).toBeInTheDocument();
    expect(screen.queryByText('UNSELECTED')).not.toBeInTheDocument();
    expect(screen.queryByTestId('benchmark_percent-line')).not.toBeInTheDocument();
  });

  it('loads all index pages and excludes inactive indices', async () => {
    const request = server(Array.from({ length: 102 }, (_, i) => index(`idx-${i}`, { active: i !== 101 })));
    render(<PortfolioPerformance {...props} request={request} />); await flush();
    expect(request).toHaveBeenCalledWith('/indices?limit=100&offset=100', expect.anything());
    expect(screen.getByRole('option', { name: 'idx-100 (^TEST)' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'idx-101 (^TEST)' })).not.toBeInTheDocument();
  });

  it.each(['duplicate', 'total', 'empty', 'error'])('retries %s index results without hiding TWR', async (failure) => {
    const good = server(); let broken = true;
    const request = vi.fn(async (url, options) => {
      if (!broken || !url.startsWith('/indices?')) return good(url, options);
      if (failure === 'error') throw new Error('Index service unavailable');
      if (url.endsWith('offset=0')) return { items: [index()], total: 2, offset: 0, limit: 100 };
      return { items: failure === 'empty' ? [] : [index(failure === 'duplicate' ? 'idx-one' : 'second')], total: failure === 'total' ? 3 : 2, offset: 1, limit: 100 };
    });
    render(<PortfolioPerformance {...props} request={request} />); await flush();
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('img')).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'idx-one (^TEST)' })).not.toBeInTheDocument();
    broken = false; fireEvent.click(screen.getByRole('button', { name: 'Retry indices' })); await flush();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'idx-one (^TEST)' })).toBeInTheDocument();
  });

  it.each(['404', 'invalid'])('hides stale chart on %s performance, permits retry and clear', async (failure) => {
    const good = server(); let broken = true;
    const request = vi.fn(async (url, options) => {
      if (broken && url.includes('benchmark_id')) {
        if (failure === '404') throw new Error('Index not found (404)');
        return performance('idx-one', { points: [{ date: 'invalid' }] });
      }
      return good(url, options);
    });
    render(<PortfolioPerformance {...props} request={request} />); await flush();
    choose('idx-one'); await flush();
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    broken = false; fireEvent.click(screen.getByRole('button', { name: 'Retry performance' })); await flush();
    expect(screen.getByTestId('benchmark_percent-line')).toBeInTheDocument();
    choose(''); await flush(); expect(screen.queryByTestId('benchmark_percent-line')).not.toBeInTheDocument();
  });

  it('ignores aborted late selection responses, including switch back to the same key', async () => {
    const good = server(), old = deferred(); let count = 0;
    const request = vi.fn((url, options) => url.includes('benchmark_id') && count++ === 0 ? old.promise : good(url, options));
    render(<PortfolioPerformance {...props} request={request} />); await flush();
    choose('idx-one'); await flush();
    const signal = request.mock.calls.at(-1)[1].signal;
    choose(''); await flush(); choose('idx-one'); await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => old.resolve(performance('idx-one', { warnings: ['STALE'] })));
    expect(screen.queryByText('STALE')).not.toBeInTheDocument();
  });

  it.each(['revision', 'auth', 'route'])('guards pending performance and indices across %s changes', async (change) => {
    const oldPerformance = deferred(), oldIndices = deferred(), good = server();
    let pending = true;
    const request = vi.fn((url, options) => pending ? (url.startsWith('/indices?') ? oldIndices.promise : oldPerformance.promise) : good(url, options));
    const view = render(<PortfolioPerformance {...props} request={request} />); await flush();
    const oldSignal = request.mock.calls[0][1].signal;
    pending = false;
    view.rerender(<PortfolioPerformance {...props} revision={change === 'revision' ? 1 : 0} portfolioPath={change === 'route' ? '/customers/d/portfolios/p' : path} request={change === 'auth' ? good : request} />);
    await flush(); expect(oldSignal.aborted).toBe(true);
    await act(async () => {
      oldPerformance.resolve(performance('', { warnings: ['STALE'] }));
      oldIndices.resolve({ items: [index('old-identity')], total: 1, offset: 0, limit: 100 });
    });
    expect(screen.queryByText('STALE')).not.toBeInTheDocument();
    if (change !== 'revision') expect(screen.queryByRole('option', { name: /old-identity/ })).not.toBeInTheDocument();
    expect(screen.getByRole('img')).toBeInTheDocument();
  });

  it.each(['auth', 'route'])('resets an existing benchmark on %s changes', async (change) => {
    const request = server(); const view = render(<PortfolioPerformance {...props} request={request} />); await flush();
    choose('idx-one'); await flush();
    view.rerender(<PortfolioPerformance {...props} request={change === 'auth' ? server() : request} portfolioPath={change === 'route' ? '/other/p' : path} />); await flush();
    expect(screen.getByLabelText('Index benchmark')).toHaveValue('');
    expect(screen.queryByTestId('benchmark_percent-line')).not.toBeInTheDocument();
  });
});

describe('Performance chart', () => {
  it('breaks null gaps, exposes dates via slider and hover, and includes negative and zero ticks', () => {
    const gapped = [...points, { date: '2026-09-23', twr_percent: null, benchmark_percent: null }, { date: '2026-09-24', twr_percent: '5', benchmark_percent: '6' }];
    render(<PerformanceChart points={gapped} benchmark={index()} />);
    expect(screen.getByTestId('twr_percent-line').getAttribute('d').match(/M/g)).toHaveLength(2);
    expect(screen.getByText('-2.5%')).toBeInTheDocument(); expect(screen.getByText('0%')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('slider'), { target: { value: '3' } });
    expect(screen.getByRole('slider')).toHaveAttribute('aria-valuetext', '2026-09-23; Portfolio TWR: Unavailable; idx-one: Unavailable');
    fireEvent.mouseEnter(document.querySelector('circle'));
    expect(screen.getByRole('slider')).toHaveValue('0');
  });
  it('handles empty, all-null and a single date without invalid SVG numbers', () => {
    const view = render(<PerformanceChart points={[]} />);
    expect(screen.getByText(/No performance history/)).toBeInTheDocument();
    view.rerender(<PerformanceChart points={[{ date: '2026-09-20', twr_percent: null }]} />);
    expect(screen.getByText(/No valid performance/)).toBeInTheDocument();
    expect(screen.getByRole('slider')).toBeDisabled();
    view.rerender(<PerformanceChart points={[{ date: '2026-09-20', twr_percent: '0' }]} />);
    expect(document.querySelector('circle')).toHaveAttribute('cx', '390');
    expect(screen.getByRole('img').outerHTML).not.toMatch(/NaN|Infinity/);
  });
  it('never displays unselected benchmark data or nonfinite SVG values', () => {
    render(<PerformanceChart points={[{ date: '2026-09-20', twr_percent: '9'.repeat(400), benchmark_percent: 'Infinity' }]} />);
    expect(screen.queryByTestId('benchmark_percent-line')).not.toBeInTheDocument();
    expect(screen.getByRole('img').outerHTML).not.toMatch(/NaN|Infinity/);
  });
});
