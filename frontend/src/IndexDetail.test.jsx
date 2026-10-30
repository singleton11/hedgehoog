import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from './App.jsx';

const ID = 'idx-00000000-0000-0000-0000-000000000001';
const OTHER = 'idx-00000000-0000-0000-0000-000000000002';
const GENERATION = '11111111-1111-1111-1111-111111111111';
const NEXT_GENERATION = '22222222-2222-2222-2222-222222222222';
const AS_OF = '2026-09-20T10:00:00Z';
const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const quote = (date, extra = {}) => ({ date, open: '100', high: '110', low: '90', close: '105', volume: '123', ...extra });
const record = (extra = {}) => ({ instrument_id: ID, name: 'S&P 500', symbol: '^GSPC', currency: null,
  return_variant: 'unknown', verification_status: 'verified', generation: GENERATION,
  summary: { quote_count: 2, first_date: '2026-09-17', last_date: '2026-09-18' }, acquisition: null, ...extra });
const deferred = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };
async function flush() { await act(async () => {}); }
async function navigate(hash) {
  await act(async () => { window.history.replaceState(null, '', hash); window.dispatchEvent(new Event('hashchange')); });
}
function server() {
  const state = { detail: record(), quotes: [quote('2026-09-17'), quote('2026-09-18')], read: null, chart: null };
  state.fetch = vi.fn(async (url, options = {}) => {
    if (url === '/api/auth/me') return response(200, { id: 'u', login: 'alice', role: 'user' });
    if (url === '/api/instruments/budget') return response(200, { provider: 'yfinance', metered: false });
    if (url.startsWith('/api/indices?')) return response(200, { items: [state.detail], total: 1, offset: 0, limit: 20 });
    if (url.includes('/quotes?')) return state.chart ? state.chart(url, options) : response(200, { items: state.quotes, total: state.quotes.length, offset: 0, as_of: AS_OF, generation: state.detail.generation });
    if (url.startsWith('/api/indices/')) return state.read ? state.read(url, options) : response(200, state.detail);
    throw new Error(`Unexpected request ${url}`);
  });
  vi.stubGlobal('fetch', state.fetch);
  state.quoteCalls = () => state.fetch.mock.calls.filter(([url]) => url.includes('/quotes?'));
  return state;
}
async function open(hash = `#/indices/${ID}`) { window.history.replaceState(null, '', hash); const view = render(<App />); await flush(); return view; }
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '#/'); });

