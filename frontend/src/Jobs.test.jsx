import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import Jobs from './Jobs.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';

const auth = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./Auth.jsx', async (original) => ({ ...await original(), useAuth: () => auth }));
const counts = { total: 100, queued: 10, running: 3, retrying: 2, succeeded: 80, failed: 5 };
const job = (extra = {}) => ({ id: 'download-1', job_type: 'market_data_acquisition', status: 'running',
  instrument_id: 'IE00B4L5Y983', requested_at: '2026-10-17T12:00:00Z', started_at: '2026-10-17T12:01:00Z', finished_at: null, error: null, ...extra });
const page = (items = [job()], extra = {}) => ({ items, counts, total: 1, ...extra });
const flush = async () => act(async () => {});
const advance = async (ms = 3000) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
beforeEach(() => { vi.useFakeTimers(); auth.request.mockReset(); auth.request.mockResolvedValue(page()); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); window.history.replaceState(null, '', '#/'); });

it('opens Jobs from navigation without selecting ETFs', async () => {
  window.history.replaceState(null, '', '#/jobs');
  render(<InstrumentWorkspace />); await flush();
  expect(screen.getByRole('heading', { name: 'Jobs' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Jobs' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: 'ETFs' })).not.toHaveAttribute('aria-current');
});

it('shows global counts, job types, statuses, timestamps and instrument links', async () => {
  auth.request.mockResolvedValue(page([job(), job({ id: 'sweep-1', job_type: 'market_data_refresh', status: 'succeeded', instrument_id: null })]));
  render(<Jobs />); await flush();
  const countsList = screen.getByLabelText('All job counts');
  expect(within(countsList).getByText('100')).toBeInTheDocument();
  expect(within(countsList).getByText('85')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'IE00B4L5Y983' })).toHaveAttribute('href', '#/instruments/IE00B4L5Y983');
  expect(screen.getAllByText('Automatic refresh sweep').length).toBeGreaterThan(0);
  expect(screen.getByRole('status')).toHaveTextContent('Auto-updates every 3 seconds');
});

it('polls without overlapping requests and stops on unmount', async () => {
  let resolve;
  auth.request.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  const view = render(<Jobs />); await advance(9000);
  expect(auth.request).toHaveBeenCalledTimes(1);
  await act(async () => resolve(page()));
  await advance(); expect(auth.request).toHaveBeenCalledTimes(2);
  const signal = auth.request.mock.calls.at(-1)[1].signal;
  view.unmount(); expect(signal.aborted).toBe(true);
  await advance(9000); expect(auth.request).toHaveBeenCalledTimes(2);
});

it('filters server-side, resets pagination and ignores stale responses', async () => {
  auth.request.mockResolvedValue(page([job()], { total: 60 }));
  render(<Jobs />); await flush();
  fireEvent.click(screen.getByRole('button', { name: 'Next jobs' })); await flush();
  expect(auth.request.mock.calls.at(-1)[0]).toContain('offset=25');
  let resolve;
  auth.request.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
  const oldSignal = auth.request.mock.calls.at(-1)[1].signal;
  fireEvent.change(screen.getByLabelText('Job type'), { target: { value: 'etf_import' } }); await flush();
  expect(oldSignal.aborted).toBe(true);
  expect(auth.request.mock.calls.at(-1)[0]).toBe('/jobs?limit=25&offset=0&status=failed&job_type=etf_import');
  await act(async () => resolve(page([job({ id: 'obsolete' })])));
  expect(screen.queryByText('obsolete')).not.toBeInTheDocument();
});

it('aborts an old request identity and discards its late response', async () => {
  let resolve;
  const previous = auth.request;
  previous.mockImplementation(() => new Promise((done) => { resolve = done; }));
  const view = render(<Jobs />); await flush();
  const signal = previous.mock.calls[0][1].signal;
  auth.request = vi.fn().mockResolvedValue(page([job({ id: 'new-session' })]));
  view.rerender(<Jobs />); await flush();
  expect(signal.aborted).toBe(true);
  await act(async () => resolve(page([job({ id: 'old-session' })])));
  expect(screen.getByText('new-session')).toBeInTheDocument();
  expect(screen.queryByText('old-session')).not.toBeInTheDocument();
});

it('retains last successful data on polling errors and recovers automatically', async () => {
  render(<Jobs />); await flush();
  auth.request.mockRejectedValueOnce(new Error('Database unavailable'));
  await advance();
  expect(screen.getByRole('alert')).toHaveTextContent('Showing the last successful update');
  expect(screen.getByText('download-1')).toBeInTheDocument();
  expect(screen.getByRole('status')).not.toHaveTextContent('Auto-updates');
  await advance(); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('reports malformed and failed initial responses with manual retry', async () => {
  auth.request.mockResolvedValueOnce({ items: [] });
  render(<Jobs />); await flush();
  expect(screen.getByRole('alert')).toHaveTextContent('unexpected job data');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh jobs' })); await flush();
  expect(screen.getByText('download-1')).toBeInTheDocument();
});

it.each([null, job({ error: { secret: 'invalid' } }), job({ status: 'toString' }), job({ job_type: '__proto__' })])('rejects malformed job rows without crashing', async (item) => {
  auth.request.mockResolvedValue(page([item]));
  render(<Jobs />); await flush();
  expect(screen.getByRole('alert')).toHaveTextContent('unexpected job data');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

it('distinguishes empty history from empty filters and corrects a vanished page', async () => {
  auth.request.mockResolvedValue(page([], { total: 0 }));
  render(<Jobs />); await flush();
  expect(screen.getByText('No jobs recorded yet.')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } }); await flush();
  expect(screen.getByText('No jobs match these filters.')).toBeInTheDocument();
  auth.request.mockResolvedValue(page([job()], { total: 26 }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh jobs' })); await flush();
  fireEvent.click(screen.getByRole('button', { name: 'Next jobs' })); await flush();
  auth.request.mockResolvedValue(page([], { total: 0 }));
  await advance();
  expect(auth.request.mock.calls.at(-1)[0]).toContain('offset=0');
});
