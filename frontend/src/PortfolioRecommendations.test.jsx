import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PortfolioRecommendations, { clearRecommendationRecovery } from './PortfolioRecommendations.jsx';

const props = { portfolioPath: '/customers/c/portfolios/p', planId: 'plan', revision: 0, recoveryScope: 'owner' };
const snapshot = {
  snapshot_token: 'snapshot', plan_id: 'plan', revision: 0, date: '2026-10-26', status: 'ready',
  funding: { scheduled_due_eur: '100.00', net_funded_eur: '0.00', shortfall_eur: '100.00', excess_eur: '0.00', due_count: 1 },
  targets: [{ instrument_id: 'ALPHA', weight: '0.6', actual_eur: '0.00', target_eur: '60.00', deviation: '-0.6' }, { instrument_id: 'BETA', weight: '0.4', actual_eur: '0.00', target_eur: '40.00', deviation: '-0.4' }],
  actions: [
    { id: 'deposit', type: 'deposit', amount_eur: '100.00', dependencies: [], eligible: true },
    { id: 'buy-a', type: 'buy', instrument_id: 'ALPHA', quantity: '6', price_eur: '10.00', commission_eur: '0.00', price_date: '2026-10-23', price_source: 'raw close', listing: { exchange: 'XAMS', currency: 'EUR' }, dependencies: ['deposit'], eligible: true },
    { id: 'buy-b', type: 'buy', instrument_id: 'BETA', quantity: '4', price_eur: '10.00', commission_eur: '0.00', dependencies: ['deposit'], eligible: true },
  ],
  policy: { version: 1, min_drift: '0.05', cooldown_days: 30, next_eligible_date: '2026-11-26' }, blockers: [],
};
const entries = [{ type: 'deposit', date: snapshot.date, amount_eur: '100.00' }, { type: 'buy', instrument_id: 'ALPHA', quantity: '6', price_eur: '10.00', commission_eur: '0.00' }];
const preview = { preview_token: 'preview', entries, before: { cash_eur: '0.00', holdings: {} }, after: { cash_eur: '40.00', holdings: { ALPHA: '6' } } };
function api({ read = snapshot, previewResult = preview, commit } = {}) {
  return vi.fn(async (path, options) => {
    if (path.endsWith('/preview')) return previewResult;
    if (path.endsWith('/fulfill')) return commit ? commit(options.body) : { operation_id: options.body.operation_id, event_ids: ['event'] };
    return read;
  });
}
async function openAll() { fireEvent.click(await screen.findByRole('button', { name: 'Fulfill all' })); }
async function review() { fireEvent.click(screen.getByRole('button', { name: 'Preview transactions' })); await screen.findByRole('button', { name: 'Confirm fulfillment' }); }
beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); });