it('opens a linked, bookmarkable index detail with metadata and candlebars without downloading', async () => {
  const s = server(); await open('#/indices');
  const link = screen.getByRole('link', { name: 'S&P 500' });
  expect(link).toHaveAttribute('href', `#/indices/${ID}`);
  await navigate(link.getAttribute('href'));
  expect(screen.getByRole('heading', { name: 'S&P 500' })).toHaveFocus();
  expect(screen.getByRole('link', { name: 'Indices' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('img', { name: /Index OHLC candlestick chart/ })).toBeInTheDocument();
  expect(screen.getByText(/Values shown in index points/)).toBeInTheDocument();
  expect(screen.getByText('2026-09-17 – 2026-09-18')).toBeInTheDocument();
  expect(screen.getByText('Verified index')).toBeInTheDocument();
  expect(screen.queryByText(/manual/)).not.toBeInTheDocument();
  expect(s.fetch.mock.calls.every(([, options]) => !options.method || options.method === 'GET')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'all', exact: true }));
  expect(screen.getByRole('button', { name: 'all', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await navigate('#/indices');
  expect(screen.getByRole('heading', { name: 'Indices (1)' })).toBeInTheDocument();
});

it('loads all pages with the index generation and server cutoff before rendering', async () => {
  const s = server(); const second = deferred();
  s.chart = (url) => new URL(url, 'http://localhost').searchParams.get('offset') === '0'
    ? response(200, { items: [quote('2026-09-17')], total: 2, offset: 0, as_of: AS_OF, generation: GENERATION }) : second.promise;
  await open();
  expect(screen.getByText('Loading all quote pages…')).toBeInTheDocument();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  const next = new URL(s.quoteCalls()[1][0], 'http://localhost');
  expect(next.pathname).toBe(`/api/indices/${ID}/quotes`);
  expect(next.searchParams.get('generation')).toBe(GENERATION);
  expect(next.searchParams.get('as_of')).toBe(AS_OF);
  expect(next.searchParams.get('offset')).toBe('1');
  second.resolve(response(200, { items: [quote('2026-09-18')], total: 2, offset: 1, as_of: AS_OF, generation: GENERATION })); await flush();
  expect(screen.getByText('2 candles · 2 of 2 quotes')).toBeInTheDocument();
});

it('shows an empty state and keeps downloads explicit', async () => {
  const s = server(); s.quotes = []; s.detail = record({ summary: { quote_count: 0 }, verification_status: 'unverified' });
  await open();
  expect(screen.getByText(/No saved quotes to chart yet/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Download or refresh data/ })).toHaveAttribute('href', '#/indices');
  expect(s.fetch.mock.calls.some(([, options]) => options.method === 'POST')).toBe(false);
});

it('rejects mixed generations, then requires a latest-snapshot refresh', async () => {
  const s = server();
  s.chart = () => response(200, { items: s.quotes, total: 2, offset: 0, as_of: AS_OF, generation: NEXT_GENERATION });
  await open();
  expect(screen.getByRole('alert')).toHaveTextContent('snapshot has changed');
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  s.chart = null;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh latest snapshot' })); await flush();
  expect(screen.getByRole('img', { name: /Index OHLC/ })).toBeInTheDocument();
});

it('requires an explicit latest-snapshot refresh after a generation 409 and reloads details before chart pages', async () => {
  const s = server();
  s.chart = () => response(409, { detail: 'This history generation is no longer current.' });
  await open();
  expect(screen.getByRole('alert')).toHaveTextContent('No provider download is started');
  expect(screen.queryByRole('button', { name: 'Retry chart' })).not.toBeInTheDocument();
  const count = s.quoteCalls().length;
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(s.quoteCalls()).toHaveLength(count);
  s.detail = record({ generation: NEXT_GENERATION }); s.chart = null;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh latest snapshot' })); await flush();
  expect(s.quoteCalls()).toHaveLength(count + 1);
  expect(s.quoteCalls().at(-1)[0]).toContain(`generation=${NEXT_GENERATION}`);
  expect(s.fetch.mock.calls.filter(([url]) => url === `/api/indices/${ID}`)).toHaveLength(2);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(s.fetch.mock.calls.every(([, options]) => !options.method || options.method === 'GET')).toBe(true);
});

it.each([
  { instrument_id: OTHER },
  { name: null },
  { summary: { quote_count: -1 } },
  { summary: { quote_count: 1.5 } },
])('rejects malformed index details before loading chart pages: %j', async (invalid) => {
  const s = server(); s.detail = record(invalid);
  await open();
  expect(screen.getByRole('alert')).toHaveTextContent('The backend returned unexpected index details. Please retry.');
  expect(s.quoteCalls()).toHaveLength(0);
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  s.detail = record();
  fireEvent.click(screen.getByRole('button', { name: 'Retry details' })); await flush();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('img', { name: /Index OHLC/ })).toBeInTheDocument();
});

it('retries a transient chart failure with the same pinned snapshot and no detail refresh or download', async () => {
  const s = server(); s.chart = () => response(503, { detail: 'Saved quotes unavailable' });
  await open();
  expect(screen.getByRole('alert')).toHaveTextContent('Saved quotes unavailable');
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Refresh latest snapshot' })).not.toBeInTheDocument();
  const failedURL = s.quoteCalls()[0][0];
  s.chart = null;
  fireEvent.click(screen.getByRole('button', { name: 'Retry chart' })); await flush();
  expect(s.quoteCalls()).toHaveLength(2);
  expect(s.quoteCalls()[1][0]).toBe(failedURL);
  expect(s.fetch.mock.calls.filter(([url]) => url === `/api/indices/${ID}`)).toHaveLength(1);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('img', { name: /Index OHLC/ })).toBeInTheDocument();
  expect(s.fetch.mock.calls.every(([, options]) => !options.method || options.method === 'GET')).toBe(true);
});

it('handles missing details with a retry and rejects malformed IDs without fetching them', async () => {
  const s = server(); s.read = () => response(404, { detail: 'Index not found in the index catalogue' });
  await open();
  expect(screen.getByRole('alert')).toHaveTextContent('Index not found');
  s.read = null;
  fireEvent.click(screen.getByRole('button', { name: 'Retry details' })); await flush();
  expect(screen.getByRole('heading', { name: 'S&P 500' })).toBeInTheDocument();
  const count = s.fetch.mock.calls.length;
  await navigate('#/indices/%E0%A4%A');
  expect(screen.getByRole('alert')).toHaveTextContent('Index not found');
  expect(s.fetch).toHaveBeenCalledTimes(count);
});

it('polls active acquisition and reloads the chart when a new generation is published', async () => {
  const s = server(); s.detail = record({ acquisition: { status: 'running', phase: 'history' } });
  await open();
  expect(s.quoteCalls()).toHaveLength(1);
  s.detail = record({ generation: NEXT_GENERATION, acquisition: { status: 'succeeded' } });
  s.quotes = [quote('2026-09-19')];
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(s.quoteCalls()).toHaveLength(2);
  expect(screen.getByText('1 candle · 1 of 1 quotes')).toBeInTheDocument();
  expect(s.quoteCalls()[1][0]).toContain(`generation=${NEXT_GENERATION}`);
  const reads = () => s.fetch.mock.calls.filter(([url]) => url.startsWith('/api/indices/'));
  const count = reads().length;
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(reads()).toHaveLength(count);
});

it('aborts quote reads and ignores late responses after switching index routes', async () => {
  const s = server(); const slow = deferred();
  s.chart = () => slow.promise;
  await open(); const signal = s.quoteCalls()[0][1].signal;
  s.detail = record({ instrument_id: OTHER, name: 'DAX', symbol: '^GDAXI' }); s.quotes = [quote('2026-09-20')]; s.chart = null;
  await navigate(`#/indices/${OTHER}`);
  expect(signal.aborted).toBe(true);
  slow.resolve(response(200, { items: [quote('2000-01-01')], total: 1, offset: 0, as_of: AS_OF, generation: GENERATION })); await flush();
  expect(screen.getByRole('heading', { name: 'DAX' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'S&P 500' })).not.toBeInTheDocument();
  expect(screen.getByRole('img')).toHaveAccessibleName(/2026-09-20/);
});
