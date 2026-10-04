import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GoalPlanner, { formatEuros, monthlyAnniversary, normalizeEuros } from './GoalPlanner.jsx';
import ReturnStatistics from './ReturnStatistics.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';
import { BUY_AND_HOLD_ID } from './goalStrategies.js';
import { POLL_INTERVAL } from './useInstrument.js';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ request, user: { id: 'user-a' }, status: 'authenticated' }) }));

const A = 'IE00B4L5Y983';
const B = 'IE00B5BMR087';
const customer = { id: 'customer-1', name: 'Alice' };
const goal = { id: 'goal-1', customer_id: customer.id, name: 'Retirement', goal_eur: '100000.00', goal_date: '2036-09-17' };
const customerRoute = '#/goal/customers/customer-1';
const goalRoute = `${customerRoute}/goals/goal-1`;
const newRoute = (isin = A) => `${goalRoute}/new?instrument=${isin}`;
const buyAndHold = {
  id: BUY_AND_HOLD_ID, name: 'Buy and hold single instrument',
  description: 'Invest starting capital and each monthly contribution in one instrument and hold it.',
  parameters: [{ key: 'instrument', type: 'instrument', label: 'Instrument', required: true, universe: 'acquired' }],
};
const selection = (instrument = A) => ({ id: BUY_AND_HOLD_ID, parameters: { instrument } });
const statistics = (isin = A, extra = {}) => ({
  instrument_id: isin, name: isin === A ? 'World &amp; Europe ETF' : 'Second ETF', listing: { symbol: 'ETF.DE', exchange: 'XETR' },
  computed_at: '2026-09-17T12:00:00Z', as_of: '2026-09-17T12:00:00Z', price_basis: 'adj_close',
  quote_count: 301, price_count: 300, return_count: 299, first_date: '2025-07-01', last_date: '2026-09-16',
  daily_mean_log_return: 0.0004,
  daily_log_return_variance: 0.00012, daily_volatility: Math.sqrt(0.00012), annualized_mean_log_return: 0.1008,
  annualized_volatility: Math.sqrt(0.00012 * 252), price_currency: 'EUR', currency_source: 'provider',
  historical_year_return: 0.12, historical_year_start_date: '2025-09-16',
  historical_year_end_date: '2026-09-16', historical_year_price_basis: 'adj_close',
  eligible: true, ineligible_reason: null, warnings: [], ...extra,
});
const simulation = (body, extra = {}) => ({
  monthly_contribution_eur: '456.78', target_probability: body.reliability, success_probability: 0.9043,
  zero_contribution_success_probability: 0.15, simulation_paths: 10000, seed: 42,
  simulation_method: body.simulation_method || 'standard', block_size: body.block_size ?? null,
  valuation_date: '2026-09-17', goal_date: body.goal_date, goal_eur: body.goal_eur, contribution_count: 120,
  trajectory: [
    { date: '2026-09-17', ...Object.fromEntries(['mean', 'p05', 'p25', 'p50', 'p75', 'p95'].map((key) => [`${key}_eur`, body.starting_capital_eur])) },
    { date: body.goal_date, mean_eur: '145000.00', p05_eur: '90000.25', p25_eur: '110000.00', p50_eur: '132000.45', p75_eur: '170000.00', p95_eur: '230000.99' },
  ],
  first_contribution_date: '2026-10-17', last_contribution_date: '2036-09-17',
  total_contributions_eur: '54813.60', total_invested_eur: '64813.60',
  terminal_percentiles_eur: { p05: '90000.25', p50: '132000.45', p95: '230000.99' },
  strategy: body.strategy,
  parameter_statistics: Object.fromEntries(Object.entries(body.strategy.parameters).map(([key, value]) => [key, statistics(value)])),
  warnings: ['Historical parameters may not persist.'], ...extra,
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
  server.strategies = async () => ({ items: [buyAndHold] });
  server.acquiredItems = [A, B].map((instrument_id) => ({ instrument_id, name: statistics(instrument_id).name, ticker: 'ETF' }));
  server.acquired = async (query) => {
    const offset = Number(query.get('offset'));
    const limit = Number(query.get('limit'));
    return { items: server.acquiredItems.slice(offset, offset + limit), total: server.acquiredItems.length, offset, limit };
  };
  server.detail = {
    instrument_id: A, catalogue: { name: 'World &amp; Europe ETF', currency: 'USD', ticker: 'ETF', inception_date: '2020-01-01' },
    listing: { symbol: 'ETF.DE', exchange: 'XETR' }, profile: null, acquisition: null, price_currency_override: null,
    summary: { quote_count: 0, first_date: null, last_date: null, history_status: 'not_requested' },
    provider_configured: false, budget: null,
  };
  request.mockImplementation(async (path, options = {}) => {
    if (path.startsWith('/instruments/search?')) return { items: [{ instrument_id: A, isin: A, name: 'World &amp; Europe ETF', ticker: 'ETF' }] };
    if (path.startsWith('/instruments/acquired?')) return server.acquired(new URL(path, 'http://localhost').searchParams, options);
    if (path === '/goals/strategies') return server.strategies(options);
    if (path === '/goals/simulate') return server.simulate(options.body, options);
    if (path.startsWith('/customers?')) return { items: [{ ...customer, net_worth_eur: '0.00', goal_count: 1,
      realized_pnl_eur: '0.00', unrealized_pnl_eur: '0.00', today_pnl_eur: '0.00', total_pnl_eur: '0.00',
      today_from_date: null, today_to_date: null }], total: 1, limit: 20, offset: 0 };
    if (path === '/customers/customer-1') return structuredClone(customer);
    if (path.startsWith('/customers/customer-1/portfolios?')) return { items: [], total: 0, limit: 20, offset: 0 };
    if (path.startsWith('/customers/customer-1/goals?')) return { items: [structuredClone(goal)], total: 1, limit: Number(new URL(path, 'http://localhost').searchParams.get('limit')), offset: 0 };
    if (path === '/customers/customer-1/goals/goal-1') return structuredClone(goal);
    if (path.startsWith('/customers/customer-1/goals/goal-1/simulations?')) return { items: [], total: 0, limit: 20, offset: 0 };
    if (path === '/customers/customer-1/goals/goal-1/simulate') {
      const inputs = { goal_eur: goal.goal_eur, goal_date: goal.goal_date, ...options.body };
      return { id: 'preview-1', customer_id: customer.id, goal_id: goal.id, calculated_at: '2026-09-17T12:00:00Z', saved_at: null,
        inputs, strategy_definition: buyAndHold, result: await server.simulate(inputs, options) };
    }
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
async function openGoal(isin = A) { const view = render(<GoalPlanner isin={isin} />); await flush(); return view; }
async function openWorkspace(hash = newRoute()) {
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
  it('offers block-size shortcuts, validates custom sizes, and sends saved-goal settings', async () => {
    mockServer();
    render(<GoalPlanner isin={A} goal={goal} apiBase="/customers/customer-1/goals/goal-1" />);
    await flush();
    expect(screen.getByRole('combobox', { name: 'Simulation method' })).toHaveValue('standard');
    expect(screen.queryByRole('spinbutton', { name: 'Block size (trading days)' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: 'Simulation method' }), { target: { value: 'block_bootstrap' } });
    expect(screen.getByRole('spinbutton', { name: 'Block size (trading days)' })).toHaveValue(10);
    fireEvent.click(screen.getByRole('button', { name: '20 trading days' }));
    expect(screen.getByRole('spinbutton', { name: 'Block size (trading days)' })).toHaveValue(20);
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Block size (trading days)' }), { target: { value: '61' } });
    await submit();
    expect(screen.getByRole('alert')).toHaveTextContent('Block size must be a whole number');
    expect(posts()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '5 trading days' }));
    await submit();
    expect(posts()[0][1].body).toEqual({ strategy: selection(), starting_capital_eur: '0.00', reliability: 0.75,
      simulation_method: 'block_bootstrap', block_size: 5 });
    expect(within(result()).getByText(/Block bootstrap · 5 trading days per block/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: 'Simulation method' }), { target: { value: 'standard' } });
    expect(result()).not.toBeInTheDocument();
    await submit();
    expect(posts()[1][1].body).not.toHaveProperty('block_size');
  });

  it('rejects bootstrap results returned with different settings', async () => {
    const server = mockServer();
    server.simulate = async (body) => simulation(body, { block_size: 20 });
    await openGoal();
    fireEvent.change(screen.getByRole('combobox', { name: 'Simulation method' }), { target: { value: 'block_bootstrap' } });
    await submit();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(result()).not.toBeInTheDocument();
  });
  it('selects a strategy and its acquired instrument parameter without catalogue search, acquisition or automatic simulation', async () => {
    mockServer();
    await openGoal(null);
    const strategy = screen.getByRole('combobox', { name: 'Strategy' });
    expect(strategy).toHaveValue(BUY_AND_HOLD_ID);
    expect(within(strategy).getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option', { name: 'Buy and hold single instrument' })).toBeInTheDocument();
    expect(screen.getByText('Instrument parameter · acquired universe only')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    expect(screen.queryByText('Model assumptions and limitations')).not.toBeInTheDocument();
    expect(calculate()).toBeDisabled();
    fireEvent.change(screen.getByRole('combobox', { name: 'Instrument' }), { target: { value: A } });
    await flush();
    expect(window.location.hash).toBe('#/');
    expect(screen.getByRole('heading', { name: 'World & Europe ETF' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('EUR · 299 daily returns');
    expect(calculate()).toBeEnabled();
    expect(request.mock.calls.map(([path]) => path)).toEqual(['/goals/strategies', '/instruments/acquired?limit=100&offset=0', `/instruments/${A}/statistics`]);
    expect(posts()).toHaveLength(0);
  });

  it('shows an empty acquired universe and links to explicit acquisition without searching the catalogue', async () => {
    const server = mockServer();
    server.acquiredItems = [];
    await openGoal(null);
    expect(screen.getByText(/No acquired instruments yet/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add an ETF' })).toHaveAttribute('href', '#/add');
    expect(within(screen.getByLabelText('Instrument')).getAllByRole('option')).toHaveLength(1);
    expect(calculate()).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Goal planner inputs' }));
    expect(posts()).toHaveLength(0);
    expect(reads()).toHaveLength(0);
    expect(request.mock.calls.some(([path]) => path.includes('/search'))).toBe(false);
  });

  it('does not trust a legacy shortcut to an unacquired instrument, even if its statistics would be eligible', async () => {
    const server = mockServer();
    server.acquiredItems = server.acquiredItems.filter((item) => item.instrument_id !== A);
    await openGoal();
    expect(screen.getByRole('alert')).toHaveTextContent('not in the acquired universe');
    expect(screen.getByRole('link', { name: 'acquire its data first' })).toHaveAttribute('href', `#/instruments/${A}`);
    expect(calculate()).toBeDisabled();
    expect(reads()).toHaveLength(0);
    fireEvent.change(screen.getByLabelText('Instrument'), { target: { value: B } });
    await flush();
    expect(calculate()).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await submit();
    expect(posts()[0][1].body.strategy).toEqual(selection(B));
  });

  it('loads all acquired pages so an instrument beyond the first 100 can be selected', async () => {
    const server = mockServer();
    server.acquiredItems = Array.from({ length: 101 }, (_, index) => ({ instrument_id: `IE${String(index).padStart(10, '0')}`, name: `Fund ${index}` }));
    await openGoal(null);
    const instrument = screen.getByLabelText('Instrument');
    expect(within(instrument).getAllByRole('option')).toHaveLength(102);
    expect(request.mock.calls.map(([path]) => path)).toContain('/instruments/acquired?limit=100&offset=100');
    fireEvent.change(instrument, { target: { value: server.acquiredItems[100].instrument_id } });
    await flush();
    await submit();
    expect(posts()[0][1].body.strategy.parameters.instrument).toBe(server.acquiredItems[100].instrument_id);
    expect(result()).toBeInTheDocument();
  });

  it('loads planner resources, reports failures and retries reads without any acquisition', async () => {
    const server = mockServer();
    const pending = deferred();
    server.strategies = () => pending.promise;
    await openGoal();
    expect(screen.getByRole('status')).toHaveTextContent('Loading strategies and acquired instruments');
    expect(calculate()).toBeDisabled();
    await act(async () => pending.reject(new Error('offline')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load planner data: offline');
    server.strategies = async () => ({ items: [buyAndHold] });
    fireEvent.click(screen.getByRole('button', { name: 'Retry planner data' }));
    await flush();
    expect(calculate()).toBeEnabled();
    expect(posts()).toHaveLength(0);
  });

  it.each(['empty', 'duplicate', 'changed total', 'network'])('rejects an incomplete acquired universe (%s) and allows an explicit retry', async (failure) => {
    const server = mockServer();
    const acquired = server.acquired;
    server.acquired = async (query) => {
      if (failure === 'network') throw new Error('offline');
      const offset = Number(query.get('offset'));
      if (failure === 'duplicate') return { items: [server.acquiredItems[0], server.acquiredItems[0]], total: 2, limit: 100, offset };
      if (failure === 'changed total') return { items: [server.acquiredItems[0]], total: offset === 0 ? 2 : 3, limit: 100, offset };
      return { items: [], total: 1, limit: 100, offset };
    };
    await openGoal();
    expect(screen.getByRole('alert')).toHaveTextContent(failure === 'network' ? 'offline' : 'list changed or was incomplete');
    expect(calculate()).toBeDisabled();
    expect(reads()).toHaveLength(0);
    server.acquired = acquired;
    fireEvent.click(screen.getByRole('button', { name: 'Retry planner data' }));
    await flush();
    expect(calculate()).toBeEnabled();
    expect(posts()).toHaveLength(0);
  });

  it.each([null, [], [{ ...buyAndHold, parameters: [{}] }], [buyAndHold, buyAndHold]])('rejects a malformed strategy catalogue: %j', async (items) => {
    const server = mockServer();
    server.strategies = async () => ({ items });
    await openGoal();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected strategy catalogue');
    expect(calculate()).toBeDisabled();
    expect(posts()).toHaveLength(0);
  });

  it('navigates using native ETF, detail and goal links and supports invalid routes', async () => {
    mockServer();
    await openWorkspace();
    fireEvent.click(screen.getByRole('link', { name: 'Instrument details' }));
    await advance(5);
    expect(window.location.hash).toBe(`#/instruments/${A}`);
    expect(screen.getByRole('region', { name: 'Return statistics' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'Plan a goal with this ETF' }));
    await advance(5);
    expect(window.location.hash).toBe(`#/goal/${A}`);
    expect(screen.getByRole('link', { name: 'Customers' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('button', { name: 'Calculate monthly contribution' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: /Alice/ }));
    await advance(5);
    expect(window.location.hash).toBe(`${customerRoute}?instrument=${A}`);
    expect(screen.getByRole('link', { name: 'Portfolios', exact: true })).toHaveAttribute('aria-current', 'page');
    fireEvent.click(screen.getByRole('link', { name: 'Goals', exact: true }));
    await advance(5);
    expect(window.location.hash).toBe(`${customerRoute}/goals?instrument=${A}`);
    fireEvent.click(screen.getByRole('link', { name: /Retirement/ }));
    await advance(5);
    expect(window.location.hash).toBe(`${goalRoute}?instrument=${A}`);
    fireEvent.click(screen.getByRole('link', { name: 'New simulation' }));
    await advance(5);
    expect(window.location.hash).toBe(newRoute());
    expect(screen.getByLabelText('Instrument')).toHaveValue(A);
    expect(calculate()).toBeEnabled();
    fireEvent.click(screen.getByRole('link', { name: 'ETFs' }));
    await advance(5);
    expect(window.location.hash).toBe('#/');
    expect(screen.queryByRole('article', { name: 'Customers' })).not.toBeInTheDocument();
    await navigate('#/goal/%ZZ');
    expect(screen.getByRole('alert')).toHaveTextContent('ETF not found');
    expect(posts()).toHaveLength(0);
  });

  it.each([
    ['non-EUR', { price_currency: 'USD', eligible: false, ineligible_reason: 'Only EUR-traded instruments are supported.' }],
    ['unknown currency', { price_currency: null, currency_source: null, eligible: false, ineligible_reason: 'Price currency is unknown.' }],
    ['no data', { quote_count: 0, price_count: 0, return_count: 0, first_date: null, last_date: null, daily_mean_log_return: null, daily_log_return_variance: null, daily_volatility: null, annualized_mean_log_return: null, annualized_volatility: null, eligible: false, ineligible_reason: 'Download history first.' }],
    ['insufficient history', { return_count: 59, eligible: false, ineligible_reason: 'At least 60 return observations are required.' }],
  ])('gates %s and explains how to obtain eligible saved data', async (_, extra) => {
    const server = mockServer();
    server.stats = async () => statistics(A, extra);
    await openGoal();
    expect(calculate()).toBeDisabled();
    expect(screen.getByText(/Not eligible for goal planning/)).toBeInTheDocument();
    expect(screen.getByText('Download history or update the price currency in instrument details.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Instrument details' })).toHaveAttribute('href', `#/instruments/${A}`);
    fireEvent.submit(screen.getByRole('form', { name: 'Goal planner inputs' }));
    expect(posts()).toHaveLength(0);
  });

  it('renders daily and annual log returns and volatility as percentages, with counts, basis and warnings', async () => {
    const server = mockServer();
    server.stats = async () => statistics(A, { price_basis: 'close', warnings: ['Some rows were unusable.'], currency_source: 'manual' });
    await openGoal();
    fireEvent.click(screen.getByText('Saved return statistics'));
    expect(screen.getAllByRole('term').slice(0, 4).map((term) => term.textContent)).toEqual([
      'Daily return', 'Daily volatility', 'Annual return', 'Annual volatility',
    ]);
    for (const [label, value] of [['Daily return', '0.04%'], ['Daily volatility', '1.1%'],
      ['Annual return', '10.08%'], ['Annual volatility', '17.39%']]) {
      expect(within(screen.getByText(label).closest('div')).getByText(value)).toBeInTheDocument();
    }
    expect(screen.getByText('Annual return')).toHaveAttribute('title', 'Average daily log return × 252 trading days');
    expect(screen.queryByText(/simple.return|fraction²/i)).not.toBeInTheDocument();
    expect(screen.queryByText('0.00012')).not.toBeInTheDocument();
    expect(screen.getByText('301 / 300 / 299')).toBeInTheDocument();
    expect(screen.getByText('2025-07-01 – 2026-09-16')).toBeInTheDocument();
    expect(screen.getByText('Unadjusted close')).toBeInTheDocument();
    expect(screen.getByText('Unadjusted prices: splits and distributions may affect returns.')).toBeInTheDocument();
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

describe('metadata-driven strategy configuration', () => {
  const pair = {
    id: 'test_pair', name: 'Test-only pair', description: 'Two acquired instruments.',
    parameters: ['primary', 'secondary'].map((key) => ({ key, type: 'instrument', label: key === 'primary' ? 'Primary instrument' : 'Secondary instrument', required: true, universe: 'acquired' })),
  };

  it('renders and submits multiple instrument parameters for a different strategy without strategy-specific form code', async () => {
    const server = mockServer();
    server.strategies = async () => ({ items: [buyAndHold, pair] });
    await openGoal();
    fireEvent.change(screen.getByLabelText('Target (EUR)'), { target: { value: '250000' } });
    await submit();
    expect(result()).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: pair.id } });
    expect(result()).not.toBeInTheDocument();
    expect(calculate()).toBeDisabled();
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue('250000');
    expect(screen.queryByLabelText('Instrument')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Primary instrument'), { target: { value: A } });
    await flush();
    expect(calculate()).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Secondary instrument'), { target: { value: B } });
    await flush();
    expect(calculate()).toBeEnabled();
    await submit();
    expect(posts().at(-1)[1].body.strategy).toEqual({ id: pair.id, parameters: { primary: A, secondary: B } });
    expect(result()).toBeInTheDocument();
    fireEvent.click(within(result()).getByText('Calculation details'));
    expect(within(result()).getByText(`Primary instrument · ${A}`)).toBeVisible();
    expect(within(result()).getByText(`Secondary instrument · ${B}`)).toBeVisible();
    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: BUY_AND_HOLD_ID } });
    expect(result()).not.toBeInTheDocument();
    expect(screen.getByLabelText('Instrument')).toHaveValue('');
    expect(calculate()).toBeDisabled();
  });

  it.each([{ type: 'number' }, { type: 'instrument', universe: 'catalogue' }])('fails closed for unsupported parameter definitions: %j', async (extra) => {
    const server = mockServer();
    server.strategies = async () => ({ items: [{ ...buyAndHold, parameters: [{ ...buyAndHold.parameters[0], ...extra }] }] });
    await openGoal();
    expect(screen.getByRole('alert')).toHaveTextContent('not supported');
    expect(calculate()).toBeDisabled();
    expect(posts()).toHaveLength(0);
  });

  it('aborts in-flight simulations and statistics when the strategy changes', async () => {
    const server = mockServer();
    server.strategies = async () => ({ items: [buyAndHold, pair] });
    await openGoal();
    const pending = deferred();
    server.simulate = () => pending.promise;
    await submit();
    const options = posts()[0][1];
    const statsSignal = reads()[0][1].signal;
    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: pair.id } });
    expect(options.signal.aborted).toBe(true);
    expect(statsSignal.aborted).toBe(true);
    await act(async () => pending.resolve(simulation(options.body)));
    expect(result()).not.toBeInTheDocument();
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
      strategy: selection(), goal_eur: '100000.00', starting_capital_eur: '10000.00', goal_date: '2036-09-17', reliability: 0.75,
    } })]]);
    expect(result()).toBeInTheDocument();
    const view = within(result());
    expect(view.getByRole('status')).toHaveTextContent('€456.78 per month');
    expect(view.getByRole('status')).toHaveTextContent('90.43% estimated success · 75% target');
    expect(view.getByText('120 payments')).toBeInTheDocument();
    expect(view.getByText('€54,813.60')).toBeInTheDocument();
    expect(view.getByText('€64,813.60')).toBeInTheDocument();
    expect(view.getByText('€90,000.25')).toBeInTheDocument();
    expect(view.getByText('€132,000.45')).toBeInTheDocument();
    expect(view.getByText('€230,000.99')).toBeInTheDocument();
    expect(view.getByRole('img', { name: 'Monte Carlo portfolio projection' })).toBeInTheDocument();
    expect(view.getByText('Goal: €100,000.00')).toBeInTheDocument();
    expect(view.queryByText('Historical parameters may not persist.')).not.toBeInTheDocument();
    expect(view.queryByRole('list', { name: 'Simulation warnings' })).not.toBeInTheDocument();
    expect(view.queryByText(/simulated paths · seed/)).not.toBeInTheDocument();
    expect(screen.queryByText('Model assumptions and limitations')).not.toBeInTheDocument();
    expect(screen.queryByText(/Independent, normally distributed log returns/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No taxes, transaction fees or inflation/)).not.toBeInTheDocument();
    const details = view.getByText('Calculation details');
    expect(details.closest('details')).not.toHaveAttribute('open');
    fireEvent.click(details);
    expect(view.getByText('2026-10-17 / 2036-09-17')).toBeVisible();
    expect(view.getByText('Daily return')).toBeVisible();
    expect(view.getByText('0.04%')).toBeVisible();
    expect(view.getByText('Daily volatility')).toBeVisible();
    expect(view.getByText('1.1%')).toBeVisible();
    expect(view.getByText('Annual return')).toBeVisible();
    expect(view.getByText('10.08%')).toBeVisible();
    expect(view.getByText('Annual volatility')).toBeVisible();
    expect(view.getByText('17.39%')).toBeVisible();
  });

  it('keeps data-quality warnings in calculation details without model caveats', async () => {
    const server = mockServer();
    server.simulate = async (body) => simulation(body, {
      parameter_statistics: { instrument: statistics(A, { warnings: ['Some rows were unusable.'] }) },
      warnings: ['Some rows were unusable.', 'Historical parameters may not persist.'],
    });
    await openGoal();
    await submit();
    const view = within(result());
    expect(view.getByText('Calculation details').closest('details')).not.toHaveAttribute('open');
    fireEvent.click(view.getByText('Calculation details'));
    expect(view.getByRole('list', { name: 'Statistics warnings' })).toHaveTextContent('Some rows were unusable.');
    expect(view.queryByText('Historical parameters may not persist.')).not.toBeInTheDocument();
  });

  it('handles a zero monthly contribution without claiming certainty', async () => {
    const server = mockServer();
    server.simulate = async (body) => simulation(body, { monthly_contribution_eur: '0.00', zero_contribution_success_probability: 0.93 });
    await openGoal();
    await submit();
    expect(within(result()).getByRole('status')).toHaveTextContent('€0.00 per month');
    expect(within(result()).getByRole('status')).toHaveTextContent('estimated success');
    expect(within(result()).getByText('No monthly contribution needed.')).toBeInTheDocument();
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
    { strategy: selection(B) }, { strategy: { id: 'wrong', parameters: { instrument: A } } },
    { strategy: { ...selection(), parameters: { instrument: A, extra: true } } },
    { parameter_statistics: { instrument: statistics(B) } }, { parameter_statistics: {} },
    { target_probability: 0.7 },
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
  it('clears results when an instrument parameter changes and preserves goal inputs', async () => {
    mockServer();
    await openGoal();
    fireEvent.change(screen.getByLabelText('Target (EUR)'), { target: { value: '123456' } });
    await submit();
    expect(result()).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Instrument'), { target: { value: B } });
    expect(result()).not.toBeInTheDocument();
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue('123456');
    await flush();
    await submit();
    expect(posts().at(-1)[1].body.strategy).toEqual(selection(B));
    fireEvent.change(screen.getByLabelText('Instrument'), { target: { value: '' } });
    expect(result()).not.toBeInTheDocument();
    expect(calculate()).toBeDisabled();
  });

  it('aborts an in-flight simulation when its parameter changes and ignores a late result', async () => {
    const server = mockServer();
    await openGoal();
    const pending = deferred();
    server.simulate = () => pending.promise;
    await submit();
    const options = posts()[0][1];
    fireEvent.change(screen.getByLabelText('Instrument'), { target: { value: B } });
    expect(options.signal.aborted).toBe(true);
    await flush();
    server.simulate = async (body) => simulation(body, { monthly_contribution_eur: '987.65' });
    await submit();
    await act(async () => pending.resolve(simulation(options.body)));
    expect(within(result()).getByRole('status')).toHaveTextContent('€987.65');
    expect(posts()).toHaveLength(2);
  });

  it('aborts planner-resource requests on unmount and ignores late acquired data', async () => {
    const server = mockServer();
    const pending = deferred();
    server.acquired = () => pending.promise;
    const view = await openGoal();
    const options = request.mock.calls.find(([path]) => path.includes('/acquired?'))[1];
    view.unmount();
    expect(options.signal.aborted).toBe(true);
    await act(async () => pending.resolve({ items: server.acquiredItems, total: 2, limit: 100, offset: 0 }));
    expect(reads()).toHaveLength(0);
    expect(posts()).toHaveLength(0);
  });

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
    fireEvent.click(screen.getByText('Saved return statistics'));
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
    await navigate(newRoute(B));
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(statistics()));
    expect(screen.getByRole('heading', { name: 'Second ETF' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'World & Europe ETF' })).not.toBeInTheDocument();
    expect(calculate()).toBeDisabled();
  });

  it.each([newRoute(B), goalRoute, customerRoute, '#/goal', '#/'])('aborts pending simulations when navigating to %s and discards late results', async (hash) => {
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
    await navigate(newRoute());
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
  it.each([1, 89])('hides the %s missing-weekday interval warning on the instrument page but retains other warnings', async (count) => {
    const server = mockServer();
    const gapWarning = `${count} interval(s) spanning missing weekdays excluded from daily returns. Exchange holidays may also be excluded; no exchange calendar is assumed.`;
    server.stats = async () => statistics(A, { warnings: [gapWarning, 'Some rows were unusable.'] });
    await openWorkspace(`#/instruments/${A}`);
    expect(screen.queryByText(gapWarning)).not.toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Statistics warnings' })).toHaveTextContent('Some rows were unusable.');
    expect(screen.getByText('301 / 300 / 299')).toBeInTheDocument();
    expect(screen.getByText('2025-07-01 – 2026-09-16')).toBeInTheDocument();

    await navigate(newRoute());
    fireEvent.click(screen.getByText('Saved return statistics'));
    expect(screen.getByText(gapWarning)).toBeVisible();
  });

  it('does not render an empty warnings list when only the missing-weekday warning exists', async () => {
    const server = mockServer();
    server.stats = async () => statistics(A, { warnings: [
      '89 interval(s) spanning missing weekdays excluded from daily returns. Exchange holidays may also be excluded; no exchange calendar is assumed.',
    ] });
    render(<ReturnStatistics isin={A} />);
    await flush();
    expect(screen.queryByRole('list', { name: 'Statistics warnings' })).not.toBeInTheDocument();
    expect(screen.queryByText(/spanning missing weekdays/)).not.toBeInTheDocument();
  });

  it('replaces daily metrics with historical annual return on the instrument page', async () => {
    mockServer();
    await openWorkspace(`#/instruments/${A}`);
    const view = within(screen.getByRole('region', { name: 'Return statistics' }));
    expect(view.getAllByRole('term').slice(0, 3).map((term) => term.textContent)).toEqual([
      'Historical annual return', 'Annual return', 'Annual volatility',
    ]);
    expect(view.queryByText('Daily return')).not.toBeInTheDocument();
    expect(view.queryByText('Daily volatility')).not.toBeInTheDocument();
    expect(view.getByText('Historical annual return')).toHaveAttribute('title',
      'Actual trailing-12-month price change ending at the latest saved quote');
    expect(view.getByText('+12%')).toHaveAttribute('title', '2025-09-16 – 2026-09-16. Adjusted closes.');
    expect(view.getByText('+12%')).toHaveClass('return-positive');
    expect(view.getByText('Annual return')).toHaveAttribute('title', 'Average daily log return × 252 trading days');
    expect(view.getByText('+10.08%')).toHaveClass('return-positive');
    expect(view.getByText('17.39%')).toBeInTheDocument();
    expect(reads()).toHaveLength(1);
    expect(posts()).toHaveLength(0);
  });

  it.each([
    [0.05, '+5%', 'return-positive'], [-0.05, '-5%', 'return-negative'], [0, '0%', ''], [-0, '0%', ''],
  ])('signs and colors both instrument returns %s while leaving volatility neutral', async (value, text, className) => {
    const server = mockServer();
    server.stats = async () => statistics(A, { historical_year_return: value, annualized_mean_log_return: value });
    render(<ReturnStatistics isin={A} />);
    await flush();
    for (const label of ['Historical annual return', 'Annual return']) {
      const display = within(screen.getByText(label).closest('div')).getByRole('definition');
      expect(display.textContent).toBe(text);
      expect(display.className).toBe(className);
    }
    const volatility = within(screen.getByText('Annual volatility').closest('div')).getByRole('definition');
    expect(volatility.textContent).toBe('17.39%');
    expect(volatility.className).toBe('');
  });

  it.each([[0.12, '+12%'], [-0.08, '-8%'], [0, '0%']])('uses the observed historical return %s without annualizing or compounding it', async (value, display) => {
    const server = mockServer();
    server.stats = async () => statistics(A, { historical_year_return: value, historical_year_price_basis: 'close' });
    render(<ReturnStatistics isin={A} />);
    await flush();
    const historicalRow = screen.getByText('Historical annual return').closest('div');
    expect(within(historicalRow).getByText(display)).toHaveAttribute('title',
      '2025-09-16 – 2026-09-16. Unadjusted closes; splits and distributions may affect the return.');
  });

  it('does not substitute annualized statistics when historical annual return is unavailable', async () => {
    const server = mockServer();
    server.stats = async () => statistics(A, { historical_year_return: null });
    render(<ReturnStatistics isin={A} />);
    await flush();
    expect(within(screen.getByText('Historical annual return').closest('div')).getByText('Unavailable'))
      .toHaveAttribute('title', expect.stringContaining('covering a full year'));
    expect(within(screen.getByText('Annual return').closest('div')).getByText('+10.08%')).toBeInTheDocument();
  });

  it.each([[null, 'Unavailable'], [-0.1008, '-10.08%'], [0, '0%']])('renders annual log mean %s without falling back to legacy simple returns', async (mean, display) => {
    const server = mockServer();
    server.stats = async () => statistics(A, { annualized_mean_log_return: mean, annualized_mean_return: 0.1 });
    render(<ReturnStatistics isin={A} />);
    await flush();
    const meanRow = screen.getByText('Annual return').closest('div');
    expect(within(meanRow).getByText(display)).toBeInTheDocument();
    expect(screen.queryByText('10%')).not.toBeInTheDocument();
  });

  it.each([[null, 'Unavailable'], [undefined, 'Unavailable'], [NaN, 'Unavailable'], [Infinity, 'Unavailable'], [-Infinity, 'Unavailable'], [0, '0%']])('renders unavailable or zero return/volatility values correctly: %s', async (value, display) => {
    const server = mockServer();
    server.stats = async () => statistics(A, {
      historical_year_return: value, annualized_mean_log_return: value, annualized_volatility: value,
    });
    render(<ReturnStatistics isin={A} />);
    await flush();
    for (const label of ['Historical annual return', 'Annual return', 'Annual volatility']) {
      const metric = within(screen.getByText(label).closest('div')).getByRole('definition');
      expect(metric.textContent).toBe(display);
      expect(metric.className).toBe('');
    }
  });

  it('shows a negative annual log return without compounding it into a simple return', async () => {
    const server = mockServer();
    server.stats = async () => statistics(A, { daily_mean_log_return: -0.0004, annualized_mean_log_return: -0.1008 });
    render(<ReturnStatistics isin={A} />);
    await flush();
    expect(screen.queryByText('Daily return')).not.toBeInTheDocument();
    expect(within(screen.getByText('Historical annual return').closest('div')).getByText('+12%')).toHaveClass('return-positive');
    expect(within(screen.getByText('Annual return').closest('div')).getByText('-10.08%')).toHaveClass('return-negative');
  });

  it('updates statistics after acquisition completion without an acquisition POST or currency controls', async () => {
    const server = mockServer();
    server.detail.acquisition = { id: 'job-1', status: 'running', requested_at: '2026-09-17T12:00:00Z' };
    await openWorkspace(`#/instruments/${A}`);
    expect(reads()).toHaveLength(1);
    server.detail = { ...server.detail, acquisition: { ...server.detail.acquisition, status: 'succeeded' } };
    await advance(POLL_INTERVAL);
    expect(reads()).toHaveLength(2);
    expect(screen.queryByRole('combobox', { name: 'Price currency' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save currency' })).not.toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });

  it('hides obsolete statistics on a refresh key change and rejects a late old response', async () => {
    const server = mockServer();
    const pending = deferred();
    server.stats = () => pending.promise;
    const view = render(<ReturnStatistics isin={A} refreshKey="running" />);
    await flush();
    const signal = reads()[0][1].signal;
    server.stats = async () => statistics(A, { return_count: 600, historical_year_return: 0.15 });
    view.rerender(<ReturnStatistics isin={A} refreshKey="succeeded" />);
    await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(statistics()));
    expect(screen.getByText('301 / 300 / 600')).toBeInTheDocument();
    expect(screen.queryByText('301 / 300 / 299')).not.toBeInTheDocument();
    expect(screen.getByText('+15%')).toBeInTheDocument();
    expect(screen.queryByText('+12%')).not.toBeInTheDocument();
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
    expect(screen.queryByText('Daily return')).not.toBeInTheDocument();
    expect(screen.queryByText('Daily volatility')).not.toBeInTheDocument();
    expect(screen.getByText('Historical annual return')).toBeInTheDocument();
    expect(screen.getByText('+12%')).toBeInTheDocument();
    expect(screen.getByText('Annual return')).toBeInTheDocument();
    expect(screen.getByText('+10.08%')).toBeInTheDocument();
    expect(screen.getByText('Annual volatility')).toBeInTheDocument();
    expect(screen.getByText('17.39%')).toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });
});
