import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GoalPlanner, { formatEuros, monthlyAnniversary, normalizeEuros } from './GoalPlanner.jsx';
import ReturnStatistics from './ReturnStatistics.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';
import { SEARCH_DELAY } from './InstrumentSearch.jsx';
import { POLL_INTERVAL } from './useInstrument.js';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ request }) }));

const A = 'IE00B4L5Y983';
const B = 'IE00B5BMR087';
const statistics = (isin = A, extra = {}) => ({
  instrument_id: isin, name: isin === A ? 'World &amp; Europe ETF' : 'Second ETF', listing: { symbol: 'ETF.DE', exchange: 'XETR' },
  computed_at: '2026-09-17T12:00:00Z', as_of: '2026-09-17T12:00:00Z', price_basis: 'adj_close',
  quote_count: 301, price_count: 300, return_count: 299, first_date: '2025-07-01', last_date: '2026-09-16',
  daily_mean_return: 0.0005, daily_return_variance: 0.000123, daily_mean_log_return: 0.0004,
  daily_log_return_variance: 0.00012, annualized_volatility: 0.176, price_currency: 'EUR', currency_source: 'provider',
  eligible: true, ineligible_reason: null, warnings: [], ...extra,
});
const simulation = (body, extra = {}) => ({
  monthly_contribution_eur: '456.78', target_probability: body.reliability, success_probability: 0.9043,
  zero_contribution_success_probability: 0.15, simulation_paths: 10000, seed: 42,
  valuation_date: '2026-09-17', goal_date: body.goal_date, goal_eur: body.goal_eur, contribution_count: 120,
  trajectory: [
    { date: '2026-09-17', ...Object.fromEntries(['mean', 'p05', 'p25', 'p50', 'p75', 'p95'].map((key) => [`${key}_eur`, body.starting_capital_eur])) },
    { date: body.goal_date, mean_eur: '145000.00', p05_eur: '90000.25', p25_eur: '110000.00', p50_eur: '132000.45', p75_eur: '170000.00', p95_eur: '230000.99' },
  ],
  first_contribution_date: '2026-10-17', last_contribution_date: '2036-09-17',
  total_contributions_eur: '54813.60', total_invested_eur: '64813.60',
  terminal_percentiles_eur: { p05: '90000.25', p50: '132000.45', p95: '230000.99' },
  statistics: statistics(body.instrument_id), warnings: ['Historical parameters may not persist.'], ...extra,
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function mockServer() {
  const server = {};
  server.stats = async (isin) => statistics(isin);
  server.simulate = async (body) => simulation(body);
  server.detail = {
    instrument_id: A, catalogue: { name: 'World &amp; Europe ETF', currency: 'USD', ticker: 'ETF', inception_date: '2020-01-01' },
    listing: { symbol: 'ETF.DE', exchange: 'XETR' }, profile: null, acquisition: null, price_currency_override: null,
    summary: { quote_count: 0, first_date: null, last_date: null, history_status: 'not_requested' },
    provider_configured: false, budget: null,
  };
  request.mockImplementation(async (path, options = {}) => {
    if (path.startsWith('/instruments/search?')) return { items: [{ instrument_id: A, isin: A, name: 'World &amp; Europe ETF', ticker: 'ETF' }] };
    if (path.startsWith('/instruments/acquired?')) return { items: [], total: 0, offset: 0, limit: 20 };
    if (path === '/goals/simulate') return server.simulate(options.body, options);
    const match = path.match(/^\/instruments\/([^/]+)(?:\/(statistics|quotes|price-currency))?(?:\?.*)?$/);
    if (match?.[2] === 'statistics') return server.stats(match[1], options);
    if (match?.[2] === 'quotes') return { items: [], total: 0, offset: 0, limit: 1000, as_of: '2026-09-17T12:00:00Z' };
    if (match?.[2] === 'price-currency') {
      server.detail = { ...server.detail, price_currency_override: options.body.price_currency };
      return { price_currency_override: options.body.price_currency };
    }
    if (match) return { ...server.detail, instrument_id: match[1] };
    throw new Error(`Unexpected request: ${path}`);
  });
  return server;
}
const posts = () => request.mock.calls.filter(([, options]) => options?.method === 'POST');
const reads = (isin = A) => request.mock.calls.filter(([path]) => path === `/instruments/${isin}/statistics`);
const calculate = () => screen.getByRole('button', { name: 'Calculate monthly contribution' });
const result = () => screen.queryByRole('region', { name: 'Goal simulation result' });
async function flush() { await act(async () => {}); }
async function advance(ms) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function openGoal(isin = A) { const view = render(<GoalPlanner isin={isin} onSelect={vi.fn()} />); await flush(); return view; }
async function openWorkspace(hash = `#/goal/${A}`) {
  window.history.replaceState(null, '', hash);
  const view = render(<InstrumentWorkspace />);
  await flush();
  return view;
}
async function navigate(hash) {
  await act(async () => { window.location.hash = hash; window.dispatchEvent(new Event('hashchange')); });
}
async function submit() { fireEvent.click(calculate()); await flush(); }

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-17T23:59:00Z'));
  request.mockReset();
  window.history.replaceState(null, '', '#/');
});
afterEach(() => { cleanup(); vi.useRealTimers(); window.history.replaceState(null, '', '#/'); });

