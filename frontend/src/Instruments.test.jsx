import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App.jsx';
import { POLL_INTERVAL } from './useInstrument.js';
import { SEARCH_DELAY } from './InstrumentSearch.jsx';
import { ACQUIRED_PAGE_SIZE } from './AcquiredEtfs.jsx';
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
  listing: null, profile: null, acquisition: null, price_currency_override: null,
  summary: { quote_count: 0, first_date: null, last_date: null, requested_from: null, history_status: 'not_requested', warning: null },
  provider: 'yfinance', price_basis: 'provider_split_adjusted', provider_configured: true, budget: null, ...extra,
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
  server.acquiredItems = [];
  server.acquired = async (params) => {
    const offset = Number(params.get('offset'));
    const limit = Number(params.get('limit'));
    return { items: server.acquiredItems.slice(offset, offset + limit), total: server.acquiredItems.length, offset, limit };
  };
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
  server.saveCurrency = async (isin, body) => {
    server.details[isin] = { ...server.details[isin], price_currency_override: body.price_currency };
    return { price_currency_override: body.price_currency };
  };
  server.fetch = vi.fn(async (url, options = {}) => {
    if (url === '/api/auth/me') return response(200, user);
    if (url === '/api/') return response(200, { message: 'Hello, World!' });
    if (url === '/api/auth/logout') return response(204);
    if (url === '/api/instruments/budget') return response(200, { provider: 'yfinance', metered: false, month: '2026-09', used: 0, limit: null, remaining: null });
    const parsed = new URL(url, 'http://localhost');
    if (parsed.pathname === '/api/instruments/acquired') return response(200, await server.acquired(parsed.searchParams, options));
    if (parsed.pathname === '/api/instruments/search') return response(200, { items: await server.search(parsed.searchParams.get('q'), options) });
    const match = parsed.pathname.match(/^\/api\/instruments\/([^/]+)(?:\/(quotes|acquisitions|price-currency))?$/);
    if (!match) throw new Error(`Unexpected URL: ${url}`);
    if (match[2] === 'quotes') return response(200, { as_of: '2026-09-17T12:00:00Z', ...await server.page(match[1], parsed.searchParams, options) });
    if (match[2] === 'acquisitions') return response(202, await server.post(match[1], JSON.parse(options.body), options));
    if (match[2] === 'price-currency') return response(200, await server.saveCurrency(match[1], JSON.parse(options.body), options));
    return response(200, await server.read(match[1], options));
  });
  vi.stubGlobal('fetch', server.fetch);
  server.posts = () => server.fetch.mock.calls.filter(([, options]) => options.method === 'POST');
  server.currencySaves = () => server.fetch.mock.calls.filter(([, options]) => options.method === 'PATCH');
  server.reads = (isin = A) => server.fetch.mock.calls.filter(([url]) => url === `/api/instruments/${isin}`);
  server.pages = (isin = A) => server.fetch.mock.calls.filter(([url]) => url.startsWith(`/api/instruments/${isin}/quotes?`));
  return server;
}
async function flush() { await act(async () => {}); }
async function advance(ms) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function openRoute(hash) {
  window.history.replaceState(null, '', hash);
  const view = render(<App />);
  await flush();
  return view;
}
async function open(isin = null) {
  return openRoute(isin ? `#/instruments/${isin}` : '#/');
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
const authorization = () => screen.getByRole('checkbox', { name: 'Allow this request to fetch Yahoo Finance data.' });
const manualOverride = () => screen.getByRole('checkbox', { name: /Manual listing override/ });
const estimateText = () => screen.getByText('Provider requests:').closest('p');
async function confirmInitial() {
  await review();
  fireEvent.click(authorization());
  fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
  await flush();
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-17T12:00:00Z')); window.history.replaceState(null, '', '#/'); });
afterEach(() => { cleanup(); vi.useRealTimers(); window.history.replaceState(null, '', '#/'); });

const acquiredItem = (isin = A, extra = {}) => ({ ...item(isin), listing: { symbol: 'ETF.DE', exchange: 'XETR' },
  collected_at: '2026-09-17T12:00:00Z', quote_count: 42, first_date: '2020-01-01', last_date: '2026-09-16',
  annualized_mean_log_return: 0.1008, annualized_volatility: 0.1739,
  historical_year_return: 0.12, historical_year_start_date: '2025-09-16',
  historical_year_end_date: '2026-09-16', historical_year_price_basis: 'adj_close',
  historical_year_volatility: 0.1423, historical_year_volatility_start_date: '2025-09-16',
  historical_year_volatility_end_date: '2026-09-16', historical_year_volatility_price_basis: 'adj_close',
  historical_year_volatility_return_count: 250, ...extra });

describe('instrument page layout', () => {
  it('shows return statistics, then price history, then the remaining sections', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { price_currency: 'EUR' })];
    await open(A);

    const article = screen.getByRole('article', { name: 'Instrument detail' });
    expect(within(article).getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent)).toEqual([
      'iShares S&P ETF', 'Return statistics', 'Price history', 'Market data',
    ]);
    expect(within(article).queryByRole('region', { name: 'History coverage' })).not.toBeInTheDocument();
    expect(within(article).queryByText('Coverage status')).not.toBeInTheDocument();
    expect(article.lastElementChild).toHaveClass('instrument-metadata');
  });

  it('keeps the chart empty state between statistics and market data when no quotes are saved', async () => {
    mockServer();
    await open(A);

    const statistics = screen.getByRole('region', { name: 'Return statistics' });
    const emptyChart = screen.getByText(/No saved quotes to chart yet/);
    const acquisition = screen.getByRole('region', { name: 'Market data acquisition' });
    expect(statistics.compareDocumentPosition(emptyChart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(emptyChart.compareDocumentPosition(acquisition) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe('manual price currency', () => {
  it.each(['EUR', 'USD', 'CHF', 'GBP', 'RUB', 'JPY'])('hides the selector when provider prices already have %s currency', async (currency) => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { price_currency: currency }), quote(1, { price_currency: currency })];
    await open(A);
    expect(screen.queryByRole('combobox', { name: 'Price currency' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save currency' })).not.toBeInTheDocument();
    expect(screen.getByText(new RegExp(`Provider split-adjusted OHLC · Listing price currency: ${currency}`))).toBeInTheDocument();
    expect(server.currencySaves()).toHaveLength(0);
  });

  it('hides the selector for a saved currency even without quotes', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { price_currency_override: 'USD' });
    await open(A);
    expect(screen.queryByRole('form', { name: 'Set price currency' })).not.toBeInTheDocument();
    expect(screen.getByText('Price currency: USD')).toBeInTheDocument();
  });

  it('does not offer a selector while the currency is still loading or quote loading fails', async () => {
    const server = mockServer();
    server.details[A] = withData();
    const pending = deferred();
    server.page = () => pending.promise;
    await open(A);
    expect(screen.getByText(/Loading all quote pages/)).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Set price currency' })).not.toBeInTheDocument();
    await act(async () => pending.reject(new Error('offline')));
    expect(screen.getByRole('button', { name: 'Retry chart' })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Set price currency' })).not.toBeInTheDocument();
    server.page = async () => ({ items: [quote()], total: 1, offset: 0, limit: 1000 });
    fireEvent.click(screen.getByRole('button', { name: 'Retry chart' }));
    await flush();
    expect(screen.getByRole('combobox', { name: 'Price currency' })).toBeInTheDocument();
  });

  it.each([null, 'USD'])('offers the selector for inconsistent price currencies (EUR and %s)', async (currency) => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { price_currency: 'EUR' }), quote(1, { price_currency: currency })];
    await open(A);
    expect(screen.getByText(/Listing price currency: Unknown/)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Price currency' })).toBeInTheDocument();
  });

  it.each(['EUR', 'USD', 'CHF', 'GBP', 'RUB'])('saves %s explicitly and retains it on revisit without changing raw quotes', async (currency) => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote()];
    await open(A);
    const select = screen.getByRole('combobox', { name: 'Price currency' });
    expect([...select.options].map((option) => option.value)).toEqual(['', 'EUR', 'USD', 'CHF', 'GBP', 'RUB']);
    expect(select).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Save currency' })).toBeDisabled();
    expect(screen.getByText(/Listing price currency: Unknown/)).toBeInTheDocument();
    fireEvent.change(select, { target: { value: currency } });
    expect(server.currencySaves()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(server.currencySaves()).toHaveLength(1);
    expect(server.currencySaves()[0]).toEqual([`/api/instruments/${A}/price-currency`, expect.objectContaining({
      method: 'PATCH', body: JSON.stringify({ price_currency: currency }), credentials: 'same-origin', signal: expect.any(AbortSignal),
    })]);
    expect(screen.queryByRole('form', { name: 'Set price currency' })).not.toBeInTheDocument();
    expect(screen.getByText(new RegExp(`Provider split-adjusted OHLC · Listing price currency: ${currency} \\(manual\\)`))).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save currency' })).not.toBeInTheDocument();
    expect(JSON.parse(screen.getByLabelText('Quote observations (current window, all provider fields)').textContent)).toEqual([quote()]);
    await navigate(B);
    expect(screen.getByRole('combobox', { name: 'Price currency' })).toHaveValue('');
    await navigate(A);
    expect(screen.queryByRole('combobox', { name: 'Price currency' })).not.toBeInTheDocument();
    expect(screen.getByText(new RegExp(`Provider split-adjusted OHLC · Listing price currency: ${currency} \\(manual\\)`))).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('can set an unknown currency without provider configuration, budget or quotes', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { provider_configured: false, budget: { limit: 0, used: 0, remaining: 0 } });
    await open(A);
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeDisabled();
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'CHF' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(server.details[A].price_currency_override).toBe('CHF');
    expect(screen.queryByRole('combobox', { name: 'Price currency' })).not.toBeInTheDocument();
    expect(screen.getByText('Price currency: CHF')).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('keeps currency unknown on failure and allows retry of the selected currency', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote()];
    const goodSave = server.saveCurrency;
    server.saveCurrency = async () => { throw new Error('offline'); };
    await open(A);
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'USD' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('Could not save price currency');
    expect(screen.getByText(/Listing price currency: Unknown/)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Price currency' })).toHaveValue('USD');
    expect(screen.queryByText('Price currency saved.')).not.toBeInTheDocument();
    server.saveCurrency = goodSave;
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText(/Listing price currency: USD \(manual\)/)).toBeInTheDocument();
    expect(server.currencySaves()).toHaveLength(2);
  });

  it('does not claim success for a malformed save response', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote()];
    server.saveCurrency = async () => ({ price_currency_override: 'CAD' });
    await open(A);
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'EUR' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(screen.queryByText('Price currency saved.')).not.toBeInTheDocument();
    expect(screen.getByText(/Listing price currency: Unknown/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save currency' })).toBeEnabled();
  });

  it('returns to login when the session expires during a save', async () => {
    const server = mockServer();
    await open(A);
    server.fetch.mockImplementationOnce(async () => response(401, { detail: 'Not authenticated' }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'EUR' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByText('Price currency saved.')).not.toBeInTheDocument();
  });

  it('blocks duplicate saves and discards a late response after navigation', async () => {
    const server = mockServer();
    const pending = deferred();
    server.saveCurrency = () => pending.promise;
    await open(A);
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'RUB' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(screen.getByRole('combobox', { name: 'Price currency' })).toBeDisabled();
    const saving = screen.getByRole('button', { name: 'Saving…' });
    expect(saving).toBeDisabled();
    fireEvent.click(saving);
    expect(server.currencySaves()).toHaveLength(1);
    await navigate(B);
    expect(server.currencySaves()[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve({ price_currency_override: 'RUB' }));
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Price currency' })).toHaveValue('');
    expect(screen.queryByText('Price currency saved.')).not.toBeInTheDocument();
    expect(server.reads(A)).toHaveLength(1);
  });

  it('ignores a stale in-flight poll after saving and resumes polling', async () => {
    const server = mockServer();
    server.details[A] = withData(A, { acquisition: job('running') });
    server.quotes[A] = [quote()];
    await open(A);
    const oldDetail = server.details[A];
    const stale = deferred();
    server.read = () => stale.promise;
    await advance(POLL_INTERVAL);
    server.read = async (isin) => server.details[isin];
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'GBP' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(server.reads(A)[1][1].signal.aborted).toBe(true);
    await act(async () => stale.resolve(oldDetail));
    expect(screen.getByText(/Listing price currency: GBP \(manual\)/)).toBeInTheDocument();
    await advance(POLL_INTERVAL);
    expect(server.reads(A)).toHaveLength(4);
    expect(screen.queryByRole('combobox', { name: 'Price currency' })).not.toBeInTheDocument();
    expect(server.pages()).toHaveLength(1);
  });
});

describe('acquired ETF inventory', () => {
  it('shows historical price change separately from annual log statistics in one inventory read', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { name: 'Growing ETF' }),
      acquiredItem(B, { name: 'Declining ETF', annualized_mean_log_return: -0.05, annualized_volatility: 0,
        historical_year_return: -0.08, historical_year_price_basis: 'close',
        historical_year_volatility: 0.2, historical_year_volatility_price_basis: 'close' })];
    await open();
    expect(screen.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'ETF', 'Saved listing', 'Historical', 'Statistical', 'Saved quotes', 'Profile collected', 'Market dataUpdate all',
      'Annual return', 'Annual volatility', 'Annual return', 'Annual volatility',
    ]);
    expect(screen.getByRole('columnheader', { name: 'Historical' })).toHaveAttribute('colspan', '2');
    expect(screen.getByRole('columnheader', { name: 'Statistical' })).toHaveAttribute('colspan', '2');
    for (const name of ['ETF', 'Saved listing', 'Saved quotes', 'Profile collected']) {
      expect(screen.getByRole('columnheader', { name })).toHaveAttribute('rowspan', '2');
    }
    const [historical, statistical] = screen.getAllByRole('columnheader', { name: 'Annual return' });
    expect(historical).toHaveAttribute('headers', 'acquired-historical');
    expect(historical).toHaveAttribute('title', expect.stringContaining('Actual trailing 12-month price change'));
    expect(statistical).toHaveAttribute('headers', 'acquired-statistical');
    expect(statistical).toHaveAttribute('title', 'Average daily log return × 252 trading days');
    const [historicalVolatility, statisticalVolatility] = screen.getAllByRole('columnheader', { name: 'Annual volatility' });
    expect(historicalVolatility).toHaveAttribute('headers', 'acquired-historical');
    expect(historicalVolatility).toHaveAttribute('title', expect.stringContaining('Trailing 12 months'));
    expect(statisticalVolatility).toHaveAttribute('headers', 'acquired-statistical');
    expect(statisticalVolatility).toHaveAttribute('title', expect.stringContaining('All saved history'));
    const growing = within(screen.getByRole('link', { name: 'Growing ETF' }).closest('tr'));
    expect(growing.getByRole('cell', { name: '+12%' })).toHaveAttribute('title', '2025-09-16 – 2026-09-16. Adjusted closes.');
    expect(growing.getByRole('cell', { name: '+12%' })).toHaveAttribute('headers', 'acquired-historical acquired-historical-return');
    expect(growing.getByRole('cell', { name: '+12%' })).toHaveClass('return-positive');
    expect(growing.getByRole('cell', { name: '+10.08%' })).toHaveAttribute('headers', 'acquired-statistical acquired-statistical-return');
    expect(growing.getByRole('cell', { name: '+10.08%' })).toHaveClass('return-positive');
    expect(growing.getByRole('cell', { name: '17.39%' })).toHaveAttribute('headers', 'acquired-statistical acquired-statistical-volatility');
    expect(growing.getByRole('cell', { name: '17.39%' })).toHaveClass('annual-statistic', { exact: true });
    expect(growing.getByRole('cell', { name: '14.23%' })).toHaveAttribute('headers', 'acquired-historical acquired-historical-volatility');
    expect(growing.getByRole('cell', { name: '14.23%' })).toHaveAttribute('title',
      '2025-09-16 – 2026-09-16. 250 usable daily log returns. Adjusted closes. Sample standard deviation × √252; missing-weekday intervals are excluded.');
    expect(growing.getByRole('cell', { name: '14.23%' })).toHaveClass('annual-statistic', { exact: true });
    const declining = within(screen.getByRole('link', { name: 'Declining ETF' }).closest('tr'));
    expect(declining.getByRole('cell', { name: '-8%' })).toHaveAttribute('title', '2025-09-16 – 2026-09-16. Unadjusted closes; splits and distributions may affect the return.');
    expect(declining.getByRole('cell', { name: '-8%' })).toHaveClass('return-negative');
    expect(declining.getByRole('cell', { name: '-5%' })).toHaveClass('return-negative');
    expect(declining.getByRole('cell', { name: '0%' })).toHaveClass('annual-statistic', { exact: true });
    expect(declining.getByRole('cell', { name: '20%' })).toHaveClass('annual-statistic', { exact: true });
    expect(declining.getByRole('cell', { name: '20%' })).toHaveAttribute('title', expect.stringContaining('Unadjusted closes; splits and distributions may affect volatility'));
    expect(server.fetch.mock.calls.filter(([url]) => url.startsWith('/api/instruments/')).map(([url]) => url))
      .toEqual(['/api/instruments/budget', '/api/instruments/acquired?limit=20&offset=0']);
    expect(server.posts()).toHaveLength(0);
  });

  it.each([null, undefined, NaN, Infinity, -Infinity, '0.1'])('shows unavailable annual estimates rather than zero or invalid percentages: %s', async (value) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { quote_count: 0, first_date: null, last_date: null,
      annualized_mean_log_return: value, annualized_volatility: value, historical_year_return: value, historical_year_volatility: value })];
    await open();
    const row = within(screen.getByRole('link', { name: 'iShares S&P ETF' }).closest('tr'));
    const unavailable = row.getAllByRole('cell', { name: 'Unavailable' });
    expect(unavailable).toHaveLength(4);
    unavailable.forEach((cell) => expect(cell).toHaveClass('annual-statistic', { exact: true }));
    expect(row.getByText('Profile only')).toBeInTheDocument();
    expect(row.queryByText('0%')).not.toBeInTheDocument();
  });

  it.each([0, -0])('shows zero returns (%s) unsigned and neutral, not unavailable', async (value) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { historical_year_return: value, annualized_mean_log_return: value })];
    await open();
    const cells = screen.getAllByRole('cell', { name: '0%' });
    expect(cells).toHaveLength(2);
    expect(cells[0]).toHaveAttribute('title', '2025-09-16 – 2026-09-16. Adjusted closes.');
    cells.forEach((cell) => expect(cell).toHaveClass('annual-statistic', { exact: true }));
  });

  it('shows zero historical volatility as neutral, not unavailable', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { historical_year_volatility: 0 })];
    await open();
    const cell = screen.getByRole('cell', { name: '0%' });
    expect(cell).toHaveAttribute('headers', 'acquired-historical acquired-historical-volatility');
    expect(cell).toHaveClass('annual-statistic', { exact: true });
  });

  it.each([0.05, -0.05])('styles each return independently when historical and statistical directions differ (%s)', async (value) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { historical_year_return: value, annualized_mean_log_return: -value })];
    await open();
    expect(screen.getByRole('cell', { name: '+5%' })).toHaveClass('return-positive');
    expect(screen.getByRole('cell', { name: '-5%' })).toHaveClass('return-negative');
  });

  it('keeps statistical estimates visible when a full historical year is unavailable', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { historical_year_return: null, historical_year_volatility: null })];
    await open();
    const unavailable = screen.getAllByRole('cell', { name: 'Unavailable' });
    expect(unavailable).toHaveLength(2);
    unavailable.forEach((cell) => expect(cell).toHaveAttribute('title', expect.stringContaining('covering a full year')));
    expect(screen.getByRole('cell', { name: '+10.08%' })).toBeInTheDocument();
  });

  it('refreshes annual values together with the saved inventory on return from details', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem()];
    await open();
    expect(screen.getByRole('cell', { name: '+10.08%' })).toBeInTheDocument();
    await navigate(A);
    server.acquiredItems = [acquiredItem(A, { annualized_mean_log_return: -0.02, annualized_volatility: 0.25, historical_year_return: -0.03, historical_year_volatility: 0.18 })];
    await navigate(null);
    expect(screen.getByRole('cell', { name: '-2%' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '25%' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '-3%' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '18%' })).toBeInTheDocument();
    expect(screen.queryByRole('cell', { name: '14.23%' })).not.toBeInTheDocument();
    expect(screen.queryByRole('cell', { name: '+12%' })).not.toBeInTheDocument();
    expect(screen.queryByRole('cell', { name: '+10.08%' })).not.toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it.each([
    ['2026-09-17T12:00:00Z', '0 days ago'],
    ['2026-09-16T12:00:01Z', '0 days ago'],
    ['2026-09-16T12:00:00Z', '1 day ago'],
    ['2026-09-14T12:00:00Z', '3 days ago'],
    ['2026-09-15T14:00:00+02:00', '2 days ago'],
    ['2026-09-17T12:01:00Z', '0 days ago'],
  ])('shows collection age for %s with the exact UTC timestamp as a tooltip', async (collectedAt, label) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { collected_at: collectedAt })];
    await open();
    expect(screen.getByRole('columnheader', { name: 'Profile collected' })).toBeInTheDocument();
    const time = screen.getByText(label);
    const exact = `${new Date(collectedAt).toLocaleString(undefined, { timeZone: 'UTC' })} UTC`;
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('datetime', collectedAt);
    expect(time).toHaveAttribute('title', exact);
    expect(time).toHaveAttribute('aria-label', `${label}, ${exact}`);
  });

  it.each([null, 'invalid'])('handles missing or invalid collection times (%s)', async (collectedAt) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { collected_at: collectedAt })];
    await open();
    expect(screen.getByText('Unknown')).toBeInTheDocument();
    expect(screen.queryByText(/NaN days ago/)).not.toBeInTheDocument();
  });

  it('loads saved data, opens native detail links and refreshes on return without acquisitions', async () => {
    const server = mockServer();
    const pending = deferred();
    server.acquired = () => pending.promise;
    await open();
    expect(screen.getByText('Loading acquired ETFs…')).toHaveAttribute('role', 'status');
    expect(screen.queryByRole('button', { name: 'Refresh list' })).not.toBeInTheDocument();
    await act(async () => pending.resolve({ items: [acquiredItem(), acquiredItem(B, { name: 'Profile-only ETF', quote_count: 0, first_date: null, last_date: null, annualized_mean_log_return: null, annualized_volatility: null, historical_year_return: null, historical_year_volatility: null })], total: 2, limit: ACQUIRED_PAGE_SIZE, offset: 0 }));
    expect(screen.getByRole('heading', { name: 'Acquired ETFs (2)' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh list' })).not.toBeInTheDocument();
    expect(screen.getByText('42 quotes')).toBeInTheDocument();
    expect(screen.getByText('2020-01-01 – 2026-09-16')).toBeInTheDocument();
    expect(screen.getByText('Profile only')).toBeInTheDocument();
    expect(screen.queryByText('No saved quotes')).not.toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'iShares S&P ETF' });
    expect(link).toHaveAttribute('href', `#/instruments/${A}`);
    fireEvent.click(link);
    await advance(5);
    expect(screen.getByRole('heading', { name: 'iShares S&P ETF' })).toBeInTheDocument();
    server.acquired = async () => ({ items: [acquiredItem(B, { name: 'Recently acquired ETF' })], total: 1, limit: ACQUIRED_PAGE_SIZE, offset: 0 });
    fireEvent.click(screen.getByRole('link', { name: '← ETFs' }));
    await advance(5);
    expect(screen.getByRole('link', { name: 'Recently acquired ETF' })).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('shows an empty state with guidance instead of including catalogue entries', async () => {
    const server = mockServer();
    await open();
    expect(screen.getByRole('heading', { name: 'No acquired ETFs yet' })).toBeInTheDocument();
    expect(screen.getByText('Choose Add ETF to find a fund and request its data.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add ETF' })).toHaveAttribute('href', '#/add');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('paginates saved ETFs and reads updated data when returning to the first page', async () => {
    const server = mockServer();
    server.acquiredItems = Array.from({ length: ACQUIRED_PAGE_SIZE + 1 }, (_, index) => acquiredItem(`IE${String(index).padStart(10, '0')}`, { name: `Fund ${index + 1}` }));
    await open();
    expect(screen.getByText('1–20 of 21 ETFs')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous ETFs' })).toBeDisabled();
    expect(screen.queryByRole('link', { name: 'Fund 21' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next ETFs' }));
    await flush();
    expect(screen.getByRole('link', { name: 'Fund 21' })).toBeInTheDocument();
    expect(screen.getByText('21–21 of 21 ETFs')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next ETFs' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Previous ETFs' }));
    await flush();
    expect(screen.getByRole('link', { name: 'Fund 1' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next ETFs' }));
    await flush();
    server.acquiredItems = [acquiredItem(A, { name: 'Freshly saved fund' })];
    fireEvent.click(screen.getByRole('button', { name: 'Previous ETFs' }));
    await flush();
    expect(screen.getByRole('link', { name: 'Freshly saved fund' })).toBeInTheDocument();
    expect(screen.getByText('1–1 of 1 ETFs')).toBeInTheDocument();
    expect(server.fetch).toHaveBeenCalledWith('/api/instruments/acquired?limit=20&offset=20', expect.objectContaining({ signal: expect.any(AbortSignal), credentials: 'same-origin' }));
    expect(server.posts()).toHaveLength(0);
  });

  it.each(['network', 'malformed'])('allows retry after a %s error', async (failure) => {
    const server = mockServer();
    const goodRead = server.acquired;
    server.acquired = async () => {
      if (failure === 'network') throw new Error('offline');
      return { items: null, total: 3 };
    };
    await open();
    expect(screen.getByRole('alert')).toHaveTextContent(failure === 'network' ? 'Could not reach the backend' : 'unexpected acquired ETF list');
    expect(screen.queryByText('No acquired ETFs yet')).not.toBeInTheDocument();
    server.acquiredItems = [acquiredItem()];
    server.acquired = goodRead;
    fireEvent.click(screen.getByRole('button', { name: 'Retry list' }));
    await flush();
    expect(screen.getByRole('link', { name: 'iShares S&P ETF' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('aborts reads on navigation and ignores late responses after remount', async () => {
    const server = mockServer();
    const stale = deferred();
    let signal;
    server.acquired = (_, options) => { signal = options.signal; return stale.promise; };
    await open();
    await navigate(A);
    expect(signal.aborted).toBe(true);
    server.acquired = async () => ({ items: [acquiredItem(B, { name: 'Current fund' })], total: 1, limit: ACQUIRED_PAGE_SIZE, offset: 0 });
    await navigate(null);
    await act(async () => stale.resolve({ items: [acquiredItem(A, { name: 'Stale fund' })], total: 1, limit: ACQUIRED_PAGE_SIZE, offset: 0 }));
    expect(screen.getByRole('link', { name: 'Current fund' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Stale fund' })).not.toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });
});

describe('acquired ETF market-data updates', () => {
  const staleAcquiredItem = (isin = A, extra = {}) => acquiredItem(isin, { collected_at: '2026-09-16T12:00:00Z', ...extra });
  const row = (name = 'iShares S&P ETF') => within(screen.getByRole('link', { name }).closest('tr'));
  async function confirmUpdate(all = false) {
    fireEvent.click(screen.getByRole('checkbox', { name: /Allow .*updates? to fetch Yahoo Finance data/ }));
    fireEvent.click(screen.getByRole('button', { name: all ? 'Confirm update all' : 'Confirm update' }));
    await flush();
  }

  it('places Update all in the action header and requires consent before updating a row', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(), acquiredItem(B, { name: 'Second ETF' })];
    await open();
    const header = screen.getByRole('columnheader', { name: /Market data Update all/ });
    expect(header).toHaveAttribute('rowspan', '2');
    expect(within(header).getByRole('button', { name: 'Update all' })).toBeEnabled();
    expect(row().getByRole('button', { name: 'Update market data for iShares S&P ETF' })).toBeEnabled();
    expect(row().getByText('0 days ago')).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
    await review('Update market data for iShares S&P ETF');
    expect(screen.getByRole('button', { name: 'Confirm update' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(server.posts()).toHaveLength(0);
    await review('Update market data for iShares S&P ETF');
    await confirmUpdate();
    expect(server.posts()).toHaveLength(1);
    expect(server.posts()[0][0]).toBe(`/api/instruments/${A}/acquisitions`);
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
    expect(row().getByRole('status')).toHaveTextContent('Queued');
    expect(row().getByRole('button', { name: /Update market data/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Update all' })).toBeDisabled();
    expect(row('Second ETF').getByRole('button', { name: /Update market data/ })).toBeEnabled();
    server.details[A].acquisition = job('running');
    await advance(POLL_INTERVAL);
    expect(row().getByRole('status')).toHaveTextContent('Updating…');
    server.details[A].acquisition = job('succeeded');
    server.acquiredItems[0] = acquiredItem(A, { quote_count: 43, annualized_mean_log_return: 0.13 });
    await advance(POLL_INTERVAL);
    expect(row().getByRole('status')).toHaveTextContent('Updated');
    expect(row().getByRole('button', { name: /Update market data/ })).toBeEnabled();
    expect(row().getByText('43 quotes')).toBeInTheDocument();
    expect(row().getByRole('cell', { name: '+13%' })).toBeInTheDocument();
    expect(server.pages()).toHaveLength(0);
    expect(server.posts()).toHaveLength(1);
    const reads = server.reads().length;
    await advance(POLL_INTERVAL * 3);
    expect(server.reads()).toHaveLength(reads);
  });

  it('captures every page before Update all queues any jobs, even when updates reorder the inventory', async () => {
    const server = mockServer();
    const original = Array.from({ length: 101 }, (_, index) => acquiredItem(`IE${String(index).padStart(10, '0')}`, {
      name: `Fund ${index + 1}`, collected_at: index % 3 === 0 ? '2026-09-17T12:00:00Z' : '2026-09-16T12:00:00Z',
    }));
    const eligible = original.filter((_, index) => index % 3 !== 0);
    server.acquiredItems = [...original];
    server.details = Object.fromEntries(original.map((item) => [item.instrument_id, detail(item.instrument_id)]));
    const post = server.post;
    server.post = async (...args) => { server.acquiredItems.reverse(); return post(...args); };
    await open();
    fireEvent.click(screen.getByRole('button', { name: 'Next ETFs' }));
    await flush();
    await review('Update all');
    expect(screen.getByRole('heading', { name: 'Update all 101 acquired ETFs?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm update all' })).toBeDisabled();
    expect(server.posts()).toHaveLength(0);
    await confirmUpdate(true);
    expect(server.posts().map(([url]) => url)).toEqual(eligible.map((item) => `/api/instruments/${item.instrument_id}/acquisitions`));
    const urls = server.fetch.mock.calls.map(([url]) => url);
    expect(urls.indexOf('/api/instruments/acquired?limit=100&offset=100')).toBeLessThan(urls.indexOf(server.posts()[0][0]));
    expect(screen.getByText('67 market-data updates in progress.')).toBeInTheDocument();
    expect(screen.getByText('Skipped 34 ETFs collected 0 days ago.')).toBeInTheDocument();
    for (const item of eligible) server.details[item.instrument_id].acquisition = job('succeeded', { instrument_id: item.instrument_id });
    await advance(POLL_INTERVAL);
    expect(screen.getByText('67 ETFs updated.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update all' })).toBeEnabled();
    expect(server.posts()).toHaveLength(67);
  });

  it.each([
    ['just collected', '2026-09-17T12:00:00Z', true],
    ['less than 24 hours, previous date', '2026-09-16T12:00:00.001Z', true],
    ['exactly 24 hours', '2026-09-16T12:00:00Z', false],
    ['older than 24 hours', '2026-09-16T11:59:59Z', false],
    ['equivalent offset timestamp', '2026-09-16T14:00:00.001+02:00', true],
    ['future timestamp displayed as zero days', '2026-09-17T12:01:00Z', true],
    ['unknown timestamp', null, false],
    ['missing timestamp', undefined, false],
    ['invalid timestamp', 'invalid', false],
  ])('uses displayed collection age for Update all: %s', async (_, collectedAt, skip) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(A, { collected_at: collectedAt })];
    await open();
    if (skip) expect(row().getByText('0 days ago')).toBeInTheDocument();
    await review('Update all');
    expect(screen.getByText(/skipping those collected 0 days ago/)).toBeInTheDocument();
    await confirmUpdate(true);
    expect(server.posts()).toHaveLength(skip ? 0 : 1);
    if (skip) {
      expect(screen.getByText('Skipped 1 ETF collected 0 days ago.')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Update all' })).toBeEnabled();
      expect(row().getByRole('button', { name: /Update market data/ })).toBeEnabled();
      await advance(POLL_INTERVAL * 3);
      expect(server.posts()).toHaveLength(0);
      expect(server.reads()).toHaveLength(0);
    } else {
      expect(row().getByRole('status')).toHaveTextContent('Queued');
      expect(screen.queryByText(/Skipped .* collected 0 days ago/)).not.toBeInTheDocument();
    }
  });

  it('rechecks saved collection times and does not repeat a completed bulk update', async () => {
    const server = mockServer();
    server.acquiredItems = [staleAcquiredItem(), staleAcquiredItem(B, { name: 'Second ETF' })];
    await open();
    await review('Update all');
    // Another client updated B after this table loaded. The bulk read must notice.
    server.acquiredItems[1].collected_at = '2026-09-17T12:00:00Z';
    await confirmUpdate(true);
    expect(server.posts().map(([url]) => url)).toEqual([`/api/instruments/${A}/acquisitions`]);
    expect(screen.getByText('Skipped 1 ETF collected 0 days ago.')).toBeInTheDocument();
    server.details[A].acquisition = job('succeeded');
    server.acquiredItems[0].collected_at = '2026-09-17T12:00:00Z';
    await advance(POLL_INTERVAL);
    await review('Update all');
    await confirmUpdate(true);
    expect(screen.getByText('Skipped 2 ETFs collected 0 days ago.')).toBeInTheDocument();
    expect(server.posts()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Update all' })).toBeEnabled();
    // An intentional per-row update is still allowed for fresh data.
    await review('Update market data for iShares S&P ETF');
    await confirmUpdate();
    expect(server.posts()).toHaveLength(2);
    expect(screen.queryByText(/Skipped .* collected 0 days ago/)).not.toBeInTheDocument();
  });

  it('tracks independent row updates without cancelling an earlier job', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(), acquiredItem(B, { name: 'Second ETF' })];
    await open();
    await review('Update market data for iShares S&P ETF');
    await confirmUpdate();
    await review('Update market data for Second ETF');
    await confirmUpdate();
    expect(server.posts()).toHaveLength(2);
    server.details[A].acquisition = job('succeeded');
    server.details[B].acquisition = job('running', { instrument_id: B });
    await advance(POLL_INTERVAL);
    expect(row().getByRole('status')).toHaveTextContent('Updated');
    expect(row('Second ETF').getByRole('status')).toHaveTextContent('Updating…');
    expect(row('Second ETF').getByRole('button', { name: /Update market data/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Update all' })).toBeDisabled();
    server.details[B].acquisition = job('succeeded', { instrument_id: B });
    await advance(POLL_INTERVAL);
    expect(row('Second ETF').getByRole('status')).toHaveTextContent('Updated');
    expect(screen.getByRole('button', { name: 'Update all' })).toBeEnabled();
  });

  it('aborts an Update all inventory read before any job can be submitted', async () => {
    const server = mockServer();
    server.acquiredItems = [staleAcquiredItem()];
    const view = await open();
    await review('Update all');
    const pending = deferred();
    let signal;
    server.acquired = (_, options) => { signal = options.signal; return pending.promise; };
    await confirmUpdate(true);
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ items: [staleAcquiredItem()], total: 1, offset: 0, limit: 100 }));
    await advance(POLL_INTERVAL * 3);
    expect(server.posts()).toHaveLength(0);
  });

  it('continues a bulk submission after an instrument-specific validation failure', async () => {
    const server = mockServer();
    server.acquiredItems = [staleAcquiredItem(), staleAcquiredItem(B, { name: 'Second ETF' })];
    const fetch = server.fetch.getMockImplementation();
    server.fetch.mockImplementation((url, options) => url === `/api/instruments/${A}/acquisitions`
      ? response(422, { detail: 'This instrument has no valid inception date' }) : fetch(url, options));
    await open();
    await review('Update all');
    await confirmUpdate(true);
    expect(server.posts()).toHaveLength(2);
    expect(row().getByRole('alert')).toHaveTextContent('no valid inception date');
    expect(row('Second ETF').getByRole('status')).toHaveTextContent('Queued');
    await advance(POLL_INTERVAL);
    expect(row().getByRole('button', { name: /Update market data/ })).toBeEnabled();
    expect(row('Second ETF').getByRole('button', { name: /Update market data/ })).toBeDisabled();
  });

  it.each(['empty', 'duplicate', 'changed total'])('does not POST if the complete inventory cannot be captured (%s)', async (failure) => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem()];
    await open();
    await review('Update all');
    server.acquired = async (params) => {
      const offset = Number(params.get('offset'));
      if (failure === 'duplicate') return { items: [acquiredItem(), acquiredItem()], total: 2, limit: 100, offset };
      if (failure === 'changed total') return { items: [acquiredItem()], total: offset === 0 ? 2 : 3, limit: 100, offset };
      return { items: [], total: 3, limit: 100, offset };
    };
    await confirmUpdate(true);
    expect(screen.getByRole('alert')).toHaveTextContent('list changed or was incomplete');
    expect(server.posts()).toHaveLength(0);
    expect(row().getByText('42 quotes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update all' })).toBeEnabled();
  });

  it('retains saved data on a failed update, retries polling errors and rejects stale status', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem()];
    await open();
    await review('Update market data for iShares S&P ETF');
    await confirmUpdate();
    server.read = async () => { throw new Error('offline'); };
    await advance(POLL_INTERVAL);
    expect(row().getByRole('alert')).toHaveTextContent('Status will retry automatically');
    expect(row().getByRole('button', { name: /Update market data/ })).toBeDisabled();
    server.read = async () => withData(); // older successful job cannot replace the acknowledged one
    await advance(POLL_INTERVAL);
    expect(row().getByRole('button', { name: /Update market data/ })).toBeDisabled();
    expect(row().getByRole('status')).toHaveTextContent('Queued');
    server.read = async () => withData(A, { acquisition: job('failed', { error: 'Provider unavailable' }) });
    await advance(POLL_INTERVAL);
    expect(row().getByRole('alert')).toHaveTextContent('Provider unavailable');
    expect(row().getByRole('status')).toHaveTextContent('Update failed');
    expect(row().getByRole('button', { name: /Update market data/ })).toBeEnabled();
    expect(row().getByText('42 quotes')).toBeInTheDocument();
    expect(screen.getByText(/1 update needs attention/)).toBeInTheDocument();
    expect(server.posts()).toHaveLength(1);
  });

  it('reconciles an uncertain POST by GET without resubmitting and stops the bulk queue', async () => {
    const server = mockServer();
    server.acquiredItems = [staleAcquiredItem(), staleAcquiredItem(B, { name: 'Second ETF' })];
    const post = server.post;
    server.post = async (...args) => { await post(...args); throw new Error('lost response'); };
    await open();
    await review('Update all');
    await confirmUpdate(true);
    expect(server.posts()).toHaveLength(1);
    expect(screen.getByText(/1 ETFs were not submitted/)).toBeInTheDocument();
    expect(row().getByRole('button', { name: /Update market data/ })).toBeDisabled();
    await advance(POLL_INTERVAL);
    expect(row().getByRole('status')).toHaveTextContent('Queued');
    expect(row().getByRole('button', { name: /Update market data/ })).toBeDisabled();
    expect(server.posts()).toHaveLength(1);
    expect(row().getByRole('alert')).toHaveTextContent('Request could not be confirmed');
  });

  it('prevents duplicate clicks while submitting and cancels pending requests on unmount', async () => {
    const server = mockServer();
    server.acquiredItems = [staleAcquiredItem(), staleAcquiredItem(B, { name: 'Second ETF' })];
    const pending = deferred();
    let signal;
    server.post = (_, __, options) => { signal = options.signal; return pending.promise; };
    const view = await open();
    await review('Update all');
    await confirmUpdate(true);
    expect(screen.getByRole('button', { name: 'Update all' })).toBeDisabled();
    expect(row().getByRole('button', { name: /Update market data/ })).toBeDisabled();
    fireEvent.click(row().getByRole('button', { name: /Update market data/ }));
    expect(server.posts()).toHaveLength(1);
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ job: job() }));
    await advance(POLL_INTERVAL * 3);
    expect(server.posts()).toHaveLength(1);
    expect(server.reads()).toHaveLength(0);
    expect(server.reads(B)).toHaveLength(0);
  });

  it('aborts an in-flight status read and ignores its late response after remount', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem()];
    const view = await open();
    await review('Update market data for iShares S&P ETF');
    await confirmUpdate();
    const pending = deferred();
    let signal;
    server.read = (_, options) => { signal = options.signal; return pending.promise; };
    await advance(POLL_INTERVAL);
    view.unmount();
    expect(signal.aborted).toBe(true);
    await open();
    const workspaceCalls = () => server.fetch.mock.calls.filter(([url]) => url !== '/api/instruments/budget');
    const calls = workspaceCalls().length;
    await act(async () => pending.resolve(withData(A, { acquisition: job('succeeded') })));
    await advance(POLL_INTERVAL * 3);
    expect(workspaceCalls()).toHaveLength(calls);
    expect(row().queryByText('Updated')).not.toBeInTheDocument();
    expect(row().getByRole('button', { name: /Update market data/ })).toBeEnabled();
  });
});

describe('ETF autocomplete and routing', () => {
  it('opens Add ETF from the table, searches with Arrow/Enter selection and returns without acquisition', async () => {
    const server = mockServer();
    server.acquiredItems = [acquiredItem(B, { name: 'Second ETF' })];
    await open();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    const addLink = screen.getByRole('link', { name: 'Add ETF' });
    expect(addLink).toHaveAttribute('href', '#/add');
    fireEvent.click(addLink);
    await advance(5);
    expect(window.location.hash).toBe('#/add');
    expect(screen.getByRole('heading', { name: 'Add ETF' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const input = screen.getByRole('combobox', { name: 'Find an ETF' });
    expect(screen.getByText('Enter at least 2 characters.')).toHaveClass('sr-only');
    fireEvent.change(input, { target: { value: 'i' } });
    expect(screen.getByText('Enter at least 2 characters.')).not.toHaveClass('sr-only');
    await advance(SEARCH_DELAY + 20);
    expect(server.fetch.mock.calls.filter(([url]) => url.includes('/search?'))).toHaveLength(0);
    fireEvent.change(input, { target: { value: 'ishares' } });
    expect(screen.getByText('Enter at least 2 characters.')).toHaveClass('sr-only');
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
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeEnabled();
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(screen.getByRole('link', { name: '← ETFs' }));
    await advance(5);
    expect(screen.getByRole('heading', { name: 'Acquired ETFs (1)' })).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    await navigate(A);
    expect(screen.getByRole('heading', { name: 'iShares S&P ETF' })).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('loads Add ETF directly and discards a pending search when returning to the empty list', async () => {
    const server = mockServer();
    const pending = deferred();
    server.search = () => pending.promise;
    await openRoute('#/add');
    expect(screen.getByRole('heading', { name: 'Add ETF' })).toBeInTheDocument();
    expect(server.fetch.mock.calls.some(([url]) => url.includes('/acquired?'))).toBe(false);
    fireEvent.change(screen.getByRole('combobox', { name: 'Find an ETF' }), { target: { value: 'ETF' } });
    await advance(SEARCH_DELAY);
    const signal = server.fetch.mock.calls.find(([url]) => url.includes('/search?'))[1].signal;
    fireEvent.click(screen.getByRole('link', { name: '← ETFs' }));
    await advance(5);
    expect(signal.aborted).toBe(true);
    expect(screen.getByText('No acquired ETFs yet')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'Add ETF' }));
    await advance(5);
    await act(async () => pending.resolve([item(A, 'Stale fund')]));
    expect(screen.getByRole('combobox', { name: 'Find an ETF' })).toHaveValue('');
    expect(screen.queryByText('Stale fund')).not.toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });

  it('links invalid routes to Add ETF instead of displaying the shared search', async () => {
    const server = mockServer();
    await openRoute('#/instruments/invalid');
    expect(screen.getByRole('alert')).toHaveTextContent('ETF not found.');
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'Search for an ETF' }));
    await advance(5);
    expect(screen.getByRole('heading', { name: 'Add ETF' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Find an ETF' })).toBeInTheDocument();
    expect(server.reads()).toHaveLength(0);
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
    await openRoute('#/add');
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
    await openRoute('#/add');
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
    await openRoute('#/add');
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
    ['unresolved', null, false, 1, 4, 9],
    ['cached', { symbol: 'ETF.DE', exchange: 'XETR' }, false, 0, 3, 8],
    ['manual', null, true, 0, 3, 8],
  ])('includes lookup only for %s automatic listings', (_, listing, manual, lookupCalls, minimumCalls, calls) => {
    const instrument = detail(A, { listing });
    expect(acquisitionEstimate(instrument, 'update', manual)).toMatchObject({ lookupCalls, minimumCalls: 0, pages: null, calls: null });
    expect(acquisitionEstimate(instrument, 'update', manual).range).toBe('Full history from 2010-05-19 to latest available');
    instrument.catalogue.inception_date = null;
    expect(acquisitionEstimate(instrument, 'update', manual)).toMatchObject({ lookupCalls, minimumCalls: 0, pages: null, calls: null });
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
    const requestDetails = screen.getByText('Request details').closest('details');
    expect(requestDetails).not.toHaveAttribute('open');
    expect(screen.getByText(/Yahoo Finance listing from Yahoo Search/)).not.toBeVisible();
    expect(screen.queryByText(/Listing selection is not ISIN verification/)).not.toBeInTheDocument();
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
    expect(screen.getAllByText('Yahoo Finance · experimental')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Hedgehoog' }).closest('header')).toContainElement(screen.getByText('Yahoo Finance · experimental'));
    expect(within(screen.getByRole('region', { name: 'Market data acquisition' })).queryByText('Yahoo Finance · experimental')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Request details'));
    expect(requestDetails).toHaveAttribute('open');
    expect(screen.getByText(/full Yahoo Finance price history/)).toBeVisible();
    expect(authorization()).not.toBeChecked();
    expect(server.posts()).toHaveLength(0);
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
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC candlestick chart/ })).toBeInTheDocument();
    expect(screen.getByText('366 candles · 366 of 1001 quotes')).toBeInTheDocument();
    expect(screen.queryByText('Plan or listing history truncated.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update Data' })).toBeEnabled();
    const pages = server.pages().slice(-2).map(([url]) => new URL(url, 'http://localhost').searchParams);
    expect(pages.map((params) => params.get('offset'))).toEqual(['0', '1000']);
    expect(pages[0].get('as_of')).toBeNull();
    expect(pages[1].get('as_of')).toBe('2026-09-17T12:00:00Z');
    fireEvent.click(screen.getByRole('button', { name: 'all' }));
    expect(screen.getByText('1001 candles · 1001 of 1001 quotes')).toBeInTheDocument();
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
    const chart = () => screen.getByRole('img', { name: /Provider split-adjusted OHLC/ });
    expect(chart()).toBeInTheDocument();
    await review('Update Data');
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Exchange MIC')).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /Manual listing override/ })).not.toBeInTheDocument();
    expect(screen.getByText('Listing:').closest('p')).toHaveTextContent('SXR8.DE / XETR · Locked');
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
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
    expect(screen.getByText('1 candle · 1 of 1 quotes')).toBeInTheDocument();
    expect(server.posts()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Update Data' })).toBeEnabled();
  });

  it('uses a separate explicit backfill confirmation for partial coverage', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote()];
    await open(A);
    await review('Retry Older History');
    expect(screen.getByText(/Full history from 2010-05-19 to latest available/)).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
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
    expect(screen.getByText('Listing:').closest('p')).toHaveTextContent('WRONG / XXXX · Saved');
    fireEvent.click(authorization());
    fireEvent.click(manualOverride());
    expect(authorization()).not.toBeChecked();
    expect(screen.getByLabelText('Provider symbol')).toHaveValue('WRONG');
    expect(screen.getByLabelText('Provider symbol')).toBeEnabled();
    expect(screen.getByLabelText('Exchange MIC')).toBeEnabled();
    expect(screen.getByRole('link', { name: 'EUNL.DE' })).toHaveAttribute('href', 'https://finance.yahoo.com/quote/EUNL.DE');
    expect(screen.getByRole('link', { name: 'IWDA.AS' })).toHaveAttribute('href', 'https://finance.yahoo.com/quote/IWDA.AS');
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
    expect(screen.getByText('Listing:').closest('p')).toHaveTextContent('ETF.DE / XETR · Saved');
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Exchange MIC')).not.toBeInTheDocument();
    expect(manualOverride()).not.toBeChecked();
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
    expect(screen.getByText(/Yahoo Search lookup/)).toBeInTheDocument();
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
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
    fireEvent.change(screen.getByLabelText('Provider symbol'), { target: { value: 'OVERRIDE.US' } });
    fireEvent.change(screen.getByLabelText('Exchange MIC'), { target: { value: 'XNYS' } });
    fireEvent.click(authorization());
    fireEvent.click(manualOverride());
    expect(authorization()).not.toBeChecked();
    expect(screen.queryByLabelText('Provider symbol')).not.toBeInTheDocument();
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    expect(server.posts()).toHaveLength(0);
    fireEvent.click(authorization());
    fireEvent.submit(screen.getByRole('form', { name: 'Confirm request' }));
    await flush();
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
  });

  it('allows Yahoo requests when budget is null or explicitly unmetered with zero limits', async () => {
    const server = mockServer();
    server.details[A].catalogue.inception_date = null;
    server.details[B] = detail(B, { listing: { symbol: 'ETF.DE', exchange: 'XETR' }, budget: { provider: 'yfinance', metered: false, month: '2026-09', used: 0, limit: 0, remaining: 0 } });
    server.details[B].catalogue.inception_date = null;
    await open(A);
    expect(screen.queryByText(/Insufficient local budget/)).not.toBeInTheDocument();
    await review();
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
    fireEvent.click(authorization());
    expect(screen.getByRole('button', { name: 'Confirm request' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await navigate(B);
    expect(screen.queryByText(/Insufficient local budget/)).not.toBeInTheDocument();
    await review();
    fireEvent.click(authorization());
    expect(screen.getByRole('button', { name: 'Confirm request' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm request' }));
    await flush();
    expect(server.posts()).toHaveLength(1);
    expect(server.posts()[0][0]).toBe(`/api/instruments/${B}/acquisitions`);
    expect(JSON.parse(server.posts()[0][1].body)).toEqual({ confirmed: true, mode: 'update' });
  });

  it('allows an explicit valid manual override with 3 calls remaining including splits and skips lookup', async () => {
    const server = mockServer();
    server.details[A].budget = { month: '2026-09', used: 9997, limit: 10000, remaining: 3 };
    server.details[A].catalogue.inception_date = null;
    await open(A);
    await review();
    fireEvent.click(manualOverride());
    expect(estimateText()).toHaveTextContent('Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.');
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

  it.each([
    ['partial', null],
    ['partial', 'Provider coverage is limited.'],
    ['available', null],
  ])('retains %s coverage in metadata and acquisition controls without a duplicate block or warning notices (%s)', async (status, warning) => {
    const server = mockServer();
    server.details[A] = withData(A, { acquisition: job('succeeded', { warning: 'Provider job warning.' }) });
    server.details[A].summary = { ...server.details[A].summary, history_status: status, warning };
    await open(A);
    expect(screen.queryByRole('region', { name: 'History coverage' })).not.toBeInTheDocument();
    expect(screen.queryByText(status, { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText('Provider job warning.')).not.toBeInTheDocument();
    expect(screen.queryByText('Provider coverage is limited.')).not.toBeInTheDocument();
    expect(screen.queryByText(/Partial history\.|gaps may remain/)).not.toBeInTheDocument();
    const raw = JSON.parse(screen.getByLabelText('Listing, acquisition and coverage metadata').textContent);
    expect(raw.summary.history_status).toBe(status);
    expect(raw.summary.warning).toBe(warning);
    expect(raw.acquisition.warning).toBe('Provider job warning.');
    await review(status === 'partial' ? 'Retry Older History' : 'Update Data');
    expect(document.querySelector('.notice')).toBeNull();
    expect(screen.queryByText(/Uses provider quota\.|History depends on your plan/)).not.toBeInTheDocument();
    expect(authorization()).toBeVisible();
    expect(authorization()).not.toBeChecked();
    expect(screen.getByRole('button', { name: status === 'partial' ? 'Confirm older history' : 'Confirm update' })).toBeDisabled();
    expect(server.posts()).toHaveLength(0);
  });

  it('plots EUNL out-of-bounds prices as supplied without filtering on old quality flags', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { date: '2018-01-01', open: '44.213', high: '44.213', low: '44.213', close: '44.21', quality_flags: ['ohlc_bounds'] }),
      ...Array.from({ length: 250 }, (_, index) => quote(index + 1))];
    await open(A);
    expect(screen.getByText('250 candles · 250 of 251 quotes')).toBeInTheDocument();
    expect(screen.queryByText(/inconsistent OHLC bounds are excluded/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'all' }));
    expect(screen.getByText('251 candles · 251 of 251 quotes')).toBeInTheDocument();
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
    expect(screen.getByText('1 candle · 1 of 1 quotes')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC/ })).toBeInTheDocument();
  });

  it('retains WEBN null-close quotes while plotting available, zero and negative prices', async () => {
    const server = mockServer();
    server.details[A] = withData();
    server.quotes[A] = [quote(0, { open: '12.338', high: '12.34', low: '12.236', close: null }),
      quote(1), quote(2, { open: '0', high: '1', low: '-1', close: '-0.5' }), quote(3, { close: { unexpected: true } })];
    await open(A);
    expect(screen.getByText('2 candles · 4 of 4 quotes')).toBeInTheDocument();
    expect(screen.queryByText(/quotes with missing or non-numeric OHLC/)).not.toBeInTheDocument();
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
    expect(screen.getByText('1 candle · 3 of 3 quotes')).toBeInTheDocument();
    expect(document.querySelector('.notice')).toBeNull();
    const profile = screen.getByLabelText('Tickerinfo profile and collection metadata');
    expect(JSON.parse(profile.textContent)).toEqual(server.details[A].profile);
    expect(JSON.parse(screen.getByLabelText('Catalogue metadata').textContent)).toEqual(server.details[A].catalogue);
    expect(screen.getByText(/Auto-selected listing:/)).toHaveTextContent('Selected & <img src=x onerror=bad()>');
    expect(screen.getByText(/Auto-selected listing:/)).not.toHaveTextContent('not ISIN verification');
    expect(screen.getByText(/Auto-selected listing:/)).not.toBeVisible();
    expect(screen.getByText('Data details').closest('details')).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('Data details'));
    expect(screen.getByText(/Auto-selected listing:/)).toBeVisible();
    expect(profile.closest('details')).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('Provider profile'));
    expect(profile).toBeVisible();
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
    expect(screen.getByText(/Market data provider is not configured/)).toBeInTheDocument();
    server.details[B] = detail(B, { provider_configured: true, budget: { provider: 'yfinance', metered: false, month: '2026-09', used: 0, limit: 0, remaining: 0 } });
    await navigate(B);
    expect(screen.getByRole('button', { name: 'Request Data' })).toBeEnabled();
    expect(screen.queryByText(/Insufficient local budget/)).not.toBeInTheDocument();
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
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC/ })).toBeInTheDocument();
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
    expect(screen.getByText('1 candle · 1 of 1 quotes')).toBeInTheDocument();
    expect(document.querySelector('.candle title').textContent).toContain('Open 5');
    expect(screen.queryByText(/of 1001 quotes/)).not.toBeInTheDocument();
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

  it('cleans up search requests and hash listeners on unmount', async () => {
    const server = mockServer();
    const pending = deferred();
    server.search = () => pending.promise;
    const view = await openRoute('#/add');
    fireEvent.change(screen.getByRole('combobox', { name: 'Find an ETF' }), { target: { value: 'ETF' } });
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

  it('cleans up scheduled detail polls and hash listeners on unmount', async () => {
    const server = mockServer();
    server.details[A] = detail(A, { acquisition: job('queued') });
    const view = await open(A);
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    view.unmount();
    const count = server.fetch.mock.calls.length;
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
    expect(screen.getByRole('img', { name: /Provider split-adjusted OHLC/ })).toBeInTheDocument();
    expect(server.posts()).toHaveLength(0);
  });
});
