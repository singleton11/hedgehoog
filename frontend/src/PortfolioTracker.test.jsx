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
const customerRoute = `#/goal/customers/${cid}`, listRoute = `${customerRoute}/portfolios`, detailRoute = `${listRoute}/${pid}`;
const pagePath = (path, offset = 0, limit = 20) => `${path}?limit=${limit}&offset=${offset}`;
const metadata = (extra = {}) => ({ id: pid, customer_id: cid, name: 'Long term', goal_id: null, currency: 'EUR', created_at: '2026-09-24T00:00:00Z', revision: 0, ...extra });
const position = (extra = {}) => ({ instrument, quantity: '1.25000001', average_price_eur: '100.00000001', cost_basis_eur: '125.00', market_price_eur: '110.00', price_date: '2026-09-23', previous_price_date: '2026-09-22', position_value_eur: '137.50', unrealized_pnl_eur: '12.50', daily_unrealized_pnl_eur: '-1.25', warning: null, ...extra });
const snapshot = (portfolio = metadata(), extra = {}) => ({ portfolio, cash_eur: '1000.00', positions_value_eur: '137.50', equity_eur: '1137.50', realized_pnl_eur: '0.00', unrealized_pnl_eur: '12.50', daily_unrealized_pnl_eur: '-1.25', daily_from_date: '2026-09-22', daily_to_date: '2026-09-23', positions: [position()], warnings: [], ...extra });
const cashEvent = (id) => ({ id: `transaction-${id}`, client_id: crypto.randomUUID(), type: 'deposit', date: '2026-09-23', amount_eur: '100.00', created_at: '2026-09-24T00:00:00Z', sequence: id });
function page(items, query) { const offset = Number(query.get('offset')), limit = Number(query.get('limit')); return { items: items.slice(offset, offset + limit), total: items.length, offset, limit }; }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function mockServer() {
  const server = { portfolios: [metadata()], goals: [goal], instruments: [instrument], transactions: [], details: new Map([[pid, snapshot()]]) };
  server.customer = async () => ({ id: cid, name: 'Alice' });
  server.portfolioList = async (query) => page(server.portfolios, query);
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
  async function dispatch(path, options = {}) {
    const url = new URL(path, 'http://localhost');
    if (url.pathname === `/customers/${cid}`) return server.customer(options);
    if (url.pathname === `/customers/${cid}/goals`) return server.goalList(url.searchParams, options);
    if (url.pathname === '/portfolio-instruments') return server.instrumentList(url.searchParams, options);
    if (url.pathname === base) return options.method === 'POST' ? server.create(options.body, options) : server.portfolioList(url.searchParams, options);
    if (url.pathname.startsWith(`${base}/`) && url.pathname.endsWith('/transactions')) return options.method === 'POST' ? server.save(options.body, options) : server.transactionList(url.searchParams, options);
    if (url.pathname.startsWith(`${base}/`)) return options.method === 'PATCH' ? server.patch(url.pathname.split('/').at(-1), options.body, options) : server.detail(url.pathname.split('/').at(-1), options);
    throw new Error(`Unexpected request: ${path}`);
  }
  server.handle = async (path, options = {}) => structuredClone(await dispatch(path, { ...options, ...(options.body ? { body: structuredClone(options.body) } : {}) }));
  request.mockImplementation(server.handle);
  return server;
}
const writes = () => request.mock.calls.filter(([, options]) => ['POST', 'PATCH'].includes(options?.method));
const tradeForm = () => screen.getByRole('form', { name: 'Trade', exact: true });
const cashForm = () => screen.getByRole('form', { name: 'Cash movement', exact: true });
const settings = () => screen.getByRole('form', { name: 'Portfolio settings' });
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
    await open(customerRoute);
    expect(screen.getByRole('link', { name: 'Portfolios', exact: true })).toHaveAttribute('href', listRoute);
    expect(screen.getByRole('link', { name: /Retirement/ })).toBeInTheDocument();
    await follow('Portfolios');
    expect(within(screen.getByRole('navigation', { name: 'Customer sections' })).getByRole('link', { name: 'Goals' })).toHaveAttribute('href', customerRoute);
    expect(screen.getByRole('link', { name: 'Customers', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText(/No portfolios yet/)).toBeInTheDocument(); expect(writes()).toHaveLength(0);
    change('Portfolio name', '  Emergency fund  '); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    expect(writes()[0]).toEqual([base, expect.objectContaining({ method: 'POST', body: { name: 'Emergency fund', goal_id: null }, signal: expect.any(AbortSignal) })]);
    expect(screen.getByLabelText('Portfolio name')).toHaveValue('');
    change('Portfolio name', 'Retirement investments'); change('Linked goal (optional)', goal.id); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    expect(writes()[1][1].body).toEqual({ name: 'Retirement investments', goal_id: goal.id });
    expect(screen.getByRole('link', { name: /Emergency fund/ })).toBeInTheDocument();
    await follow(/Emergency fund/);
    expect(screen.getByRole('link', { name: 'Alice · Goals' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Emergency fund' })).toBeInTheDocument();
    server.goals.push({ ...goal, id: 'goal-b', name: 'Home' });
    // Returning to the route reloads goal choices, as a goal may be created later.
    await navigate(listRoute); await navigate(`${listRoute}/portfolio-0`);
    change('Portfolio name', 'Safety net'); change('Linked goal (optional)', goal.id); await submit(settings());
    expect(writes().at(-1)[1].body).toEqual({ name: 'Safety net', goal_id: goal.id });
    expect(screen.getByRole('heading', { name: 'Safety net' })).toBeInTheDocument();
    change('Linked goal (optional)', 'goal-b'); await submit(settings());
    expect(writes().at(-1)[1].body.goal_id).toBe('goal-b');
    change('Linked goal (optional)', ''); await submit(settings());
    expect(writes().at(-1)[1].body.goal_id).toBeNull();
    expect(screen.getByText('EUR · No linked goal')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['', `?instrument=${A}`])('switches customer tabs using bookmarkable routes and preserves planning context: %s', async (query) => {
    mockServer(); const view = await open(`${customerRoute}${query}`);
    const tabs = () => within(screen.getByRole('navigation', { name: 'Customer sections' }));
    expect(tabs().getAllByRole('link').map((link) => link.textContent)).toEqual(['Portfolios', 'Goals']);
    expect(tabs().getByRole('link', { name: 'Goals' })).toHaveAttribute('aria-current', 'page');
    expect(tabs().getByRole('link', { name: 'Portfolios' })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('heading', { name: 'Alice', level: 2 })).toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'New goal' })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
    await follow('Portfolios');
    expect(window.location.hash).toBe(`${listRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Portfolios' })).toHaveAttribute('aria-current', 'page');
    expect(tabs().getByRole('link', { name: 'Goals' })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('heading', { name: 'Alice', level: 2 })).toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'New portfolio' })).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'New goal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Retirement/ })).not.toBeInTheDocument();
    // A reload opens the same tab without relying on local component state.
    view.unmount(); await open(`${listRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Portfolios' })).toHaveAttribute('aria-current', 'page');
    await follow('Goals');
    expect(window.location.hash).toBe(`${customerRoute}${query}`);
    expect(tabs().getByRole('link', { name: 'Goals' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: /Retirement/ })).toHaveAttribute('href', `${customerRoute}/goals/${goal.id}${query}`);
    expect(screen.queryByRole('form', { name: 'New portfolio' })).not.toBeInTheDocument();
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
    expect(screen.getByRole('form', { name: 'New goal' })).toBeInTheDocument();
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
    expect(screen.getByRole('link', { name: 'Alice · Goals' })).toBeInTheDocument(); expect(cashForm()).toBeInTheDocument();
  });

  it('does not implicitly detach a linked goal when goals are unavailable, and exposes retry and save errors', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata({ goal_id: goal.id })));
    server.goalList = async () => { throw new Error('Goals temporarily unavailable.'); };
    server.patch = async () => { throw new Error('Could not save settings.'); };
    await open(); expect(screen.getByText('Goals temporarily unavailable.')).toHaveAttribute('role', 'alert');
    change('Portfolio name', 'Rename only'); await submit(settings());
    expect(writes()[0][1].body).toEqual({ name: 'Rename only', goal_id: goal.id });
    expect(screen.getByText('Could not save settings.')).toHaveAttribute('role', 'alert');
    server.goalList = async (query) => page(server.goals, query);
    fireEvent.click(screen.getByRole('button', { name: 'Retry goals' })); await flush();
    expect(screen.getByRole('option', { name: 'Retirement' })).toBeInTheDocument();
  });
});

