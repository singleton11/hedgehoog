import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InstrumentDetail from './InstrumentDetail.jsx';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ request }) }));
const ISIN = 'IE00B4L5Y983';
const FIRST = '11111111-1111-4111-8111-111111111111';
const NEXT = '22222222-2222-4222-8222-222222222222';
const detail = (generation) => ({
  instrument_id: ISIN, generation, provider: 'yfinance', price_basis: 'provider_split_adjusted',
  provider_configured: true, catalogue: { name: 'Saved ETF' }, acquisition: null,
  summary: { quote_count: 1, first_date: '2020-01-01', last_date: '2020-01-01' },
  // A collection timestamp must never become the chart's historical quote-date bound.
  history_as_of: '2019-01-01T00:00:00Z',
});
const statistics = { instrument_id: ISIN, eligible: true, warnings: [], quote_count: 1, price_count: 1, return_count: 0 };
const page = (generation) => ({ items: [{ date: '2020-01-01', open: '100', high: '110', low: '90', close: '105' }],
  total: 1, offset: 0, as_of: '2026-09-17T12:00:00Z', generation });
const flush = async () => { await act(async () => {}); };
beforeEach(() => { vi.useFakeTimers(); request.mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

function server(conflict) {
  const state = { generation: FIRST, conflict, failDetail: false };
  request.mockImplementation(async (path) => {
    if (path === `/instruments/${ISIN}`) {
      if (state.failDetail) throw new Error('Details unavailable');
      return detail(state.generation);
    }
    if (state.conflict && path.includes(`/${state.conflict}?`)) {
      throw Object.assign(new Error('Stale history generation'), { status: 409 });
    }
    if (path.includes('/quotes?')) return page(state.generation);
    if (path.includes('/statistics')) return { ...statistics, generation: state.generation };
    throw new Error(`Unexpected request ${path}`);
  });
  return state;
}

describe('explicit latest-snapshot refresh', () => {
  it('rejects malformed initial details without reading an unpinned snapshot, then retries explicitly', async () => {
    server(null);
    const original = request.getMockImplementation();
    let malformed = true;
    request.mockImplementation(async (path, options) => {
      if (path === `/instruments/${ISIN}` && malformed) return { ...detail(FIRST), instrument_id: 'OTHER' };
      return original(path, options);
    });
    render(<InstrumentDetail isin={ISIN} />); await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('Instrument details returned an unexpected response.');
    expect(request.mock.calls.map(([path]) => path)).toEqual([`/instruments/${ISIN}`]);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    malformed = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry details' })); await flush();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC/ })).toBeInTheDocument();
    expect(request.mock.calls.filter(([path]) => path.includes('/quotes?'))[0][0]).toContain(`generation=${FIRST}`);
    expect(request.mock.calls.every(([, options]) => !options.method || options.method === 'GET')).toBe(true);
  });

  it('requires explicit refresh when the first publication races an empty detail', async () => {
    const state = server(null);
    const original = request.getMockImplementation();
    request.mockImplementation(async (path, options) => {
      if (path === `/instruments/${ISIN}` && !state.refreshed) return detail(null);
      return original(path, options);
    });
    render(<InstrumentDetail isin={ISIN} />); await flush();
    expect(screen.getAllByText(/The saved Yahoo Finance snapshot has changed/)).toHaveLength(2);
    expect(screen.queryByRole('img', { name: /Provider split-adjusted OHLC/ })).not.toBeInTheDocument();
    state.refreshed = true;
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh latest snapshot' })[0]); await flush();
    expect(screen.queryByText(/snapshot has changed/)).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC/ })).toBeInTheDocument();
  });

  it.each(['quotes', 'statistics'])('refreshes details and both reads explicitly after a %s 409', async (resource) => {
    const state = server(resource);
    render(<InstrumentDetail isin={ISIN} />); await flush();
    expect(screen.getByText(/The saved Yahoo Finance snapshot has changed/)).toHaveTextContent('No provider download is started');
    expect(screen.queryByRole('button', { name: resource === 'quotes' ? 'Retry chart' : 'Retry statistics' })).not.toBeInTheDocument();
    const before = request.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(request).toHaveBeenCalledTimes(before);
    state.generation = NEXT; state.conflict = null;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh latest snapshot' })); await flush();
    expect(request.mock.calls.filter(([path]) => path === `/instruments/${ISIN}`)).toHaveLength(2);
    expect(request.mock.calls.filter(([path]) => path.includes('/quotes?')).at(-1)[0]).toContain(`generation=${NEXT}`);
    expect(request.mock.calls.filter(([path]) => path.includes('/statistics?')).at(-1)[0]).toContain(`generation=${NEXT}`);
    expect(screen.queryByText(/snapshot has changed/)).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC/ })).toBeInTheDocument();
    for (const [path, options] of request.mock.calls) {
      expect(options.method).toBeUndefined();
      if (path.includes('/quotes?')) expect(new URL(path, 'http://localhost').searchParams.has('as_of')).toBe(false);
    }
  });

  it('keeps stale errors visible after a failed detail refresh without silently unpinning the generation', async () => {
    const state = server('quotes');
    render(<InstrumentDetail isin={ISIN} />); await flush();
    state.failDetail = true;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh latest snapshot' })); await flush();
    expect(screen.getByText(/Could not refresh acquisition status/)).toHaveTextContent('Details unavailable');
    expect(screen.getByRole('button', { name: 'Refresh latest snapshot' })).toBeEnabled();
    expect(request.mock.calls.filter(([path]) => path.includes('/quotes?'))).toHaveLength(1);
    state.failDetail = false; state.conflict = null;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh latest snapshot' })); await flush();
    expect(request.mock.calls.filter(([path]) => path.includes('/quotes?'))).toHaveLength(2);
    expect(screen.queryByText(/snapshot has changed/)).not.toBeInTheDocument();
  });
});
