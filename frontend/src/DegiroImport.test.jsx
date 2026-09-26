import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DegiroImport from './DegiroImport.jsx';
import userEvent from '@testing-library/user-event';

const path = '/customers/c/portfolios/p';
const csv = 'Date,Description\r\n01-01-2026,Storting €100\r\n';
const preview = (extra = {}) => ({ can_import: true, preview_token: 'exact-token',
  transactions: [{ date: '2026-01-01', type: 'deposit', amount_eur: '100.00', source_rows: [2], status: 'duplicate' },
    { date: '2026-01-02', type: 'buy', instrument_id: 'IE00B4L5Y983', instrument: { name: 'World', symbol: 'WORLD', exchange: 'XETR', currency: 'EUR' }, quantity: '0.12345678', price_eur: '10.12345678', commission_eur: '1.00', source_rows: [3, 4], status: 'new' }],
  ignored_rows: [{ row: 5, description: 'Reserved iDEAL', reason: 'Not final deposit' }], errors: [], warnings: ['Check acquired listing'],
  summary: { new_count: 1, duplicate_count: 1, cash_eur: '97.75', deposits_eur: '100.00', withdrawals_eur: '0.00', commission_eur: '1.00', positions: [{ instrument_id: 'IE00B4L5Y983', quantity: '0.12345678', cost_basis_eur: '2.25' }] }, ...extra });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let readers;
