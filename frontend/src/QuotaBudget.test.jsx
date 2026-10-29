import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import QuotaBudget, { BUDGET_POLL_INTERVAL } from './QuotaBudget.jsx';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ request }) }));
const budget = { provider: 'yfinance', metered: false, month: '2026-09', used: 0, limit: null, remaining: null };
const flush = async () => { await act(async () => {}); };
const advance = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(BUDGET_POLL_INTERVAL); }); };

beforeEach(() => {
  vi.useFakeTimers();
  request.mockReset();
  request.mockResolvedValue(budget);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('header provider status', () => {
  it('shows Yahoo Finance experimental status and keeps it current without acquisition requests', async () => {
    render(<QuotaBudget />);
    expect(screen.getByText('Loading provider status…')).toBeInTheDocument();
    await flush();
    expect(screen.getByRole('group', { name: 'Provider status' })).toHaveTextContent('Yahoo Finance · experimental');
    expect(screen.getByText('Yahoo Finance · experimental').closest('p')).toHaveAttribute('title', '2026-09 · unmetered experimental provider.');
    request.mockResolvedValue({ ...budget, month: '2026-10' });
    await advance();
    expect(screen.getByRole('group', { name: 'Provider status' })).toHaveTextContent('Yahoo Finance · experimental');
    expect(request).toHaveBeenCalledTimes(2);
    for (const [path, options] of request.mock.calls) {
      expect(path).toBe('/instruments/budget');
      expect(options).toEqual({ signal: expect.any(AbortSignal) });
    }
  });

  it('does not offer legacy metered provider status or quota displays', async () => {
    request.mockResolvedValueOnce({ month: '2026-09', limit: 10000, used: 125, remaining: 9875 });
    render(<QuotaBudget />);
    await flush();
    expect(screen.getByText('Provider status unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/Local budget|calls left/)).not.toBeInTheDocument();
    await advance();
    expect(screen.getByText('Yahoo Finance · experimental')).toBeInTheDocument();
  });

  it('does not present old usage as current after an error, and recovers automatically', async () => {
    render(<QuotaBudget />);
    await flush();
    request.mockRejectedValueOnce(new Error('offline'));
    await advance();
    expect(screen.getByText('Provider status unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/Yahoo Finance/)).not.toBeInTheDocument();
    await advance();
    expect(screen.getByText('Yahoo Finance · experimental')).toBeInTheDocument();
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
    expect(request).toHaveBeenCalledTimes(1);
    view.unmount();
    await act(async () => resolve(budget));
    await advance();
    expect(request).toHaveBeenCalledTimes(1);
  });
});