describe('Portfolio recommendations', () => {
  it('renders generic allocation and funding separately from forecast, with policy/provenance', async () => {
    render(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    expect(await screen.findByText('ALPHA')).toBeInTheDocument();
    expect(screen.getByText('BETA')).toBeInTheDocument();
    expect(screen.getByText('Capital due').nextElementSibling).toHaveTextContent('€100.00');
    expect(screen.getByText(/Funding and allocation are separate/)).toBeInTheDocument();
    expect(screen.getByText(/raw close/)).toHaveTextContent('2026-10-23');
    expect(screen.getByText('cooldown days')).toBeInTheDocument();
    expect(screen.queryByText(/broker|attest|shared.plan/i)).not.toBeInTheDocument();
  });
  it('requires dependencies explicitly rather than silently adding them', async () => {
    render(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Fulfill Buy ALPHA' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Missing prerequisites: deposit');
    expect(screen.getByRole('button', { name: 'Preview transactions' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Fulfill Buy ALPHA' })).toHaveFocus();
  });
  it('defaults every eligible trade, previews atomically and refreshes on success', async () => {
    const request = api(), onRefresh = vi.fn();
    render(<PortfolioRecommendations {...props} request={request} onRefresh={onRefresh} />);
    await openAll();
    expect(screen.getByRole('dialog', { name: 'Review recommendations' })).toHaveFocus();
    expect(screen.getByLabelText('Price (EUR) — ALPHA')).toHaveValue('10.00');
    expect(screen.getByLabelText('Fee (EUR) — BETA')).toHaveValue('0.00');
    await review();
    expect(request.mock.calls.find(([path]) => path.endsWith('/preview'))[1].body).toEqual({ snapshot_token: 'snapshot', action_ids: ['deposit', 'buy-a', 'buy-b'], date: snapshot.date, overrides: {} });
    expect(within(screen.getByRole('region', { name: 'After' })).getByText('ALPHA: 6')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledOnce());
    expect(request.mock.calls.filter(([path]) => path.endsWith('/fulfill'))).toHaveLength(1);
    expect(sessionStorage.length).toBe(0);
  });
  it('invalidates the review on price, fee and quantity edits and re-previews', async () => {
    const request = api();
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    fireEvent.change(screen.getByLabelText('Price (EUR) — ALPHA'), { target: { value: '11.00' } });
    fireEvent.change(screen.getByLabelText('Fee (EUR) — ALPHA'), { target: { value: '1.00' } });
    fireEvent.change(screen.getByLabelText('Quantity — ALPHA'), { target: { value: '5' } });
    expect(screen.queryByRole('button', { name: 'Confirm fulfillment' })).not.toBeInTheDocument();
    await review();
    const calls = request.mock.calls.filter(([path]) => path.endsWith('/preview'));
    expect(calls[1][1].body.overrides).toEqual({ 'buy-a': { price_eur: '11.00', commission_eur: '1.00', quantity: '5' } });
  });
  it('guards rapid duplicate clicks and preserves the exact operation after a lost reply and remount', async () => {
    let reject;
    const request = api({ commit: () => new Promise((_, fail) => { reject = fail; }) });
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    const confirm = screen.getByRole('button', { name: 'Confirm fulfillment' });
    fireEvent.click(confirm); fireEvent.click(confirm);
    expect(request.mock.calls.filter(([path]) => path.endsWith('/fulfill'))).toHaveLength(1);
    const body = request.mock.calls.find(([path]) => path.endsWith('/fulfill'))[1].body;
    await act(async () => reject(new Error('Network disconnected')));
    expect(screen.getByRole('alert')).toHaveTextContent('uncertain');
    view.unmount();
    const retry = api(), refreshed = vi.fn();
    render(<PortfolioRecommendations {...props} revision={2} request={retry} onRefresh={refreshed} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry saved operation' }));
    await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
    expect(retry.mock.calls.find(([path]) => path.endsWith('/fulfill'))[1].body).toEqual(body);
    expect(body.operation_id).toMatch(/^[0-9a-f-]{36}$/);
  });
  it('discards a stale 409 review and requires new selection', async () => {
    const request = api({ commit: () => { throw Object.assign(new Error('Stale snapshot'), { status: 409 }); } });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review(); fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent('review again');
    expect(sessionStorage.length).toBe(0);
  });
  it('ignores late responses on portfolio or request identity changes', async () => {
    let resolve;
    const request = vi.fn(() => new Promise((done) => { resolve = done; }));
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    view.rerender(<PortfolioRecommendations {...props} planId="new-plan" request={api({ read: { ...snapshot, plan_id: 'new-plan', targets: [], actions: [], status: 'aligned' } })} onRefresh={vi.fn()} />);
    await screen.findByText('Funding and allocation are aligned.');
    await act(async () => resolve(snapshot));
    expect(screen.queryByText('ALPHA')).not.toBeInTheDocument();
  });
  it('renders blocked and unsupported states rather than suggesting alignment', async () => {
    render(<PortfolioRecommendations {...props} request={api({ read: { ...snapshot, status: 'unsupported', actions: [], targets: [], blockers: ['Strategy unavailable'] } })} onRefresh={vi.fn()} />);
    expect(await screen.findByText('Strategy unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fulfill all' })).toBeDisabled();
    expect(screen.queryByText('Funding and allocation are aligned.')).not.toBeInTheDocument();
  });
  it('does not leak a saved operation to a different owner or portfolio', async () => {
    sessionStorage.setItem('portfolio-recommendation:["other","/customers/c/portfolios/p"]', JSON.stringify({ body: { operation_id: 'secret', preview_token: 'secret', action_ids: ['a'] } }));
    render(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    await screen.findByText('ALPHA');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('keeps keyboard focus inside the review and closes with Escape', async () => {
    render(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    await openAll();
    const cancel = screen.getByRole('button', { name: 'Cancel' }); cancel.focus();
    fireEvent.keyDown(cancel, { key: 'Tab' });
    expect(screen.getByLabelText('Price (EUR) — ALPHA')).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fulfill all' })).toHaveFocus();
  });
  it('keeps recovery across dismissal, and prevents new operations until reconciliation', async () => {
    const request = api({ commit: () => { throw new Error('Lost reply'); } });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    await screen.findByText(/The result is uncertain/);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fulfill all' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Resume saved operation' }));
    expect(screen.getByRole('button', { name: 'Retry saved operation' })).toBeInTheDocument();
  });
  it('requires every rebalance group member and displays cooldown blockers', async () => {
    const read = { ...snapshot, actions: [
      { ...snapshot.actions[1], dependencies: [], group: ['buy-a', 'buy-b'] },
      { ...snapshot.actions[2], eligible: false, blockers: ['Rebalance cooldown is active.'] },
    ] };
    render(<PortfolioRecommendations {...props} request={api({ read })} onRefresh={vi.fn()} />);
    expect(await screen.findByText('Rebalance cooldown is active.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fulfill Buy BETA' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Fulfill Buy ALPHA' }));
    expect(screen.getByRole('alert')).toHaveTextContent('buy-b');
    expect(screen.getByRole('button', { name: 'Preview transactions' })).toBeDisabled();
  });
  it('allows a valid override when server eligibility permits a missing default price', async () => {
    const read = { ...snapshot, actions: [{ ...snapshot.actions[1], dependencies: [], price_eur: null, quantity: null, blockers: ['Price override required.'] }] };
    const request = api({ read });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Fulfill Buy ALPHA' }));
    fireEvent.change(screen.getByLabelText('Price (EUR) — ALPHA'), { target: { value: '12.00' } });
    await review();
    expect(request.mock.calls.find(([path]) => path.endsWith('/preview'))[1].body.overrides).toEqual({ 'buy-a': { price_eur: '12.00' } });
  });
  it('fails closed before sending if durable recovery storage is unavailable', async () => {
    const request = api();
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Disabled'); });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Secure retry storage is unavailable');
    expect(request.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
    storage.mockRestore();
  });
  it('ignores a late preview when the request identity changes', async () => {
    let resolve;
    const request = vi.fn((path) => path.endsWith('/preview') ? new Promise((done) => { resolve = done; }) : Promise.resolve(snapshot));
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); fireEvent.click(screen.getByRole('button', { name: 'Preview transactions' }));
    view.rerender(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    await screen.findByText('ALPHA');
    await act(async () => resolve(preview));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('does not request a snapshot without a linked plan', () => {
    const request = vi.fn();
    const view = render(<PortfolioRecommendations {...props} planId={null} request={request} onRefresh={vi.fn()} />);
    expect(request).not.toHaveBeenCalled();
    view.unmount();
  });
});

const recoveryKey = `portfolio-recommendation:${JSON.stringify([props.recoveryScope, props.portfolioPath])}`;
function savedOperation(extra = {}) {
  return { body: { operation_id: 'e3ad6fab-d575-4a3b-a9f1-4c03dc9ea8db', preview_token: 'reviewed', snapshot_token: 'old', action_ids: ['deposit'], date: snapshot.date, overrides: {} }, ...extra };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

describe('Recommendation error handling and review details', () => {
  it.each([
    ['missing response', null],
    ['wrong plan', { ...snapshot, plan_id: 'another-plan' }],
    ['missing action array', { ...snapshot, actions: null }],
    ['missing target array', { ...snapshot, targets: {} }],
    ['missing token', { ...snapshot, snapshot_token: null }],
  ])('rejects a malformed read: %s', async (_, read) => {
    const request = vi.fn().mockResolvedValueOnce(read).mockResolvedValueOnce(snapshot);
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unexpected recommendations response.');
    expect(screen.getByRole('button', { name: 'Fulfill all' })).toBeDisabled();
    expect(screen.queryByText('ALPHA')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh recommendations' }));
    expect(await screen.findByText('ALPHA')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(2);
  });
  it.each([['Service temporarily unavailable', 'Service temporarily unavailable'], ['', 'Unable to load recommendations.']])('offers a retry for a failed read (%s)', async (message, expected) => {
    const request = vi.fn().mockRejectedValueOnce(new Error(message)).mockResolvedValueOnce(snapshot);
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(expected);
    expect(screen.getByText('Recommendations unavailable.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh recommendations' }));
    await screen.findByText('ALPHA');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('aborts reads and ignores a late rejection when the portfolio changes', async () => {
    const old = deferred();
    const request = vi.fn().mockImplementationOnce(() => old.promise).mockResolvedValue(snapshot);
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    const signal = request.mock.calls[0][1].signal;
    view.rerender(<PortfolioRecommendations {...props} portfolioPath="/customers/c/portfolios/other" request={request} onRefresh={vi.fn()} />);
    expect(signal.aborted).toBe(true);
    await screen.findByText('ALPHA');
    await act(async () => old.reject(new Error('Old portfolio failed')));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it.each([
    ['missing preview', null],
    ['missing token', { entries: [] }],
    ['missing entries', { preview_token: 'bad', entries: null }],
  ])('does not allow confirmation after an invalid preview: %s', async (_, previewResult) => {
    const request = api({ previewResult });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll();
    fireEvent.click(screen.getByRole('button', { name: 'Preview transactions' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unexpected preview response.');
    expect(screen.queryByRole('button', { name: 'Confirm fulfillment' })).not.toBeInTheDocument();
    expect(request.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
  });
  it.each([409, 422])('handles preview HTTP %s without committing', async (status) => {
    const request = vi.fn(async (path) => {
      if (path.endsWith('/preview')) throw Object.assign(new Error('Selection no longer valid'), { status });
      return snapshot;
    });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll();
    fireEvent.click(screen.getByRole('button', { name: 'Preview transactions' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(status === 409 ? 'Recommendations changed. Refresh recommendations and review again.' : 'Selection no longer valid');
    if (status === 409) {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      await waitFor(() => expect(request.mock.calls.filter(([path]) => path.endsWith('/recommendations'))).toHaveLength(2));
    } else expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(request.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
  });
  it('suppresses a rejected preview after navigation without leaking its error', async () => {
    const old = deferred();
    const request = vi.fn((path) => path.endsWith('/preview') ? old.promise : Promise.resolve(snapshot));
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); fireEvent.click(screen.getByRole('button', { name: 'Preview transactions' }));
    view.rerender(<PortfolioRecommendations {...props} revision={1} request={request} onRefresh={vi.fn()} />);
    await screen.findByText('ALPHA');
    await act(async () => old.reject(Object.assign(new Error('Old preview stale'), { status: 409 })));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it.each(['resolved', 'rejected'])('preserves the saved request when fulfillment is %s after unmount', async (outcome) => {
    const pending = deferred(), onRefresh = vi.fn();
    const request = api({ commit: () => pending.promise });
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={onRefresh} />);
    await openAll(); await review();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    const saved = JSON.parse(sessionStorage.getItem(recoveryKey));
    view.unmount();
    await act(async () => {
      if (outcome === 'resolved') pending.resolve({ operation_id: saved.body.operation_id, event_ids: ['event'] });
      else pending.reject(new Error('Lost after navigation'));
    });
    expect(onRefresh).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem(recoveryKey))).toEqual(saved);
  });
  it.each([
    ['missing response', () => null],
    ['wrong operation', () => ({ operation_id: 'another-operation', event_ids: [] })],
    ['missing receipt entries', (body) => ({ operation_id: body.operation_id, event_ids: null })],
  ])('retains retry identity for a malformed fulfillment: %s', async (_, commit) => {
    const onRefresh = vi.fn(), request = api({ commit });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={onRefresh} />);
    await openAll(); await review();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('result is uncertain');
    const sent = request.mock.calls.find(([path]) => path.endsWith('/fulfill'))[1].body;
    expect(JSON.parse(sessionStorage.getItem(recoveryKey)).body).toEqual(sent);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Retry saved operation' })).toBeEnabled();
  });
  it('discards an invalid 422 fulfillment with a fallback reason and requires fresh review', async () => {
    const request = api({ commit: () => { throw { status: 422 }; } });
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Recommendations changed. Refresh and review again.');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(recoveryKey)).toBeNull();
  });
  it.each(['{not-json', '{}', '{"body":{}}', '{"body":{"operation_id":"id"}}', '{"body":{"operation_id":"id","preview_token":"token","action_ids":"deposit"}}'])('ignores unusable saved recovery data: %s', async (saved) => {
    sessionStorage.setItem(recoveryKey, saved);
    render(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    await screen.findByText('ALPHA');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fulfill all' })).toBeEnabled();
  });
  it('can read recommendations when storage access is disabled but refuses to submit', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Storage blocked'); });
    const request = api();
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Secure retry storage is unavailable');
    expect(request.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
  });
  it('requires verifiable persistence when session storage silently drops a write', async () => {
    const request = api();
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {});
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Secure retry storage is unavailable');
    expect(request.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
  });
  it('does not send an operation if cryptographic UUID generation fails', async () => {
    const request = api();
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); await review();
    vi.spyOn(globalThis.crypto, 'getRandomValues').mockImplementation(() => { throw new Error('Crypto unavailable'); });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm fulfillment' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Web Crypto');
    expect(sessionStorage.getItem(recoveryKey)).toBeNull();
    expect(request.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
  });
  it('clears only recommendation recovery records and tolerates disabled storage', () => {
    sessionStorage.setItem(recoveryKey, JSON.stringify(savedOperation()));
    sessionStorage.setItem('unrelated-preference', 'keep');
    clearRecommendationRecovery();
    expect(sessionStorage.getItem(recoveryKey)).toBeNull();
    expect(sessionStorage.getItem('unrelated-preference')).toBe('keep');
    vi.spyOn(Storage.prototype, 'key').mockImplementation(() => { throw new Error('Disabled'); });
    expect(() => clearRecommendationRecovery()).not.toThrow();
  });
  it('clears recovered data on request replacement, rather than submitting under a new authentication context', async () => {
    sessionStorage.setItem(recoveryKey, JSON.stringify(savedOperation()));
    const first = api(), next = api();
    const view = render(<PortfolioRecommendations {...props} request={first} onRefresh={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Retry saved operation' })).toBeInTheDocument();
    view.rerender(<PortfolioRecommendations {...props} request={next} onRefresh={vi.fn()} />);
    await screen.findByText('ALPHA');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(recoveryKey)).toBeNull();
    expect(next.mock.calls.some(([path]) => path.endsWith('/fulfill'))).toBe(false);
  });
  it('hides recovery from a new owner and restores it only on return to its original scope', async () => {
    sessionStorage.setItem(recoveryKey, JSON.stringify(savedOperation()));
    const request = api();
    const view = render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    view.rerender(<PortfolioRecommendations {...props} recoveryScope="different-owner" request={request} onRefresh={vi.fn()} />);
    await screen.findByText('ALPHA');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    view.rerender(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Retry saved operation' })).toBeInTheDocument();
  });
  it('recovers a minimal saved operation without requiring a current linked plan', async () => {
    const saved = savedOperation();
    sessionStorage.setItem(recoveryKey, JSON.stringify(saved));
    const request = api(), onRefresh = vi.fn();
    render(<PortfolioRecommendations {...props} planId={null} request={request} onRefresh={onRefresh} />);
    expect(screen.getByText('Assign a saved plan to see recommendations.')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Before' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close review' }));
    fireEvent.click(screen.getByRole('button', { name: 'Resume saved operation' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry saved operation' }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledOnce());
    expect(request).toHaveBeenCalledExactlyOnceWith(`${props.portfolioPath}/recommendations/fulfill`, { method: 'POST', body: saved.body });
  });
  it('renders effective preview prices, fees and alternative position shapes without hiding residuals', async () => {
    const selection = [snapshot.actions[0], { ...snapshot.actions[1], price_source: 'override', price_eur: '12.00' }, { ...snapshot.actions[2], price_source: null, price_date: null, listing: 'EUR / XETR' }];
    const previewResult = { ...preview, selection, entries: [{ ...entries[1], id: 'trade', commission_eur: '1.25' }, { ...entries[0] }],
      before: { cash_eur: '100.00', equity_eur: '130.00', holdings: [{ instrument: { instrument_id: 'OLD', name: 'Old holding' }, quantity: '3' }] },
      after: { cash_eur: '38.75', equity_eur: '128.75', positions: [{ instrument_id: 'ALPHA', label: 'First fund', quantity: '5' }] }, total_commission_eur: '1.25', residual_eur: '38.75' };
    render(<PortfolioRecommendations {...props} request={api({ previewResult })} onRefresh={vi.fn()} />);
    await openAll(); await review();
    expect(screen.getByText(/Edited price €12.00/)).toHaveTextContent('override');
    expect(screen.getByText(/Saved last\/close €10.00/)).toHaveTextContent('EUR / XETR');
    expect(screen.getAllByText('Total fees: €1.25')).toHaveLength(2);
    expect(screen.getByText('Residual cash: €38.75')).toBeInTheDocument();
    const before = screen.getByRole('region', { name: 'Before' });
    expect(within(before).getByText('Old holding: 3')).toBeInTheDocument();
    expect(within(before).getByText('Equity').nextElementSibling).toHaveTextContent('€130.00');
    expect(within(screen.getByRole('region', { name: 'After' })).getByText('First fund: 5')).toBeInTheDocument();
  });
  it('renders a preview with no projections or trade commissions', async () => {
    const read = { ...snapshot, actions: [snapshot.actions[0]] };
    render(<PortfolioRecommendations {...props} request={api({ read, previewResult: { preview_token: 'minimal', entries: [entries[0]] } })} onRefresh={vi.fn()} />);
    await openAll(); await review();
    expect(screen.getByText('Total fees: €0.00')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Before' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm fulfillment' })).toBeEnabled();
  });
  it('shows cash, unavailable target diagnostics, structured blockers and a next payment without guessing alignment', async () => {
    const read = { ...snapshot, status: 'blocked', funding: { ...snapshot.funding, due_count: null, next_payment: { date: '2026-11-26', amount_eur: '100.00' } },
      targets: [{ instrument_id: null, actual_eur: '10.00', target_eur: '20.00', weight: null, deviation: null }], actions: [],
      blockers: [{ message: 'No suitable valuation' }, { reason: 'Listing unavailable' }, { code: 'QUOTE_REQUIRED' }, {}],
      policy: { version: 1, optional: null, constraints: {} }, next_eligible_date: '2026-11-26' };
    render(<PortfolioRecommendations {...props} request={api({ read })} onRefresh={vi.fn()} />);
    expect(await screen.findByText('Cash')).toBeInTheDocument();
    expect(screen.getByText(/Weight deviation Unavailable/)).toHaveTextContent('(Unavailable)');
    expect(screen.getByText(/Scheduled payments due: Unavailable/)).toBeInTheDocument();
    expect(screen.getByText('Next scheduled payment: 2026-11-26 · €100.00')).toBeInTheDocument();
    expect(screen.getByText('Next rebalance eligibility: 2026-11-26')).toBeInTheDocument();
    expect(screen.getByText('No suitable valuation; Listing unavailable; QUOTE_REQUIRED')).toBeInTheDocument();
    expect(screen.getByText('No eligible corrective actions.')).toBeInTheDocument();
    expect(screen.queryByText('constraints')).not.toBeInTheDocument();
    expect(screen.queryByText('optional')).not.toBeInTheDocument();
  });
  it('shows suppressed actions and unresolved dependency labels without making them executable', async () => {
    const read = { ...snapshot, funding: { ...snapshot.funding, next_payment: '2026-11-26' }, policy: null, blockers: undefined,
      actions: [{ id: 'sell', type: 'sell', instrument: { instrument_id: 'OLD', name: 'Old fund' }, amount_eur: '50.00', price_eur: '10.00', listing: 'XAMS EUR', eligible: true, commission_eur: null, reason: 'Requires group review', dependencies: ['unknown-prerequisite'], group: ['sell', 'unavailable-buy'] }], targets: [] };
    render(<PortfolioRecommendations {...props} request={api({ read })} onRefresh={vi.fn()} />);
    expect(await screen.findByText('Sell Old fund')).toBeInTheDocument();
    expect(screen.getByText('Next scheduled payment: 2026-11-26')).toBeInTheDocument();
    expect(screen.getByText(/Price €10.00/)).toHaveTextContent('XAMS EUR');
    expect(screen.getByText('Requires group review')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Fulfill Sell Old fund' }));
    expect(screen.getByRole('alert')).toHaveTextContent('unknown-prerequisite, unavailable-buy');
    expect(screen.getByLabelText('Fee (EUR) — Old fund')).toHaveValue('0.00');
    expect(screen.getByRole('button', { name: 'Preview transactions' })).toBeDisabled();
  });
  it('wraps backward focus from the heading and first input, while leaving ordinary keys alone', async () => {
    render(<PortfolioRecommendations {...props} request={api()} onRefresh={vi.fn()} />);
    await openAll();
    const dialog = screen.getByRole('dialog'), cancel = screen.getByRole('button', { name: 'Cancel' });
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(cancel).toHaveFocus();
    const first = screen.getByLabelText('Price (EUR) — ALPHA'); first.focus();
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(cancel).toHaveFocus();
    first.focus(); fireEvent.keyDown(first, { key: 'ArrowLeft' });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: 'Tab' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
  it('keeps the pending review open when Escape is pressed, then permits closing after a failure', async () => {
    const pending = deferred();
    const request = vi.fn((path) => path.endsWith('/preview') ? pending.promise : Promise.resolve(snapshot));
    render(<PortfolioRecommendations {...props} request={request} onRefresh={vi.fn()} />);
    await openAll(); fireEvent.click(screen.getByRole('button', { name: 'Preview transactions' }));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    await act(async () => pending.reject(new Error('Cannot prepare review')));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fulfill all' })).toHaveFocus();
  });
});
