import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from './App.jsx';

const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const record = (id = 'idx-one', extra = {}) => ({ instrument_id: id, name: 'S&P 500', symbol: '^GSPC', currency: null, return_variant: 'unknown', verification_status: 'unverified', summary: { quote_count: 0, first_date: null, last_date: null }, acquisition: null, ...extra });
const page = (items, offset = 0, total = items.length) => ({ items, total, limit: 20, offset });
const deferred = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };
async function flush() { await act(async () => {}); }
async function advance(ms) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function navigate(hash) { await act(async () => { window.history.replaceState(null, '', hash); window.dispatchEvent(new Event('hashchange')); }); }

function server() {
  const state = { items: [record()], list: null, add: null, download: null };
  state.fetch = vi.fn(async (url, options = {}) => {
    if (url === '/api/auth/me') return response(200, { id: 'u', login: 'alice', role: 'user' });
    if (url === '/api/instruments/budget') return response(200, { provider: 'yfinance', metered: false });
    if (url.startsWith('/api/instruments/acquired?')) return response(200, page([]));
    if (url.startsWith('/api/indices/search?')) return state.search ? state.search(url, options) : response(200, { items: [] });
    if (url.startsWith('/api/indices?')) {
      if (state.list) return state.list(url, options);
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
      return response(200, page(state.items.slice(offset, offset + 20), offset, state.items.length));
    }
    if (url === '/api/indices' && options.method === 'POST') {
      if (state.add) return state.add(url, options);
      const item = record('idx-new', JSON.parse(options.body)); state.items.push(item);
      return response(201, item);
    }
    if (url.startsWith('/api/indices/') && options.method === 'DELETE') {
      if (state.remove) return state.remove(url, options);
      state.items = state.items.filter((item) => url !== `/api/indices/${item.instrument_id}`);
      return response(200, { removed: true });
    }
    if (url.endsWith('/acquisitions') && options.method === 'POST') {
      if (state.download) return state.download(url, options);
      state.items[0] = { ...state.items[0], acquisition: { id: 'job', status: 'queued', phase: 'queued' } };
      return response(202, { job: state.items[0].acquisition });
    }
    throw new Error(`Unexpected request ${url}`);
  });
  vi.stubGlobal('fetch', state.fetch);
  state.posts = () => state.fetch.mock.calls.filter(([, options]) => options.method === 'POST');
  state.reads = () => state.fetch.mock.calls.filter(([url]) => url.startsWith('/api/indices?'));
  return state;
}
async function open() { window.history.replaceState(null, '', '#/indices'); const view = render(<App />); await flush(); return view; }
async function confirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Download market data for S&P 500' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Allow this request to fetch Yahoo Finance data.' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm download' }));
  await flush();
}
beforeEach(() => { vi.useFakeTimers(); window.history.replaceState(null, '', '#/'); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('confirms catalogue removal, supports cancel and refreshes the list', async () => {
  const s = server(); await open();
  fireEvent.click(screen.getByRole('button', { name: 'Remove S&P 500 from catalogue' }));
  expect(screen.getByText(/Downloaded data is preserved/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(s.fetch.mock.calls.filter(([, o]) => o.method === 'DELETE')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: 'Remove S&P 500 from catalogue' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' })); await flush();
  expect(s.fetch.mock.calls.filter(([, o]) => o.method === 'DELETE')).toHaveLength(1);
  expect(screen.getByRole('heading', { name: 'Indices (0)' })).toBeInTheDocument();
  expect(screen.getByText(/Index removed from the catalogue/)).toBeInTheDocument();
});

it('returns to the preceding page after removing its last index', async () => {
  const s = server(); s.items = Array.from({ length: 21 }, (_, i) => record(`idx-${i}`, { name: `Index ${i}` }));
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Next indices' })); await flush();
  fireEvent.click(screen.getByRole('button', { name: 'Remove Index 20 from catalogue' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' })); await flush();
  expect(screen.getByText('Index 0')).toBeInTheDocument();
  expect(screen.queryByText('Index 20')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Previous indices' })).toBeDisabled();
});

it('keeps removal errors retryable and blocks duplicate submissions', async () => {
  const s = server(); const pending = deferred(); s.remove = () => pending.promise;
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Remove S&P 500 from catalogue' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' })); await flush();
  expect(screen.getByRole('button', { name: 'Confirm removal' })).toBeDisabled();
  pending.resolve(response(503, { detail: 'Removal unavailable' })); await flush();
  expect(screen.getByText(/Removal unavailable/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Confirm removal' })).toBeEnabled();
  expect(s.fetch.mock.calls.filter(([, o]) => o.method === 'DELETE')).toHaveLength(1);
});

it('shows a separate catalogue without downloading on direct load or navigation', async () => {
  const s = server(); await open();
  expect(screen.getByRole('heading', { name: 'Indices (1)' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Indices' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: 'ETFs' })).not.toHaveAttribute('aria-current');
  expect(screen.getByText('Unknown currency')).toBeInTheDocument();
  expect(screen.getByText('No saved data')).toBeInTheDocument();
  expect(s.posts()).toHaveLength(0);
  await navigate('#/');
  expect(screen.getByRole('heading', { name: 'Acquired ETFs (0)' })).toBeInTheDocument();
  await navigate('#/indices');
  expect(s.reads()).toHaveLength(2);
  expect(s.posts()).toHaveLength(0);
});

it('adds catalogue metadata without acquisition, with unknown defaults', async () => {
  const s = server(); await open();
  fireEvent.click(screen.getByRole('button', { name: 'Add index' }));
  fireEvent.click(screen.getByRole('button', { name: 'Manual entry (advanced)' }));
  fireEvent.change(screen.getByLabelText('Index name'), { target: { value: 'My index' } });
  fireEvent.change(screen.getByLabelText('Yahoo symbol'), { target: { value: '^test' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save index' })); await flush();
  expect(s.posts()).toHaveLength(1);
  expect(s.posts()[0][0]).toBe('/api/indices');
  expect(JSON.parse(s.posts()[0][1].body)).toEqual({ name: 'My index', symbol: '^TEST', currency: null, return_variant: 'unknown' });
  expect(screen.getByText('My index')).toBeInTheDocument();
  expect(screen.getByText('Index saved. No market data was downloaded.')).toBeInTheDocument();
});

it('debounces index search and saves a keyboard-selected result without downloading', async () => {
  const s = server();
  s.search = () => response(200, { items: [{ name: 'MSCI World Net', symbol: '^WORLD', currency: null, return_variant: 'unknown' }] });
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Add index' }));
  const input = screen.getByRole('combobox', { name: 'Search indices' });
  fireEvent.change(input, { target: { value: 'M' } }); await advance(400);
  expect(s.fetch.mock.calls.filter(([url]) => url.includes('/search?'))).toHaveLength(0);
  fireEvent.change(input, { target: { value: 'MSCI World net' } }); await advance(349);
  expect(s.fetch.mock.calls.filter(([url]) => url.includes('/search?'))).toHaveLength(0);
  await advance(1);
  expect(screen.getByRole('option')).toHaveTextContent('MSCI World Net · ^WORLD · Unknown currency · Unknown return variant');
  expect(screen.getByRole('button', { name: 'Save index' })).toBeDisabled();
  fireEvent.keyDown(input, { key: 'ArrowDown' }); fireEvent.keyDown(input, { key: 'Enter' });
  expect(s.posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: 'Save index' })); await flush();
  expect(s.posts()).toHaveLength(1);
  expect(JSON.parse(s.posts()[0][1].body)).toEqual({ name: 'MSCI World Net', symbol: '^WORLD', currency: null, return_variant: 'unknown' });
});

it('ignores stale search responses and clears selection when editing', async () => {
  const s = server(); const old = deferred();
  s.search = (url) => url.includes('Old') ? old.promise : response(200, { items: [{ name: 'New index', symbol: '^NEW' }] });
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Add index' }));
  const input = screen.getByRole('combobox');
  fireEvent.change(input, { target: { value: 'Old' } }); await advance(350);
  const oldCall = s.fetch.mock.calls.find(([url]) => url.includes('q=Old'));
  fireEvent.change(input, { target: { value: 'New' } }); await advance(350);
  expect(oldCall[1].signal.aborted).toBe(true);
  old.resolve(response(200, { items: [{ name: 'Old index', symbol: '^OLD' }] })); await flush();
  expect(screen.getByRole('option')).toHaveTextContent('New index');
  fireEvent.click(screen.getByRole('option'));
  expect(screen.getByRole('button', { name: 'Save index' })).toBeEnabled();
  fireEvent.change(input, { target: { value: 'x' } });
  expect(screen.getByRole('button', { name: 'Save index' })).toBeDisabled();
  expect(s.posts()).toHaveLength(0);
});

it('handles empty search, provider errors, retry and cancellation', async () => {
  const s = server(); await open(); fireEvent.click(screen.getByRole('button', { name: 'Add index' }));
  const input = screen.getByRole('combobox');
  fireEvent.change(input, { target: { value: 'World' } }); await advance(350);
  expect(screen.getByText(/No matching indices found/)).toBeInTheDocument();
  s.search = () => response(503, { detail: 'Search unavailable' });
  fireEvent.change(input, { target: { value: 'World net' } }); await advance(350);
  expect(screen.getByText('Search unavailable')).toBeInTheDocument();
  const pending = deferred(); s.search = () => pending.promise;
  fireEvent.click(screen.getByRole('button', { name: 'Retry search' })); await advance(350);
  const call = s.fetch.mock.calls.filter(([url]) => url.includes('/search?')).at(-1);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(call[1].signal.aborted).toBe(true);
  pending.resolve(response(200, { items: [{ name: 'Late', symbol: '^LATE' }] })); await flush();
  expect(screen.queryByRole('option')).not.toBeInTheDocument();
  expect(s.posts()).toHaveLength(0);
});

it('requires confirmation, polls saved status and displays downloaded coverage', async () => {
  const s = server(); await open();
  fireEvent.click(screen.getByRole('button', { name: 'Download market data for S&P 500' }));
  expect(screen.getByRole('button', { name: 'Confirm download' })).toBeDisabled();
  expect(s.posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await confirm();
  expect(s.posts()).toHaveLength(1);
  expect(s.posts()[0][0]).toBe('/api/indices/idx-one/acquisitions');
  expect(JSON.parse(s.posts()[0][1].body)).toEqual({ confirmed: true });
  expect(screen.getByRole('button', { name: 'Download market data for S&P 500' })).toBeDisabled();
  s.items[0] = { ...s.items[0], acquisition: { id: 'job', status: 'succeeded' }, verification_status: 'verified', summary: { quote_count: 100, first_date: '2020-01-01', last_date: '2020-05-20' } };
  await advance(2000);
  expect(screen.getByText('100 quotes')).toBeInTheDocument();
  expect(screen.getByText('Verified index')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Download market data for S&P 500' })).toHaveTextContent('Refresh data');
  const count = s.reads().length; await advance(6000);
  expect(s.reads()).toHaveLength(count); expect(s.posts()).toHaveLength(1);
});

it('preserves saved coverage after failed refresh and retries a polling error without reposting', async () => {
  const s = server();
  s.items[0].summary = { quote_count: 42, first_date: '2020-01-01', last_date: '2020-03-01' };
  await open(); await confirm();
  s.list = () => response(503, { detail: 'Temporarily unavailable' });
  await advance(2000);
  expect(screen.getByText('Temporarily unavailable')).toBeInTheDocument();
  expect(screen.getByText('42 quotes')).toBeInTheDocument();
  s.list = null; s.items[0].acquisition = { status: 'failed', error: 'Provider unavailable' };
  fireEvent.click(screen.getByRole('button', { name: 'Retry list' })); await flush();
  expect(screen.getByText('Provider unavailable')).toBeInTheDocument();
  expect(screen.getByText('42 quotes')).toBeInTheDocument();
  expect(s.posts()).toHaveLength(1);
});

it('paginates and rejects late list results after leaving and returning', async () => {
  const s = server(); s.items = Array.from({ length: 21 }, (_, i) => record(`idx-${i}`, { name: `Index ${i}` }));
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Next indices' })); await flush();
  expect(screen.getByText('Index 20')).toBeInTheDocument(); expect(screen.queryByText('Index 0')).not.toBeInTheDocument();
  const old = deferred(); s.list = () => old.promise;
  fireEvent.click(screen.getByRole('button', { name: 'Previous indices' })); await flush();
  const oldRead = s.reads().at(-1);
  await navigate('#/'); expect(oldRead[1].signal.aborted).toBe(true);
  s.list = null; s.items = [record('idx-fresh', { name: 'Fresh index' })];
  await navigate('#/indices');
  old.resolve(response(200, page([record('idx-stale', { name: 'Stale index' })]))); await flush();
  expect(screen.getByText('Fresh index')).toBeInTheDocument(); expect(screen.queryByText('Stale index')).not.toBeInTheDocument();
  expect(s.posts()).toHaveLength(0);
});

it('aborts an in-flight POST on unmount and does not poll from its late response', async () => {
  const s = server(); const old = deferred(); s.download = () => old.promise;
  await open(); await confirm();
  const post = s.posts()[0]; await navigate('#/'); expect(post[1].signal.aborted).toBe(true);
  const count = s.reads().length;
  old.resolve(response(202, { job: { status: 'queued' } })); await flush(); await advance(8000);
  expect(s.reads()).toHaveLength(count);
  expect(screen.queryByText('Download requested. Status will update automatically.')).not.toBeInTheDocument();
});

it('does not allow an aborted pre-save read to replace the updated catalogue', async () => {
  const s = server(); s.items[0].acquisition = { status: 'queued' }; await open();
  const old = deferred(); s.list = () => old.promise; await advance(2000);
  // The row is pending, so use add metadata to exercise cancellation of that read.
  fireEvent.click(screen.getByRole('button', { name: 'Add index' }));
  fireEvent.click(screen.getByRole('button', { name: 'Manual entry (advanced)' }));
  fireEvent.change(screen.getByLabelText('Index name'), { target: { value: 'New index' } });
  fireEvent.change(screen.getByLabelText('Yahoo symbol'), { target: { value: '^NEW' } });
  s.list = null; fireEvent.click(screen.getByRole('button', { name: 'Save index' })); await flush();
  old.resolve(response(200, page([record('idx-stale', { name: 'Stale index' })]))); await flush();
  expect(screen.getByText('New index')).toBeInTheDocument(); expect(screen.queryByText('Stale index')).not.toBeInTheDocument();
});

it('stops pending-job polling after unmount', async () => {
  const s = server(); s.items[0].acquisition = { status: 'running', phase: 'quotes' };
  const view = await open(); const count = s.reads().length;
  view.unmount(); await advance(10000);
  expect(s.reads()).toHaveLength(count); expect(s.posts()).toHaveLength(0);
});

it('rejects malformed catalogue responses and allows a read-only retry', async () => {
  const s = server(); s.list = () => response(200, { items: [], total: -1, offset: 0, limit: 20 });
  await open(); expect(screen.getByText(/unexpected index list/)).toBeInTheDocument();
  s.list = null; fireEvent.click(screen.getByRole('button', { name: 'Retry list' })); await flush();
  expect(screen.getByText('S&P 500')).toBeInTheDocument(); expect(s.posts()).toHaveLength(0);
});

it('keeps manual metadata after a duplicate error and never requests market data', async () => {
  const s = server(); s.add = () => response(409, { detail: 'This Yahoo symbol is already in the catalogue' });
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Add index' }));
  fireEvent.click(screen.getByRole('button', { name: 'Manual entry (advanced)' }));
  fireEvent.change(screen.getByLabelText('Index name'), { target: { value: 'Duplicate' } });
  fireEvent.change(screen.getByLabelText('Yahoo symbol'), { target: { value: '^GSPC' } });
  fireEvent.change(screen.getByLabelText('Currency (optional)'), { target: { value: 'usd' } });
  fireEvent.change(screen.getByLabelText('Return variant'), { target: { value: 'price' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save index' })); await flush();
  expect(screen.getByText(/already in the catalogue/)).toBeInTheDocument();
  expect(screen.getByLabelText('Index name')).toHaveValue('Duplicate');
  expect(JSON.parse(s.posts()[0][1].body)).toEqual({ name: 'Duplicate', symbol: '^GSPC', currency: 'USD', return_variant: 'price' });
  expect(s.posts()).toHaveLength(1);
});

it('reconciles an uncertain POST error through reads, without automatically resubmitting', async () => {
  const s = server(); s.download = () => {
    s.items[0].acquisition = { status: 'queued' };
    return response(503, { detail: 'Connection interrupted' });
  };
  await open(); await confirm();
  expect(screen.getByText(/Connection interrupted/)).toBeInTheDocument();
  expect(within(screen.getByRole('region', { name: 'Index market data' })).getByText('queued')).toBeInTheDocument();
  await advance(4000); expect(s.posts()).toHaveLength(1);
});
