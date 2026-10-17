import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import PerformanceSettings from './PerformanceSettings.jsx';

const { auth, request } = vi.hoisted(() => ({ auth: { user: { id: 'a', role: 'admin' } }, request: vi.fn() }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ ...auth, request: auth.request || request }) }));
const instrument = { instrument_id: 'idx-00000000-0000-0000-0000-000000000001', name: 'Overnight index', symbol: '^TEST' };
const flush = async () => act(async () => {});
beforeEach(() => {
  auth.user = { id: 'a', role: 'admin' }; delete auth.request;
  request.mockReset().mockImplementation(async (url, options) => {
    if (url === '/settings/market-data') return { version: 1, method: 'smooth', seed: 0 };
    if (url === '/settings/performance') return { proxy: options?.body?.instrument_id ? instrument : null };
    return { items: [instrument], total: 1, offset: 0, limit: 100 };
  });
});
afterEach(cleanup);

it('loads saved choices and explicitly saves an app-wide selection or clears it', async () => {
  render(<PerformanceSettings />); await flush();
  expect(screen.getByText(/missing currency metadata is assumed to be EUR/)).toBeInTheDocument();
  expect(request).toHaveBeenCalledWith('/settings/performance/indices?limit=100&offset=0', expect.anything());
  expect(screen.getByRole('link', { name: 'Acquire an index' })).toHaveAttribute('href', '#/indices');
  expect(request.mock.calls.every(([, options]) => !options.method)).toBe(true);
  fireEvent.change(screen.getByLabelText('Saved EUR index'), { target: { value: instrument.instrument_id } });
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' })); await flush();
  expect(request).toHaveBeenCalledWith('/settings/performance', { method: 'PUT', body: { instrument_id: instrument.instrument_id } });
  expect(screen.getByRole('status')).toHaveTextContent('Saved for all users');
  fireEvent.change(screen.getByLabelText('Saved EUR index'), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' })); await flush();
  expect(request).toHaveBeenLastCalledWith('/settings/performance', { method: 'PUT', body: { instrument_id: null } });
});

it('is read-only for non-admins', async () => {
  auth.user.role = 'user'; render(<PerformanceSettings />); await flush();
  expect(screen.getByLabelText('Saved EUR index')).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Save settings' })).not.toBeInTheDocument();
});

it('does not offer a partial list after pagination failure and can retry', async () => {
  const good = request.getMockImplementation();
  request.mockImplementation(async (url, options) => url.includes('offset=0')
    ? { items: [instrument], total: 2, offset: 0, limit: 100 }
    : url.includes('offset=1') ? { items: [instrument], total: 2, offset: 1, limit: 100 } : good(url, options));
  render(<PerformanceSettings />); await flush();
  expect(screen.getByRole('alert')).toHaveTextContent('Invalid saved instrument list');
  expect(screen.queryByLabelText('Saved EUR index')).not.toBeInTheDocument();
  request.mockImplementation(good);
  fireEvent.click(screen.getByRole('button', { name: 'Retry settings' })); await flush();
  expect(screen.getByLabelText('Saved EUR index')).toBeInTheDocument();
});

it('ignores a late settings response after auth changes', async () => {
  let resolve;
  const good = request.getMockImplementation();
  request.mockImplementation(() => new Promise((r) => { resolve = r; }));
  const view = render(<PerformanceSettings />); await flush();
  const signal = request.mock.calls[0][1].signal;
  auth.request = vi.fn(good); view.rerender(<PerformanceSettings />); await flush();
  expect(signal.aborted).toBe(true);
  await act(async () => resolve({ proxy: instrument }));
  expect(screen.getByLabelText('Saved EUR index')).toHaveValue('');
});

it('isolates missing-quote settings from proxy-list failures', async () => {
  const good = request.getMockImplementation();
  request.mockImplementation(async (url, options) => {
    if (url.includes('/indices?')) throw new Error('Proxy list failed');
    return good(url, options);
  });
  render(<PerformanceSettings />); await flush();
  expect(screen.getByRole('alert')).toHaveTextContent('Proxy list failed');
  expect(screen.getByLabelText('Missing quote method')).toHaveValue('smooth');
  fireEvent.click(screen.getByRole('button', { name: 'Save missing-quote settings' })); await flush();
  expect(screen.getByRole('status')).toHaveTextContent('Missing-quote policy saved');
});
