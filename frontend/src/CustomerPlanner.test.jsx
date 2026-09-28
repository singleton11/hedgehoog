import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CustomerPlanner from './CustomerPlanner.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';
import { BUY_AND_HOLD_ID } from './goalStrategies.js';

const { request, auth } = vi.hoisted(() => ({ request: vi.fn(), auth: { user: { id: 'user-a' }, status: 'authenticated' } }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ ...auth, request: auth.request || request }) }));
const A = 'IE00B4L5Y983', B = 'IE00B5BMR087';
const uid = (number) => number.toString(16).padStart(24, '0');
const customer = { id: uid(1), name: 'Alice' };
const customerSummary = (value) => ({ net_worth_eur: '0.00', goal_count: 0,
  realized_pnl_eur: '0.00', unrealized_pnl_eur: '0.00', today_pnl_eur: '0.00', total_pnl_eur: '0.00',
  today_from_date: null, today_to_date: null, ...value });
const goal = { id: uid(101), customer_id: customer.id, name: 'Retirement', goal_eur: '100000.00', goal_date: '2036-09-24', created_at: '2026-08-01T12:00:00.000000Z' };
const buyAndHold = {
  id: BUY_AND_HOLD_ID, name: 'Buy and hold single instrument', description: 'Invest and hold one acquired instrument.',
  parameters: [{ key: 'instrument', type: 'instrument', label: 'Instrument', required: true, universe: 'acquired' }],
};
const pair = { id: 'test_pair', name: 'Two instruments', description: 'A test-only strategy with generic parameters.',
  parameters: ['primary', 'secondary'].map((key) => ({ key, label: `${key} instrument`, type: 'instrument', required: true, universe: 'acquired' })) };
const selection = (instrument = A) => ({ id: BUY_AND_HOLD_ID, parameters: { instrument } });
const settings = (extra = {}) => ({ strategy: selection(), starting_capital_eur: '0.00', reliability: 0.75, ...extra });
const customerPath = (cid = customer.id) => `/customers/${cid}`;
const goalPath = (gid = goal.id, cid = customer.id) => `${customerPath(cid)}/goals/${gid}`;
const customerRoute = (cid = customer.id) => `#/goal/customers/${cid}`;
const goalRoute = (gid = goal.id, cid = customer.id) => `${customerRoute(cid)}/goals/${gid}`;
const newRoute = (isin = A, gid = goal.id) => `${goalRoute(gid)}/new${isin ? `?instrument=${isin}` : ''}`;
const reportRoute = (sid, gid = goal.id) => `${goalRoute(gid)}/simulations/${sid}`;
const reportPath = (sid, gid = goal.id) => `${goalPath(gid)}/simulations/${sid}`;
const pagePath = (path, offset = 0) => `${path}?limit=20&offset=${offset}`;
const historyPath = (offset = 0) => pagePath(`${goalPath()}/simulations`, offset);
function statistics(isin = A) {
  return { instrument_id: isin, name: isin === A ? 'World ETF' : 'Second ETF', listing: { symbol: 'ETF.DE', exchange: 'XETR' },
    computed_at: '2026-09-24T12:00:00Z', as_of: '2026-09-24T11:00:00Z', price_basis: 'adj_close',
    quote_count: 301, price_count: 300, return_count: 299, first_date: '2025-07-01', last_date: '2026-09-23',
    daily_mean_log_return: 0.0004, daily_log_return_variance: 0.00012, daily_volatility: Math.sqrt(0.00012),
    annualized_mean_log_return: 0.1008, annualized_volatility: Math.sqrt(0.00012 * 252), price_currency: 'EUR', currency_source: 'provider',
    eligible: true, ineligible_reason: null, warnings: ['Some rows were unusable.'] };
}
function simulation(inputs, monthly = '456.78') {
  return { strategy: structuredClone(inputs.strategy), goal_eur: inputs.goal_eur, goal_date: inputs.goal_date,
    monthly_contribution_eur: monthly, target_probability: inputs.reliability, success_probability: 0.9043,
    zero_contribution_success_probability: 0.15, simulation_paths: 10000, seed: 42,
    valuation_date: '2026-09-24', contribution_count: 120, first_contribution_date: '2026-10-24', last_contribution_date: inputs.goal_date,
    total_contributions_eur: '54813.60', total_invested_eur: '64813.60',
    terminal_percentiles_eur: { p05: '90000.25', p50: '132000.45', p95: '230000.99' },
    trajectory: [
      { date: '2026-09-24', ...Object.fromEntries(['mean', 'p05', 'p25', 'p50', 'p75', 'p95'].map((key) => [`${key}_eur`, inputs.starting_capital_eur])) },
      { date: inputs.goal_date, mean_eur: '145000.00', p05_eur: '90000.25', p25_eur: '110000.00', p50_eur: '132000.45', p75_eur: '170000.00', p95_eur: '230000.99' },
    ],
    parameter_statistics: Object.fromEntries(Object.entries(inputs.strategy.parameters).map(([key, value]) => [key, statistics(value)])), warnings: [],
  };
}
function summary(run) {
  const { id, calculated_at, saved_at, inputs, strategy_definition, result } = run;
  return { id, is_plan: run.is_plan || false, calculated_at, saved_at, inputs: structuredClone(inputs), strategy_name: strategy_definition.name,
    monthly_contribution_eur: result.monthly_contribution_eur, success_probability: result.success_probability };
}
function page(items, query) {
  const offset = Number(query.get('offset')), limit = Number(query.get('limit'));
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function mockServer({ seed = true } = {}) {
  let nextCustomer = 2, nextGoal = 102, nextSimulation = 1001;
  const server = { customers: new Map(), goals: new Map(), previews: new Map(), saved: new Map(), definitions: [buyAndHold, pair], monthly: '456.78',
    acquiredItems: [A, B].map((instrument_id) => ({ instrument_id, name: statistics(instrument_id).name })) };
  if (seed) { server.customers.set(customer.id, structuredClone(customer)); server.goals.set(goal.id, structuredClone(goal)); }
  server.preview = (gid = goal.id, body = settings()) => {
    const fixed = server.goals.get(gid);
    const inputs = structuredClone({ ...body, goal_eur: fixed.goal_eur, goal_date: fixed.goal_date });
    const run = { id: uid(nextSimulation++), customer_id: fixed.customer_id, goal_id: gid, calculated_at: '2026-09-24T12:00:00Z', saved_at: null,
      inputs, strategy_definition: structuredClone(server.definitions.find((item) => item.id === inputs.strategy.id)), result: simulation(inputs, server.monthly) };
    server.previews.set(run.id, structuredClone(run));
    return structuredClone(run);
  };
  server.persist = (sid) => {
    if (!server.saved.has(sid)) {
      if (!server.previews.has(sid)) throw new Error('Preview expired. Calculate again.');
      server.saved.set(sid, structuredClone({ ...server.previews.get(sid), saved_at: new Date().toISOString() }));
    }
    return structuredClone(server.saved.get(sid));
  };
  server.seedRun = (body = settings(), gid = goal.id) => server.persist(server.preview(gid, body).id);
  server.createCustomer = async (body) => {
    const value = { id: uid(nextCustomer++), ...body };
    server.customers.set(value.id, structuredClone(value)); return value;
  };
  server.createGoal = async (cid, body) => {
    const value = { id: uid(nextGoal++), customer_id: cid, created_at: new Date().toISOString(), ...body };
    server.goals.set(value.id, structuredClone(value)); return value;
  };
  server.updateGoal = async (cid, gid, body) => {
    const current = await server.goal(cid, gid);
    if (!current) throw new Error('Goal not found.');
    const updated = { ...current, ...body };
    server.goals.set(gid, structuredClone(updated)); return updated;
  };
  server.customer = async (cid) => server.customers.get(cid);
  server.goal = async (cid, gid) => server.goals.get(gid)?.customer_id === cid ? server.goals.get(gid) : null;
  server.customerList = async (query) => page([...server.customers.values()].map((item) => customerSummary({
    goal_count: [...server.goals.values()].filter((goal) => goal.customer_id === item.id).length, ...item,
  })), query);
  server.goalList = async (cid, query) => page([...server.goals.values()].filter((item) => item.customer_id === cid)
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id)), query);
  server.history = async (cid, gid, query) => page([...server.saved.values()].filter((run) => run.customer_id === cid && run.goal_id === gid)
    .sort((a, b) => b.calculated_at.localeCompare(a.calculated_at) || b.id.localeCompare(a.id)).map(summary), query);
  server.report = async (cid, gid, sid) => {
    const run = server.saved.get(sid);
    if (!run || run.customer_id !== cid || run.goal_id !== gid) throw new Error('Saved simulation not found.');
    return run;
  };
  server.simulate = async (cid, gid, body) => server.preview(gid, body);
  server.save = async (cid, gid, sid) => server.persist(sid);
  server.makePlan = async (cid, gid, sid) => {
    const run = await server.report(cid, gid, sid);
    run.is_plan = true;
    return run;
  };
  server.strategies = async () => ({ items: server.definitions });
  server.acquired = async (query) => page(server.acquiredItems, query);
  server.stats = async (isin) => statistics(isin);
  async function dispatch(path, options) {
    const url = new URL(path, 'http://localhost'), post = options.method === 'POST';
    if (url.pathname === '/customers') return post ? server.createCustomer(options.body, options) : server.customerList(url.searchParams, options);
    if (/^\/customers\/[^/]+\/(portfolios|plans)$/.test(url.pathname)) return page([], url.searchParams);
    if (path === '/goals/strategies') return server.strategies(options);
    if (url.pathname === '/instruments/acquired') return server.acquired(url.searchParams, options);
    const stats = url.pathname.match(/^\/instruments\/([^/]+)\/statistics$/);
    if (stats) return server.stats(stats[1], options);
    const match = url.pathname.match(/^\/customers\/([^/]+)(?:\/goals(?:\/([^/]+)(?:\/(simulate|simulations)(?:\/([^/]+)(?:\/(save|plan))?)?)?)?)?$/);
    if (!match) throw new Error(`Unexpected request: ${path}`);
    const [, cid, gid, operation, sid, save] = match;
    if (!gid) {
      if (url.pathname.endsWith('/goals')) return post ? server.createGoal(cid, options.body, options) : server.goalList(cid, url.searchParams, options);
      return server.customer(cid, options);
    }
    if (operation === 'simulate' && post) return server.simulate(cid, gid, options.body, options);
    if (operation === 'simulations') {
      if (save === 'plan' && post) return server.makePlan(cid, gid, sid, options);
      if (save === 'save' && post) return server.save(cid, gid, sid, options);
      return sid ? server.report(cid, gid, sid, options) : server.history(cid, gid, url.searchParams, options);
    }
    return options.method === 'PATCH' ? server.updateGoal(cid, gid, options.body, options) : server.goal(cid, gid, options);
  }
  // JSON-like boundaries prevent rendered data or submitted bodies from aliasing persistent server objects.
  server.handle = async (path, options = {}) => structuredClone(await dispatch(path,
    { ...options, ...(options.body ? { body: structuredClone(options.body) } : {}) }));
  request.mockImplementation(server.handle);
  return server;
}
const posts = () => request.mock.calls.filter(([, options]) => options?.method === 'POST');
const patches = () => request.mock.calls.filter(([, options]) => options?.method === 'PATCH');
const reads = (path) => request.mock.calls.filter(([url]) => url === path);
const result = () => screen.queryByRole('region', { name: 'Goal simulation result' });
const calculate = () => screen.getByRole('button', { name: 'Calculate monthly contribution' });
const saveButton = () => screen.queryByRole('button', { name: 'Save simulation' });
function change(label, value) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function addCustomer() { fireEvent.click(screen.getByRole('button', { name: 'Add customer' })); }
function addGoal() { fireEvent.click(screen.getByRole('button', { name: 'Add goal' })); }
async function flush() { await act(async () => {}); }
async function advance(ms = 5) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function open(route = '#/goal') {
  window.history.replaceState(null, '', route);
  const view = render(<InstrumentWorkspace />); await flush(); return view;
}
async function navigate(route) {
  await act(async () => { window.history.pushState(null, '', route); window.dispatchEvent(new Event('hashchange')); });
}
async function follow(name) { fireEvent.click(screen.getByRole('link', { name })); await advance(); }
async function submit() { fireEvent.click(calculate()); await flush(); }
async function save() { fireEvent.click(saveButton()); await flush(); await advance(); }
function comparison() { return screen.getByRole('table', { name: `Saved simulations for ${goal.name}` }); }
function historyLinks() { return within(comparison()).getAllByRole('link').map((link) => link.getAttribute('href')); }
function expectNoPlannerReads() {
  expect(request.mock.calls.some(([path]) => path.startsWith('/instruments') || path.startsWith('/goals/'))).toBe(false);
}
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-24T12:00:00Z')); request.mockReset();
  auth.user = { id: 'user-a' }; auth.status = 'authenticated'; delete auth.request;
  window.history.replaceState(null, '', '#/');
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); window.history.replaceState(null, '', '#/'); });

