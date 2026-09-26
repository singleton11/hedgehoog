import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PortfolioTracker from './PortfolioTracker.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';

const { request, auth } = vi.hoisted(() => ({ request: vi.fn(), auth: { user: { id: 'user-a' }, status: 'authenticated' } }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ ...auth, request: auth.request || request }) }));
const cid = 'customer-a', pid = 'portfolio-a';
const A = 'IE00B4L5Y983', B = 'IE00B5BMR087';
const instrument = { instrument_id: A, name: 'World ETF', symbol: 'WORLD', exchange: 'XETR', currency: 'EUR' };
const held = { ...instrument, instrument_id: B, name: 'Retired ETF', symbol: 'HELD' };
const goal = { id: 'goal-a', customer_id: cid, name: 'Retirement', goal_eur: '100000.00', goal_date: '2036-09-24' };
const base = `/customers/${cid}/portfolios`, detailPath = `${base}/${pid}`, transactionsPath = `${detailPath}/transactions`;
const customerRoute = `#/goal/customers/${cid}`, goalsRoute = `${customerRoute}/goals`, listRoute = `${customerRoute}/portfolios`, detailRoute = `${listRoute}/${pid}`;
const pagePath = (path, offset = 0, limit = 20) => `${path}?limit=${limit}&offset=${offset}`;
const metadata = (extra = {}) => ({ id: pid, customer_id: cid, name: 'Long term', goal_id: null, currency: 'EUR', created_at: '2026-09-24T00:00:00Z', revision: 0, ...extra });
const portfolioSummary = (extra = {}) => ({ ...metadata(), net_worth_eur: '1137.50', realized_pnl_eur: '0.00',
  unrealized_pnl_eur: '12.50', today_pnl_eur: '-1.25', total_pnl_eur: '12.50', today_from_date: '2026-09-22', today_to_date: '2026-09-23', ...extra });
const position = (extra = {}) => ({ instrument, quantity: '1.25000001', average_price_eur: '100.00000001', cost_basis_eur: '125.00', market_price_eur: '110.00', price_date: '2026-09-23', previous_price_date: '2026-09-22', position_value_eur: '137.50', unrealized_pnl_eur: '12.50', daily_unrealized_pnl_eur: '-1.25', warning: null, ...extra });
const snapshot = (portfolio = metadata(), extra = {}) => ({ portfolio, cash_eur: '1000.00', positions_value_eur: '137.50', equity_eur: '1137.50', realized_pnl_eur: '0.00', unrealized_pnl_eur: '12.50', daily_unrealized_pnl_eur: '-1.25', daily_from_date: '2026-09-22', daily_to_date: '2026-09-23', positions: [position()], warnings: [], ...extra });
const cashEvent = (id) => ({ id: `transaction-${id}`, client_id: crypto.randomUUID(), type: 'deposit', date: '2026-09-23', amount_eur: '100.00', created_at: '2026-09-24T00:00:00Z', sequence: id });
function page(items, query) { const offset = Number(query.get('offset')), limit = Number(query.get('limit')); return { items: items.slice(offset, offset + limit), total: items.length, offset, limit }; }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function mockServer() {
  const server = { portfolios: [metadata()], goals: [goal], instruments: [instrument], transactions: [], details: new Map([[pid, snapshot()]]) };
  server.customer = async () => ({ id: cid, name: 'Alice' });
  server.portfolioList = async (query) => page(server.portfolios.map(portfolioSummary), query);
  server.goalList = async (query) => page(server.goals, query);
  server.instrumentList = async (query) => page(server.instruments, query);
  server.transactionList = async (query) => page(server.transactions, query);
  server.detail = async (id) => server.details.get(id);
  server.create = async (body) => { const item = metadata({ id: `portfolio-${server.portfolios.length}`, ...body }); server.portfolios.unshift(item); server.details.set(item.id, snapshot(item)); return item; };
  server.patch = async (id, body) => { const current = server.details.get(id).portfolio; Object.assign(current, body, { revision: current.revision + 1 }); return current; };
  server.save = async (body) => {
    const existing = server.transactions.find((item) => item.client_id === body.client_id);
    if (existing) return existing;
    const item = { ...body, id: `transaction-${server.transactions.length + 1}`, sequence: server.transactions.length + 1, created_at: '2026-09-24T00:00:00Z',
      ...(['buy', 'sell'].includes(body.type) ? { instrument: body.instrument_id === B ? held : instrument } : {}) };
    server.transactions.unshift(item); return item;
  };
  server.remove = async (id) => { server.transactions = server.transactions.filter((item) => item.id !== id); return { id, removed: true }; };
  async function dispatch(path, options = {}) {
    const url = new URL(path, 'http://localhost');
    if (url.pathname === `/customers/${cid}`) return server.customer(options);
    if (url.pathname === `/customers/${cid}/goals`) return server.goalList(url.searchParams, options);
    if (url.pathname === '/portfolio-instruments') return server.instrumentList(url.searchParams, options);
    if (url.pathname === base) return options.method === 'POST' ? server.create(options.body, options) : server.portfolioList(url.searchParams, options);
    if (url.pathname.startsWith(`${transactionsPath}/`) && options.method === 'DELETE') return server.remove(url.pathname.split('/').at(-1), options);
    if (url.pathname.startsWith(`${base}/`) && url.pathname.endsWith('/transactions')) return options.method === 'POST' ? server.save(options.body, options) : server.transactionList(url.searchParams, options);
    if (url.pathname.startsWith(`${base}/`)) return options.method === 'PATCH' ? server.patch(url.pathname.split('/').at(-1), options.body, options) : server.detail(url.pathname.split('/').at(-1), options);
    throw new Error(`Unexpected request: ${path}`);
  }
  server.handle = async (path, options = {}) => structuredClone(await dispatch(path, { ...options, ...(options.body ? { body: structuredClone(options.body) } : {}) }));
  request.mockImplementation(server.handle);
  return server;
}
const writes = () => request.mock.calls.filter(([, options]) => ['POST', 'PATCH', 'DELETE'].includes(options?.method));
const transactionForm = () => screen.getByRole('form', { name: 'Add transaction' });
const tradeForm = transactionForm, cashForm = transactionForm;
const settings = () => screen.getByRole('form', { name: 'Rename portfolio' });
const transactionEntry = () => document.getElementById('transaction-entry');
function addTransaction(type = 'deposit') {
  fireEvent.click(screen.getByRole('button', { name: 'Add transaction' }));
  change('Transaction type', type);
  return transactionForm();
}
function transactionType(type) { change('Transaction type', type); }
function transactionsTab() { fireEvent.click(screen.getByRole('tab', { name: 'Transactions' })); }
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
async function flush() { await act(async () => {}); }
async function submit(form) { fireEvent.submit(form); await flush(); }
async function open(route = detailRoute) { window.history.replaceState(null, '', route); const view = render(<InstrumentWorkspace />); await flush(); return view; }
async function navigate(route) { await act(async () => { window.history.pushState(null, '', route); window.dispatchEvent(new Event('hashchange')); }); }
async function follow(name) { fireEvent.click(screen.getByRole('link', { name })); await act(async () => { await vi.advanceTimersByTimeAsync(5); }); }
function fillTrade(extra = {}) {
  for (const [label, value] of Object.entries({ 'Trade instrument': A, Quantity: '1.25000001', 'Average execution price (EUR)': '123.12345678', ...extra })) change(label, value);
}
function summaryValue(label) { return within(screen.getByRole('region', { name: 'Portfolio summary' })).getByText(label).nextElementSibling; }

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-24T00:15:00Z')); request.mockReset();
  auth.user = { id: 'user-a' }; auth.status = 'authenticated'; delete auth.request;
  window.history.replaceState(null, '', '#/');
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); window.history.replaceState(null, '', '#/'); });