describe('planner routes and eligibility', () => {
  it('selects a planner ISIN via catalogue search without detail reads, acquisition or automatic simulation', async () => {
    mockServer();
    await openWorkspace('#/goal');
    expect(screen.getByText(/Select an ETF to check/)).toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Goal planner' })).toHaveAttribute('aria-current', 'page');
    fireEvent.change(screen.getByRole('combobox', { name: 'Find an ETF' }), { target: { value: 'World' } });
    await advance(SEARCH_DELAY);
    fireEvent.click(screen.getByRole('option', { name: /World & Europe ETF/ }));
    await flush();
    expect(window.location.hash).toBe(`#/goal/${A}`);
    expect(screen.getByRole('heading', { name: 'World & Europe ETF' })).toBeInTheDocument();
    expect(screen.getByText(/Eligible for EUR goal planning/)).toBeInTheDocument();
    expect(calculate()).toBeEnabled();
    expect(request.mock.calls.map(([path]) => path)).toEqual([expect.stringContaining('/instruments/search?'), `/instruments/${A}/statistics`]);
    expect(posts()).toHaveLength(0);
  });

  it('navigates using native ETF, detail and goal links and supports invalid routes', async () => {
    mockServer();
    await openWorkspace();
    fireEvent.click(screen.getByRole('link', { name: 'Instrument details and data' }));
    await advance(5);
    expect(window.location.hash).toBe(`#/instruments/${A}`);
    expect(screen.getByRole('region', { name: 'Return statistics' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'Plan a goal with this ETF' }));
    await advance(5);
    expect(window.location.hash).toBe(`#/goal/${A}`);
    expect(calculate()).toBeEnabled();
    fireEvent.click(screen.getByRole('link', { name: 'ETFs' }));
    await advance(5);
    expect(window.location.hash).toBe('#/');
    expect(screen.queryByRole('article', { name: 'Goal planner' })).not.toBeInTheDocument();
    await navigate('#/goal/%ZZ');
    expect(screen.getByRole('alert')).toHaveTextContent('ETF not found');
    expect(posts()).toHaveLength(0);
  });

  it.each([
    ['non-EUR', { price_currency: 'USD', eligible: false, ineligible_reason: 'Only EUR-traded instruments are supported.' }],
    ['unknown currency', { price_currency: null, currency_source: null, eligible: false, ineligible_reason: 'Price currency is unknown.' }],
    ['no data', { quote_count: 0, price_count: 0, return_count: 0, first_date: null, last_date: null, daily_mean_return: null, daily_return_variance: null, annualized_volatility: null, eligible: false, ineligible_reason: 'Download history first.' }],
    ['insufficient history', { return_count: 59, eligible: false, ineligible_reason: 'At least 60 return observations are required.' }],
  ])('gates %s and explains how to obtain eligible saved data', async (_, extra) => {
    const server = mockServer();
    server.stats = async () => statistics(A, extra);
    await openGoal();
    expect(calculate()).toBeDisabled();
    expect(screen.getByText(/Not eligible for goal planning/)).toBeInTheDocument();
    expect(screen.getByText(/Open instrument details to explicitly download/)).toHaveTextContent('set the correct listing price currency');
    expect(screen.getByRole('link', { name: 'Instrument details and data' })).toHaveAttribute('href', `#/instruments/${A}`);
    fireEvent.submit(screen.getByRole('form', { name: 'Goal planner inputs' }));
    expect(posts()).toHaveLength(0);
  });

  it('renders daily statistics, raw fraction-squared variance, counts, basis and warnings', async () => {
    const server = mockServer();
    server.stats = async () => statistics(A, { price_basis: 'close', warnings: ['Some rows were unusable.'], currency_source: 'manual' });
    await openGoal();
    fireEvent.click(screen.getByText('Saved return statistics'));
    expect(screen.getByText('0.05%')).toBeInTheDocument();
    expect(screen.getByText('0.000123')).toBeInTheDocument();
    expect(screen.queryByText('0.0123%')).not.toBeInTheDocument();
    expect(screen.getByText('17.6%')).toBeInTheDocument();
    expect(screen.getByText('301 / 300 / 299')).toBeInTheDocument();
    expect(screen.getByText('2025-07-01 – 2026-09-16')).toBeInTheDocument();
    expect(screen.getByText('Unadjusted close')).toBeInTheDocument();
    expect(screen.getByText(/unadjusted closes may distort/)).toBeInTheDocument();
    expect(screen.getByText('Some rows were unusable.')).toBeInTheDocument();
    expect(screen.getByText('EUR (manual)')).toBeInTheDocument();
  });

  it('shows loading, handles statistics failures and retries only the saved-data read', async () => {
    const server = mockServer();
    const pending = deferred();
    server.stats = () => pending.promise;
    await openGoal();
    expect(screen.getByText(/Checking saved data/)).toHaveAttribute('role', 'status');
    expect(calculate()).toBeDisabled();
    await act(async () => pending.reject(new Error('offline')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load statistics: offline');
    server.stats = async () => statistics();
    fireEvent.click(screen.getByRole('button', { name: 'Retry statistics' }));
    await flush();
    expect(calculate()).toBeEnabled();
    expect(reads()).toHaveLength(2);
    expect(posts()).toHaveLength(0);
  });

  it('does not enable simulation for malformed or wrong-instrument statistics', async () => {
    const server = mockServer();
    server.stats = async () => statistics(B);
    await openGoal();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(calculate()).toBeDisabled();
  });
});

describe('explicit monthly simulation', () => {
  it('sends exact euro strings and a probability, then displays the returned schedule and outcomes', async () => {
    mockServer();
    await openGoal();
    fireEvent.change(screen.getByLabelText('Target (EUR)'), { target: { value: '0100000.00' } });
    fireEvent.change(screen.getByLabelText('Starting capital (EUR)'), { target: { value: '10000' } });
    expect(posts()).toHaveLength(0);
    await submit();
    expect(posts()).toEqual([['/goals/simulate', expect.objectContaining({ method: 'POST', signal: expect.any(AbortSignal), body: {
      instrument_id: A, goal_eur: '100000.00', starting_capital_eur: '10000.00', goal_date: '2036-09-17', reliability: 0.9,
    } })]]);
    expect(result()).toBeInTheDocument();
    const view = within(result());
    expect(view.getByRole('status')).toHaveTextContent('€456.78 per month');
    expect(view.getByRole('status')).toHaveTextContent('90.43% model-estimated success');
    expect(view.getByText('120 payments')).toBeInTheDocument();
    expect(view.getByText('2026-10-17 / 2036-09-17')).toBeInTheDocument();
    expect(view.getByText('€54,813.60')).toBeInTheDocument();
    expect(view.getByText('€64,813.60')).toBeInTheDocument();
    expect(view.getByText('€90,000.25')).toBeInTheDocument();
    expect(view.getByText('€132,000.45')).toBeInTheDocument();
    expect(view.getByText('€230,000.99')).toBeInTheDocument();
    expect(view.getByText('Historical parameters may not persist.')).toBeInTheDocument();
    expect(view.getByText(/10,000 simulated paths · seed 42/)).toBeInTheDocument();
    expect(view.getByRole('img', { name: 'Monte Carlo portfolio projection' })).toBeInTheDocument();
    expect(view.getByText(/all 10,000 simulated traces/)).toBeInTheDocument();
    expect(view.getByText('Goal: €100,000.00')).toBeInTheDocument();
    expect(screen.getByText('Model assumptions and limitations').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText(/Independent, normally distributed log returns/)).toHaveTextContent('252 trading days');
    expect(screen.getByText(/No taxes, transaction fees or inflation/)).toBeInTheDocument();
  });

  it('handles a zero monthly contribution without claiming certainty', async () => {
    const server = mockServer();
    server.simulate = async (body) => simulation(body, { monthly_contribution_eur: '0.00', zero_contribution_success_probability: 0.93 });
    await openGoal();
    await submit();
    expect(within(result()).getByRole('status')).toHaveTextContent('€0.00 per month');
    expect(within(result()).getByRole('status')).toHaveTextContent('Not a guarantee');
    expect(within(result()).getByText(/no additional monthly contribution is needed/)).toBeInTheDocument();
  });

  it('blocks duplicates while busy and makes failures explicitly retryable', async () => {
    const server = mockServer();
    const pending = deferred();
    server.simulate = () => pending.promise;
    await openGoal();
    await submit();
    expect(screen.getByRole('button', { name: 'Simulating…' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Goal planner inputs' }));
    expect(posts()).toHaveLength(1);
    await act(async () => pending.reject(new Error('Backend UTC date validation failed.')));
    expect(result()).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Backend UTC date validation failed');
    await advance(10000);
    expect(posts()).toHaveLength(1);
    server.simulate = async (body) => simulation(body);
    await submit();
    expect(result()).toBeInTheDocument();
    expect(posts()).toHaveLength(2);
  });

  it('rejects an unexpected simulation response', async () => {
    const server = mockServer();
    server.simulate = async () => ({ monthly_contribution_eur: '4' });
    await openGoal();
    await submit();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(result()).not.toBeInTheDocument();
  });

  it.each([
    { trajectory: [] }, { goal_eur: '1.00' }, { simulation_paths: 0 },
    { trajectory: [{ date: '2026-09-17', mean_eur: 'NaN' }] },
  ])('rejects missing or inconsistent projection data: %j', async (extra) => {
    const server = mockServer();
    server.simulate = async (body) => simulation(body, extra);
    await openGoal();
    await submit();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(screen.queryByRole('img', { name: 'Monte Carlo portfolio projection' })).not.toBeInTheDocument();
  });

  it.each([
    ['Target (EUR)', '0', /target greater than zero/],
    ['Target (EUR)', '-10', /target greater than zero/],
    ['Target (EUR)', '100.001', /two decimal places/],
    ['Target (EUR)', '1e6', /two decimal places/],
    ['Starting capital (EUR)', '-1', /starting capital of zero or more/],
    ['Starting capital (EUR)', '', /starting capital of zero or more/],
    ['Reliability target (%)', '49', /between 50% and 99%/],
    ['Reliability target (%)', '100', /between 50% and 99%/],
    ['Reliability target (%)', '', /between 50% and 99%/],
    ['Goal date (UTC)', '2026-10-16', /next monthly anniversary/],
    ['Goal date (UTC)', '2076-09-18', /50 years/],
    ['Goal date (UTC)', '', /next monthly anniversary/],
  ])('validates %s = %s before POST', async (label, value, message) => {
    mockServer();
    await openGoal();
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    await submit();
    expect(screen.getByRole('alert')).toHaveTextContent(message);
    expect(posts()).toHaveLength(0);
  });

  it.each([['50', '2026-10-17'], ['99', '2076-09-17']])('accepts inclusive reliability %s and date %s boundaries', async (reliability, date) => {
    mockServer();
    await openGoal();
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveAttribute('min', '2026-10-17');
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveAttribute('max', '2076-09-17');
    fireEvent.change(screen.getByLabelText('Reliability target (%)'), { target: { value: reliability } });
    fireEvent.change(screen.getByLabelText('Goal date (UTC)'), { target: { value: date } });
    fireEvent.change(screen.getByLabelText('Starting capital (EUR)'), { target: { value: '0' } });
    await submit();
    expect(posts()[0][1].body).toMatchObject({ reliability: Number(reliability) / 100, goal_date: date, starting_capital_eur: '0.00' });
    expect(result()).toBeInTheDocument();
  });

  it('preserves exact large decimal amounts and clamps UTC anniversaries', () => {
    expect(normalizeEuros('9007199254740993.01')).toBe('9007199254740993.01');
    expect(formatEuros('9007199254740993.01')).toBe('€9,007,199,254,740,993.01');
    expect(monthlyAnniversary(new Date('2026-01-31T23:59:00Z'), 1)).toBe('2026-02-28');
    expect(monthlyAnniversary(new Date('2028-01-31T00:00:00Z'), 1)).toBe('2028-02-29');
    expect(monthlyAnniversary(new Date('2028-02-29T00:00:00Z'), 600)).toBe('2078-02-28');
  });
});

describe('stale reads and results', () => {
  it.each([
    ['Target (EUR)', '200000'], ['Starting capital (EUR)', '20000'],
    ['Reliability target (%)', '95'], ['Goal date (UTC)', '2037-09-17'],
  ])('immediately clears completed results when %s changes', async (label, value) => {
    mockServer();
    await openGoal();
    await submit();
    expect(result()).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    expect(result()).not.toBeInTheDocument();
    await flush();
    expect(posts()).toHaveLength(1);
  });

  it.each(['resolve', 'reject'])('aborts an outdated POST and ignores its late %s even if fetch ignores cancellation', async (outcome) => {
    const server = mockServer();
    const pending = deferred();
    server.simulate = () => pending.promise;
    await openGoal();
    await submit();
    const [, first] = posts()[0];
    fireEvent.change(screen.getByLabelText('Target (EUR)'), { target: { value: '200000' } });
    expect(first.signal.aborted).toBe(true);
    expect(calculate()).toBeEnabled();
    server.simulate = async (body) => simulation(body, { monthly_contribution_eur: '987.65' });
    await submit();
    await act(async () => outcome === 'resolve' ? pending.resolve(simulation(first.body)) : pending.reject(new Error('stale error')));
    expect(within(result()).getByRole('status')).toHaveTextContent('€987.65');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(posts()).toHaveLength(2);
  });

  it('invalidates a completed result on statistics refresh and disables calculation while refreshing', async () => {
    const server = mockServer();
    await openGoal();
    await submit();
    const pending = deferred();
    server.stats = () => pending.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh statistics' }));
    expect(result()).not.toBeInTheDocument();
    expect(calculate()).toBeDisabled();
    await act(async () => pending.resolve(statistics(A, { eligible: false, price_currency: 'USD', ineligible_reason: 'EUR required' })));
    expect(calculate()).toBeDisabled();
    expect(posts()).toHaveLength(1);
  });

  it('aborts late statistics on instrument changes and never shows the old eligibility', async () => {
    const server = mockServer();
    const pending = deferred();
    server.stats = (isin) => isin === A ? pending.promise : Promise.resolve(statistics(B, { eligible: false, price_currency: 'USD' }));
    await openWorkspace();
    const signal = reads()[0][1].signal;
    await navigate(`#/goal/${B}`);
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(statistics()));
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'World & Europe ETF' })).not.toBeInTheDocument();
    expect(calculate()).toBeDisabled();
  });

  it.each([`#/goal/${B}`, '#/goal', '#/'])('aborts pending simulations when navigating to %s and discards late results', async (hash) => {
    const server = mockServer();
    const pending = deferred();
    server.simulate = () => pending.promise;
    await openWorkspace();
    await submit();
    const [, options] = posts()[0];
    await navigate(hash);
    expect(options.signal.aborted).toBe(true);
    await act(async () => pending.resolve(simulation(options.body)));
    expect(result()).not.toBeInTheDocument();
    await navigate(`#/goal/${A}`);
    expect(result()).not.toBeInTheDocument();
    expect(posts()).toHaveLength(1);
  });

  it('cleans up pending statistics and simulations on unmount', async () => {
    const server = mockServer();
    const statsPending = deferred();
    server.stats = () => statsPending.promise;
    const first = await openGoal();
    const statsSignal = reads()[0][1].signal;
    first.unmount();
    expect(statsSignal.aborted).toBe(true);
    await act(async () => statsPending.resolve(statistics()));
    server.stats = async () => statistics();
    const second = await openGoal();
    const postPending = deferred();
    server.simulate = () => postPending.promise;
    await submit();
    const options = posts()[0][1];
    second.unmount();
    expect(options.signal.aborted).toBe(true);
    await act(async () => postPending.resolve(simulation(options.body)));
    expect(result()).not.toBeInTheDocument();
  });
});

describe('detail statistics refresh', () => {
  it('updates statistics after acquisition completion and a saved manual currency without an acquisition POST', async () => {
    const server = mockServer();
    server.detail.acquisition = { id: 'job-1', status: 'running', requested_at: '2026-09-17T12:00:00Z' };
    await openWorkspace(`#/instruments/${A}`);
    expect(reads()).toHaveLength(1);
    server.detail = { ...server.detail, acquisition: { ...server.detail.acquisition, status: 'succeeded' } };
    await advance(POLL_INTERVAL);
    expect(reads()).toHaveLength(2);
    fireEvent.change(screen.getByRole('combobox', { name: 'Price currency' }), { target: { value: 'EUR' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save currency' }));
    await flush();
    expect(reads()).toHaveLength(3);
    expect(posts()).toHaveLength(0);
  });

  it('hides obsolete statistics on a refresh key change and rejects a late old response', async () => {
    const server = mockServer();
    const pending = deferred();
    server.stats = () => pending.promise;
    const view = render(<ReturnStatistics isin={A} refreshKey="running" />);
    await flush();
    const signal = reads()[0][1].signal;
    server.stats = async () => statistics(A, { return_count: 600 });
    view.rerender(<ReturnStatistics isin={A} refreshKey="succeeded" />);
    await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(statistics()));
    expect(screen.getByText('301 / 300 / 600')).toBeInTheDocument();
    expect(screen.queryByText('301 / 300 / 299')).not.toBeInTheDocument();
  });

  it('keeps detail usable when the statistics endpoint is unavailable and offers retry', async () => {
    const server = mockServer();
    server.stats = async () => { throw new Error('not available'); };
    await openWorkspace(`#/instruments/${A}`);
    expect(screen.getByRole('heading', { name: 'World & Europe ETF' })).toBeInTheDocument();
    expect(screen.getByText('Return statistics unavailable: not available')).toBeInTheDocument();
    server.stats = async () => statistics();
    fireEvent.click(screen.getByRole('button', { name: 'Retry statistics' }));
    await flush();
    expect(screen.getByText('0.000123')).toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });
});