describe('exact manually dated cash and trade transactions', () => {
  it('defaults both dates to today UTC and submits exact deposit, withdrawal, buy and sell strings without ledger calculation', async () => {
    const server = mockServer(); await open();
    expect(screen.getByLabelText('Trade date (UTC)')).toHaveValue('2026-09-24');
    expect(screen.getByLabelText('Cash date (UTC)')).toHaveValue('2026-09-24'); expect(writes()).toHaveLength(0);
    change('Amount (EUR)', '9007199254740993.01'); await submit(cashForm());
    expect(writes()[0][1].body).toEqual({ client_id: expect.any(String), date: '2026-09-24', type: 'deposit', amount_eur: '9007199254740993.01', currency: 'EUR' });
    change('Cash movement type', 'withdrawal'); change('Cash date (UTC)', '2020-02-29'); change('Amount (EUR)', '000123.4'); await submit(cashForm());
    expect(writes()[1][1].body).toEqual({ client_id: expect.any(String), date: '2020-02-29', type: 'withdrawal', amount_eur: '123.40', currency: 'EUR' });
    fillTrade({ Quantity: '9007199254740993.12345678', 'Trade date (UTC)': '2020-01-02', 'Commission (EUR, optional)': '1.25' }); await submit(tradeForm());
    expect(writes()[2][1].body).toEqual({ client_id: expect.any(String), date: '2020-01-02', type: 'buy', instrument_id: A, quantity: '9007199254740993.12345678', price_eur: '123.12345678', commission_eur: '1.25' });
    change('Trade type', 'sell'); fillTrade({ Quantity: '0.00000001', 'Average execution price (EUR)': '1.00000001' }); await submit(tradeForm());
    expect(writes()[3][1].body).toEqual({ client_id: expect.any(String), date: '2026-09-24', type: 'sell', instrument_id: A, quantity: '0.00000001', price_eur: '1.00000001', commission_eur: '0.00' });
    expect(new Set(writes().map(([, options]) => options.body.client_id)).size).toBe(4);
    expect(server.transactions).toHaveLength(4); expect(screen.getByText('Sell saved.')).toBeInTheDocument();
    expect(summaryValue('Cash')).toHaveTextContent('€1,000.00'); // server fixture, never inferred from submitted cash
    expect(within(screen.getByRole('table', { name: /Transactions ·/ })).getAllByRole('row')).toHaveLength(5);
    expect(request.mock.calls.some(([path]) => path.includes('/instruments/acquired'))).toBe(false);
  });

  it('discloses the immutable ledger before saving and links empty EUR choices to acquisition and currency settings', async () => {
    const server = mockServer(); server.instruments = []; await open();
    expect(screen.getByRole('link', { name: 'Add an ETF' })).toHaveAttribute('href', '#/add');
    expect(screen.getByRole('link', { name: 'your ETFs' })).toHaveAttribute('href', '#/');
    expect(screen.getByText(/set its price currency to EUR/)).toBeInTheDocument();
    for (const form of [cashForm(), tradeForm()]) {
      const notice = within(form).getByText(/Transactions are append-only/);
      expect(notice).toHaveTextContent('saved entries cannot be edited or deleted');
      expect(notice).toHaveTextContent('Dividends, splits and other corporate actions are not processed');
      expect(form).toHaveAttribute('aria-describedby', notice.id);
      expect(notice.compareDocumentPosition(within(form).getByRole('button', { name: /Save/ })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(writes()).toHaveLength(0);
  });

  it('loads paginated EUR instruments and permits selling held snapshots when inventory is unavailable', async () => {
    const server = mockServer();
    server.instruments = Array.from({ length: 101 }, (_, i) => ({ ...instrument, instrument_id: `IE${String(i).padStart(10, '0')}`, name: `Fund ${i}` }));
    await open(); expect(screen.getByRole('option', { name: /Fund 100 ·/ })).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith('/portfolio-instruments?limit=100&offset=100', expect.anything());
    await navigate(listRoute);
    server.instrumentList = async () => { throw new Error('Inventory unavailable.'); };
    server.details.set(pid, snapshot(metadata(), { positions: [position({ instrument: held })] })); await navigate(detailRoute);
    expect(screen.getByText('Inventory unavailable.')).toHaveAttribute('role', 'alert');
    expect(screen.queryByRole('option', { name: /Retired ETF/ })).not.toBeInTheDocument();
    change('Trade type', 'sell'); fillTrade({ 'Trade instrument': B, Quantity: '1' }); await submit(tradeForm());
    expect(writes()[0][1].body).toMatchObject({ type: 'sell', instrument_id: B, quantity: '1' });
    expect(screen.getByText('Sell saved.')).toBeInTheDocument();
  });

  it('disables pending submissions and reuses client IDs for failed unchanged payloads, including edit-and-revert', async () => {
    const server = mockServer(), pending = deferred(); server.save = () => pending.promise;
    await open(); fillTrade(); await submit(tradeForm());
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
    await submit(tradeForm()); expect(writes()[4][1].body).toEqual(first); expect(screen.getByLabelText('Quantity')).toHaveValue('');
    fillTrade(); await submit(tradeForm()); expect(writes()[5][1].body.client_id).not.toBe(first.client_id);
  });

  it('validates a missing instrument, cash date and portfolio name before submitting', async () => {
    mockServer(); await open();
    await submit(tradeForm()); expect(screen.getByRole('alert')).toHaveTextContent('Choose an available EUR instrument');
    change('Cash date (UTC)', ''); change('Amount (EUR)', '10'); await submit(cashForm());
    expect(within(cashForm()).getByRole('alert')).toHaveTextContent('valid transaction date');
    change('Portfolio name', '   '); await submit(settings());
    expect(within(settings()).getByRole('alert')).toHaveTextContent('Enter a portfolio name'); expect(writes()).toHaveLength(0);
  });

  it('keeps independent form drafts and pending IDs intact while another transaction refreshes the portfolio', async () => {
    const server = mockServer(), save = server.save;
    server.save = async (body) => { if (body.type === 'buy') throw new Error('Insufficient cash.'); return save(body); };
    await open(); fillTrade({ 'Trade date (UTC)': '2020-02-29', 'Commission (EUR, optional)': '1.00' });
    await submit(tradeForm()); const retryBody = writes()[0][1].body;
    change('Cash date (UTC)', '2020-01-01'); change('Amount (EUR)', '500'); await submit(cashForm());
    expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    expect(screen.getByLabelText('Trade date (UTC)')).toHaveValue('2020-02-29');
    expect(screen.getByLabelText('Commission (EUR, optional)')).toHaveValue('1.00');
    server.save = save; await submit(tradeForm()); expect(writes()[2][1].body).toEqual(retryBody);
  });

  it('retains an uncertain cash client ID even if the response is malformed', async () => {
    const server = mockServer(); server.save = async () => ({ id: 'bad' }); await open();
    change('Amount (EUR)', '10'); await submit(cashForm()); expect(screen.getByRole('alert')).toHaveTextContent('Unexpected response');
    await submit(cashForm()); expect(writes()[1][1].body).toEqual(writes()[0][1].body);
  });

  it.each(['0', '-1', '1.001', '1e3', 'NaN', ''])('validates cash amount %j without a write', async (value) => {
    mockServer(); await open(); change('Amount (EUR)', value); await submit(cashForm());
    expect(screen.getByRole('alert')).toHaveTextContent('positive cash amount'); expect(writes()).toHaveLength(0);
  });
  it.each([
    ['Quantity', '0', 'positive quantity'], ['Quantity', '1.123456789', 'positive quantity'], ['Average execution price (EUR)', '-1', 'positive quantity'],
    ['Average execution price (EUR)', '1e2', 'positive quantity'], ['Commission (EUR, optional)', '-1', 'non-negative commission'], ['Commission (EUR, optional)', '0.001', 'non-negative commission'],
    ['Trade date (UTC)', '2026-09-25', 'valid transaction date'], ['Trade date (UTC)', '', 'valid transaction date'],
  ])('validates %s=%j without a write', async (label, value, message) => {
    mockServer(); await open(); fillTrade({ [label]: value }); await submit(tradeForm());
    expect(screen.getByRole('alert')).toHaveTextContent(message); expect(writes()).toHaveLength(0);
  });

  it('shows backend long-only and cash-funding validation errors without clearing inputs', async () => {
    const server = mockServer(); server.save = async (body) => { throw new Error(body.type === 'buy' ? 'Insufficient cash on transaction date.' : 'Cannot sell more than held.'); };
    await open(); fillTrade(); await submit(tradeForm());
    expect(screen.getByRole('alert')).toHaveTextContent('Insufficient cash on transaction date.');
    expect(screen.getByLabelText('Quantity')).toHaveValue('1.25000001');
    change('Trade type', 'sell'); await submit(tradeForm()); expect(screen.getByRole('alert')).toHaveTextContent('Cannot sell more than held.');
    expect(writes()[1][1].body.client_id).not.toBe(writes()[0][1].body.client_id);
  });
});

describe('valuation, transaction pagination and read failures', () => {
  it('distinguishes unavailable, zero and negative P&L and shows price dates and warnings', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata(), { cash_eur: '0.00', equity_eur: null, positions_value_eur: null,
      realized_pnl_eur: '-12.34', unrealized_pnl_eur: '0.00', daily_unrealized_pnl_eur: null, daily_from_date: null, daily_to_date: null,
      positions: [position({ market_price_eur: null, price_date: null, previous_price_date: null, position_value_eur: null, unrealized_pnl_eur: null, daily_unrealized_pnl_eur: null, warning: 'No market price.' })], warnings: ['Valuation incomplete.'] }));
    await open(); expect(summaryValue('Cash')).toHaveTextContent('€0.00'); expect(summaryValue('Equity')).toHaveTextContent('Unavailable');
    expect(summaryValue('Realized P&L')).toHaveTextContent('-€12.34'); expect(summaryValue('Total unrealized P&L')).toHaveTextContent('€0.00');
    expect(summaryValue('Daily unrealized P&L')).toHaveTextContent('Unavailable');
    expect(summaryValue('Realized P&L')).toHaveClass('return-negative');
    expect(summaryValue('Total unrealized P&L')).not.toHaveClass('return-positive', 'return-negative');
    expect(summaryValue('Daily unrealized P&L')).not.toHaveClass('return-positive', 'return-negative');
    expect(screen.getByText('Valuation incomplete.')).toBeInTheDocument(); expect(screen.getByText('No market price.')).toBeInTheDocument();
    const table = screen.getByRole('table', { name: 'Current holdings · EUR' });
    expect(within(table).getAllByText('Unavailable')).toHaveLength(4); expect(within(table).getByText('1.25000001')).toBeInTheDocument();
    expect(within(table).getByText('€100.00000001')).toBeInTheDocument();
  });

  it('trims only insignificant display decimals and provides scroll cues for both wide tables', async () => {
    const server = mockServer(); server.details.set(pid, snapshot(metadata(), {
      positions: [position({ average_price_eur: '100.20000000', market_price_eur: '9007199254740993.12345678', cost_basis_eur: '123.45670000' })],
    })); server.transactions = [{ ...cashEvent(1), amount_eur: '123.40000000' }];
    await open(); const positions = screen.getByRole('table', { name: 'Current holdings · EUR' });
    expect(within(positions).getByText('€100.20')).toBeInTheDocument();
    expect(within(positions).getByText('€9,007,199,254,740,993.12345678')).toBeInTheDocument();
    expect(within(positions).getByText('€123.4567')).toBeInTheDocument();
    expect(within(screen.getByRole('table', { name: /Transactions ·/ })).getByText('€123.40')).toBeInTheDocument();
    expect(screen.getAllByText('Scroll horizontally to view all columns.')).toHaveLength(2);
    expect(screen.getByRole('region', { name: 'Positions table' })).toHaveAccessibleDescription('Scroll horizontally to view all columns.');
    expect(screen.getByRole('region', { name: 'Transactions table' })).toHaveAccessibleDescription('Scroll horizontally to view all columns.');
    expect(summaryValue('Total unrealized P&L')).toHaveClass('return-positive');
  });

  it('paginates transaction history and retries an explicit read failure without any write', async () => {
    const server = mockServer(); server.transactions = Array.from({ length: 21 }, (_, i) => cashEvent(21 - i));
    const original = server.transactionList; server.transactionList = async () => { throw new Error('History unavailable.'); };
    await open(); expect(screen.getByText('History unavailable.')).toHaveAttribute('role', 'alert');
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
    await open(); expect(screen.getByRole('alert')).toHaveTextContent('could not be loaded completely');
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
    server.create = () => pendingCreate.promise; change('Portfolio name', 'Old create'); await submit(screen.getByRole('form', { name: 'New portfolio' }));
    const createSignal = writes()[0][1].signal;
    server.detail = original; await navigate(detailRoute); expect(createSignal.aborted).toBe(true);
    await act(async () => { pendingDetail.resolve(snapshot(metadata({ name: 'Stale name' }))); pendingCreate.resolve(metadata({ name: 'Old create' })); });
    expect(screen.getByRole('heading', { name: 'Long term' })).toBeInTheDocument(); expect(screen.queryByText('Stale name')).not.toBeInTheDocument();
    expect(window.location.hash).toBe(detailRoute);
    await navigate(listRoute); expect(screen.getByLabelText('Portfolio name')).toHaveValue(''); expect(screen.queryByText('Portfolio created.')).not.toBeInTheDocument();
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
    change('Amount (EUR)', '123.45'); await submit(cashForm()); const savedBody = writes()[0][1].body, signal = writes()[0][1].signal;
    if (kind === 'user') auth.user = { id: 'user-b' };
    if (kind === 'request') auth.request = vi.fn(server.handle);
    if (kind === 'status') auth.status = 'anonymous';
    view.rerender(<PortfolioTracker customerId={cid} portfolioId={kind === 'route' ? undefined : pid} />); await flush();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ ...savedBody, id: 'late', created_at: '2026-09-24T00:00:00Z', sequence: 1 }));
    expect(screen.queryByText('Deposit saved.')).not.toBeInTheDocument();
    if (kind === 'route') expect(screen.getByRole('form', { name: 'New portfolio' })).toBeInTheDocument();
    else if (kind === 'status') { expect(screen.getByRole('status')).toHaveTextContent('Sign in'); expect(screen.queryByRole('form')).not.toBeInTheDocument(); }
    else expect(screen.getByLabelText('Amount (EUR)')).toHaveValue('');
  });
});