describe('portfolio routes, creation and optional goals', () => {
  it('links from existing customer goals with Customers active, creates multiple portfolios and attaches, changes or detaches a goal later', async () => {
    const server = mockServer(); server.portfolios = [];
    await open(goalsRoute);
    expect(screen.getByRole('link', { name: 'Portfolios', exact: true })).toHaveAttribute('href', listRoute);
    expect(screen.getByRole('link', { name: /Retirement/ })).toBeInTheDocument();
    await follow('Portfolios');
    expect(within(screen.getByRole('navigation', { name: 'Customer sections' })).getByRole('link', { name: 'Goals' })).toHaveAttribute('href', goalsRoute);
    expect(screen.getByRole('link', { name: 'Customers', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText(/No portfolios yet/)).toBeInTheDocument(); expect(writes()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    change('Portfolio name', '  Emergency fund  '); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    expect(writes()[0]).toEqual([base, expect.objectContaining({ method: 'POST', body: { name: 'Emergency fund', goal_id: null }, signal: expect.any(AbortSignal) })]);
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add portfolio' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    expect(screen.getByLabelText('Portfolio name')).toHaveValue('');
    change('Portfolio name', 'Retirement investments'); change('Linked goal (optional)', goal.id); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    expect(writes()[1][1].body).toEqual({ name: 'Retirement investments', goal_id: goal.id });
    expect(screen.getByRole('link', { name: /Emergency fund/ })).toBeInTheDocument();
    await follow(/Emergency fund/);
    expect(screen.getByRole('link', { name: 'Alice · Goals' })).toHaveAttribute('href', goalsRoute);
    expect(screen.getByRole('heading', { name: 'Emergency fund' })).toBeInTheDocument();
    server.goals.push({ ...goal, id: 'goal-b', name: 'Home' });
    // Returning to the route reloads goal choices, as a goal may be created later.
    await navigate(listRoute); await navigate(`${listRoute}/portfolio-0`);
    fireEvent.click(screen.getByRole('button', { name: 'Edit portfolio name' }));
    change('Portfolio name', 'Safety net'); await submit(settings());
    expect(writes().at(-1)[1].body).toEqual({ name: 'Safety net', goal_id: null });
    expect(screen.getByRole('heading', { name: 'Safety net' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Assign goal' }));
    change('Linked goal (optional)', goal.id); await submit(screen.getByRole('form', { name: 'Assign portfolio goal' }));
    expect(writes().at(-1)[1].body).toEqual({ name: 'Safety net', goal_id: goal.id });
    fireEvent.click(screen.getByRole('button', { name: 'Change goal' }));
    change('Linked goal (optional)', 'goal-b'); await submit(screen.getByRole('form', { name: 'Assign portfolio goal' }));
    expect(writes().at(-1)[1].body).toEqual({ name: 'Safety net', goal_id: 'goal-b' });
    fireEvent.click(screen.getByRole('button', { name: 'Change goal' }));
    fireEvent.click(screen.getByRole('button', { name: 'Unassign goal' })); await flush();
    expect(writes().at(-1)[1].body).toEqual({ name: 'Safety net', goal_id: null });
    expect(screen.getByText('EUR · No linked goal')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['', `?instrument=${A}`])('defaults to Portfolios and switches bookmarkable tabs preserving planning context: %s', async (query) => {
    mockServer(); const view = await open(`${customerRoute}${query}`);
    const tabs = () => within(screen.getByRole('navigation', { name: 'Customer sections' }));
    expect(tabs().getAllByRole('link').map((link) => link.textContent)).toEqual(['Portfolios', 'Goals']);
    expect(tabs().getByRole('link', { name: 'Portfolios' })).toHaveAttribute('href', `${listRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Goals' })).toHaveAttribute('href', `${goalsRoute}${query}`);
    expect(window.location.hash).toBe(`${customerRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Portfolios' })).toHaveAttribute('aria-current', 'page');
    expect(tabs().getByRole('link', { name: 'Goals' })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('heading', { name: 'Alice', level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add portfolio' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Retirement/ })).not.toBeInTheDocument();
    // A reload opens the same tab without relying on local component state.
    view.unmount(); await open(`${listRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Portfolios' })).toHaveAttribute('aria-current', 'page');
    await follow('Goals');
    expect(window.location.hash).toBe(`${goalsRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Goals' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: /Retirement/ })).toHaveAttribute('href', `${customerRoute}/goals/${goal.id}${query}`);
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    await follow('Portfolios');
    expect(window.location.hash).toBe(`${listRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Portfolios' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add portfolio' })).toBeEnabled();
    expect(writes()).toHaveLength(0);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('aborts the previous tab read and ignores its late response after switching to goals', async () => {
    const server = mockServer(), pending = deferred();
    server.portfolioList = () => pending.promise;
    await open(listRoute);
    const signal = request.mock.calls.find(([path]) => path === pagePath(base))[1].signal;
    expect(screen.getByText('Loading portfolios…')).toBeInTheDocument();
    await follow('Goals');
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(page([metadata()], new URLSearchParams('limit=20&offset=0'))));
    expect(screen.getByRole('button', { name: 'Add goal' })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Goal summary for Alice' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Long term/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([`${listRoute}/bad%2Fid`, '#/goal/customers/%ZZ/portfolios', `${detailRoute}/extra`, `${listRoute}?instrument=invalid`])('rejects malformed routes without requests: %s', async (route) => {
    mockServer(); await open(route); expect(screen.getByRole('alert')).toHaveTextContent('ETF not found'); expect(request).not.toHaveBeenCalled();
  });

  it('paginates portfolios and loads every goal page rather than silently truncating choices', async () => {
    const server = mockServer();
    server.portfolios = Array.from({ length: 21 }, (_, i) => metadata({ id: `p-${i}`, name: `Portfolio ${i}` }));
    server.goals = Array.from({ length: 101 }, (_, i) => ({ ...goal, id: `g-${i}`, name: `Goal ${i}` }));
    await open(listRoute);
    fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    expect(screen.getByRole('option', { name: 'Goal 100', exact: true })).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith(`/customers/${cid}/goals?limit=100&offset=100`, expect.anything());
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Portfolios pagination' })).getByRole('button', { name: 'Next' })); await flush();
    expect(screen.getByRole('link', { name: /Portfolio 20/ })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /^Portfolio 0/ })).not.toBeInTheDocument();
    expect(screen.getByText('21–21 of 21')).toBeInTheDocument();
    change('Portfolio name', 'New last goal portfolio'); change('Linked goal (optional)', 'g-100'); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    expect(writes()[0][1].body.goal_id).toBe('g-100'); expect(screen.getByText('1–20 of 22')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /New last goal portfolio/ })).toBeInTheDocument();
  });

  it('requires identified customer context and offers retry when the customer cannot be loaded', async () => {
    const server = mockServer(); server.customer = async () => { throw new Error('Customer unavailable.'); };
    await open(); expect(screen.getByRole('alert')).toHaveTextContent('Customer unavailable.'); expect(screen.queryByRole('form')).not.toBeInTheDocument();
    server.customer = async () => ({ id: cid, name: 'Alice' });
    fireEvent.click(screen.getByRole('button', { name: 'Retry customer' })); await flush();
    expect(screen.getByRole('link', { name: 'Alice · Goals' })).toBeInTheDocument(); expect(transactionEntry()).not.toBeVisible();
  });

  it('does not implicitly detach a linked goal when goals are unavailable, and exposes retry and save errors', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata({ goal_id: goal.id })));
    server.goalList = async () => { throw new Error('Goals temporarily unavailable.'); };
    server.patch = async () => { throw new Error('Could not save settings.'); };
    await open(); fireEvent.click(screen.getByRole('button', { name: 'Change goal' }));
    expect(screen.getByText('Goals temporarily unavailable.')).toHaveAttribute('role', 'alert');
    fireEvent.click(within(screen.getByRole('form', { name: 'Assign portfolio goal' })).getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit portfolio name' }));
    change('Portfolio name', 'Rename only'); await submit(settings());
    expect(writes()[0][1].body).toEqual({ name: 'Rename only', goal_id: goal.id });
    expect(screen.getByText('Could not save settings.')).toHaveAttribute('role', 'alert');
    fireEvent.click(within(settings()).getByRole('button', { name: 'Cancel' }));
    server.goalList = async (query) => page(server.goals, query);
    fireEvent.click(screen.getByRole('button', { name: 'Change goal' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry goals' })); await flush();
    expect(screen.getByRole('option', { name: 'Retirement' })).toBeInTheDocument();
  });
});

describe('portfolio list creation and financial summary', () => {
  const table = () => screen.getByRole('table', { name: 'Portfolio summary' });

  it('opens creation only on request, focuses the input and discards canceled drafts without writes', async () => {
    mockServer(); await open(listRoute);
    const add = screen.getByRole('button', { name: 'Add portfolio' });
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    expect(add).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(add);
    expect(add).toBeDisabled(); expect(add).toHaveAttribute('aria-expanded', 'true');
    expect(add).toHaveAttribute('aria-controls', screen.getByRole('form', { name: 'New portfolio' }).id);
    expect(screen.getByLabelText('Portfolio name')).toHaveFocus();
    change('Portfolio name', 'Discard this'); change('Linked goal (optional)', goal.id);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(add).toHaveFocus(); expect(add).toBeEnabled(); expect(writes()).toHaveLength(0);
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    fireEvent.click(add);
    expect(screen.getByLabelText('Portfolio name')).toHaveValue('');
    expect(screen.getByLabelText('Linked goal (optional)')).toHaveValue('');
    expect(table()).toBeInTheDocument();
  });

  it('retains failed creation drafts and disables cancellation and duplicate submission while pending', async () => {
    const server = mockServer(), pending = deferred(); server.create = () => pending.promise;
    await open(listRoute); fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    change('Portfolio name', 'Keep this'); change('Linked goal (optional)', goal.id);
    const form = screen.getByRole('form', { name: 'New portfolio' });
    await submit(form); await submit(form);
    expect(writes()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled();
    await act(async () => pending.reject(new Error('Could not create portfolio.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not create portfolio.');
    expect(screen.getByLabelText('Portfolio name')).toHaveValue('Keep this');
    expect(screen.getByLabelText('Linked goal (optional)')).toHaveValue(goal.id);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
  });

  it('matches customer financial columns and formats exact money, signs, goals and unavailable values', async () => {
    const server = mockServer();
    server.portfolios = [portfolioSummary({ name: 'Linked investments', goal_id: goal.id, net_worth_eur: '9007199254740993.01',
      realized_pnl_eur: '-12.34', unrealized_pnl_eur: '0.00', today_pnl_eur: '12.34', total_pnl_eur: '-12.34' }),
    portfolioSummary({ id: 'missing', name: 'Missing prices', net_worth_eur: null, unrealized_pnl_eur: null,
      today_pnl_eur: null, total_pnl_eur: null, today_from_date: null, today_to_date: null })];
    await open(listRoute);
    expect(within(table()).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual([
      'Portfolio', 'Linked goal', 'Net worth (EUR)', 'Realized P&L (incl. fees, EUR)', 'Unrealized P&L (EUR)', 'Today P&L (EUR)', 'Total P&L (EUR)',
    ]);
    const rows = within(table()).getAllByRole('row');
    expect(within(rows[1]).getByRole('link', { name: 'Linked investments' })).toHaveAttribute('href', detailRoute);
    expect(within(rows[1]).getByText('Retirement')).toBeInTheDocument();
    expect(within(rows[1]).getByText('€9,007,199,254,740,993.01')).toBeInTheDocument();
    expect(within(rows[1]).getByText('€12.34')).toHaveClass('return-positive');
    expect(within(rows[1]).getAllByText('-€12.34').every((cell) => cell.classList.contains('return-negative'))).toBe(true);
    expect(within(rows[1]).getByText('€0.00')).not.toHaveClass('return-positive', 'return-negative');
    expect(within(rows[1]).queryByText(/2026-09-2[23]/)).not.toBeInTheDocument();
    expect(within(rows[1]).getAllByRole('cell')[4]).toHaveTextContent(/^€12\.34$/);
    expect(within(rows[1]).getAllByRole('cell')[4]).toHaveAttribute('title', 'Price date: 2026-09-23');
    expect(within(rows[2]).getByText('No goal')).toBeInTheDocument();
    expect(within(rows[2]).getAllByText('Unavailable')).toHaveLength(4);
    expect(within(rows[2]).getAllByRole('cell')[4]).not.toHaveAttribute('title');
    expect(within(rows[2]).getByText('Some holdings cannot be valued.')).toBeInTheDocument();
    expect(within(rows[2]).getByText('€0.00')).toBeInTheDocument();
    expect(table()).not.toHaveAttribute('aria-describedby');
    expect(screen.queryByText(/External assets and debts are not included/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Today P&L uses the latest saved closes/)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Portfolios table' })).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('region', { name: 'Portfolios table' })).toHaveAccessibleDescription('Scroll horizontally to view all columns.');
    expect(request.mock.calls.some(([path]) => path.startsWith(`${base}/`))).toBe(false);
    expect(writes()).toHaveLength(0);
  });

  it.each([
    { net_worth_eur: 12.34 }, { net_worth_eur: '-1.00' }, { net_worth_eur: '1.234' },
    { realized_pnl_eur: null }, { realized_pnl_eur: 'NaN' }, { unrealized_pnl_eur: undefined },
    { total_pnl_eur: '1e3' }, { today_pnl_eur: undefined }, { today_to_date: null },
    { today_from_date: '2026-02-30' }, { today_from_date: '2026-09-24' },
  ])('rejects malformed portfolio summaries and permits an explicit retry: %j', async (invalid) => {
    const server = mockServer(); server.portfolios = [portfolioSummary(invalid)];
    await open(listRoute);
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected portfolios');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    server.portfolios = [metadata()];
    fireEvent.click(screen.getByRole('button', { name: 'Retry portfolios' })); await flush();
    expect(table()).toBeInTheDocument(); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('hides outdated amounts on refresh failure and ignores superseded summary responses', async () => {
    const server = mockServer(); await open(listRoute);
    const pending = deferred(); server.portfolioList = () => pending.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolios' })); await flush();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const signal = request.mock.calls.filter(([path]) => path === pagePath(base)).at(-1)[1].signal;
    server.portfolioList = async () => { throw new Error('Summary unavailable.'); };
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolios' })); await flush();
    expect(signal.aborted).toBe(true);
    expect(screen.getByRole('alert')).toHaveTextContent('Summary unavailable.');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    server.portfolioList = async (query) => page([portfolioSummary({ net_worth_eur: '222.22' })], query);
    fireEvent.click(screen.getByRole('button', { name: 'Retry portfolios' })); await flush();
    await act(async () => pending.resolve(page([portfolioSummary({ net_worth_eur: '999.99' })], new URLSearchParams('limit=20&offset=0'))));
    expect(within(table()).getByText('€222.22')).toBeInTheDocument();
    expect(screen.queryByText('€999.99')).not.toBeInTheDocument();
  });

  it.each(['user', 'request', 'status', 'route'])('aborts creation and discards the draft after a %s change', async (kind) => {
    const server = mockServer(), pending = deferred(); server.create = () => pending.promise;
    const view = render(<PortfolioTracker customerId={cid} />); await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    change('Portfolio name', 'Private draft'); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    const signal = writes()[0][1].signal;
    if (kind === 'user') auth.user = { id: 'user-b' };
    if (kind === 'request') auth.request = vi.fn(server.handle);
    if (kind === 'status') auth.status = 'anonymous';
    view.rerender(<PortfolioTracker customerId={cid} portfolioId={kind === 'route' ? pid : undefined} />); await flush();
    expect(signal.aborted).toBe(true);
    const calls = request.mock.calls.length;
    await act(async () => pending.resolve(metadata({ name: 'Private draft' })));
    expect(request.mock.calls).toHaveLength(calls);
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    expect(screen.queryByText('Private draft')).not.toBeInTheDocument();
  });
});

describe('exact manually dated cash and trade transactions', () => {
  it('defaults both dates to today UTC and submits exact deposit, withdrawal, buy and sell strings without ledger calculation', async () => {
    const server = mockServer(); await open();
    expect(transactionEntry()).not.toBeVisible();
    expect(screen.queryByRole('form', { name: 'Add transaction' })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Rename portfolio' })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Assign portfolio goal' })).not.toBeInTheDocument();
    addTransaction(); expect(screen.getByLabelText('Transaction date (UTC)')).toHaveValue('2026-09-24'); expect(writes()).toHaveLength(0);
    change('Amount (EUR)', '9007199254740993.01'); await submit(cashForm());
    expect(transactionEntry()).not.toBeVisible(); addTransaction('withdrawal');
    expect(writes()[0][1].body).toEqual({ client_id: expect.any(String), date: '2026-09-24', type: 'deposit', amount_eur: '9007199254740993.01', currency: 'EUR' });
    change('Transaction date (UTC)', '2020-02-29'); change('Amount (EUR)', '000123.4'); await submit(cashForm());
    expect(transactionEntry()).not.toBeVisible(); addTransaction('buy');
    expect(screen.getByLabelText('Transaction date (UTC)')).toHaveValue('2026-09-24');
    expect(writes()[1][1].body).toEqual({ client_id: expect.any(String), date: '2020-02-29', type: 'withdrawal', amount_eur: '123.40', currency: 'EUR' });
    fillTrade({ Quantity: '9007199254740993.12345678', 'Transaction date (UTC)': '2020-01-02', 'Commission (EUR, optional)': '1.25' }); await submit(tradeForm());
    expect(transactionEntry()).not.toBeVisible(); addTransaction('sell');
    expect(writes()[2][1].body).toEqual({ client_id: expect.any(String), date: '2020-01-02', type: 'buy', instrument_id: A, quantity: '9007199254740993.12345678', price_eur: '123.12345678', commission_eur: '1.25' });
    fillTrade({ Quantity: '0.00000001', 'Average execution price (EUR)': '1.00000001' }); await submit(tradeForm());
    expect(writes()[3][1].body).toEqual({ client_id: expect.any(String), date: '2026-09-24', type: 'sell', instrument_id: A, quantity: '0.00000001', price_eur: '1.00000001', commission_eur: '0.00' });
    expect(new Set(writes().map(([, options]) => options.body.client_id)).size).toBe(4);
    expect(server.transactions).toHaveLength(4); expect(screen.getByText('Sell saved.')).toBeInTheDocument();
    expect(summaryValue('Cash')).toHaveTextContent('€1,000.00'); // server fixture, never inferred from submitted cash
    transactionsTab(); expect(within(screen.getByRole('table', { name: /Transactions ·/ })).getAllByRole('row')).toHaveLength(5);
    expect(request.mock.calls.some(([path]) => path.includes('/instruments/acquired'))).toBe(false);
  });

  it('discloses ledger correction rules before saving and links empty EUR choices to acquisition and currency settings', async () => {
    const server = mockServer(); server.instruments = []; await open(); addTransaction('buy');
    expect(screen.getByRole('link', { name: 'Add an ETF' })).toHaveAttribute('href', '#/add');
    expect(screen.getByRole('link', { name: 'your ETFs' })).toHaveAttribute('href', '#/');
    expect(screen.getByText(/set its price currency to EUR/)).toBeInTheDocument();
    for (const type of ['buy', 'deposit']) {
      transactionType(type);
      const form = transactionForm(), notice = within(form).getByText(/Saved entries cannot be edited/);
      expect(notice).toHaveTextContent('can be removed from transaction history if the remaining cash and holdings stay non-negative');
      expect(notice).toHaveTextContent('Dividends, splits and other corporate actions are not processed');
      expect(form).toHaveAttribute('aria-describedby', notice.id);
      expect(notice.compareDocumentPosition(within(form).getByRole('button', { name: 'Save transaction' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(writes()).toHaveLength(0);
  });

  it('loads paginated EUR instruments and permits selling held snapshots when inventory is unavailable', async () => {
    const server = mockServer();
    server.instruments = Array.from({ length: 101 }, (_, i) => ({ ...instrument, instrument_id: `IE${String(i).padStart(10, '0')}`, name: `Fund ${i}` }));
    await open(); addTransaction('buy'); expect(screen.getByRole('option', { name: /Fund 100 ·/ })).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith('/portfolio-instruments?limit=100&offset=100', expect.anything());
    await navigate(listRoute);
    server.instrumentList = async () => { throw new Error('Inventory unavailable.'); };
    server.details.set(pid, snapshot(metadata(), { positions: [position({ instrument: held })] })); await navigate(detailRoute); addTransaction('buy');
    expect(screen.getByText('Inventory unavailable.')).toHaveAttribute('role', 'alert');
    expect(screen.queryByRole('option', { name: /Retired ETF/ })).not.toBeInTheDocument();
    transactionType('sell'); expect(screen.getByRole('option', { name: /Retired ETF/ })).toBeInTheDocument();
    fillTrade({ 'Trade instrument': B, Quantity: '1' }); await submit(tradeForm());
    expect(writes()[0][1].body).toMatchObject({ type: 'sell', instrument_id: B, quantity: '1' });
    expect(screen.getByText('Sell saved.')).toBeInTheDocument();
  });

  it('disables pending submissions and reuses client IDs for failed unchanged payloads, including edit-and-revert', async () => {
    const server = mockServer(), pending = deferred(); server.save = () => pending.promise;
    await open(); addTransaction('buy'); fillTrade(); await submit(tradeForm());
    const first = writes()[0][1].body;
    expect(first.client_id).toMatch(/^[0-9a-f-]{36}$/i);
    expect(within(tradeForm()).getByRole('button', { name: 'Saving…' })).toBeDisabled();
    expect(screen.getByLabelText('Quantity')).toBeDisabled();
    await submit(tradeForm()); expect(writes()).toHaveLength(1);
    await act(async () => pending.reject(new Error('Connection interrupted; save outcome unknown.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Connection interrupted');
    expect(screen.getByLabelText('Quantity')).toHaveValue(first.quantity);
    server.save = async () => { throw new Error('Still offline.'); };
    await submit(tradeForm()); expect(writes()[1][1].body).toEqual(first);
    change('Quantity', '2'); await submit(tradeForm()); expect(writes()[2][1].body.client_id).not.toBe(first.client_id);
    change('Quantity', first.quantity); await submit(tradeForm()); expect(writes()[3][1].body).toEqual(first);
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); }); expect(writes()).toHaveLength(4);
    server.save = async (body) => ({ ...body, id: 'saved', instrument, sequence: 1, created_at: '2026-09-24T00:00:00Z' });
    await submit(tradeForm()); expect(writes()[4][1].body).toEqual(first); expect(transactionEntry()).not.toBeVisible();
    addTransaction('buy'); expect(screen.getByLabelText('Quantity')).toHaveValue('');
    fillTrade(); await submit(tradeForm()); expect(writes()[5][1].body.client_id).not.toBe(first.client_id);
  });

  it('validates a missing instrument, cash date and portfolio name before submitting', async () => {
    mockServer(); await open(); addTransaction('buy');
    await submit(tradeForm()); expect(screen.getByRole('alert')).toHaveTextContent('Choose an available EUR instrument');
    transactionType('deposit'); change('Transaction date (UTC)', ''); change('Amount (EUR)', '10'); await submit(cashForm());
    expect(within(cashForm()).getByRole('alert')).toHaveTextContent('valid transaction date');
    fireEvent.click(within(transactionForm()).getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit portfolio name' }));
    change('Portfolio name', '   '); await submit(settings());
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a portfolio name'); expect(writes()).toHaveLength(0);
  });

  it('keeps independent form drafts and pending IDs intact while another transaction refreshes the portfolio', async () => {
    const server = mockServer(), save = server.save;
    server.save = async (body) => { if (body.type === 'buy') throw new Error('Insufficient cash.'); return save(body); };
    await open(); addTransaction('buy'); fillTrade({ 'Transaction date (UTC)': '2020-02-29', 'Commission (EUR, optional)': '1.00' });
    await submit(tradeForm()); const retryBody = writes()[0][1].body;
    transactionType('deposit'); change('Transaction date (UTC)', '2020-01-01'); change('Amount (EUR)', '500'); await submit(cashForm());
    expect(transactionEntry()).not.toBeVisible(); addTransaction('buy');
    expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    expect(screen.getByLabelText('Transaction date (UTC)')).toHaveValue('2020-02-29');
    expect(screen.getByLabelText('Commission (EUR, optional)')).toHaveValue('1.00');
    server.save = save; await submit(tradeForm()); expect(writes()[2][1].body).toEqual(retryBody);
  });

  it('retains an uncertain cash client ID even if the response is malformed', async () => {
    const server = mockServer(); server.save = async () => ({ id: 'bad' }); await open(); addTransaction();
    change('Amount (EUR)', '10'); await submit(cashForm()); expect(screen.getByRole('alert')).toHaveTextContent('Unexpected response');
    await submit(cashForm()); expect(writes()[1][1].body).toEqual(writes()[0][1].body);
  });

  it.each(['0', '-1', '1.001', '1e3', 'NaN', ''])('validates cash amount %j without a write', async (value) => {
    mockServer(); await open(); addTransaction(); change('Amount (EUR)', value); await submit(cashForm());
    expect(screen.getByRole('alert')).toHaveTextContent('positive cash amount'); expect(writes()).toHaveLength(0);
  });
  it.each([
    ['Quantity', '0', 'positive quantity'], ['Quantity', '1.123456789', 'positive quantity'], ['Average execution price (EUR)', '-1', 'positive quantity'],
    ['Average execution price (EUR)', '1e2', 'positive quantity'], ['Commission (EUR, optional)', '-1', 'non-negative commission'], ['Commission (EUR, optional)', '0.001', 'non-negative commission'],
    ['Transaction date (UTC)', '2026-09-25', 'valid transaction date'], ['Transaction date (UTC)', '', 'valid transaction date'],
  ])('validates %s=%j without a write', async (label, value, message) => {
    mockServer(); await open(); addTransaction('buy'); fillTrade({ [label]: value }); await submit(tradeForm());
    expect(screen.getByRole('alert')).toHaveTextContent(message); expect(writes()).toHaveLength(0);
  });

  it('shows backend long-only and cash-funding validation errors without clearing inputs', async () => {
    const server = mockServer(); server.save = async (body) => { throw new Error(body.type === 'buy' ? 'Insufficient cash on transaction date.' : 'Cannot sell more than held.'); };
    await open(); addTransaction('buy'); fillTrade(); await submit(tradeForm());
    expect(screen.getByRole('alert')).toHaveTextContent('Insufficient cash on transaction date.');
    expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    transactionType('sell'); await submit(tradeForm()); expect(screen.getByRole('alert')).toHaveTextContent('Cannot sell more than held.');
    expect(writes()[1][1].body.client_id).not.toBe(writes()[0][1].body.client_id);
  });
});

describe('portfolio detail interactions', () => {
  it.each(['Positions', 'Transactions'])('expands transaction entry inline above %s and preserves the draft while using the page', async (tab) => {
    mockServer(); await open();
    fireEvent.click(screen.getByRole('tab', { name: tab }));
    const add = screen.getByRole('button', { name: 'Add transaction' });
    expect(add).toHaveAttribute('aria-expanded', 'false');
    expect(add).toHaveAttribute('aria-controls', transactionEntry().id);
    expect(add).not.toHaveAttribute('aria-haspopup');
    expect(document.querySelector('dialog, [role="dialog"]')).not.toBeInTheDocument();
    expect(transactionEntry()).not.toBeVisible();
    addTransaction('buy');
    const form = transactionForm();
    expect(add).toHaveAttribute('aria-expanded', 'true'); expect(add).toBeDisabled();
    expect(screen.getByLabelText('Transaction type')).toHaveFocus();
    expect(form).toBeVisible(); expect(form.closest('[role="tabpanel"]')).toBeNull();
    expect(form.compareDocumentPosition(screen.getByRole('tabpanel')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fillTrade();
    const otherTab = screen.getByRole('tab', { name: tab === 'Positions' ? 'Transactions' : 'Positions' });
    fireEvent.click(otherTab); otherTab.focus();
    expect(otherTab).toHaveFocus(); expect(otherTab).toHaveAttribute('aria-selected', 'true');
    // Escape outside the form does not dismiss the draft.
    fireEvent.keyDown(otherTab, { key: 'Escape' });
    expect(transactionForm()).toBe(form); expect(form).toBeVisible();
    expect(screen.getByRole('region', { name: 'Portfolio summary' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolio' })); await flush();
    expect(transactionForm()).toBe(form); expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    fireEvent.click(within(form).getByRole('button', { name: 'Cancel' }));
    expect(form).not.toBeVisible(); expect(form).toBeInTheDocument();
    expect(add).toHaveFocus(); expect(add).toHaveAttribute('aria-expanded', 'false'); expect(add).toBeEnabled();
    fireEvent.click(add);
    expect(transactionForm()).toBe(form); expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    expect(writes()).toHaveLength(0);
    await submit(form);
    expect(transactionEntry()).not.toBeVisible(); expect(add).toHaveFocus();
    expect(add).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('Buy saved.')).toBeVisible(); expect(writes()).toHaveLength(1);
  });

  it('shows statement import only on Transactions and preserves its draft across tabs', async () => {
    mockServer(); await open();
    expect(screen.queryByRole('button', { name: 'Import DEGIRO CSV' })).not.toBeInTheDocument();
    transactionsTab();
    const panel = screen.getByRole('tabpanel', { name: 'Transactions' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Import DEGIRO CSV' }));
    const file = within(panel).getByLabelText(/DEGIRO CSV file/);
    expect(file).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Positions' }));
    expect(screen.queryByRole('button', { name: 'Import DEGIRO CSV' })).not.toBeInTheDocument();
    expect(file).not.toBeVisible();
    transactionsTab();
    expect(within(panel).getByLabelText(/DEGIRO CSV file/)).toBe(file);
    expect(file).toBeVisible();
    expect(within(panel).getByRole('button', { name: 'Cancel import' })).toBeVisible();
    expect(writes()).toHaveLength(0);
  });

  it('uses keyboard-operable tabs with shared summary and mounted but hidden panels', async () => {
    const server = mockServer(); server.transactions = [cashEvent(1)]; await open();
    const positions = screen.getByRole('tab', { name: 'Positions' }), transactions = screen.getByRole('tab', { name: 'Transactions' });
    expect(positions).toHaveAttribute('aria-selected', 'true'); expect(positions).toHaveAttribute('tabindex', '0');
    expect(transactions).toHaveAttribute('aria-selected', 'false'); expect(transactions).toHaveAttribute('tabindex', '-1');
    expect(document.getElementById('portfolio-transactions-panel')).toHaveAttribute('hidden');
    expect(document.getElementById('portfolio-positions-panel')).not.toHaveAttribute('hidden');
    positions.focus(); fireEvent.keyDown(positions, { key: 'ArrowRight' });
    expect(transactions).toHaveFocus(); expect(transactions).toHaveAttribute('aria-selected', 'true');
    expect(document.getElementById('portfolio-positions-panel')).toHaveAttribute('hidden');
    expect(screen.getByRole('table', { name: /Transactions ·/ })).toBeInTheDocument();
    expect(summaryValue('Cash')).toHaveTextContent('€1,000.00');
    fireEvent.keyDown(transactions, { key: 'Home' }); expect(positions).toHaveFocus();
    fireEvent.keyDown(positions, { key: 'End' }); expect(transactions).toHaveFocus();
    fireEvent.keyDown(transactions, { key: 'ArrowLeft' }); expect(positions).toHaveFocus();
  });

  it('retains transaction pagination and the shared summary when switching tabs', async () => {
    const server = mockServer(); server.transactions = Array.from({ length: 21 }, (_, i) => cashEvent(21 - i));
    await open(); const summary = screen.getByRole('region', { name: 'Portfolio summary' });
    transactionsTab(); fireEvent.click(within(screen.getByRole('navigation', { name: 'Transactions pagination' })).getByRole('button', { name: 'Next' })); await flush();
    const reads = request.mock.calls.length;
    fireEvent.click(screen.getByRole('tab', { name: 'Positions' }));
    expect(screen.queryByRole('table', { name: /Transactions ·/ })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Portfolio summary' })).toBe(summary);
    transactionsTab(); expect(screen.getByText('21–21 of 21')).toBeVisible();
    expect(screen.getByRole('region', { name: 'Portfolio summary' })).toBe(summary);
    expect(request.mock.calls).toHaveLength(reads); expect(writes()).toHaveLength(0);
  });

  it.each(['name', 'goal'])('allows canceling a %s draft after a portfolio refresh fails but blocks stale saves', async (editor) => {
    const server = mockServer(); await open();
    fireEvent.click(screen.getByRole('button', { name: editor === 'name' ? 'Edit portfolio name' : 'Assign goal' }));
    const form = screen.getByRole('form', { name: editor === 'name' ? 'Rename portfolio' : 'Assign portfolio goal' });
    server.detail = async () => { throw new Error('Portfolio refresh unavailable.'); };
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolio' })); await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('Portfolio refresh unavailable.');
    expect(within(form).getByRole('button', { name: editor === 'name' ? 'Save name' : 'Save goal' })).toBeDisabled();
    await submit(form); expect(writes()).toHaveLength(0);
    expect(within(form).getByRole('button', { name: 'Cancel' })).toBeEnabled();
    fireEvent.click(within(form).getByRole('button', { name: 'Cancel' }));
    expect(form).not.toBeInTheDocument();
  });

  it('retains uncertain trade and cash retry IDs, their separate dates and drafts across cancel/reopen', async () => {
    const server = mockServer(); server.save = async () => { throw new Error('Outcome unknown.'); }; await open();
    addTransaction('buy'); fillTrade({ 'Transaction date (UTC)': '2020-02-29' }); await submit(tradeForm());
    const trade = writes()[0][1].body;
    fireEvent.click(within(transactionForm()).getByRole('button', { name: 'Cancel' }));
    expect(transactionEntry()).not.toBeVisible(); addTransaction('deposit');
    expect(screen.getByLabelText('Transaction date (UTC)')).toHaveValue('2026-09-24');
    change('Transaction date (UTC)', '2020-01-01'); change('Amount (EUR)', '25'); await submit(cashForm());
    const cash = writes()[1][1].body;
    fireEvent.click(within(transactionForm()).getByRole('button', { name: 'Cancel' }));
    addTransaction('buy'); expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    expect(screen.getByLabelText('Transaction date (UTC)')).toHaveValue('2020-02-29');
    await submit(tradeForm()); expect(writes()[2][1].body).toEqual(trade);
    transactionType('deposit'); expect(screen.getByLabelText('Transaction date (UTC)')).toHaveValue('2020-01-01');
    expect(screen.getByLabelText('Amount (EUR)')).toHaveValue('25');
    await submit(cashForm()); expect(writes()[3][1].body).toEqual(cash);
  });

  it('blocks cancel and Escape while a transaction is pending, then allows Escape after failure', async () => {
    const server = mockServer(), pending = deferred(); server.save = () => pending.promise; await open();
    addTransaction(); change('Amount (EUR)', '10'); await submit(cashForm());
    expect(within(transactionForm()).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    fireEvent.keyDown(transactionForm(), { key: 'Escape' }); expect(transactionEntry()).toBeVisible();
    expect(screen.getByRole('button', { name: 'Add transaction' })).toBeDisabled();
    transactionsTab(); expect(transactionEntry()).toBeVisible();
    await act(async () => pending.reject(new Error('Outcome unknown.')));
    fireEvent.keyDown(screen.getByLabelText('Amount (EUR)'), { key: 'Escape' }); expect(transactionEntry()).not.toBeVisible();
    expect(screen.getByRole('button', { name: 'Add transaction' })).toHaveFocus();
    addTransaction(); expect(screen.getByLabelText('Amount (EUR)')).toHaveValue('10');
  });

  it.each(['user', 'request', 'status', 'route'])('ignores a late metadata save after %s changes', async (kind) => {
    const server = mockServer(), pending = deferred(); server.patch = () => pending.promise;
    const view = render(<PortfolioTracker customerId={cid} portfolioId={pid} />); await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Edit portfolio name' }));
    change('Portfolio name', 'Private rename'); await submit(settings());
    const signal = writes()[0][1].signal;
    if (kind === 'user') auth.user = { id: 'user-b' };
    if (kind === 'request') auth.request = vi.fn(server.handle);
    if (kind === 'status') auth.status = 'anonymous';
    view.rerender(<PortfolioTracker customerId={cid} portfolioId={kind === 'route' ? undefined : pid} />); await flush();
    expect(signal.aborted).toBe(true);
    const count = request.mock.calls.length;
    await act(async () => pending.resolve(metadata({ name: 'Private rename' })));
    expect(request.mock.calls).toHaveLength(count);
    expect(screen.queryByText('Private rename')).not.toBeInTheDocument();
    expect(screen.queryByText('Portfolio name saved.')).not.toBeInTheDocument();
  });

  it('cancels metadata edits without writes and retains the full name and goal PATCH contract', async () => {
    mockServer(); await open(); const rename = screen.getByRole('button', { name: 'Edit portfolio name' });
    fireEvent.click(rename); change('Portfolio name', 'Discarded');
    fireEvent.click(within(settings()).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Edit portfolio name' })).toHaveFocus();
    expect(screen.queryByRole('form', { name: 'Rename portfolio' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Assign goal' }));
    change('Linked goal (optional)', goal.id);
    fireEvent.click(within(screen.getByRole('form', { name: 'Assign portfolio goal' })).getByRole('button', { name: 'Cancel' }));
    expect(writes()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Assign goal' }));
    expect(screen.getByLabelText('Linked goal (optional)')).toHaveValue('');
    change('Linked goal (optional)', goal.id); await submit(screen.getByRole('form', { name: 'Assign portfolio goal' }));
    expect(writes()[0][1].body).toEqual({ name: 'Long term', goal_id: goal.id });
  });
});

describe('transaction removal', () => {
  const history = () => within(screen.getByRole('region', { name: 'Transaction history' }));
  const confirmation = () => screen.getByRole('form', { name: 'Remove transaction' });
  async function selectFirst() { transactionsTab(); fireEvent.click(history().getAllByRole('button', { name: /^Remove / })[0]); await flush(); }

  it('requires confirmation, supports cancel, and refreshes history, holdings and authoritative balances', async () => {
    const server = mockServer(); server.transactions = [cashEvent(1)];
    const original = server.remove;
    server.remove = async (id) => {
      const result = await original(id);
      server.details.set(pid, snapshot(metadata(), { cash_eur: '42.00', positions: [], equity_eur: '42.00', realized_pnl_eur: '3.00' }));
      return result;
    };
    await open(); addTransaction('buy'); fillTrade(); transactionType('deposit'); change('Amount (EUR)', '25');
    fireEvent.click(within(transactionForm()).getByRole('button', { name: 'Cancel' })); transactionsTab(); await selectFirst();
    expect(confirmation()).toHaveTextContent('Deposit · 2026-09-23 · €100.00');
    expect(within(confirmation()).getByRole('heading')).toHaveFocus();
    expect(confirmation()).toHaveTextContent('It cannot be undone'); expect(writes()).toHaveLength(0);
    fireEvent.click(within(confirmation()).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('form', { name: 'Remove transaction' })).not.toBeInTheDocument(); expect(writes()).toHaveLength(0);
    expect(history().getByRole('button', { name: /^Remove deposit/ })).toHaveFocus();
    await selectFirst(); await submit(confirmation());
    expect(writes()).toEqual([[`${transactionsPath}/transaction-1`, expect.objectContaining({ method: 'DELETE', body: undefined, signal: expect.any(AbortSignal) })]]);
    expect(history().getByText('Transaction removed.')).toHaveAttribute('role', 'status');
    expect(history().getByRole('heading', { name: 'Transaction history' })).toHaveFocus();
    expect(history().getByText(/No transactions yet/)).toBeInTheDocument();
    expect(summaryValue('Cash')).toHaveTextContent('€42.00'); expect(summaryValue('Realized P&L (incl. fees)')).toHaveTextContent('€3.00');
    fireEvent.click(screen.getByRole('tab', { name: 'Positions' }));
    expect(screen.getByText('No open positions.')).toBeVisible();
    addTransaction(); expect(screen.getByLabelText('Amount (EUR)')).toHaveValue('25');
    transactionType('buy'); expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
  });

  it('shows trade details and keeps failed removals visible with a safe explicit retry', async () => {
    const server = mockServer(), original = server.remove;
    server.transactions = [{ ...cashEvent(1), type: 'buy', instrument, quantity: '1.25000001', price_eur: '100.00000001', commission_eur: '0.00' }];
    server.remove = async () => { throw new Error('Cannot remove this transaction: Insufficient holdings. Remove dependent transactions first.'); };
    await open(); await selectFirst();
    expect(confirmation()).toHaveTextContent('World ETF'); expect(confirmation()).toHaveTextContent('1.25000001 at €100.00000001');
    await submit(confirmation());
    expect(history().getByRole('alert')).toHaveTextContent('Remove dependent transactions first');
    expect(history().getByRole('button', { name: /^Remove buy/ })).toBeInTheDocument();
    expect(summaryValue('Cash')).toHaveTextContent('€1,000.00');
    server.remove = original; await submit(confirmation());
    expect(writes()).toHaveLength(2); expect(writes()[0][0]).toBe(writes()[1][0]);
    expect(history().queryByRole('alert')).not.toBeInTheDocument(); expect(history().getByText('Transaction removed.')).toBeInTheDocument();
  });

  it('prevents duplicate requests and pagination during removal and recovers an emptied last page', async () => {
    const server = mockServer(), pending = deferred(), original = server.remove;
    server.transactions = Array.from({ length: 21 }, (_, i) => cashEvent(21 - i));
    server.remove = () => pending.promise;
    await open(); transactionsTab(); fireEvent.click(within(screen.getByRole('navigation', { name: 'Transactions pagination' })).getByRole('button', { name: 'Next' })); await flush();
    await selectFirst(); const form = confirmation(); await submit(form); await submit(form);
    expect(writes()).toHaveLength(1); expect(within(form).getByRole('button', { name: 'Removing…' })).toBeDisabled();
    expect(within(form).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(history().getByRole('button', { name: /^Remove deposit/ })).toBeDisabled();
    expect(within(screen.getByRole('navigation', { name: 'Transactions pagination' })).getByRole('button', { name: 'Previous' })).toBeDisabled();
    await act(async () => pending.resolve(await original('transaction-1')));
    expect(history().getByText('1–20 of 20')).toBeInTheDocument();
    expect(history().getAllByRole('button', { name: /^Remove deposit/ })).toHaveLength(20);
  });

  it.each(['lost response', 'malformed response'])('retries the same removal after a %s without changing local balances', async (failure) => {
    const server = mockServer(), original = server.remove; server.transactions = [cashEvent(1)];
    server.remove = async (id) => {
      await original(id);
      if (failure === 'lost response') throw new Error('Connection interrupted; retry removal.');
      return { id: 'wrong', removed: true };
    };
    await open(); await selectFirst(); await submit(confirmation());
    expect(history().getByRole('alert')).toHaveTextContent(failure === 'lost response' ? 'Connection interrupted' : 'Unexpected response');
    expect(history().getByRole('button', { name: /^Remove deposit/ })).toBeInTheDocument();
    expect(summaryValue('Cash')).toHaveTextContent('€1,000.00');
    server.remove = original; await submit(confirmation());
    expect(writes()[0][0]).toBe(writes()[1][0]); expect(history().getByText(/No transactions yet/)).toBeInTheDocument();
  });

  it.each(['user', 'request', 'status', 'route'])('ignores a late removal after %s changes', async (kind) => {
    const server = mockServer(), pending = deferred(); server.transactions = [cashEvent(1)]; server.remove = () => pending.promise;
    const view = render(<PortfolioTracker customerId={cid} portfolioId={pid} />); await flush();
    await selectFirst(); await submit(confirmation()); const signal = writes()[0][1].signal;
    if (kind === 'user') auth.user = { id: 'user-b' };
    if (kind === 'request') auth.request = vi.fn(server.handle);
    if (kind === 'status') auth.status = 'anonymous';
    view.rerender(<PortfolioTracker customerId={cid} portfolioId={kind === 'route' ? undefined : pid} />); await flush();
    expect(signal.aborted).toBe(true);
    const reads = request.mock.calls.length;
    await act(async () => pending.resolve({ id: 'transaction-1', removed: true }));
    expect(request.mock.calls).toHaveLength(reads);
    expect(screen.queryByText('Transaction removed.')).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Remove transaction' })).not.toBeInTheDocument();
  });
});

describe('valuation, transaction pagination and read failures', () => {
  it('shows an immediate commission loss separately from unchanged holdings', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata(), {
      cash_eur: '0.00', positions_value_eur: '1000.00', equity_eur: '1000.00',
      realized_pnl_eur: '-6.00', unrealized_pnl_eur: '0.00', daily_unrealized_pnl_eur: '0.00',
      positions: [position({ quantity: '10', average_price_eur: '100.00000000', cost_basis_eur: '1000.00',
        market_price_eur: '100', position_value_eur: '1000.00', unrealized_pnl_eur: '0.00', daily_unrealized_pnl_eur: '0.00' })],
    }));
    await open();
    expect(summaryValue('Realized P&L (incl. fees)')).toHaveTextContent('-€6.00');
    expect(summaryValue('Realized P&L (incl. fees)')).toHaveClass('return-negative');
    expect(summaryValue('Total unrealized P&L')).toHaveTextContent('€0.00');
    expect(summaryValue('Daily unrealized P&L')).toHaveTextContent('€0.00');
    expect(screen.queryByText(/Daily price comparison/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Weighted-average cost excludes commissions/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Values are calculated by the server/)).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it('distinguishes unavailable, zero and negative P&L and shows price dates and warnings', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata(), { cash_eur: '0.00', equity_eur: null, positions_value_eur: null,
      realized_pnl_eur: '-12.34', unrealized_pnl_eur: '0.00', daily_unrealized_pnl_eur: null, daily_from_date: null, daily_to_date: null,
      positions: [position({ market_price_eur: null, price_date: null, previous_price_date: null, position_value_eur: null, unrealized_pnl_eur: null, daily_unrealized_pnl_eur: null, warning: 'No market price.' })], warnings: ['Valuation incomplete.'] }));
    await open(); expect(summaryValue('Cash')).toHaveTextContent('€0.00'); expect(summaryValue('Equity')).toHaveTextContent('Unavailable');
    expect(summaryValue('Realized P&L (incl. fees)')).toHaveTextContent('-€12.34'); expect(summaryValue('Total unrealized P&L')).toHaveTextContent('€0.00');
    expect(summaryValue('Daily unrealized P&L')).toHaveTextContent('Unavailable');
    expect(summaryValue('Realized P&L (incl. fees)')).toHaveClass('return-negative');
    expect(summaryValue('Total unrealized P&L')).not.toHaveClass('return-positive', 'return-negative');
    expect(summaryValue('Daily unrealized P&L')).not.toHaveClass('return-positive', 'return-negative');
    expect(screen.getByText('Valuation incomplete.')).toBeInTheDocument(); expect(screen.getByText('No market price.')).toBeInTheDocument();
    const table = screen.getByRole('table', { name: 'Current holdings · EUR' });
    expect(within(table).getAllByText('Unavailable')).toHaveLength(4); expect(within(table).getByText('1.25000001')).toBeInTheDocument();
    expect(within(table).getByText('€100.00')).toBeInTheDocument();
    expect(within(table).getAllByTitle('Price date unavailable')).toHaveLength(2);
    expect(within(table).queryByText(/Date unavailable/)).not.toBeInTheDocument();
  });

  it('keeps Positions price dates only in market-price and daily-P&L tooltips', async () => {
    const server = mockServer();
    server.details.set(pid, snapshot(metadata(), { positions: [position({
      previous_price_date: '2026-09-23', price_date: '2026-09-24',
    })] }));
    await open();
    const table = screen.getByRole('table', { name: 'Current holdings · EUR' });
    const cells = within(within(table).getAllByRole('row')[1]).getAllByRole('cell');
    expect(cells[3]).toHaveTextContent(/^€110\.00$/);
    expect(cells[6]).toHaveTextContent(/^-€1\.25$/);
    for (const cell of [cells[3], cells[6]]) {
      expect(cell).toHaveAttribute('title', 'Price date: 2026-09-24');
      expect(cell).toHaveAccessibleDescription('Price date: 2026-09-24');
    }
    expect(within(table).getByRole('columnheader', { name: 'Market price' })).toBeInTheDocument();
    expect(within(table).queryByText(/2026-09-2[34]/)).not.toBeInTheDocument();
  });

  it.each([
    ['100.12345678', '€100.12'], ['100.12500000', '€100.13'], ['1.00500000', '€1.01'],
    ['999.99999999', '€1,000.00'], ['9007199254740993.12500000', '€9,007,199,254,740,993.13'],
    ['0.00000001', '€0.00'], ['12', '€12.00'],
  ])('rounds average cost %s to two display decimals without changing source values', async (average, expected) => {
    const server = mockServer();
    const data = snapshot(metadata(), { positions: [position({ average_price_eur: average })] });
    const before = JSON.stringify(data);
    server.details.set(pid, data);
    await open();
    const table = screen.getByRole('table', { name: 'Current holdings · EUR' });
    const row = within(table).getAllByRole('row')[1];
    expect(within(row).getAllByRole('cell')[1].textContent).toBe(expected);
    expect(JSON.stringify(data)).toBe(before);
    expect(writes()).toHaveLength(0);
  });

  it('trims only insignificant display decimals for other prices and provides scroll cues for both wide tables', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata(), {
      positions: [position({ average_price_eur: '100.20000000', market_price_eur: '9007199254740993.12345678', cost_basis_eur: '123.45670000' })],
    })); server.transactions = [{ ...cashEvent(1), amount_eur: '123.40000000' }];
    await open(); const positions = screen.getByRole('table', { name: 'Current holdings · EUR' });
    expect(within(positions).getByText('€100.20')).toBeInTheDocument();
    expect(within(positions).getByText('€9,007,199,254,740,993.12345678')).toBeInTheDocument();
    expect(within(positions).getByText('€123.4567')).toBeInTheDocument();
    transactionsTab(); expect(within(screen.getByRole('table', { name: /Transactions ·/ })).getByText('€123.40')).toBeInTheDocument();
    expect(document.getElementById('portfolio-positions-panel')).toHaveAttribute('hidden');
    expect(document.getElementById('portfolio-positions-scroll')).toHaveTextContent('Scroll horizontally to view all columns.');
    expect(document.getElementById('portfolio-transactions-scroll')).toHaveTextContent('Scroll horizontally to view all columns.');
    expect(screen.getByRole('region', { name: 'Positions table', hidden: true })).toHaveAccessibleDescription('Scroll horizontally to view all columns.');
    expect(screen.getByRole('region', { name: 'Transactions table' })).toHaveAccessibleDescription('Scroll horizontally to view all columns.');
    expect(summaryValue('Total unrealized P&L')).toHaveClass('return-positive');
  });

  it('paginates transaction history and retries an explicit read failure without any write', async () => {
    const server = mockServer(); server.transactions = Array.from({ length: 21 }, (_, i) => cashEvent(21 - i));
    const original = server.transactionList; server.transactionList = async () => { throw new Error('History unavailable.'); };
    await open(); transactionsTab(); expect(screen.getByText('History unavailable.')).toHaveAttribute('role', 'alert');
    server.transactionList = original; fireEvent.click(screen.getByRole('button', { name: 'Retry transactions' })); await flush();
    const table = () => screen.getByRole('table', { name: /Transactions ·/ });
    expect(within(table()).getAllByRole('row')).toHaveLength(21);
    const nav = () => within(screen.getByRole('navigation', { name: 'Transactions pagination' }));
    fireEvent.click(nav().getByRole('button', { name: 'Next' })); await flush();
    expect(request).toHaveBeenCalledWith(pagePath(transactionsPath, 20), expect.anything());
    expect(within(table()).getAllByRole('row')).toHaveLength(2); expect(screen.getByText('21–21 of 21')).toBeInTheDocument();
    expect(nav().getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(nav().getByRole('button', { name: 'Previous' })); await flush(); expect(screen.getByText('1–20 of 21')).toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it('fails visibly on malformed pages and never exposes a silently incomplete instrument selector', async () => {
    const server = mockServer(); server.instrumentList = async (query) => Number(query.get('offset')) === 0
      ? { items: [instrument], total: 2, limit: 100, offset: 0 } : { items: [], total: 2, limit: 100, offset: 1 };
    await open(); addTransaction('buy'); expect(screen.getByRole('alert')).toHaveTextContent('could not be loaded completely');
    expect(screen.queryByRole('option', { name: /World ETF/ })).not.toBeInTheDocument();
    server.instrumentList = async (query) => page([instrument], query); fireEvent.click(screen.getByRole('button', { name: 'Retry instruments' })); await flush();
    expect(screen.getByRole('option', { name: /World ETF/ })).toBeInTheDocument();
    await navigate(listRoute); server.portfolioList = async () => ({ items: [], total: 2, offset: 0, limit: 20 });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolios' })); await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected portfolios');
  });
});

describe('route and session isolation', () => {
  it('aborts late list-page responses when navigating to detail and never renders old data on return', async () => {
    const server = mockServer(), pending = deferred();
    server.portfolios = Array.from({ length: 21 }, (_, i) => metadata({ id: `p-${i}`, name: `Portfolio ${i}` }));
    const original = server.portfolioList;
    server.portfolioList = (query) => Number(query.get('offset')) === 20 ? pending.promise : original(query);
    await open(listRoute); fireEvent.click(within(screen.getByRole('navigation', { name: 'Portfolios pagination' })).getByRole('button', { name: 'Next' })); await flush();
    const options = request.mock.calls.find(([path]) => path === pagePath(base, 20))[1];
    await navigate(detailRoute); expect(options.signal.aborted).toBe(true);
    await act(async () => pending.resolve({ items: [metadata({ id: 'late', name: 'Late portfolio' })], total: 21, offset: 20, limit: 20 }));
    expect(screen.getByRole('heading', { name: 'Long term' })).toBeInTheDocument(); expect(screen.queryByText('Late portfolio')).not.toBeInTheDocument();
    await navigate(listRoute); expect(screen.getByText('1–20 of 21')).toBeInTheDocument(); expect(writes()).toHaveLength(0);
  });

  it('aborts late detail and creation completions on navigation, including navigation away and back', async () => {
    const server = mockServer(), pendingDetail = deferred(), pendingCreate = deferred();
    const original = server.detail; server.detail = () => pendingDetail.promise;
    await open(); const signal = request.mock.calls.find(([path]) => path === detailPath)[1].signal;
    await navigate(listRoute); expect(signal.aborted).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    server.create = () => pendingCreate.promise; change('Portfolio name', 'Old create'); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    const createSignal = writes()[0][1].signal;
    server.detail = original; await navigate(detailRoute); expect(createSignal.aborted).toBe(true);
    await act(async () => { pendingDetail.resolve(snapshot(metadata({ name: 'Stale name' }))); pendingCreate.resolve(metadata({ name: 'Old create' })); });
    expect(screen.getByRole('heading', { name: 'Long term' })).toBeInTheDocument(); expect(screen.queryByText('Stale name')).not.toBeInTheDocument();
    expect(window.location.hash).toBe(detailRoute);
    await navigate(listRoute); expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add portfolio' }));
    expect(screen.getByLabelText('Portfolio name')).toHaveValue(''); expect(screen.queryByText('Portfolio created.')).not.toBeInTheDocument();
  });

  it('ignores an aborted refresh even if it resolves after the latest valuation', async () => {
    const server = mockServer(); await open(); const old = deferred(); server.detail = () => old.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolio' })); await flush();
    const oldSignal = request.mock.calls.filter(([path]) => path === detailPath).at(-1)[1].signal;
    server.detail = async () => snapshot(metadata(), { cash_eur: '222.22' });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh portfolio' })); await flush(); expect(oldSignal.aborted).toBe(true);
    await act(async () => old.resolve(snapshot(metadata(), { cash_eur: '999.99' })));
    expect(summaryValue('Cash')).toHaveTextContent('€222.22');
  });

  it.each(['user', 'request', 'status', 'route'])('resets standalone state and aborts an old transaction on %s change', async (kind) => {
    const server = mockServer(), pending = deferred(); server.save = () => pending.promise;
    const view = render(<PortfolioTracker customerId={cid} portfolioId={pid} />); await flush();
    addTransaction(); change('Amount (EUR)', '123.45'); await submit(cashForm()); const savedBody = writes()[0][1].body, signal = writes()[0][1].signal;
    if (kind === 'user') auth.user = { id: 'user-b' };
    if (kind === 'request') auth.request = vi.fn(server.handle);
    if (kind === 'status') auth.status = 'anonymous';
    view.rerender(<PortfolioTracker customerId={cid} portfolioId={kind === 'route' ? undefined : pid} />); await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ ...savedBody, id: 'late', created_at: '2026-09-24T00:00:00Z', sequence: 1 }));
    expect(screen.queryByText('Deposit saved.')).not.toBeInTheDocument();
    if (kind === 'route') expect(screen.getByRole('button', { name: 'Add portfolio' })).toBeInTheDocument();
    else if (kind === 'status') { expect(screen.getByRole('status')).toHaveTextContent('Sign in'); expect(screen.queryByRole('form')).not.toBeInTheDocument(); }
    else { addTransaction(); expect(screen.getByLabelText('Amount (EUR)')).toHaveValue(''); }
  });
});