class Reader {
  constructor() { readers.push(this); }
  readAsText(file) { this.file = file; }
  abort() { this.aborted = true; }
  finish(result = csv) { this.result = result; this.onload(); }
  fail() { this.onerror(); }
}
beforeEach(() => { readers = []; vi.stubGlobal('FileReader', Reader); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const flush = async () => { await act(async () => {}); };
function setup(request = vi.fn().mockResolvedValue(preview())) {
  const onImported = vi.fn();
  const view = render(<DegiroImport request={request} portfolioPath={path} onImported={onImported} />);
  fireEvent.click(screen.getByRole('button', { name: 'Import DEGIRO CSV' }));
  return { ...view, request, onImported };
}
function select(name = 'statement.csv', content = csv) {
  fireEvent.change(screen.getByLabelText(/DEGIRO CSV file/), { target: { files: [new File([content], name)] } });
}
function read() { select(); act(() => readers.at(-1).finish()); }
async function showPreview() { read(); fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush(); }
function confirm() { fireEvent.click(screen.getByRole('checkbox')); }
function save() { fireEvent.click(screen.getByRole('button', { name: 'Import transactions' })); }

describe('DEGIRO explicit preview/import', () => {
  it('sends exact CSV only on preview, displays listings/duplicates/ignored lines and saves only after confirmation', async () => {
    const view = setup(); read(); expect(view.request).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush();
    expect(view.request).toHaveBeenCalledWith(`${path}/imports/degiro/preview`, expect.objectContaining({ method: 'POST', body: { csv } }));
    expect(screen.getByText('World · WORLD · XETR · IE00B4L5Y983')).toBeInTheDocument();
    expect(screen.getByText('10.12345678')).toBeInTheDocument();
    expect(screen.getByText(/1 duplicates \(skipped\)/)).toBeInTheDocument();
    expect(screen.getByText('Line 5: Reserved iDEAL — Not final deposit')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import transactions' })).toBeDisabled(); save(); expect(view.request).toHaveBeenCalledTimes(1);
    confirm(); view.request.mockResolvedValueOnce({ imported_count: 1, duplicate_count: 1 }); save(); await flush();
    expect(view.request.mock.calls[1][1].body).toEqual({ csv, preview_token: 'exact-token' });
    expect(view.onImported).toHaveBeenCalledOnce(); expect(screen.getByText(/Imported 1 transactions/)).toBeInTheDocument();
  });
  it('blocks previews with errors and shows statement and source line errors', async () => {
    setup(vi.fn().mockResolvedValue(preview({ can_import: false, errors: [{ row: 7, message: 'Missing EUR listing' }, { row: null, message: 'Insufficient funding' }] })));
    await showPreview(); expect(screen.getByText('Line 7: Missing EUR listing')).toBeInTheDocument();
    expect(screen.getByText('Statement: Insufficient funding')).toBeInTheDocument(); expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Import transactions' })).toBeDisabled();
  });
  it('retries read and preview failures without saving', async () => {
    const view = setup(vi.fn().mockRejectedValueOnce(new Error('Preview unavailable')).mockResolvedValue(preview()));
    select(); act(() => readers[0].fail()); expect(screen.getByRole('alert')).toHaveTextContent('Unable to read');
    expect(screen.getByRole('button', { name: 'Preview statement' })).toBeDisabled();
    await showPreview(); expect(screen.getByRole('alert')).toHaveTextContent('Preview unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush();
    expect(view.request).toHaveBeenCalledTimes(2); expect(view.onImported).not.toHaveBeenCalled();
  });
  it('preserves exact CSV/token on uncertain save and disables cancellation/file changes while saving', async () => {
    const commit = deferred(); const view = setup(vi.fn().mockResolvedValueOnce(preview()).mockReturnValueOnce(commit.promise).mockResolvedValueOnce({ imported_count: 1, duplicate_count: 1 }));
    await showPreview(); confirm(); save();
    expect(screen.getByLabelText(/DEGIRO CSV file/)).toBeDisabled(); expect(screen.getByRole('button', { name: 'Cancel import' })).toBeDisabled();
    await act(async () => commit.reject(new Error('Connection lost')));
    expect(screen.getByText(/The import may have been saved/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview statement' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry import' })); await flush();
    expect(view.request.mock.calls[2][1].body).toEqual(view.request.mock.calls[1][1].body); expect(view.onImported).toHaveBeenCalledOnce();
  });
  it.each([409, 422])('clears a definitively rejected %s commit and requires a new preview and confirmation', async (status) => {
    const failure = Object.assign(new Error(status === 409 ? 'Preview is stale' : 'Import blocked'), { status });
    const view = setup(vi.fn().mockResolvedValueOnce(preview()).mockRejectedValueOnce(failure)
      .mockResolvedValueOnce(preview({ preview_token: 'fresh-token' })).mockResolvedValueOnce({ imported_count: 1, duplicate_count: 1 }));
    await showPreview(); confirm(); save(); await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('Preview the statement again');
    expect(screen.queryByText('Statement preview')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry import' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview statement' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush();
    expect(screen.getByRole('checkbox')).not.toBeChecked(); expect(screen.getByRole('button', { name: 'Import transactions' })).toBeDisabled();
    confirm(); save(); await flush();
    expect(view.request.mock.calls[3][1].body).toEqual({ csv, preview_token: 'fresh-token' });
    expect(view.onImported).toHaveBeenCalledOnce();
  });
  it('clears the native input after success so the same file can be previewed and imported as duplicates', async () => {
    const user = userEvent.setup();
    const view = setup(vi.fn().mockResolvedValueOnce(preview()).mockResolvedValueOnce({ imported_count: 1, duplicate_count: 1 })
      .mockResolvedValueOnce(preview({ summary: { ...preview().summary, new_count: 0, duplicate_count: 2 }, transactions: preview().transactions.map((item) => ({ ...item, status: 'duplicate' })) }))
      .mockResolvedValueOnce({ imported_count: 0, duplicate_count: 2 }));
    const file = new File([csv], 'repeat.csv', { type: 'text/csv' }); const input = screen.getByLabelText(/DEGIRO CSV file/);
    await user.upload(input, file); act(() => readers.at(-1).finish());
    expect(input.value).toContain('repeat.csv');
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush(); confirm(); save(); await flush();
    expect(input.value).toBe(''); expect(screen.getByText(/Imported 1 transactions/)).toBeInTheDocument();
    await user.upload(input, file); expect(readers).toHaveLength(2); act(() => readers.at(-1).finish());
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush();
    expect(screen.getByText(/0 new transactions · 2 duplicates/)).toBeInTheDocument(); confirm(); save(); await flush();
    expect(screen.getByText('Imported 0 transactions; 2 duplicates skipped.')).toBeInTheDocument(); expect(view.onImported).toHaveBeenCalledTimes(2);
  });
  it('exposes a focusable horizontally scrollable table and precise statement guidance', async () => {
    setup(); await showPreview();
    const region = screen.getByRole('region', { name: 'DEGIRO preview transactions' });
    expect(region).toHaveAttribute('tabindex', '0'); expect(region).toHaveClass('planner-table-scroll');
    expect(region).toHaveAccessibleDescription('Scroll horizontally to view all columns.');
    expect(region.querySelector('table')).toHaveClass('planner-table', 'portfolio-table');
    expect(screen.getByText(/Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id/)).toBeInTheDocument();
    expect(screen.getByText(/Unmatched or ambiguous reservations block import/)).toBeInTheDocument();
  });
  it('preserves exact retry after a server failure', async () => {
    const view = setup(vi.fn().mockResolvedValueOnce(preview()).mockRejectedValueOnce(Object.assign(new Error('Server error'), { status: 503 }))
      .mockResolvedValueOnce({ imported_count: 1, duplicate_count: 1 }));
    await showPreview(); confirm(); save(); await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Retry import' })); await flush();
    expect(view.request.mock.calls[2][1].body).toEqual(view.request.mock.calls[1][1].body);
  });
  it('invalidates a preview and confirmation immediately after selecting another file', async () => {
    setup(); await showPreview(); confirm(); select('replacement.csv');
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument(); expect(screen.queryByText('Statement preview')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview statement' })).toBeDisabled();
  });
  it('ignores late reads and preview responses after selection or cancellation', async () => {
    const pending = deferred(); const view = setup(vi.fn().mockReturnValue(pending.promise));
    select(); const oldReader = readers[0]; select('next.csv'); act(() => oldReader.finish('stale'));
    expect(oldReader.aborted).toBe(true); expect(screen.getByRole('button', { name: 'Preview statement' })).toBeDisabled();
    act(() => readers[1].finish()); fireEvent.click(screen.getByRole('button', { name: 'Preview statement' }));
    select('third.csv'); expect(view.request.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve(preview())); expect(screen.queryByText('Statement preview')).not.toBeInTheDocument();
    const latest = readers.at(-1); fireEvent.click(screen.getByRole('button', { name: 'Cancel import' })); act(() => latest.finish());
    expect(latest.aborted).toBe(true); expect(screen.queryByLabelText(/DEGIRO CSV file/)).not.toBeInTheDocument();
  });
  it('aborts save on unmount and ignores late success from an old route/session', async () => {
    const pending = deferred(); const view = setup(vi.fn().mockResolvedValueOnce(preview()).mockReturnValueOnce(pending.promise));
    await showPreview(); confirm(); save(); const signal = view.request.mock.calls[1][1].signal;
    view.unmount(); expect(signal.aborted).toBe(true); await act(async () => pending.resolve({ imported_count: 1, duplicate_count: 0 }));
    expect(view.onImported).not.toHaveBeenCalled();
  });
  it('isolates late previews across the tracker keyed session boundary and aborts unmounted reads', async () => {
    const pending = deferred(); const request = vi.fn().mockReturnValue(pending.promise); const onImported = vi.fn();
    const view = render(<DegiroImport key="session-a" request={request} portfolioPath={path} onImported={onImported} />);
    fireEvent.click(screen.getByRole('button', { name: 'Import DEGIRO CSV' })); read();
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' }));
    view.rerender(<DegiroImport key="session-b" request={request} portfolioPath={path} onImported={onImported} />);
    expect(request.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve(preview()));
    expect(screen.queryByText('Statement preview')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Import DEGIRO CSV' })); select(); const last = readers.at(-1);
    view.unmount(); expect(last.aborted).toBe(true); act(() => last.finish()); expect(onImported).not.toHaveBeenCalled();
  });
  it('rejects wrong extension, oversized and empty files and measures UTF-8 bytes', () => {
    setup(); select('statement.txt'); expect(screen.getByRole('alert')).toHaveTextContent('Choose a .csv');
    select('large.csv', 'x'.repeat(1_000_001)); expect(screen.getByRole('alert')).toHaveTextContent('1,000,000'); expect(readers).toHaveLength(0);
    select(); act(() => readers.at(-1).finish('')); expect(screen.getByRole('alert')).toHaveTextContent('empty');
    select(); act(() => readers.at(-1).finish('€'.repeat(333_334))); expect(screen.getByRole('alert')).toHaveTextContent('1,000,000');
  });
  it('fails closed for malformed preview and treats malformed save as uncertain', async () => {
    const view = setup(vi.fn().mockResolvedValueOnce({ can_import: true }).mockResolvedValueOnce(preview()).mockResolvedValueOnce({}));
    await showPreview(); expect(screen.getByRole('alert')).toHaveTextContent('Unexpected preview');
    fireEvent.click(screen.getByRole('button', { name: 'Preview statement' })); await flush(); confirm(); save(); await flush();
    expect(screen.getByRole('button', { name: 'Retry import' })).toBeEnabled(); expect(view.onImported).not.toHaveBeenCalled();
  });
});
