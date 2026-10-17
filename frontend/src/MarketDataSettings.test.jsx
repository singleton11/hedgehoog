import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import MarketDataSettings from './MarketDataSettings.jsx';

const policy = { version: 1, method: 'smooth', seed: 0 };
const flush = async () => act(async () => {});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('loads, edits, securely regenerates and explicitly saves the global policy', async () => {
  const request = vi.fn(async (_, options) => ({ ...policy, ...options?.body }));
  vi.spyOn(crypto, 'getRandomValues').mockImplementation((array) => { array[0] = 4294967295; return array; });
  render(<MarketDataSettings request={request} admin />); await flush();
  fireEvent.change(screen.getByLabelText('Missing quote method'), { target: { value: 'normal' } });
  fireEvent.click(screen.getByRole('button', { name: 'Generate / Regenerate seed' }));
  expect(screen.getByLabelText('Global missing-quote seed')).toHaveValue('4294967295');
  expect(request).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Save missing-quote settings' })); await flush();
  expect(request).toHaveBeenLastCalledWith('/settings/market-data', expect.objectContaining({
    method: 'PUT', body: { method: 'normal', seed: 4294967295 },
  }));
  expect(screen.getByRole('status')).toHaveTextContent('Existing saved reports are unchanged');
});

it('validates uint32 and is read-only for non-admins', async () => {
  const request = vi.fn(async () => policy);
  const view = render(<MarketDataSettings request={request} admin />); await flush();
  for (const value of ['-1', '1.5', '4294967296', '', '1e3']) {
    fireEvent.change(screen.getByLabelText('Global missing-quote seed'), { target: { value } });
    expect(screen.getByRole('button', { name: 'Save missing-quote settings' })).toBeDisabled();
  }
  view.unmount();
  render(<MarketDataSettings request={request} admin={false} />); await flush();
  expect(screen.getByLabelText('Missing quote method')).toBeDisabled();
  expect(screen.getByLabelText('Global missing-quote seed')).toBeDisabled();
  expect(screen.queryByRole('button', { name: /Save|Regenerate/ })).not.toBeInTheDocument();
});

it('ignores aborted load/save replies after unmount and request changes', async () => {
  let resolve;
  const request = vi.fn(() => new Promise((r) => { resolve = r; }));
  const view = render(<MarketDataSettings request={request} admin />); await flush();
  const signal = request.mock.calls[0][1].signal;
  const next = vi.fn(async () => ({ ...policy, seed: 7 }));
  view.rerender(<MarketDataSettings request={next} admin />); await flush();
  expect(signal.aborted).toBe(true);
  await act(async () => resolve({ ...policy, seed: 99 }));
  expect(screen.getByLabelText('Global missing-quote seed')).toHaveValue('7');
  next.mockImplementation(() => new Promise((r) => { resolve = r; }));
  fireEvent.click(screen.getByRole('button', { name: 'Save missing-quote settings' }));
  const saveSignal = next.mock.calls.at(-1)[1].signal;
  view.unmount(); expect(saveSignal.aborted).toBe(true);
  await act(async () => resolve({ ...policy, seed: 99 }));
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('reports load/save failures and retries loading independently', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('Unavailable')).mockResolvedValue(policy);
  render(<MarketDataSettings request={request} admin />); await flush();
  expect(screen.getByRole('alert')).toHaveTextContent('Unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Retry missing-quote settings' })); await flush();
  request.mockRejectedValueOnce(new Error('Save failed'));
  fireEvent.click(screen.getByRole('button', { name: 'Save missing-quote settings' })); await flush();
  expect(screen.getByRole('alert')).toHaveTextContent('Save failed');
  expect(screen.getByRole('button', { name: 'Save missing-quote settings' })).toBeEnabled();
});
