import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import QuotaBudget, { BUDGET_POLL_INTERVAL } from './QuotaBudget.jsx';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ request }) }));
const budget = { month: '2026-09', limit: 10000, used: 125, remaining: 9875 };
const flush = async () => { await act(async () => {}); };
const advance = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(BUDGET_POLL_INTERVAL); }); };

beforeEach(() => {
  vi.useFakeTimers();
  request.mockReset();
  request.mockResolvedValue(budget);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('header quota budget', () => {
  it('loads global usage and keeps it current without acquisition requests', async () => {
    render(<QuotaBudget />);
    expect(screen.getByText('Loading local budget…')).toBeInTheDocument();
    await flush();
    expect(screen.getByRole('group', { name: 'Quota budget' })).toHaveTextContent('Local budget: 9,875 / 10,000 calls left');
    expect(screen.getByText('Local budget:').closest('p')).toHaveAttribute('title', '2026-09 · 125 used. Excludes requests outside Hedgehoog.');
    request.mockResolvedValue({ ...budget, used: 150, remaining: 9850 });
    await advance();
    expect(screen.getByRole('group', { name: 'Quota budget' })).toHaveTextContent('9,850 / 10,000 calls left');
    expect(request).toHaveBeenCalledTimes(2);
    for (const [path, options] of request.mock.calls) {
      expect(path).toBe('/instruments/budget');
      expect(options).toEqual({ signal: expect.any(AbortSignal) });
    }
  });

  it('does not present old usage as current after an error, and recovers automatically', async () => {
    render(<QuotaBudget />);
    await flush();
    request.mockRejectedValueOnce(new Error('offline'));
    await advance();
    expect(screen.getByText('Local budget unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/calls left/)).not.toBeInTheDocument();
    await advance();
    expect(screen.getByText(/9,875 \/ 10,000 calls left/)).toBeInTheDocument();
  });

  it('displays an exhausted budget instead of treating zero as unavailable', async () => {
    request.mockResolvedValue({ ...budget, used: 10000, remaining: 0 });
    render(<QuotaBudget />);
    await flush();
    expect(screen.getByRole('group', { name: 'Quota budget' })).toHaveTextContent('0 / 10,000 calls left');
  });

  it('cancels the next refresh on unmount', async () => {
    const view = render(<QuotaBudget />);
    await flush();
    const signal = request.mock.calls[0][1].signal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await advance();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('ignores a late response after unmount and does not restart polling', async () => {
    let resolve;
    request.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const view = render(<QuotaBudget />);
    await advance();
    expect(request).toHaveBeenCalledTimes(1); // No overlapping reads.
    view.unmount();
    await act(async () => resolve(budget));
    await advance();
    expect(request).toHaveBeenCalledTimes(1);
  });
});
