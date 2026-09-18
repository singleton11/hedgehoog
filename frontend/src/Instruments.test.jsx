import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App.jsx';
import { POLL_INTERVAL } from './useInstrument.js';
import { SEARCH_DELAY } from './InstrumentSearch.jsx';
import { acquisitionEstimate, displayText } from './instruments.js';

const A = 'IE00B4L5Y983';
const B = 'IE00B5BMR087';
const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const user = { id: 'user-1', login: 'alice', role: 'user' };
const item = (isin = A, name = 'iShares S&amp;P ETF') => ({ instrument_id: isin, isin, name, ticker: 'ETF', currency: 'EUR', asset_class: 'Equity' });
const job = (status = 'queued', extra = {}) => ({ id: 'job-1', instrument_id: A, status, phase: status === 'queued' ? 'pending' : 'eod', mode: 'update', requested_at: '2026-09-17T10:00:00Z', started_at: null, finished_at: null, pages: 0, rows: 0, calls: 0, error: null, warning: null, ...extra });
const quote = (index = 0, extra = {}) => ({ date: new Date(Date.UTC(2020, 0, 1 + index)).toISOString().slice(0, 10), collected_at: '2026-09-17T11:00:00Z', open: '100.123456789', high: '103', low: '99', close: '102.000000001', volume: '9007199254740993', adj_open: null, adj_high: null, adj_low: null, adj_close: '100', price_currency: null, ...extra });
const detail = (isin = A, extra = {}) => ({
  instrument_id: isin, catalogue: { ...item(isin, isin === A ? 'iShares S&amp;P ETF' : 'Second ETF'), inception_date: '2010-05-19', unknown: { holdings: [{ label: 'Nested &amp; raw', amount: null }] } },
  listing: null, profile: null, acquisition: null,
  summary: { quote_count: 0, first_date: null, last_date: null, requested_from: null, history_status: 'not_requested', warning: null },
  provider_configured: true, budget: { limit: 10000, used: 0, remaining: 10000, month: '2026-09' }, ...extra,
});
function withData(isin = A, extra = {}) {
  return detail(isin, { listing: { symbol: 'SXR8.DE', exchange: 'XETR' }, profile: { collected_at: '2026-09-17T10:00:01Z', collection_date: '2026-09-17', data: { name: 'Full profile', nested: { future_unknown: [null, 'opaque', { special: '<script>bad()</script>' }] } } },
    summary: { quote_count: 1, first_date: '2020-01-01', last_date: '2020-01-01', requested_from: '2010-05-19', history_status: 'partial', warning: 'Returned history may be truncated.' },
    acquisition: job('succeeded', { instrument_id: isin, id: 'previous', requested_at: '2026-09-16T10:00:00Z' }), ...extra });
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function mockServer() {
  const server = { details: { [A]: detail(), [B]: detail(B) }, quotes: { [A]: [], [B]: [] } };
  server.search = async () => [item(), item(B, 'Second ETF')];
  server.read = async (isin) => server.details[isin];
  server.page = async (isin, params) => {
    const items = server.quotes[isin];
    const offset = Number(params.get('offset'));
    return { items: items.slice(offset, offset + 1000), offset, limit: 1000, total: items.length };
  };
  server.post = async (isin, body) => {
    const accepted = job('queued', { instrument_id: isin, mode: body.mode });
    server.details[isin] = { ...server.details[isin], listing: body.symbol ? { symbol: body.symbol, exchange: body.exchange } : server.details[isin].listing, acquisition: accepted };
    return { job: accepted };
  };
  server.fetch = vi.fn(async (url, options = {}) => {
    if (url === '/api/auth/me') return response(200, user);
    if (url === '/api/') return response(200, { message: 'Hello, World!' });
    if (url === '/api/auth/logout') return response(204);
    const parsed = new URL(url, 'http://localhost');
    if (parsed.pathname === '/api/instruments/search') return response(200, { items: await server.search(parsed.searchParams.get('q'), options) });
    const match = parsed.pathname.match(/^\/api\/instruments\/([^/]+)(?:\/(quotes|acquisitions))?$/);
    if (!match) throw new Error(`Unexpected URL: ${url}`);
    if (match[2] === 'quotes') return response(200, { as_of: '2026-09-17T12:00:00Z', ...await server.page(match[1], parsed.searchParams, options) });
    if (match[2] === 'acquisitions') return response(202, await server.post(match[1], JSON.parse(options.body), options));
    return response(200, await server.read(match[1], options));
  });
  vi.stubGlobal('fetch', server.fetch);
  server.posts = () => server.fetch.mock.calls.filter(([, options]) => options.method === 'POST');
  server.reads = (isin = A) => server.fetch.mock.calls.filter(([url]) => url === `/api/instruments/${isin}`);
  server.pages = (isin = A) => server.fetch.mock.calls.filter(([url]) => url.startsWith(`/api/instruments/${isin}/quotes?`));
  return server;
}
async function flush() { await act(async () => {}); }
async function advance(ms) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function open(isin = null) {
  window.history.replaceState(null, '', isin ? `#/instruments/${isin}` : '#/');
  const view = render(<App />);
  await flush();
  return view;
}
async function navigate(isin) {
  await act(async () => {
    window.location.hash = isin ? `/instruments/${isin}` : '/';
    window.dispatchEvent(new Event('hashchange'));
  });
}
async function review(name = 'Request Data') {
  fireEvent.click(screen.getByRole('button', { name }));
  await flush();
}
const authorization = () => screen.getByRole('checkbox', { name: 'I authorize this quota-consuming acquisition.' });
const manualOverride = () => screen.getByRole('checkbox', { name: /Manual listing override/ });
const estimateText = () => screen.getByText('Estimated provider calls:').closest('p');
async function confirmInitial() {
  await review();
  fireEvent.click(authorization());
  fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
  await flush();
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-17T12:00:00Z')); window.history.replaceState(null, '', '#/'); });
afterEach(() => { cleanup(); vi.useRealTimers(); window.history.replaceState(null, '', '#/'); });

describe('ETF autocomplete and routing', () => {
  it('debounces name/ticker/ISIN queries, supports Arrow/Enter selection and back navigation without acquisition', async () => {
    const server = mockServer();
    await open();
    const input = screen.getByRole('combobox', { name: 'Find an ETF' });
    fireEvent.change(input, { target: { value: 'i' } });
    await advance(SEARCH_DELAY + 20);
    expect(server.fetch.mock.calls.filter(([url]) => url.includes('/search?'))).toHaveLength(0);
    fireEvent.change(input, { target: { value: 'ishares' } });
    await advance(SEARCH_DELAY - 1);
    expect(server.fetch.mock.calls.filter(([url]) => url.includes('/search?'))).toHaveLength(0);
    await advance(1);
    expect(server.fetch).toHaveBeenCalledWith('/api/instruments/search?q=ishares&limit=15', expect.objectContaining({ credentials: 'same-origin', signal: expect.any(AbortSignal) }));
    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(screen.getAllByRole('option')[0]).toHaveTextContent('iShares S&P ETF');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(screen.getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(input).toHaveAttribute('aria-activedescendant', screen.getAllByRole('option')[0].id);
    fireEvent.keyDown(input, { key: 'Enter' });
    await flush();
    expect(window.location.hash).toBe(`#/instruments/${A}`);
    expect(screen.getByRole('heading', { name: 'iShares S&P ETF' })).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(screen.getByRole('link', { name: /Back to ETF search/ }));
    await advance(5);
    expect(screen.getByRole('heading', { name: 'Your market-data workspace' })).toBeInTheDocument();
    await navigate(A);
    expect(screen.getByRole('heading', { name: 'iShares S&P ETF' })).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('aborts stale search requests and ignores late results after a different selection', async () => {
    const server = mockServer();
    const stale = deferred();
    let oldSignal;
    server.search = async (q, options) => {
      if (q === 'old') { oldSignal = options.signal; return stale.promise; }
      return [item(B, 'Selected fund')];
    };
    await open();
    const input = screen.getByRole('combobox');
    fireEvent.change(input, { target: { value: 'old' } });
    await advance(SEARCH_DELAY);
    fireEvent.change(input, { target: { value: B } });
    expect(oldSignal.aborted).toBe(true);
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(server.reads()).toHaveLength(0);
    await advance(SEARCH_DELAY);
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    fireEvent.keyDown(input, { key: 'Enter' });
    await flush();
    await act(async () => stale.resolve([item(A, 'Stale fund')]));
    expect(window.location.hash).toBe(`#/instruments/${B}`);
    expect(screen.queryByText('Stale fund')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(server.reads(A)).toHaveLength(0);
  });

  it('clears selectable results immediately on query edits and Escape cancels pending work', async () => {
    const server = mockServer();
    await open();
    const input = screen.getByRole('combobox');
    fireEvent.change(input, { target: { value: 'ETF' } });
    await advance(SEARCH_DELAY);
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.change(input, { target: { value: 'changed' } });
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Escape' });
    await advance(SEARCH_DELAY);
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(server.reads()).toHaveLength(0);
    expect(server.fetch.mock.calls.filter(([url]) => url.includes('/search?'))).toHaveLength(1);
    const pending = deferred();
    server.search = () => pending.promise;
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    await advance(SEARCH_DELAY);
    const signal = server.fetch.mock.calls.at(-1)[1].signal;
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve([item()]));
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });

  it('offers keyboard-reachable retry and an empty search state', async () => {
    const server = mockServer();
    server.search = async () => { throw new Error('offline'); };
    await open();
    const input = screen.getByRole('combobox');
    fireEvent.change(input, { target: { value: 'ZZ' } });
    await advance(SEARCH_DELAY);
    expect(screen.getByRole('alert')).toHaveTextContent('Could not reach the backend');
    const retry = screen.getByRole('button', { name: 'Retry search' });
    fireEvent.blur(input, { relatedTarget: retry });
    expect(retry).toBeInTheDocument();
    server.search = async () => [];
    fireEvent.click(retry);
    await advance(SEARCH_DELAY);
    expect(screen.getByText(/No matching ETFs/)).toBeInTheDocument();
  });
});

describe('acquisition estimates', () => {
  it.each([
    ['unresolved', null, false, 1, 3, 8],
    ['cached', { symbol: 'ETF.DE', exchange: 'XETR' }, false, 0, 2, 7],
    ['manual', null, true, 0, 2, 7],
  ])('includes lookup only for %s automatic listings', (_, listing, manual, lookupCalls, minimumCalls, calls) => {
    const instrument = detail(A, { listing });
    expect(acquisitionEstimate(instrument, 'update', manual)).toMatchObject({ lookupCalls, minimumCalls, pages: 6, calls });
    instrument.catalogue.inception_date = null;
    expect(acquisitionEstimate(instrument, 'update', manual)).toMatchObject({ lookupCalls, minimumCalls, pages: null, calls: null });
  });
});

describe('explicit acquisition and complete charts', () => {
  it('requires quota authorization without listing inputs, then polls queued → running → succeeded and loads all quote pages', async () => {
    const server = mockServer();
    await open(A);
    expect(server.posts()).toHaveLength(0);
    expect(screen.getByText(/No saved quotes to chart yet/)).toBeInTheDocument();
    await review();
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Exchange MIC')).not.toBeInTheDocument();
    expect(manualOverride()).not.toBeChecked();
    expect(screen.getByText(/first named, EOD-enabled exact ticker.exchange match/)).toHaveTextContent('caches that listing for this instrument');
    expect(screen.getByText(/Listing selection is not ISIN verification/)).toBeInTheDocument();
    expect(estimateText()).toHaveTextContent('About 8 = at least 1 tickerslist lookup + 1 tickerinfo + 6 EOD page(s)');
    expect(estimateText()).toHaveTextContent('multiple pages');
    expect(screen.getByRole('button', { name: 'Confirm request' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(authorization());
    fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
    await flush();
    expect(server.posts()).toHaveLength(1);
    expect(server.posts()[0]).toEqual([`/api/instruments/${A}/acquisitions`, expect.objectContaining({ method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirmed: true, mode: 'update' }) })]);
    expect(screen.getByText('Acquisition: queued')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeDisabled();
    server.details[A] = { ...server.details[A], acquisition: job('running', { pages: 1, rows: 1000, calls: 2 }) };
    await advance(POLL_INTERVAL);
    expect(screen.getByText('Acquisition: running')).toBeInTheDocument();
    server.quotes[A] = Array.from({ length: 1001 }, (_, index) => quote(index));
    server.details[A] = withData(A, { acquisition: job('succeeded'), summary: { quote_count: 1001, first_date: '2020-01-01', last_date: server.quotes[A].at(-1).date, requested_from: '2010-05-19', history_status: 'partial', warning: 'Plan or listing history truncated.' } });
    await advance(POLL_INTERVAL);
    expect(screen.getByText('Acquisition: succeeded')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Unadjusted OHLC candlestick chart/ })).toBeInTheDocument();
    expect(screen.getByText('1001 quotes loaded · 366 in this window · 366 candles plotted')).toBeInTheDocument();
    expect(screen.getByText('Plan or listing history truncated.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update Data' })).toBeEnabled();
    const pages = server.pages().slice(-2).map(([url]) => new URL(url, 'http://localhost').searchParams);
    expect(pages.map((params) => params.get('offset'))).toEqual(['0', '1000']);
    expect(pages[0].get('as_of')).toBeNull();
    expect(pages[1].get('as_of')).toBe('2026-09-17T12:00:00Z');
    fireEvent.click(screen.getByRole('button', { name: 'all' }));
    expect(screen.getByText('1001 quotes loaded · 1001 in this window · 1001 candles plotted')).toBeInTheDocument();
    const reads = server.reads().length;
    await advance(POLL_INTERVAL * 2);
    expect(server.reads()).toHaveLength(reads);
    expect(server.posts()).toHaveLength(1);
  });

  it('reuses a locked saved listing without inputs and retains old candles after a failed update and quote refresh', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote()];
    await open(A);
    const chart = () => screen.getByRole('img', { name: /Unadjusted OHLC/ });
    expect(chart()).toBeInTheDocument();
    await review('Update Data');
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Exchange MIC')).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /Manual listing override/ })).not.toBeInTheDocument();
    expect(screen.getByText(/will be reused/)).toHaveTextContent('SXR8.DE / XETR');
    expect(screen.getByText(/This saved listing is locked/)).toBeInTheDocument();
    expect(estimateText()).toHaveTextContent('About 4 = 1 tickerinfo + 3 EOD page(s)');
    fireEvent.click(authorization());
    fireEvent.click(screen.getByRole('button', { name: 'Confirm update' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
    expect(chart()).toBeInTheDocument();
    server.details[A] = { ...server.details[A], acquisition: job('failed', { error: 'Provider quota exhausted' }) };
    server.page = async () => { throw new Error('quote outage'); };
    await advance(POLL_INTERVAL);
    expect(screen.getByText(/Acquisition failed: Provider quota exhausted/)).toBeInTheDocument();
    expect(screen.getByText(/Could not load the complete chart/)).toBeInTheDocument();
    expect(chart()).toBeInTheDocument();
    expect(screen.getByText('1 quotes loaded · 1 in this window · 1 candles plotted')).toBeInTheDocument();
    expect(server.posts()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Update Data' })).toBeEnabled();
  });

  it('uses a separate explicit backfill confirmation for partial coverage', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote()];
    await open(A);
    await review('Retry Older History');
    expect(screen.getByText(/From 2010-05-19 to before 2020-01-01/)).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
    expect(estimateText()).toHaveTextContent('About 5 = 1 tickerinfo + 4 EOD page(s)');
    expect(screen.queryByRole('checkbox', { name: /Manual listing override/ })).not.toBeInTheDocument();
    fireEvent.click(authorization());
    fireEvent.click(screen.getByRole('button', { name: 'Confirm older history' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'backfill' });
  });

  it('lets a no-data failure explicitly override its saved listing and requires renewed authorization after edits', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { listing: { symbol: 'WRONG', exchange: 'XXXX' }, acquisition: job('failed', { error: 'Listing mismatch' }) });
    await open(A);
    await review();
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(screen.getByText(/will be reused/)).toHaveTextContent('WRONG / XXXX');
    fireEvent.click(authorization());
    fireEvent.click(manualOverride());
    expect(authorization()).not.toBeChecked();
    expect(screen.getByLabelText('Provider symbol')).toHaveValue('WRONG');
    expect(screen.getByLabelText('Provider symbol')).toBeEnabled();
    expect(screen.getByLabelText('Exchange MIC')).toBeEnabled();
    expect(screen.getByRole('link', { name: /public Marketstack ticker workbook/ })).toHaveAttribute('href', 'https://marketstack.com/download/v2_eod_tickers_finnworlds_tiingo.xlsx');
    fireEvent.change(screen.getByLabelText('Provider symbol'), { target: { value: 'SXR8.DE' } });
    fireEvent.change(screen.getByLabelText('Exchange MIC'), { target: { value: 'xetr' } });
    fireEvent.click(authorization());
    fireEvent.change(screen.getByLabelText('Provider symbol'), { target: { value: ' sxr8.de ' } });
    expect(authorization()).not.toBeChecked();
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(authorization());
    fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ symbol: 'SXR8.DE', exchange: 'XETR', confirmed: true, mode: 'update' });
  });

  it('reuses a cached listing without quotes or listing inputs by default', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { listing: { symbol: 'ETF.DE', exchange: 'XETR' } });
    await open(A);
    await review();
    expect(screen.getByText(/will be reused/)).toHaveTextContent('ETF.DE / XETR');
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Exchange MIC')).not.toBeInTheDocument();
    expect(manualOverride()).not.toBeChecked();
    expect(estimateText()).toHaveTextContent('About 7 = 1 tickerinfo + 6 EOD page(s)');
    expect(estimateText()).not.toHaveTextContent('tickerslist');
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(authorization());
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
  });

  it('drops manual inputs and resets authorization when switching back to automatic selection', async () => {
    const server = mockServer();
    await open(A);
    await review();
    fireEvent.click(authorization());
    fireEvent.click(manualOverride());
    expect(authorization()).not.toBeChecked();
    expect(estimateText()).toHaveTextContent('About 7 = 1 tickerinfo + 6 EOD page(s)');
    fireEvent.change(screen.getByLabelText('Provider symbol'), { target: { value: 'OVERRIDE.US' } });
    fireEvent.change(screen.getByLabelText('Exchange MIC'), { target: { value: 'XNYS' } });
    fireEvent.click(authorization());
    fireEvent.click(manualOverride());
    expect(authorization()).not.toBeChecked();
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(estimateText()).toHaveTextContent('About 8 = at least 1 tickerslist lookup');
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(authorization());
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
  });

  it('requires 3 calls for unresolved acquisition but only 2 to reuse a cached listing', async () => {
    const server = mockServer();
    const budget = { month: '2026-09', used: 9998, limit: 10000, remaining: 2 };
    server.details[A].budget = budget;
    server.details[A].catalogue.inception_date = null;
    server.details[B] = detail(B, { listing: { symbol: 'ETF.DE', exchange: 'XETR' }, budget });
    server.details[B].catalogue.inception_date = null;
    await open(A);
    expect(screen.getByText(/Insufficient local budget for the minimum 3/)).toBeInTheDocument();
    await review();
    expect(estimateText()).toHaveTextContent('At least 3 = at least 1 tickerslist lookup + 1 tickerinfo + at least 1 EOD page');
    fireEvent.click(authorization());
    expect(screen.getByRole('button', { name: 'Confirm request' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await navigate(B);
    expect(screen.queryByText(/Insufficient local budget/)).not.toBeInTheDocument();
    await review();
    expect(estimateText()).toHaveTextContent('At least 2 = 1 tickerinfo + at least 1 EOD page');
    fireEvent.click(authorization());
    expect(screen.getByRole('button', { name: 'Confirm request' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
    await flush();
    expect(server.posts()).toHaveLength(1);
    expect(server.posts()[0][0]).toBe(`/api/instruments/${B}/acquisitions`);
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
  });

  it('allows an explicit valid manual override with 2 calls remaining and skips the lookup estimate', async () => {
    const server = mockServer();
    server.details[A].budget = { month: '2026-09', used: 9998, limit: 10000, remaining: 2 };
    server.details[A].catalogue.inception_date = null;
    await open(A);
    await review();
    fireEvent.click(manualOverride());
    expect(estimateText()).toHaveTextContent('At least 2 = 1 tickerinfo + at least 1 EOD page');
    expect(screen.queryByText(/This request needs at least/)).not.toBeInTheDocument();
    fireEvent.click(authorization());
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    expect(server.posts()).toHaveLength(0);
    fireEvent.change(screen.getByLabelText('Provider symbol'), { target: { value: ' sxr8.de ' } });
    fireEvent.change(screen.getByLabelText('Exchange MIC'), { target: { value: 'X@TR' } });
    fireEvent.click(authorization());
    expect(screen.getByRole('button', { name: 'Confirm request' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    expect(server.posts()).toHaveLength(0);
    fireEvent.change(screen.getByLabelText('Exchange MIC'), { target: { value: 'xetr' } });
    expect(authorization()).not.toBeChecked();
    fireEvent.click(authorization());
    fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ symbol: 'SXR8.DE', exchange: 'XETR', confirmed: true, mode: 'update' });
  });

  it('warns about plan and listing coverage rather than assuming a Free plan', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.details[A].summary.warning = null;
    await open(A);
    expect(screen.getByText(/Only partial history is available/)).toHaveTextContent('Plan access and listing coverage');
    await review('Retry Older History');
    expect(screen.getByText(/Your plan and the selected listing determine available history/)).toBeInTheDocument();
    expect(screen.queryByText(/Free plan/)).not.toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('plots EUNL out-of-bounds prices as supplied without filtering on old quality flags', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { date: '2018-01-01', open: '44.213', high: '44.213', low: '44.213', close: '44.21', quality_flags: ['ohlc_bounds'] }),
      ...Array.from({ length: 250 }, (_, index) => quote(index + 1))];
    await open(A);
    expect(screen.getByText('251 quotes loaded · 250 in this window · 250 candles plotted')).toBeInTheDocument();
    expect(screen.queryByText(/inconsistent OHLC bounds are excluded/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'all' }));
    expect(screen.getByText('251 quotes loaded · 251 in this window · 251 candles plotted')).toBeInTheDocument();
    const raw = JSON.parse(screen.getByLabelText('Quote observations (current window, all provider fields)').textContent);
    expect(raw[0]).toMatchObject({ low: '44.213', close: '44.21', quality_flags: ['ohlc_bounds'] });
    expect([...document.querySelectorAll('.candle title')].some((title) => title.textContent.includes('Close 44.21'))).toBe(true);
    expect(server.posts()).toHaveLength(0);
  });

  it('does not apply historical quality flags as chart filtering rules', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { open: '1', high: '1', low: '1', close: '0.99999999999999999999999999999', quality_flags: ['ohlc_bounds'] })];
    await open(A);
    expect(screen.getByText('1 quotes loaded · 1 in this window · 1 candles plotted')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Unadjusted OHLC/ })).toBeInTheDocument();
  });

  it('retains WEBN null-close quotes while plotting available, zero and negative prices', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { open: '12.338', high: '12.34', low: '12.236', close: null }),
      quote(1), quote(2, { open: '0', high: '1', low: '-1', close: '-0.5' }), quote(3, { close: { unexpected: true } })];
    await open(A);
    expect(screen.getByText('4 quotes loaded · 4 in this window · 2 candles plotted')).toBeInTheDocument();
    expect(screen.getByText(/2 quotes with missing or non-numeric OHLC cannot be drawn/)).toBeInTheDocument();
    const raw = JSON.parse(screen.getByLabelText('Quote observations (current window, all provider fields)').textContent);
    expect(raw[0].close).toBeNull();
    expect(raw[3].close).toEqual({ unexpected: true });
    expect([...document.querySelectorAll('.candle title')].some((title) => title.textContent.includes('Close -0.5'))).toBe(true);
  });

  it('renders listing selection and nested unknown metadata as text without substituting currency or missing OHLC', async () => {
    const server = mockServer();
    server.details[A] = withData(A, { listing_resolution: { source: 'tickerslist', query: 'ETF', name: 'Selected &amp; &lt;img src=x onerror=bad()&gt;', collected_at: '2026-09-17T10:00:00Z', data: { symbol: 'ETF.DE', stock_exchange: { mic: 'XETR' }, future_unknown: null } } });
    server.details[A].catalogue.name = 'Fund &amp; &lt;img src=x onerror=bad()&gt;';
    server.quotes[A] = [quote(), quote(1, { open: null }), quote(2, { low: '' })];
    server.details[A].summary.quote_count = 3;
    await open(A);
    expect(screen.getByRole('heading', { name: 'Fund & <img src=x onerror=bad()>' })).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText(/Listing price currency: Unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/Listing price currency: EUR/)).not.toBeInTheDocument();
    expect(screen.getByText('3 quotes loaded · 3 in this window · 1 candles plotted')).toBeInTheDocument();
    expect(screen.getByText(/2 quotes with missing or non-numeric OHLC/)).toBeInTheDocument();
    const profile = screen.getByLabelText('Tickerinfo profile and collection metadata');
    expect(JSON.parse(profile.textContent)).toEqual(server.details[A].profile);
    expect(JSON.parse(screen.getByLabelText('Catalogue metadata').textContent)).toEqual(server.details[A].catalogue);
    expect(screen.getByText(/Automatically selected from Marketstack's ticker list:/)).toHaveTextContent('Selected & <img src=x onerror=bad()>');
    expect(screen.getByText(/Automatically selected from Marketstack's ticker list:/)).toHaveTextContent('not ISIN verification');
    expect(JSON.parse(screen.getByLabelText('Listing, acquisition and coverage metadata').textContent).listing_resolution).toEqual(server.details[A].listing_resolution);
    expect(screen.getByLabelText('Quote observations (current window, all provider fields)')).toHaveTextContent('9007199254740993');
    const candleTitle = document.querySelector('.candle title');
    expect(candleTitle.textContent).toContain('Open 100.123456789');
    expect(candleTitle.textContent).toContain('Price currency: unknown');
    expect(displayText('&#38; &#x3c; &amp;')).toBe('& < &');
  });

  it('disables acquisitions when the provider or local budget is unavailable', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { provider_configured: false });
    await open(A);
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeDisabled();
    expect(screen.getByText(/Marketstack is not configured/)).toBeInTheDocument();
    server.details[B] = detail(B, { budget: { month: '2026-09', used: 99, limit: 100, remaining: 1 } });
    await navigate(B);
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeDisabled();
    expect(screen.getByText(/Insufficient local budget/)).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });
});