describe('customer portfolio summary table', () => {
  it('shows exact EUR totals, zero and unavailable values with native customer links', async () => {
    const server = mockServer();
    server.customers.set(customer.id, { ...customer, net_worth_eur: '9007199254740993.01' });
    server.customers.set(uid(2), { id: uid(2), name: 'No portfolios', net_worth_eur: '0.00' });
    server.customers.set(uid(3), { id: uid(3), name: 'Missing prices', net_worth_eur: null });
    await open();
    const table = within(screen.getByRole('table', { name: 'Customer portfolio summary' }));
    expect(table.getByRole('columnheader', { name: 'Customer', exact: true })).toBeInTheDocument();
    expect(table.getByRole('columnheader', { name: 'Net worth (EUR)' })).toBeInTheDocument();
    expect(table.getAllByRole('row')).toHaveLength(4);
    expect(table.getByRole('row', { name: /Alice/ })).toHaveTextContent('€9,007,199,254,740,993.01');
    expect(table.getByRole('row', { name: /No portfolios/ })).toHaveTextContent('€0.00');
    const unavailable = within(table.getByRole('row', { name: /Missing prices/ }));
    expect(unavailable.getByText('Unavailable')).toBeInTheDocument();
    expect(unavailable.getAllByRole('cell')[1]).not.toHaveTextContent('€0.00');
    expect(unavailable.getByText('Some holdings cannot be valued.')).toBeInTheDocument();
    expect(table.getByRole('link', { name: /Alice/ })).toHaveAttribute('href', customerRoute());
    expect(screen.queryByText(/External assets and debts are not included/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Today P&L uses the latest saved closes/)).not.toBeInTheDocument();
    expect(request.mock.calls.map(([path]) => path)).toEqual([pagePath('/customers')]);
    expect(posts()).toHaveLength(0);
  });

  it('shows goal counts and signed exact P&L with latest-date tooltips, zeroes and unavailable values', async () => {
    const server = mockServer();
    server.customers.set(customer.id, { ...customer, goal_count: 23, realized_pnl_eur: '-9007199254740993.01',
      unrealized_pnl_eur: '0.00', total_pnl_eur: '-9007199254740993.01', today_pnl_eur: '12.34',
      today_from_date: '2026-09-22', today_to_date: '2026-09-23' });
    server.customers.set(uid(2), { id: uid(2), name: 'Unpriced', net_worth_eur: null, unrealized_pnl_eur: null, today_pnl_eur: null, total_pnl_eur: null });
    await open();
    const table = within(screen.getByRole('table'));
    expect(table.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Customer', 'Goals', 'Net worth (EUR)', 'Realized P&L (incl. fees, EUR)', 'Unrealized P&L (EUR)', 'Today P&L (EUR)', 'Total P&L (EUR)',
    ]);
    const row = within(table.getByRole('row', { name: /Alice/ }));
    expect(row.getAllByRole('cell')[0]).toHaveTextContent(/^23$/);
    expect(row.getAllByText('-€9,007,199,254,740,993.01')).toHaveLength(2);
    row.getAllByText('-€9,007,199,254,740,993.01').forEach((value) => expect(value).toHaveClass('return-negative'));
    expect(row.getByText('€12.34')).toHaveClass('return-positive');
    expect(row.getAllByRole('cell')[3].firstChild).not.toHaveClass('return-positive', 'return-negative');
    expect(row.getAllByRole('cell')[4]).toHaveTextContent(/^€12\.34$/);
    expect(row.getAllByRole('cell')[4]).toHaveAttribute('title', 'Price date: 2026-09-23');
    expect(row.queryByText(/2026-09-2[23]/)).not.toBeInTheDocument();
    const missing = within(table.getByRole('row', { name: /Unpriced/ }));
    expect(missing.getAllByText('Unavailable')).toHaveLength(4);
    expect(missing.getAllByRole('cell')[4]).not.toHaveAttribute('title');
    missing.getAllByText('Unavailable').forEach((value) => expect(value).not.toHaveClass('return-positive', 'return-negative'));
    expect(screen.getByRole('table')).not.toHaveAttribute('aria-describedby');
    expect(request.mock.calls.map(([path]) => path)).toEqual([pagePath('/customers')]);
  });

  it.each([
    { goal_count: -1 }, { goal_count: 1.5 }, { goal_count: '1' }, { goal_count: undefined },
    { realized_pnl_eur: null }, { realized_pnl_eur: 12.34 }, { unrealized_pnl_eur: 'NaN' },
    { today_pnl_eur: undefined }, { total_pnl_eur: '1.234' }, { total_pnl_eur: '1e5' },
    { today_pnl_eur: '1.00' }, { today_from_date: '2026-09-22' },
    { today_from_date: '2026-02-30', today_to_date: '2026-03-02' },
    { today_from_date: '2026-09-24', today_to_date: '2026-09-23' },
  ])('rejects malformed customer metrics %j', async (invalid) => {
    const server = mockServer(); server.customers.set(customer.id, { ...customer, ...invalid });
    await open();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it.each([undefined, 123.45, 'NaN', '-1.00', '1.234', ''])('rejects malformed net worth %s and supports explicit retry', async (value) => {
    const server = mockServer(); server.customers.set(customer.id, { ...customer, net_worth_eur: value });
    await open();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    server.customers.set(customer.id, { ...customer, net_worth_eur: '123.45' });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' })); await flush();
    expect(screen.getByRole('table')).toHaveTextContent('€123.45'); expect(posts()).toHaveLength(0);
  });

  it('hides old totals during refresh, reports read failures and loads updated totals on retry', async () => {
    const server = mockServer(); await open();
    const customerList = server.customerList, pending = deferred(); server.customerList = () => pending.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh customers' })); await flush();
    expect(screen.queryByRole('table')).not.toBeInTheDocument(); expect(screen.getByRole('status')).toHaveTextContent('Loading');
    await act(async () => pending.reject(new Error('Could not load customer valuations.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load customer valuations');
    server.customerList = customerList; server.customers.set(customer.id, { ...customer, net_worth_eur: '456.78' });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' })); await flush();
    expect(screen.getByRole('table')).toHaveTextContent('€456.78'); expect(posts()).toHaveLength(0);
  });

  it.each(['user', 'request'])('aborts pending customer totals and ignores late private values after a %s change', async (identity) => {
    const server = mockServer(), pending = deferred(); server.customerList = () => pending.promise;
    const view = render(<CustomerPlanner />); await flush();
    const signal = reads(pagePath('/customers'))[0][1].signal;
    server.customerList = async () => ({ items: [customerSummary({ id: uid(2), name: 'New session', net_worth_eur: '1.00' })], total: 1, limit: 20, offset: 0 });
    if (identity === 'user') auth.user = { id: 'user-b' }; else auth.request = vi.fn(server.handle);
    view.rerender(<CustomerPlanner />); await flush(); expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ items: [customerSummary({ ...customer, net_worth_eur: '999.00', total_pnl_eur: '9999.99' })], total: 1, limit: 20, offset: 0 }));
    expect(screen.getByRole('table')).toHaveTextContent('New session');
    expect(screen.queryByText('€999.00')).not.toBeInTheDocument(); expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    expect(screen.queryByText('€9,999.99')).not.toBeInTheDocument();
  });
});

