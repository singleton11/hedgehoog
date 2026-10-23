import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ManualSplits from './ManualSplits.jsx';
import InstrumentDetail from './InstrumentDetail.jsx';

const { request, useInstrument, statistics } = vi.hoisted(() => ({ request: vi.fn(), useInstrument: vi.fn(), statistics: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ request }) }));
vi.mock('./useInstrument.js', () => ({ default: useInstrument }));
vi.mock('./ReturnStatistics.jsx', () => ({ default: (props) => { statistics(props); return <section>Statistics</section>; } }));

const ISIN = 'IE00B5BMR087';
const listing = { symbol: 'SXR8.DE', exchange: 'XETR' };
const path = `/instruments/${ISIN}/splits`;
const saved = (extra = {}) => ({ id: 'split-1', date: '2020-06-15', factor: '25', note: 'Issuer notice', created_at: '2026-09-17T12:00:00Z', ...extra });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
let server;

beforeEach(() => {
  request.mockReset(); statistics.mockReset();
  server = { instrument_id: ISIN, listing, manual: [], provider: [], error: null };
  request.mockImplementation(async (url, options = {}) => {
    if (options.method === 'POST') {
      const entry = saved(options.body);
      server.manual = [entry];
      return { item: entry };
    }
    if (options.method === 'DELETE') { server.manual = []; return null; }
    return structuredClone(server);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const props = (extra = {}) => ({ isin: ISIN, listing, hasQuotes: true, inception: '2010-05-19', onChanged: vi.fn(), ...extra });
async function open(extra = {}) {
  const input = props(extra);
  const view = render(<ManualSplits {...input} />);
  fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
  await screen.findByRole('button', { name: 'Add manual split' });
  return { ...view, input };
}
async function form(factor = '25', day = '2020-06-15') {
  fireEvent.click(screen.getByRole('button', { name: 'Add manual split' }));
  fireEvent.change(screen.getByLabelText('Effective date (ex-date)'), { target: { value: day } });
  fireEvent.change(screen.getByLabelText('Split factor (new shares per old share)'), { target: { value: factor } });
  fireEvent.change(screen.getByLabelText('Source or note (optional)'), { target: { value: '  Issuer notice  ' } });
}
const writes = () => request.mock.calls.filter(([, options]) => options.method);

describe('manual split controls', () => {
  it('loads saved splits only when opened, with the current listing and scope explained', async () => {
    render(<ManualSplits {...props()} />);
    expect(request).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await screen.findByText('No manual splits saved.');
    expect(request).toHaveBeenCalledWith(path, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.getByText(/Manual entries are shared by all users/)).toHaveTextContent('survive provider updates');
    expect(screen.getByText(/not raw candles/)).toHaveTextContent('portfolio share balances or saved reports');
    expect(writes()).toHaveLength(0);
  });

  it.each(['25', '0.5', '1.0000000000000000001', '0.9999999999999999999', '0.000000001', '1000000000'])('saves the exact factor %s and refreshes statistics without an acquisition', async (factor) => {
    const { input } = await open();
    await form(factor);
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    await screen.findByText('Manual split saved.');
    expect(writes()).toHaveLength(1);
    expect(writes()[0]).toEqual([path, expect.objectContaining({ method: 'POST', body: {
      id: expect.stringMatching(/^[0-9a-f-]{36}$/), ...listing, date: '2020-06-15', factor, note: 'Issuer notice',
    } })]);
    expect(input.onChanged).toHaveBeenCalledOnce();
    expect(await screen.findByRole('button', { name: 'Remove split on 2020-06-15' })).toBeEnabled();
  });

  it('shows an error and sends no split when secure randomness fails', async () => {
    vi.stubGlobal('crypto', { getRandomValues: () => { throw new Error('unavailable'); } });
    await open(); await form();
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Secure split IDs are unavailable.');
    expect(writes()).toHaveLength(0);
    expect(screen.getByLabelText('Split factor (new shares per old share)')).toHaveValue('25');
  });

  it.each(['0', '1', '1.000', '-2', 'NaN', 'Infinity', '1e2', '1,5', '0.0000000009', '1000000000.0000000001'])('rejects invalid or out-of-bounds factor %s', async (factor) => {
    await open();
    await form(factor);
    expect(screen.getByRole('button', { name: 'Save split' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Add manual split' }));
    expect(writes()).toHaveLength(0);
  });

  it.each(['2009-01-01', '2999-01-01', ''])('rejects out-of-range effective date %s', async (day) => {
    await open(); await form('25', day);
    expect(screen.getByRole('button', { name: 'Save split' })).toBeDisabled();
    expect(writes()).toHaveLength(0);
  });

  it('shows exact manual entries close to one and retains their removal controls', async () => {
    server.manual = [saved({ factor: '1.0000000000000000001' })];
    await open();
    expect(screen.getByText(/factor 1\.0000000000000000001/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove split on 2020-06-15' })).toBeEnabled();
  });

  it('requires explicit removal confirmation and refreshes the list and statistics', async () => {
    server.manual = [saved()];
    server.provider = [{ date: '2020-06-15', factor: '25' }];
    const { input } = await open();
    fireEvent.click(screen.getByRole('button', { name: 'Remove split on 2020-06-15' }));
    expect(writes()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel removal' }));
    expect(writes()).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Remove split on 2020-06-15' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' }));
    await screen.findByText('Manual split removed.');
    expect(writes()[0]).toEqual([`${path}/split-1`, expect.objectContaining({ method: 'DELETE' })]);
    await screen.findByText('No manual splits saved.');
    expect(screen.getByText('2020-06-15 · factor 25')).toBeInTheDocument();
    expect(input.onChanged).toHaveBeenCalledOnce();
  });

  it.each([false, true])('reuses the request ID after an uncertain failure (randomUUID unavailable: %s)', async (unavailable) => {
    if (unavailable) vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const original = request.getMockImplementation();
    let first = true;
    request.mockImplementation((url, options = {}) => {
      if (options.method === 'POST' && first) { first = false; return Promise.reject(new Error('Connection lost')); }
      return original(url, options);
    });
    const { input } = await open(); await form();
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save split' })).toBeEnabled());
    expect(screen.getByLabelText('Split factor (new shares per old share)')).toHaveValue('25');
    expect(writes()).toHaveLength(1);  // Never auto-retry.
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    await screen.findByText('Manual split saved.');
    expect(writes()[1][1].body).toEqual(writes()[0][1].body);
    expect(input.onChanged).toHaveBeenCalledTimes(2);
  });

  it('preserves reconciliation when the disclosure is collapsed during a write', async () => {
    const pending = deferred();
    const original = request.getMockImplementation();
    request.mockImplementation((url, options = {}) => options.method === 'POST' ? pending.promise : original(url, options));
    const { input } = await open(); await form();
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    const body = writes()[0][1].body;
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled();
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await act(async () => { server.manual = [saved(body)]; pending.resolve({ item: saved(body) }); });
    expect(writes()[0][1].signal.aborted).toBe(false);
    expect(input.onChanged).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await screen.findByRole('button', { name: 'Remove split on 2020-06-15' });
  });

  it('aborts writes on route changes and ignores their late response', async () => {
    const pending = deferred();
    const original = request.getMockImplementation();
    request.mockImplementation((url, options = {}) => options.method === 'POST' ? pending.promise : original(url, options));
    const { input, rerender } = await open(); await form();
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    const body = writes()[0][1].body;
    server = { ...server, instrument_id: 'IE00B4L5Y983', manual: [] };
    rerender(<ManualSplits {...input} isin={server.instrument_id} />);
    await screen.findByText('No manual splits saved.');
    expect(writes()[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve({ item: saved(body) }));
    expect(input.onChanged).not.toHaveBeenCalled();
    expect(screen.queryByText('Manual split saved.')).not.toBeInTheDocument();
  });

  it('ignores a late history response after a newer refresh completes', async () => {
    const pending = deferred();
    const staleHistory = { ...structuredClone(server), manual: [saved()] };
    request.mockImplementationOnce(() => pending.promise);
    const input = props();
    const { rerender } = render(<ManualSplits {...input} />);
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await screen.findByText('Loading split history…');
    const signal = request.mock.calls[0][1].signal;

    rerender(<ManualSplits {...input} refreshKey="newer-history" />);
    await screen.findByText('No manual splits saved.');
    expect(signal.aborted).toBe(true);

    await act(async () => pending.resolve(staleHistory));
    expect(screen.getByText('No manual splits saved.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove split on 2020-06-15' })).not.toBeInTheDocument();
    expect(screen.queryByText('Loading split history…')).not.toBeInTheDocument();
  });

  it('blocks submission during a history refresh and reconciles the subsequent write', async () => {
    const pending = deferred();
    const { input, rerender } = await open(); await form();
    request.mockImplementationOnce(() => pending.promise);
    rerender(<ManualSplits {...input} refreshKey="updated-acquisition" />);
    // Programmatic submit is blocked while a read is loading.
    await screen.findByText('Loading split history…');
    fireEvent.submit(screen.getByRole('form', { name: 'Add manual split' }));
    expect(writes()).toHaveLength(0);
    await act(async () => pending.resolve(structuredClone(server)));
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    await screen.findByText('Manual split saved.');
    expect(await screen.findByRole('button', { name: 'Remove split on 2020-06-15' })).toBeInTheDocument();
  });

  it('recovers from a failed or malformed history response without permitting blind writes', async () => {
    request.mockResolvedValueOnce({ ...server, instrument_id: 'WRONG' });
    render(<ManualSplits {...props()} />);
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await screen.findByText(/unexpected response/);
    expect(screen.queryByRole('button', { name: 'Add manual split' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry splits' }));
    expect(await screen.findByRole('button', { name: 'Add manual split' })).toBeEnabled();
  });

  it('shows provider conflicts without hiding the manual removal controls', async () => {
    server.manual = [saved()];
    server.error = 'Manual and provider split factors conflict on 2020-06-15.';
    await open();
    expect(screen.getByRole('alert')).toHaveTextContent('conflict');
    expect(screen.getByRole('button', { name: 'Remove split on 2020-06-15' })).toBeEnabled();
  });

  it('does not offer a write without saved quotes, or while acquisition is pending', async () => {
    const input = props({ hasQuotes: false });
    const { rerender } = render(<ManualSplits {...input} />);
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await screen.findByText('Save quotes for this ETF before adding manual splits.');
    expect(request).not.toHaveBeenCalled();
    rerender(<ManualSplits {...input} hasQuotes disabled />);
    expect(await screen.findByRole('button', { name: 'Add manual split' })).toBeDisabled();
  });

  it('invalidates the instrument statistics refresh key after a manual save', async () => {
    useInstrument.mockReturnValue({ detail: {
      instrument_id: ISIN, catalogue: { name: 'ETF', inception_date: '2010-05-19' }, listing,
      summary: { quote_count: 1 }, provider_configured: false, acquisition: null,
    }, quotes: [], quotesLoading: false });
    render(<InstrumentDetail isin={ISIN} />);
    const before = statistics.mock.lastCall[0].refreshKey;
    fireEvent.click(screen.getByText('Splits', { selector: 'summary' }));
    await screen.findByRole('button', { name: 'Add manual split' });
    await form();
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }));
    await screen.findByText('Manual split saved.');
    expect(statistics.mock.lastCall[0].refreshKey).not.toBe(before);
  });
});