describe('polling, navigation races and cleanup', () => {
  it('recovers from polling errors without losing data or retrying an acquisition', async () => {
    const server = mockServer();
    server.details[A] = withData(A, { acquisition: job('queued') });
    server.quotes[A] = [quote()];
    await open(A);
    server.read = async () => { throw new Error('offline'); };
    await advance(POLL_INTERVAL);
    expect(screen.getByText(/Could not refresh acquisition status/)).toHaveTextContent('Polling will retry automatically');
    expect(screen.getByRole('img', { name: /Unadjusted OHLC/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update Data' })).toBeDisabled();
    server.read = async () => ({ ...server.details[A], acquisition: job('running') });
    fireEvent.click(screen.getByRole('button', { name: 'Retry polling' }));
    await flush();
    expect(screen.getByText('Acquisition: running')).toBeInTheDocument();
    expect(screen.queryByText(/Could not refresh acquisition status/)).not.toBeInTheDocument();
    const count = server.reads().length;
    await advance(POLL_INTERVAL);
    expect(server.reads()).toHaveLength(count + 1);
    expect(server.posts()).toHaveLength(0);
  });

  it('ignores a stale job read after POST and a running-to-queued regression', async () => {
    const server = mockServer();
    const initial = server.details[A];
    await open(A);
    let stale = true;
    server.read = async () => stale ? initial : server.details[A];
    await confirmInitial();
    expect(screen.getByText('Acquisition: queued')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeDisabled();
    stale = false;
    server.details[A] = { ...server.details[A], acquisition: job('running') };
    await advance(POLL_INTERVAL);
    expect(screen.getByText('Acquisition: running')).toBeInTheDocument();
    server.details[A] = { ...server.details[A], acquisition: job('queued') };
    await advance(POLL_INTERVAL);
    expect(screen.getByText('Acquisition: running')).toBeInTheDocument();
    server.details[A] = { ...server.details[A], acquisition: job('succeeded') };
    await advance(POLL_INTERVAL);
    expect(screen.getByText('Acquisition: succeeded')).toBeInTheDocument();
    expect(server.posts()).toHaveLength(1);
  });

  it('ignores a late initial detail response when navigating to another ISIN', async () => {
    const server = mockServer();
    const pending = deferred();
    server.read = async (isin) => isin === A ? pending.promise : server.details[isin];
    await open(A);
    expect(screen.getByText('Loading instrument…')).toBeInTheDocument();
    const oldSignal = server.reads()[0][1].signal;
    await navigate(B);
    expect(oldSignal.aborted).toBe(true);
    await act(async () => pending.resolve(withData()));
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'iShares S&P ETF' })).not.toBeInTheDocument();
    expect(server.pages(A)).toHaveLength(0);
  });

  it('aborts an in-flight poll on navigation and never schedules another poll for the old ISIN', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { acquisition: job('queued') });
    await open(A);
    const pending = deferred();
    server.read = async (isin) => isin === A ? pending.promise : server.details[isin];
    await advance(POLL_INTERVAL);
    const oldSignal = server.reads().at(-1)[1].signal;
    await navigate(B);
    expect(oldSignal.aborted).toBe(true);
    await act(async () => pending.resolve(withData(A, { acquisition: job('succeeded') })));
    const count = server.reads().length;
    await advance(POLL_INTERVAL * 3);
    expect(server.reads()).toHaveLength(count);
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('aborts pagination on navigation and does not replace the new instrument chart with old pages', async () => {
    const server = mockServer();
    const pending = deferred();
    server.details[A] = withData();
    server.details[B] = withData(B);
    server.quotes[B] = [quote(0, { open: '5', high: '7', low: '4', close: '6' })];
    const normalPage = server.page;
    server.page = async (isin, params, options) => {
      if (isin !== A) return normalPage(isin, params, options);
      if (params.get('offset') === '0') return { items: Array.from({ length: 1000 }, (_, index) => quote(index)), total: 1001, offset: 0, limit: 1000 };
      return pending.promise;
    };
    await open(A);
    expect(server.pages()).toHaveLength(2);
    const oldSignal = server.pages().at(-1)[1].signal;
    await navigate(B);
    expect(oldSignal.aborted).toBe(true);
    await act(async () => pending.resolve({ items: [quote(1000)], total: 1001, offset: 1000, limit: 1000 }));
    expect(screen.getByText('1 quotes loaded · 1 in this window · 1 candles plotted')).toBeInTheDocument();
    expect(document.querySelector('.candle title').textContent).toContain('Open 5');
    expect(screen.queryByText(/1001 quotes loaded/)).not.toBeInTheDocument();
  });

  it('suppresses double submissions and ignores an acknowledged POST after navigating away', async () => {
    const server = mockServer();
    const pending = deferred();
    server.post = () => pending.promise;
    await open(A);
    await review();
    fireEvent.click(authorization());
    const form = screen.getByRole('form', { name: 'Confirm request' });
    fireEvent.submit(form);
    fireEvent.submit(form);
    await flush();
    expect(server.posts()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Submitting…' })).toBeDisabled();
    const signal = server.posts()[0][1].signal;
    await navigate(B);
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ job: job() }));
    await advance(POLL_INTERVAL * 2);
    expect(server.reads(A)).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(screen.queryByText('Acquisition: queued')).not.toBeInTheDocument();
  });

  it('cleans up search requests, scheduled polls and hash listeners on unmount', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { acquisition: job('queued') });
    const pending = deferred();
    server.search = () => pending.promise;
    const view = await open(A);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'ETF' } });
    await advance(SEARCH_DELAY);
    const signal = server.fetch.mock.calls.at(-1)[1].signal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    const count = server.fetch.mock.calls.length;
    await act(async () => pending.resolve([item()]));
    await navigate(B);
    await advance(POLL_INTERVAL * 3);
    expect(server.fetch).toHaveBeenCalledTimes(count);
  });

  it('reports a broken later quote page and allows a complete chart retry', async () => {
    const server = mockServer();
    server.details[A] = withData();
    const firstPage = Array.from({ length: 1000 }, (_, index) => quote(index));
    server.page = async (_, params) => params.get('offset') === '0'
      ? { items: firstPage, total: 1001, offset: 0, limit: 1000 }
      : { items: [], total: 1001, offset: 1000, limit: 1000 };
    await open(A);
    expect(screen.getByText(/backend returned an incomplete quote page/)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    server.page = async () => ({ items: [quote()], total: 1, offset: 0, limit: 1000 });
    fireEvent.click(screen.getByRole('button', { name: 'Retry chart' }));
    await flush();
    expect(screen.getByRole('img', { name: /Unadjusted OHLC/ })).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });
});