describe('goal summary table and on-demand creation', () => {
  it('shows exact targets, UTC dates and time remaining with native goal links and no extra requests', async () => {
    const server = mockServer(); server.goals.set(goal.id, { ...goal, goal_eur: '9007199254740993.01' });
    await open(`${customerRoute()}/goals?instrument=${A}`);
    const table = within(screen.getByRole('table', { name: 'Goal summary for Alice' }));
    expect(table.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Goal', 'Target (EUR)', 'Goal date (UTC)', 'Time remaining', 'Created (UTC)',
    ]);
    const row = within(table.getByRole('row', { name: /Retirement/ }));
    expect(row.getByText('€9,007,199,254,740,993.01')).toBeInTheDocument();
    expect(row.getByText('2036-09-24')).toHaveAttribute('datetime', '2036-09-24');
    expect(row.getByText('3,653 days left')).toBeInTheDocument();
    expect(row.getByText('2026-08-01')).toHaveAttribute('datetime', goal.created_at);
    expect(row.getByRole('link', { name: 'Retirement' })).toHaveAttribute('href', `${goalRoute()}?instrument=${A}`);
    expect(screen.getByRole('region', { name: 'Goals table' })).toHaveAttribute('tabindex', '0');
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(request.mock.calls.map(([path]) => path)).toEqual([customerPath(), pagePath(`${customerPath()}/goals`)]);
    expect(posts()).toHaveLength(0);
  });

  it.each([
    ['2026-09-25', '1 day left'], ['2026-09-26', '2 days left'], ['2026-09-24', 'Today'],
    ['2026-09-23', '1 day past target'], ['2026-09-22', '2 days past target'],
  ])('compares %s against the UTC calendar day, not local time or partial days', async (date, expected) => {
    vi.setSystemTime(new Date('2026-09-24T23:59:59Z'));
    const server = mockServer(); server.goals.set(goal.id, { ...goal, goal_date: date, created_at: '2026-08-01T23:30:00-02:00' });
    await open(`${customerRoute()}/goals`);
    const table = within(screen.getByRole('table'));
    expect(table.getByText(expected)).toBeInTheDocument();
    expect(table.getByText('2026-08-02')).toBeInTheDocument();
  });

  it.each([undefined, null, 'invalid'])('shows unavailable rather than a fabricated creation date for %s', async (created_at) => {
    const server = mockServer(); server.goals.set(goal.id, { ...goal, created_at });
    await open(`${customerRoute()}/goals`);
    expect(within(screen.getByRole('table')).getByText('Unavailable')).toBeInTheDocument();
  });

  it.each(['2026-02-30', 'not-a-date', undefined])('rejects malformed goal dates before calculating time remaining: %s', async (goal_date) => {
    const server = mockServer(); server.goals.set(goal.id, { ...goal, goal_date });
    await open(`${customerRoute()}/goals`);
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('opens creation only on demand, focuses it and discards cancelled drafts and errors', async () => {
    mockServer(); await open(`${customerRoute()}/goals`);
    const trigger = screen.getByRole('button', { name: 'Add goal' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    addGoal();
    expect(trigger).toBeDisabled(); expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('form', { name: 'New goal' })).toHaveAttribute('id', trigger.getAttribute('aria-controls'));
    expect(screen.getByLabelText('Goal name')).toHaveFocus();
    expect(screen.getByRole('table')).toBeInTheDocument();
    change('Goal name', '   '); fireEvent.submit(screen.getByRole('form', { name: 'New goal' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a goal name.');
    change('Goal name', 'Discard me'); change('Target (EUR)', '123'); change('Goal date (UTC)', '2031-01-01');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus(); expect(trigger).toHaveAttribute('aria-expanded', 'false');
    addGoal();
    expect(screen.getByLabelText('Goal name')).toHaveValue('');
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue('100000');
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveValue('2036-09-24');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(posts()).toHaveLength(0); expect(reads(pagePath(`${customerPath()}/goals`))).toHaveLength(1);
  });

  it('closes creation, restores focus and returns to the first page after adding a goal', async () => {
    const server = mockServer();
    for (let n = 200; n < 220; n++) server.goals.set(uid(n), { ...goal, id: uid(n), name: `Goal ${n}` });
    await open(`${customerRoute()}/goals`);
    fireEvent.click(screen.getByRole('button', { name: 'Next' })); await flush();
    expect(screen.getByText('21–21 of 21')).toBeInTheDocument();
    addGoal(); change('Goal name', '  New goal  '); change('Target (EUR)', '012345.67');
    fireEvent.click(screen.getByRole('button', { name: 'Create goal' })); await flush();
    expect(posts()[0][1].body).toEqual({ name: 'New goal', goal_eur: '12345.67', goal_date: '2036-09-24' });
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add goal' })).toHaveFocus();
    expect(screen.getByText('1–20 of 22')).toBeInTheDocument();
    expect(screen.getByRole('row', { name: /New goal/ })).toHaveTextContent('€12,345.67');
    expect(reads(pagePath(`${customerPath()}/goals`))).toHaveLength(2);
    expectNoPlannerReads();
  });

  it('locks goal creation while busy and retains the draft after failure for explicit retry', async () => {
    const server = mockServer(), pending = deferred(), createGoal = server.createGoal;
    server.createGoal = () => pending.promise;
    await open(`${customerRoute()}/goals`); addGoal(); change('Goal name', 'Home');
    fireEvent.click(screen.getByRole('button', { name: 'Create goal' })); await flush();
    for (const label of ['Goal name', 'Target (EUR)', 'Goal date (UTC)']) expect(screen.getByLabelText(label)).toBeDisabled();
    for (const name of ['Creating…', 'Cancel', 'Add goal']) expect(screen.getByRole('button', { name })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'New goal' })); expect(posts()).toHaveLength(1);
    await act(async () => pending.reject(new Error('Could not create goal.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not create goal');
    expect(screen.getByLabelText('Goal name')).toHaveValue('Home');
    await advance(10000); expect(posts()).toHaveLength(1);
    server.createGoal = createGoal;
    fireEvent.click(screen.getByRole('button', { name: 'Create goal' })); await flush();
    expect(screen.getByRole('link', { name: 'Home' })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(posts()).toHaveLength(2);
  });

  it.each(['route', 'user', 'request'].flatMap((identity) => ['resolve', 'reject'].map((outcome) => [identity, outcome])))('ignores late goal creation after %s changes (%s)', async (identity, outcome) => {
    const server = mockServer(), pending = deferred(); server.createGoal = () => pending.promise;
    const view = await open(`${customerRoute()}/goals`); addGoal(); change('Goal name', 'Private goal');
    fireEvent.click(screen.getByRole('button', { name: 'Create goal' })); await flush();
    const options = posts()[0][1];
    if (identity === 'route') await navigate(customerRoute());
    else {
      if (identity === 'user') auth.user = { id: 'user-b' }; else auth.request = vi.fn(server.handle);
      view.rerender(<InstrumentWorkspace />); await flush();
    }
    expect(options.signal.aborted).toBe(true);
    const readCount = request.mock.calls.length;
    await act(async () => outcome === 'resolve' ? pending.resolve({ ...goal, id: uid(102), name: 'Private goal' }) : pending.reject(new Error('Stale goal error')));
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Private goal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(request.mock.calls).toHaveLength(readCount);
  });
});

describe('customers, fixed goals and instrument shortcuts', () => {
  it('only opens customer creation on demand and discards cancelled drafts and errors', async () => {
    mockServer(); await open();
    const trigger = screen.getByRole('button', { name: 'Add customer' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('form', { name: 'New customer' })).not.toBeInTheDocument();
    addCustomer();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(trigger).toBeDisabled();
    expect(screen.getByLabelText('Customer name')).toHaveFocus();
    expect(screen.getByRole('form', { name: 'New customer' })).toHaveAttribute('id', trigger.getAttribute('aria-controls'));
    expect(screen.getByRole('table', { name: 'Customer portfolio summary' })).toBeInTheDocument();
    change('Customer name', '   ');
    fireEvent.submit(screen.getByRole('form', { name: 'New customer' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a customer name.');
    change('Customer name', 'Discard this draft');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('form', { name: 'New customer' })).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).toHaveFocus();
    addCustomer();
    expect(screen.getByLabelText('Customer name')).toHaveValue('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(posts()).toHaveLength(0);
    expect(reads(pagePath('/customers'))).toHaveLength(1);
  });

  it('creates a name-only customer and multiple named goals without calculating or saving', async () => {
    const server = mockServer({ seed: false }); await open();
    expect(screen.getByText(/No customers yet/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Customers', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('form', { name: 'New customer' })).not.toBeInTheDocument();
    addCustomer();
    expect(within(screen.getByRole('form', { name: 'New customer' })).getAllByRole('textbox')).toHaveLength(1);
    expect(screen.queryByLabelText('Target (EUR)')).not.toBeInTheDocument();
    change('Customer name', '  Ada Lovelace  '); fireEvent.click(screen.getByRole('button', { name: 'Create customer' })); await flush();
    const created = [...server.customers.values()][0];
    expect(posts()[0]).toEqual(['/customers', expect.objectContaining({ body: { name: 'Ada Lovelace' }, method: 'POST', signal: expect.any(AbortSignal) })]);
    expect(screen.queryByRole('form', { name: 'New customer' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add customer' })).toHaveFocus();
    expect(reads(pagePath('/customers'))).toHaveLength(2);
    addCustomer();
    expect(screen.getByLabelText('Customer name')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(window.location.hash).toBe('#/goal');
    await follow(/Ada Lovelace/);
    expect(screen.getByRole('link', { name: 'Portfolios', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText(/No portfolios yet/)).toBeInTheDocument();
    await follow('Goals');
    expect(screen.getByText('No goals yet. Select Add goal to create your first goal.')).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    for (const [name, amount, date] of [['Retirement', '0100000.00', '2036-09-24'], ['Home deposit', '999999999999.99', '2031-09-24']]) {
      addGoal();
      change('Goal name', name); change('Target (EUR)', amount); change('Goal date (UTC)', date);
      fireEvent.click(screen.getByRole('button', { name: 'Create goal' })); await flush();
    }
    const goals = [...server.goals.values()];
    expect(goals).toHaveLength(2);
    expect(posts().slice(1).map(([path, options]) => [path, options.body])).toEqual([
      [`${customerPath(created.id)}/goals`, { name: 'Retirement', goal_eur: '100000.00', goal_date: '2036-09-24' }],
      [`${customerPath(created.id)}/goals`, { name: 'Home deposit', goal_eur: '999999999999.99', goal_date: '2031-09-24' }],
    ]);
    expect(screen.getByRole('link', { name: /Home deposit/ })).toHaveAttribute('href', goalRoute(goals[1].id, created.id));
    expectNoPlannerReads(); expect(server.saved.size).toBe(0); expect(server.previews.size).toBe(0);
    await follow(/Retirement/); expect(screen.getByText(/No saved simulations yet/)).toBeInTheDocument();
    await follow('New simulation');
    expect(window.location.hash).toBe(`${goalRoute(goals[0].id, created.id)}/new`);
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue('100000.00');
    expect(screen.getByLabelText('Target (EUR)')).toHaveAttribute('readonly');
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveValue('2036-09-24');
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveAttribute('readonly');
    expect(screen.getByLabelText('Instrument')).toHaveValue(''); expect(calculate()).toBeDisabled(); expect(posts()).toHaveLength(3);
  });

  it.each([`#/goal/${A}`, `#/goal?instrument=${A}`])('carries the instrument shortcut from %s through native customer, goal and new links', async (route) => {
    mockServer(); await open(route);
    expect(screen.getByText(`Planning with ${A}. Choose a customer and goal first; this instrument will be preselected for the new simulation.`)).toBeInTheDocument();
    expect(screen.queryByLabelText('Instrument')).not.toBeInTheDocument(); expectNoPlannerReads();
    expect(screen.getByRole('link', { name: /Alice/ })).toHaveAttribute('href', `${customerRoute()}?instrument=${A}`);
    await follow(/Alice/);
    expect(screen.getByRole('link', { name: '← Customers', exact: true })).toHaveAttribute('href', `#/goal?instrument=${A}`);
    expect(screen.getByRole('link', { name: 'Portfolios', exact: true })).toHaveAttribute('aria-current', 'page');
    await follow('Goals');
    expect(screen.getByRole('link', { name: /Retirement/ })).toHaveAttribute('href', `${goalRoute()}?instrument=${A}`);
    await follow(/Retirement/);
    expect(screen.getByRole('link', { name: /Alice.*Goals/ })).toHaveAttribute('href', `${customerRoute()}/goals?instrument=${A}`);
    expect(screen.getByRole('link', { name: 'New simulation' })).toHaveAttribute('href', newRoute()); expectNoPlannerReads();
    await follow('New simulation');
    expect(screen.getByLabelText('Instrument')).toHaveValue(A); expect(calculate()).toBeEnabled();
    expect(screen.getByRole('link', { name: /Retirement.*Saved simulations/ })).toHaveAttribute('href', `${goalRoute()}?instrument=${A}`);
    expect(posts()).toHaveLength(0);
  });

  it.each(['#/goal/customers/%ZZ', '#/goal/customers/invalid%2Fid', `${goalRoute()}/simulations/bad%3Fid`, `${newRoute()}!`])('rejects malformed hierarchy routes: %s', async (route) => {
    mockServer(); await open(route);
    expect(screen.getByRole('alert')).toHaveTextContent('ETF not found'); expect(request).not.toHaveBeenCalled();
  });

  it('blocks duplicate creation while busy and retries a failed creation only explicitly', async () => {
    const server = mockServer({ seed: false }), pending = deferred();
    server.createCustomer = () => pending.promise; await open(); addCustomer();
    change('Customer name', 'Alice'); fireEvent.click(screen.getByRole('button', { name: 'Create customer' })); await flush();
    expect(screen.getByLabelText('Customer name')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Creating…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add customer' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'New customer' })); expect(posts()).toHaveLength(1);
    await act(async () => pending.reject(new Error('Could not create customer.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not create customer');
    expect(screen.getByLabelText('Customer name')).toHaveValue('Alice');
    await advance(10000); expect(posts()).toHaveLength(1);
    server.createCustomer = async (body) => { server.customers.set(customer.id, { ...customer, ...body }); return server.customers.get(customer.id); };
    fireEvent.click(screen.getByRole('button', { name: 'Create customer' })); await flush();
    expect(screen.getByRole('link', { name: /Alice/ })).toBeInTheDocument(); expect(posts()).toHaveLength(2);
    expect(screen.queryByRole('form', { name: 'New customer' })).not.toBeInTheDocument();
  });
});

describe('editing existing goals', () => {
  async function editGoal() { fireEvent.click(screen.getByRole('button', { name: 'Edit goal' })); await flush(); }
  async function saveChanges() { fireEvent.click(screen.getByRole('button', { name: 'Save changes' })); await flush(); }

  it('prefills the goal, saves a trimmed name-only change, and persists it across navigation and reload', async () => {
    const server = mockServer(); const view = await open(goalRoute()); await editGoal();
    expect(screen.getByLabelText('Goal name')).toHaveValue(goal.name);
    expect(screen.getByLabelText('Goal name')).toHaveFocus();
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue(goal.goal_eur);
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveValue(goal.goal_date);
    change('Goal name', '  Our retirement  ');
    expect(patches()).toHaveLength(0);
    await saveChanges();
    expect(patches()).toEqual([[goalPath(), expect.objectContaining({ method: 'PATCH', body: { name: 'Our retirement' }, signal: expect.any(AbortSignal) })]]);
    expect(server.goals.get(goal.id)).toEqual({ ...goal, name: 'Our retirement' });
    expect(screen.getByRole('heading', { name: 'Our retirement' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Goal updated.');
    expect(screen.queryByRole('form', { name: 'Edit goal' })).not.toBeInTheDocument();
    expectNoPlannerReads(); expect(posts()).toHaveLength(0);
    await follow(/Alice.*Goals/);
    expect(screen.getByRole('link', { name: /Our retirement/ })).toHaveAttribute('href', goalRoute());
    view.unmount(); await open(goalRoute());
    expect(screen.getByRole('heading', { name: 'Our retirement' })).toBeInTheDocument();
  });

  it('cancels edits locally and does not send a no-op update', async () => {
    const server = mockServer(); await open(goalRoute()); await editGoal();
    change('Goal name', 'Discard this'); change('Target (EUR)', '250000'); change('Goal date (UTC)', '2031-09-24');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); await flush();
    expect(screen.getByRole('heading', { name: goal.name })).toBeInTheDocument();
    expect(server.goals.get(goal.id)).toEqual(goal); expect(patches()).toHaveLength(0);
    await editGoal(); expect(screen.getByLabelText('Goal name')).toHaveValue(goal.name);
    await saveChanges(); expect(patches()).toHaveLength(0);
    expect(screen.queryByRole('form', { name: 'Edit goal' })).not.toBeInTheDocument();
  });

  it('updates all goal fields for new simulations while displaying original targets in saved history and reports', async () => {
    const server = mockServer(); const original = server.seedRun(); await open(goalRoute()); await editGoal();
    change('Goal name', 'Home deposit'); change('Target (EUR)', '0250000.5'); change('Goal date (UTC)', '2031-09-24');
    await saveChanges();
    const changes = { name: 'Home deposit', goal_eur: '250000.50', goal_date: '2031-09-24' };
    expect(patches()[0][1].body).toEqual(changes);
    expect(server.goals.get(goal.id)).toEqual({ ...goal, ...changes });
    const table = screen.getByRole('table', { name: 'Saved simulations for Home deposit' });
    expect(within(table).getByRole('columnheader', { name: 'Goal target / date' })).toBeInTheDocument();
    expect(within(table).getByText('€100,000.00')).toBeInTheDocument();
    expect(within(table).getByText(goal.goal_date)).toBeInTheDocument();
    expectNoPlannerReads(); expect(posts()).toHaveLength(0);
    fireEvent.click(within(table).getByRole('link')); await advance();
    expect(result()).toBeInTheDocument(); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText(/Read-only result for Home deposit/)).toBeInTheDocument();
    expect(screen.getByText('Target', { selector: 'dt' }).nextElementSibling).toHaveTextContent('€100,000.00');
    expect(screen.getByText('Goal date', { selector: 'dt' }).nextElementSibling).toHaveTextContent(goal.goal_date);
    expect(server.saved.get(original.id)).toEqual(original);
    await follow('New simulation');
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue(changes.goal_eur);
    expect(screen.getByLabelText('Goal date (UTC)')).toHaveValue(changes.goal_date);
    change('Instrument', A); await flush(); await submit();
    expect(result()).toBeInTheDocument();
    const preview = [...server.previews.values()].at(-1);
    expect(preview.inputs.goal_eur).toBe(changes.goal_eur); expect(preview.inputs.goal_date).toBe(changes.goal_date);
    expect(server.saved.get(original.id)).toEqual(original);
  });

  it('allows renaming an expired goal without requiring a new date', async () => {
    const server = mockServer(); server.goals.get(goal.id).goal_date = '2025-01-01';
    await open(goalRoute()); await editGoal(); change('Goal name', 'Completed goal');
    expect(screen.getByRole('form', { name: 'Edit goal' }).checkValidity()).toBe(true);
    await saveChanges();
    expect(patches()[0][1].body).toEqual({ name: 'Completed goal' });
    expect(server.goals.get(goal.id).goal_date).toBe('2025-01-01');
  });

  it.each([['Goal name', '   '], ['Target (EUR)', '0'], ['Target (EUR)', '1.001'], ['Target (EUR)', '-1']])('validates %s before updating', async (label, value) => {
    mockServer(); await open(goalRoute()); await editGoal(); change(label, value); await saveChanges();
    expect(screen.getByRole('alert')).toHaveTextContent(label === 'Goal name' ? 'Enter a goal name' : 'positive target amount');
    expect(patches()).toHaveLength(0);
  });

  it.each(['offline', 'lost reply', 'wrong ID', 'wrong customer', 'malformed', 'unapplied change'])('keeps edits and explicitly retries after %s', async (failure) => {
    const server = mockServer(), update = server.updateGoal;
    server.updateGoal = async (cid, gid, body) => {
      if (failure === 'offline') throw new Error('Could not update goal.');
      const value = await update(cid, gid, body);
      if (failure === 'lost reply') throw new Error('Reply lost.');
      if (failure === 'wrong ID') return { ...value, id: uid(999) };
      if (failure === 'wrong customer') return { ...value, customer_id: uid(999) };
      if (failure === 'unapplied change') return goal;
      return { id: gid };
    };
    await open(goalRoute()); await editGoal(); change('Goal name', 'New name'); await saveChanges();
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByLabelText('Goal name')).toHaveValue('New name');
    expect(screen.getByRole('heading', { name: goal.name })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled();
    await advance(10000); expect(patches()).toHaveLength(1);
    server.updateGoal = update; await saveChanges();
    expect(screen.getByRole('heading', { name: 'New name' })).toBeInTheDocument();
    expect(patches()).toHaveLength(2); expect(server.goals.size).toBe(1);
  });

  it('locks controls and prevents duplicate updates while saving', async () => {
    const server = mockServer(), pending = deferred(); server.updateGoal = () => pending.promise;
    await open(goalRoute()); await editGoal(); change('Goal name', 'New name'); await saveChanges();
    for (const label of ['Goal name', 'Target (EUR)', 'Goal date (UTC)']) expect(screen.getByLabelText(label)).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Saving changes…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Edit goal' })); expect(patches()).toHaveLength(1);
    await act(async () => pending.resolve({ ...goal, name: 'New name' }));
    expect(screen.getByRole('heading', { name: 'New name' })).toBeInTheDocument();
  });

  it.each(['route', 'user', 'status', 'request', 'unmount'].flatMap((identity) => ['resolve', 'reject'].map((outcome) => [identity, outcome])))('ignores a late update after %s changes (%s)', async (identity, outcome) => {
    const server = mockServer(), pending = deferred(), update = server.updateGoal; server.updateGoal = () => pending.promise;
    const view = await open(goalRoute()); await editGoal(); change('Goal name', 'Private edit'); await saveChanges();
    const options = patches()[0][1];
    if (identity === 'route') await navigate(customerRoute());
    else if (identity === 'unmount') view.unmount();
    else {
      if (identity === 'user') auth.user = { id: 'user-b' };
      if (identity === 'status') auth.status = 'checking';
      if (identity === 'request') auth.request = vi.fn(server.handle);
      view.rerender(<InstrumentWorkspace />); await flush();
    }
    expect(options.signal.aborted).toBe(true);
    const route = window.location.hash;
    // The server may commit despite abort; the obsolete callback must not change the current UI.
    await act(async () => outcome === 'resolve' ? pending.resolve(await update(customer.id, goal.id, options.body)) : pending.reject(new Error('Stale edit error')));
    expect(screen.queryByRole('heading', { name: 'Private edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Edit goal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('Goal updated.')).not.toBeInTheDocument();
    expect(window.location.hash).toBe(route); expect(patches()).toHaveLength(1);
  });
});

describe('simulation plans', () => {
  it('promotes each saved simulation from its list or report and persists the label', async () => {
    const server = mockServer(), first = server.seedRun(), second = server.seedRun();
    await open(goalRoute());
    const row = within(comparison()).getAllByRole('row').find((item) => within(item).queryByRole('link')?.getAttribute('href') === reportRoute(first.id));
    fireEvent.click(within(row).getByRole('button', { name: 'Make plan' })); await flush();
    expect(within(row).getByText('Plan')).toBeInTheDocument();
    expect(posts()).toHaveLength(1);
    expect(posts()[0]).toEqual([`${reportPath(first.id)}/plan`, expect.objectContaining({ method: 'POST', body: {} })]);
    expect(server.saved.get(first.id)).toEqual({ ...first, is_plan: true });
    await navigate(reportRoute(first.id));
    expect(screen.getByText('Plan')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Make plan' })).not.toBeInTheDocument();
    await navigate(reportRoute(second.id));
    fireEvent.click(screen.getByRole('button', { name: 'Make plan' })); await flush();
    expect(screen.getByText('Plan')).toBeInTheDocument();
    await navigate(goalRoute());
    expect(within(comparison()).getAllByText('Plan')).toHaveLength(3);
    expect(screen.queryByRole('button', { name: 'Make plan' })).not.toBeInTheDocument();
    expect(server.saved.get(second.id)).toEqual({ ...second, is_plan: true });
  });

  it('shows promotion errors, supports retry and aborts stale requests on navigation', async () => {
    const server = mockServer(), run = server.seedRun(), promote = server.makePlan;
    server.makePlan = async () => { throw new Error('Plan unavailable.'); };
    await open(reportRoute(run.id));
    fireEvent.click(screen.getByRole('button', { name: 'Make plan' })); await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('Plan unavailable.');
    const pending = deferred(); server.makePlan = () => pending.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Make plan' })); await flush();
    expect(screen.getByRole('button', { name: 'Making plan…' })).toBeDisabled();
    const signal = posts().at(-1)[1].signal;
    await navigate(goalRoute()); expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ ...run, is_plan: true }));
    expect(screen.getByRole('button', { name: 'Make plan' })).toBeInTheDocument();
    server.makePlan = promote;
    fireEvent.click(screen.getByRole('button', { name: 'Make plan' })); await flush();
    expect(screen.queryByRole('button', { name: 'Make plan' })).not.toBeInTheDocument();
  });
});

describe('preview first, explicit idempotent save and immutable reports', () => {
  it('keeps previews out of history, saves only chosen results, and reloads the original unchanged after another saved calculation', async () => {
    const server = mockServer(); const view = await open(newRoute());
    const fixed = structuredClone(server.goals.get(goal.id));
    expect(screen.getByLabelText('Starting capital (EUR)')).toHaveValue('0');
    expect(screen.getByLabelText('Reliability target (%)')).toHaveValue(75);
    await submit();
    const discarded = structuredClone([...server.previews.values()][0]);
    expect(result()).toBeInTheDocument(); expect(saveButton()).toBeEnabled(); expect(server.saved.size).toBe(0);
    expect(posts()[0]).toEqual([`${goalPath()}/simulate`, expect.objectContaining({ method: 'POST', body: settings() })]);
    await navigate(goalRoute()); expect(screen.getByText(/No saved simulations yet/)).toBeInTheDocument();
    await follow('New simulation'); change('Instrument', A); await flush(); await submit();
    const preview = structuredClone([...server.previews.values()][1]);
    expect(preview.id).not.toBe(discarded.id); expect(preview.saved_at).toBeNull();
    expect(posts()).toHaveLength(2); expect(server.saved.size).toBe(0);
    const savedAt = new Date().toISOString();
    await save();
    const original = structuredClone(server.saved.get(preview.id));
    expect(window.location.hash).toBe(reportRoute(preview.id));
    expect(original).toEqual({ ...preview, saved_at: savedAt });
    expect(posts()[2]).toEqual([`${reportPath(preview.id)}/save`, expect.objectContaining({ method: 'POST' })]);
    expect(posts()[2][1]).not.toHaveProperty('body');
    expect(screen.getByText(/Read-only result for Retirement/)).toBeInTheDocument();
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); expect(saveButton()).not.toBeInTheDocument();
    await follow('New simulation');
    change('Starting capital (EUR)', '20000'); change('Reliability target (%)', '95'); change('Instrument', B); await flush();
    expect(result()).not.toBeInTheDocument(); expect(posts()).toHaveLength(3);
    server.monthly = '987.65'; await advance(1000); await submit();
    expect(within(result()).getByRole('status')).toHaveTextContent('€987.65');
    expect(server.saved.size).toBe(1); expect(server.saved.get(original.id)).toEqual(original);
    await save();
    const later = [...server.saved.values()][1];
    expect(later.id).not.toBe(original.id); expect(later.inputs).toEqual({ ...settings({ strategy: selection(B), starting_capital_eur: '20000.00', reliability: 0.95 }), goal_eur: goal.goal_eur, goal_date: goal.goal_date });
    await follow(/Retirement.*Saved simulations/);
    expect(historyLinks()).toEqual([reportRoute(later.id), reportRoute(original.id)]);
    expect(historyLinks()).not.toContain(reportRoute(discarded.id));
    const rows = within(comparison()).getAllByRole('row');
    expect(rows[1]).toHaveTextContent('€20,000.00'); expect(rows[1]).toHaveTextContent('95%'); expect(rows[1]).toHaveTextContent('€987.65');
    expect(rows[2]).toHaveTextContent('€0.00'); expect(rows[2]).toHaveTextContent('75%'); expect(rows[2]).toHaveTextContent('€456.78');
    view.unmount(); server.definitions = []; server.acquiredItems = []; request.mockClear();
    await open(reportRoute(original.id));
    expect(within(result()).getByRole('status')).toHaveTextContent('€456.78');
    expect(screen.getByText(`instrument: ${A}`)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Monte Carlo portfolio projection' })).toBeInTheDocument();
    expect(server.saved.get(original.id)).toEqual(original); expect(server.goals.get(goal.id)).toEqual(fixed);
    expect(request.mock.calls.map(([path]) => path)).toEqual([customerPath(), goalPath(), reportPath(original.id)]);
    expectNoPlannerReads(); expect(posts()).toHaveLength(0);
  });

  it.each([['Starting capital (EUR)', '20000'], ['Reliability target (%)', '95'], ['Instrument', B], ['Strategy', pair.id]])('invalidates preview and Save immediately when %s changes', async (label, value) => {
    const server = mockServer(); await open(newRoute()); await submit();
    expect(saveButton()).toBeEnabled(); change(label, value);
    expect(saveButton()).not.toBeInTheDocument(); expect(result()).not.toBeInTheDocument();
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue(goal.goal_eur); expect(screen.getByLabelText('Goal date (UTC)')).toHaveValue(goal.goal_date);
    await flush(); expect(posts()).toHaveLength(1); expect(server.saved.size).toBe(0);
    if (label === 'Strategy') {
      expect(calculate()).toBeDisabled(); expect(screen.getByLabelText('primary instrument')).toHaveValue('');
      change('primary instrument', A); change('secondary instrument', B); await flush(); await submit();
      expect(posts()[1][1].body.strategy).toEqual({ id: pair.id, parameters: { primary: A, secondary: B } }); expect(saveButton()).toBeEnabled();
    }
  });

  it('invalidates Save when statistics are refreshed and requires fresh eligibility and calculation', async () => {
    const server = mockServer(); await open(newRoute()); await submit(); const pending = deferred(); server.stats = () => pending.promise;
    fireEvent.click(screen.getByText('Saved return statistics')); fireEvent.click(screen.getByRole('button', { name: 'Refresh statistics' }));
    expect(saveButton()).not.toBeInTheDocument(); expect(result()).not.toBeInTheDocument(); expect(calculate()).toBeDisabled();
    await act(async () => pending.resolve(statistics())); expect(calculate()).toBeEnabled(); expect(saveButton()).not.toBeInTheDocument(); expect(posts()).toHaveLength(1);
  });

  it.each(['offline before save', 'reply lost after save', 'malformed save response'])('retries the same preview idempotently after %s without recalculation', async (failure) => {
    const server = mockServer(); await open(newRoute()); await submit();
    const preview = structuredClone([...server.previews.values()][0]);
    server.save = async (cid, gid, sid) => {
      if (failure === 'offline before save') throw new Error('Save unavailable. Retry explicitly.');
      server.persist(sid);
      if (failure === 'reply lost after save') throw new Error('Save reply lost. Retry explicitly.');
      return { id: sid };
    };
    await save();
    expect(screen.getByRole('alert')).toHaveTextContent(failure === 'malformed save response' ? 'Unexpected save response' : /Retry explicitly/);
    expect(window.location.hash).toBe(newRoute()); expect(saveButton()).toBeEnabled(); expect(result()).toBeInTheDocument();
    const persisted = structuredClone(server.saved.get(preview.id));
    expect(server.saved.size).toBe(failure === 'offline before save' ? 0 : 1);
    await advance(10000); expect(posts()).toHaveLength(2);
    server.save = async (cid, gid, sid) => server.persist(sid); await save();
    expect(window.location.hash).toBe(reportRoute(preview.id)); expect(server.saved.size).toBe(1); expect(server.previews.size).toBe(1);
    expect(server.saved.get(preview.id)).toEqual(persisted || { ...preview, saved_at: expect.any(String) });
    expect(posts().map(([path]) => path)).toEqual([`${goalPath()}/simulate`, `${reportPath(preview.id)}/save`, `${reportPath(preview.id)}/save`]);
  });

  it('locks controls and duplicate submission during a pending save without recalculating', async () => {
    const server = mockServer(); await open(newRoute()); await submit(); const pending = deferred(); server.save = () => pending.promise;
    const preview = [...server.previews.values()][0]; await save();
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled();
    for (const label of ['Strategy', 'Instrument', 'Starting capital (EUR)', 'Reliability target (%)']) expect(screen.getByLabelText(label)).toBeDisabled();
    expect(calculate()).toBeDisabled(); fireEvent.submit(screen.getByRole('form', { name: 'Goal planner inputs' }));
    fireEvent.click(screen.getByRole('button', { name: 'Saving…' })); expect(posts()).toHaveLength(2);
    await act(async () => pending.resolve(server.persist(preview.id))); await advance();
    expect(window.location.hash).toBe(reportRoute(preview.id)); expect(server.saved.size).toBe(1); expect(posts()).toHaveLength(2);
  });

  it('keeps failed calculations out of history and makes retry explicit', async () => {
    const server = mockServer(), pending = deferred(); server.simulate = () => pending.promise;
    await open(newRoute()); await submit();
    expect(screen.getByRole('button', { name: 'Simulating…' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Goal planner inputs' })); expect(posts()).toHaveLength(1);
    await act(async () => pending.reject(new Error('Insufficient history.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Insufficient history'); expect(result()).not.toBeInTheDocument(); expect(saveButton()).not.toBeInTheDocument();
    await advance(10000); expect(posts()).toHaveLength(1); expect(server.saved.size).toBe(0); expect(server.previews.size).toBe(0);
    server.simulate = async (cid, gid, body) => server.preview(gid, body); await submit();
    expect(saveButton()).toBeEnabled(); expect(server.saved.size).toBe(0); expect(posts()).toHaveLength(2);
  });

  it.each([
    ['wrong customer', (run) => { run.customer_id = uid(99); }], ['wrong goal', (run) => { run.goal_id = uid(99); }],
    ['missing ID', (run) => { delete run.id; }], ['already saved', (run) => { run.saved_at = run.calculated_at; }],
    ['wrong capital', (run) => { run.inputs.starting_capital_eur = '999.00'; }],
    ['wrong goal target', (run) => { run.inputs.goal_eur = '1.00'; }], ['wrong result strategy', (run) => { run.result.strategy = selection(B); }],
  ])('rejects a mismatched preview envelope: %s', async (_, mutate) => {
    const server = mockServer(); server.simulate = async (cid, gid, body) => { const run = server.preview(gid, body); mutate(run); return run; };
    await open(newRoute()); await submit(); expect(screen.getByRole('alert')).toHaveTextContent('unexpected response');
    expect(result()).not.toBeInTheDocument(); expect(saveButton()).not.toBeInTheDocument(); expect(server.saved.size).toBe(0);
  });
});

describe('saved-data reads and pagination', () => {
  it('renders compact saved summaries and saved snapshot names without catalogue, inventory or statistics requests', async () => {
    const server = mockServer(); const run = server.seedRun();
    server.saved.get(run.id).strategy_definition.name = 'Retired strategy snapshot'; server.definitions = []; server.acquiredItems = [];
    await open(goalRoute());
    expect(historyLinks()).toEqual([reportRoute(run.id)]);
    for (const name of ['Saved', 'Strategy / parameters', 'Starting capital', 'Reliability target', 'Monthly contribution', 'Estimated success']) {
      expect(within(comparison()).getByRole('columnheader', { name })).toBeInTheDocument();
    }
    expect(within(comparison()).getByText('Retired strategy snapshot')).toBeInTheDocument();
    expect(request.mock.calls.map(([path]) => path)).toEqual([customerPath(), goalPath(), historyPath()]);
    fireEvent.click(within(comparison()).getByRole('link')); await advance();
    expect(result()).toBeInTheDocument(); expect(screen.getByText('Retired strategy snapshot', { selector: 'dd' })).toBeInTheDocument();
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expectNoPlannerReads(); expect(posts()).toHaveLength(0);
  });

  it.each(['Customers', 'Goals', 'Saved simulations'])('paginates %s by 20 with bounded reads and no calculations', async (label) => {
    const server = mockServer();
    if (label === 'Customers') for (let n = 2; n <= 41; n++) server.customers.set(uid(n), { id: uid(n), name: `Customer ${n}` });
    if (label === 'Goals') for (let n = 102; n <= 141; n++) server.goals.set(uid(n), { ...goal, id: uid(n), name: `Goal ${n}` });
    if (label === 'Saved simulations') for (let n = 0; n < 41; n++) server.seedRun(settings({ starting_capital_eur: `${10000 + n}.00` }));
    const path = label === 'Customers' ? '/customers' : label === 'Goals' ? `${customerPath()}/goals` : `${goalPath()}/simulations`;
    await open(label === 'Customers' ? '#/goal' : label === 'Goals' ? `${customerRoute()}/goals` : goalRoute());
    const pages = () => within(screen.getByRole('navigation', { name: `${label} pagination` }));
    const list = () => within(screen.getByRole('region', { name: label }));
    expect(list().getAllByRole('link')).toHaveLength(20); expect(pages().getByText('1–20 of 41')).toBeInTheDocument();
    expect(pages().getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(pages().getByRole('button', { name: 'Next' })); await flush();
    expect(list().getAllByRole('link')).toHaveLength(20); expect(pages().getByText('21–40 of 41')).toBeInTheDocument();
    fireEvent.click(pages().getByRole('button', { name: 'Next' })); await flush();
    expect(list().getAllByRole('link')).toHaveLength(1); expect(pages().getByText('41–41 of 41')).toBeInTheDocument();
    expect(pages().getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(pages().getByRole('button', { name: 'Previous' })); await flush();
    expect(pages().getByText('21–40 of 41')).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.startsWith(`${path}?`)).map(([url]) => url)).toEqual([pagePath(path), pagePath(path, 20), pagePath(path, 40), pagePath(path, 20)]);
    expectNoPlannerReads(); expect(posts()).toHaveLength(0);
  });

  it('retries a failed later page at the same offset and hides obsolete rows while loading', async () => {
    const server = mockServer(); for (let n = 0; n < 21; n++) server.seedRun();
    const history = server.history, pending = deferred();
    server.history = (cid, gid, query) => query.get('offset') === '20' ? pending.promise : history(cid, gid, query);
    await open(goalRoute()); fireEvent.click(screen.getByRole('button', { name: 'Next' })); await flush();
    expect(screen.queryByRole('table')).not.toBeInTheDocument(); expect(screen.getByRole('status')).toHaveTextContent('Loading');
    await act(async () => pending.reject(new Error('History page offline.')));
    expect(screen.getByRole('alert')).toHaveTextContent('History page offline');
    server.history = history; fireEvent.click(screen.getByRole('button', { name: 'Retry' })); await flush();
    expect(screen.getByText('21–21 of 21')).toBeInTheDocument(); expect(reads(historyPath(20))).toHaveLength(2); expect(posts()).toHaveLength(0);
  });

  it.each([
    ['wrong limit', (data) => { data.limit = 100; }], ['wrong offset', (data) => { data.offset = 20; }],
    ['duplicate IDs', (data) => { data.items.push(data.items[0]); data.total = 2; }],
    ['invalid total', (data) => { data.total = -1; }], ['invalid summary', (data) => { data.items[0].saved_at = null; }],
  ])('fails closed for malformed paged history: %s', async (_, mutate) => {
    const server = mockServer(); server.seedRun(); const history = server.history;
    server.history = async (...args) => { const data = await history(...args); mutate(data); return data; };
    await open(goalRoute()); expect(screen.getByRole('alert')).toHaveTextContent('unexpected response'); expect(screen.queryByRole('table')).not.toBeInTheDocument();
    server.history = history; fireEvent.click(screen.getByRole('button', { name: 'Retry' })); await flush();
    expect(comparison()).toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['not found', 'wrong ID', 'unsaved'])('rejects a saved report that is %s without falling back to calculation', async (failure) => {
    const server = mockServer(); const run = server.seedRun();
    server.report = async () => {
      if (failure === 'not found') throw new Error('Saved simulation not found.');
      return { ...run, ...(failure === 'wrong ID' ? { id: uid(99) } : { saved_at: null }) };
    };
    await open(reportRoute(run.id)); expect(screen.getByRole('alert')).toHaveTextContent(failure === 'not found' ? 'not found' : 'unexpected response');
    expect(result()).not.toBeInTheDocument(); expectNoPlannerReads(); expect(posts()).toHaveLength(0);
  });
});

describe('route, authentication and request identity isolation', () => {
  it.each(['resolve', 'reject'])('aborts old report reads and ignores a late %s after changing simulation IDs', async (outcome) => {
    const server = mockServer(); const first = server.seedRun(); server.monthly = '987.65'; const second = server.seedRun(settings({ strategy: selection(B) }));
    const pending = deferred(), report = server.report;
    server.report = (cid, gid, sid) => sid === first.id ? pending.promise : report(cid, gid, sid);
    await open(reportRoute(first.id)); const signal = reads(reportPath(first.id))[0][1].signal;
    await navigate(reportRoute(second.id)); expect(signal.aborted).toBe(true);
    await act(async () => outcome === 'resolve' ? pending.resolve(first) : pending.reject(new Error('stale report error')));
    expect(within(result()).getByRole('status')).toHaveTextContent('€987.65'); expect(screen.getByText(`instrument: ${B}`)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expectNoPlannerReads(); expect(posts()).toHaveLength(0);
  });

  it('aborts stale goal reads when changing goals, retaining the new fixed target and date', async () => {
    const server = mockServer(), pending = deferred(), readGoal = server.goal;
    const second = { ...goal, id: uid(102), name: 'Home deposit', goal_eur: '250000.00', goal_date: '2031-09-24' }; server.goals.set(second.id, second);
    server.goal = (cid, gid) => gid === goal.id ? pending.promise : readGoal(cid, gid);
    await open(newRoute()); const signal = reads(goalPath())[0][1].signal;
    await navigate(newRoute(B, second.id)); expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(goal));
    expect(screen.getByLabelText('Target (EUR)')).toHaveValue(second.goal_eur); expect(screen.getByLabelText('Goal date (UTC)')).toHaveValue(second.goal_date);
    expect(screen.getByLabelText('Instrument')).toHaveValue(B); expect(posts()).toHaveLength(0);
  });

  it('aborts an obsolete same-page refresh and ignores its late data', async () => {
    const server = mockServer(); const first = server.seedRun(); await open(goalRoute());
    const history = server.history, pending = deferred(); server.history = () => pending.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh saved simulations' })); await flush();
    const options = reads(historyPath()).at(-1)[1]; expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const second = server.seedRun(); server.history = history;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh saved simulations' })); await flush(); expect(options.signal.aborted).toBe(true);
    await act(async () => pending.resolve({ items: [summary(first)], total: 1, limit: 20, offset: 0 }));
    expect(historyLinks()).toEqual([reportRoute(second.id), reportRoute(first.id)]); expect(posts()).toHaveLength(0);
  });

  it.each(['user', 'status', 'request'])('hides loaded private data and resets form state when %s identity changes', async (identity) => {
    const server = mockServer(); const run = server.seedRun(); const view = await open(reportRoute(run.id)); expect(result()).toBeInTheDocument();
    const pending = deferred(); server.customer = () => pending.promise;
    if (identity === 'user') auth.user = { id: 'user-b' };
    if (identity === 'status') auth.status = 'checking';
    if (identity === 'request') auth.request = vi.fn(server.handle);
    view.rerender(<InstrumentWorkspace />);
    expect(result()).not.toBeInTheDocument(); expect(screen.queryByText(/Read-only result/)).not.toBeInTheDocument(); await flush();
    expect(screen.getByRole('status')).toHaveTextContent('Loading');
    view.unmount(); await act(async () => pending.resolve(customer)); expect(result()).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['user', 'request'])('aborts pending creation on %s change and never adds a late customer to the new session', async (identity) => {
    const server = mockServer({ seed: false }), pending = deferred(); server.createCustomer = () => pending.promise;
    const view = render(<CustomerPlanner />); await flush(); addCustomer(); change('Customer name', 'Private draft');
    fireEvent.click(screen.getByRole('button', { name: 'Create customer' })); await flush(); const options = posts()[0][1];
    if (identity === 'user') auth.user = { id: 'user-b' }; else auth.request = vi.fn(server.handle);
    view.rerender(<CustomerPlanner />); await flush(); expect(options.signal.aborted).toBe(true);
    expect(screen.queryByRole('form', { name: 'New customer' })).not.toBeInTheDocument();
    addCustomer();
    expect(screen.getByLabelText('Customer name')).toHaveValue('');
    await act(async () => pending.resolve({ id: uid(20), name: 'Private draft' }));
    expect(screen.queryByRole('link', { name: /Private draft/ })).not.toBeInTheDocument(); expect(posts()).toHaveLength(1);
  });

  it.each(['route', 'user', 'request', 'unmount'])('aborts pending calculation on %s change and never exposes its late preview', async (identity) => {
    const server = mockServer(), pending = deferred(); server.simulate = () => pending.promise;
    const view = await open(newRoute()); await submit(); const options = posts()[0][1];
    if (identity === 'route') await navigate(newRoute(B));
    else if (identity === 'unmount') view.unmount();
    else { if (identity === 'user') auth.user = { id: 'user-b' }; else auth.request = vi.fn(server.handle); view.rerender(<InstrumentWorkspace />); await flush(); }
    expect(options.signal.aborted).toBe(true);
    await act(async () => pending.resolve(server.preview(goal.id, options.body)));
    expect(result()).not.toBeInTheDocument(); expect(saveButton()).not.toBeInTheDocument(); expect(server.saved.size).toBe(0); expect(posts()).toHaveLength(1);
  });

  it.each(['route', 'user', 'request', 'unmount'])('aborts pending save on %s change and never navigates on its late successful reply', async (identity) => {
    const server = mockServer(); const view = await open(newRoute()); await submit();
    const preview = [...server.previews.values()][0], pending = deferred(); server.save = () => pending.promise; await save();
    const options = posts()[1][1];
    if (identity === 'route') await navigate(goalRoute());
    else if (identity === 'unmount') view.unmount();
    else { if (identity === 'user') auth.user = { id: 'user-b' }; else auth.request = vi.fn(server.handle); view.rerender(<InstrumentWorkspace />); await flush(); }
    const route = window.location.hash; expect(options.signal.aborted).toBe(true);
    // Abort cannot undo a save committed on the server; only the current client must ignore the reply.
    await act(async () => pending.resolve(server.persist(preview.id))); await advance();
    expect(window.location.hash).toBe(route); expect(result()).not.toBeInTheDocument(); expect(saveButton()).not.toBeInTheDocument(); expect(posts()).toHaveLength(2);
  });
});
