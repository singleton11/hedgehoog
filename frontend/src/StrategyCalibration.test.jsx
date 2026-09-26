import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import StrategyCalibration from './StrategyCalibration.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';
import { BUY_AND_HOLD_ID } from './goalStrategies.js';
import {
  calibrationRequest, DEFAULT_CALIBRATION_INPUTS, savedCalibrationInputs, validCalibrationHistory,
  validCalibrationResult, validCalibrationRunSummary, validCalibrationSummary,
} from './strategyCalibration.js';

const { request, auth } = vi.hoisted(() => ({ request: vi.fn(), auth: { user: { id: 'user-a' }, status: 'authenticated' } }));
vi.mock('./Auth.jsx', () => ({ useAuth: () => ({ ...auth, request: auth.request || request }) }));
const A = 'IE00B4L5Y983', B = 'IE00B5BMR087';
const buyAndHold = {
  id: BUY_AND_HOLD_ID, name: 'Buy and hold single instrument', description: 'Invest and hold one acquired instrument.', supports_calibration: true,
  parameters: [{ key: 'instrument', type: 'instrument', label: 'Instrument', required: true, universe: 'acquired' }],
};
const pair = {
  id: 'test pair/second', name: 'Test pair', description: 'Two acquired instruments.', supports_calibration: true,
  parameters: ['primary', 'secondary'].map((key) => ({ key, type: 'instrument', label: `${key} instrument`, required: true, universe: 'acquired' })),
};
const selection = { id: BUY_AND_HOLD_ID, parameters: { instrument: A } };
const expectedBody = {
  mode: 'random', strategy: selection, scenario_count: 1000, min_horizon_months: 3, max_horizon_months: 24,
  lookback_months: 12, start_date: null, end_date: null, seed: null,
};
const runId = (number = 1) => number.toString(16).padStart(24, '0');
const reportRoute = (id = runId()) => `#/calibration/${encodeURIComponent(id)}`;
const cloneRoute = (id = runId()) => `#/calibration/new?from=${encodeURIComponent(id)}`;
const reportPath = (id = runId()) => `/goals/calibrations/${encodeURIComponent(id)}`;
const historyPath = (offset = 0) => `/goals/calibrations?limit=20&offset=${offset}`;
function generatedCalibration(body = expectedBody, sampleCount = body.scenario_count, perfect = false) {
  const { strategy, seed, ...settings } = structuredClone(body);
  const date = (month) => new Date(Date.UTC(2023, month, 1)).toISOString().slice(0, 10);
  const forecasts = Array.from({ length: sampleCount }, (_, index) => {
    const month = index % 30;
    const horizon = body.min_horizon_months + index % (body.max_horizon_months - body.min_horizon_months + 1);
    const success = perfect || index % 2 === 0;
    return { scenario_id: `scenario-${index + 1}`, origin_date: date(month),
      training_start_date: date(month - body.lookback_months), training_end_date: date(month),
      goal_date: date(month + horizon), valuation_date: date(month + horizon), training_return_count: 252, price_basis: 'adj_close',
      goal_eur: '15000.00', starting_capital_eur: `${10000 + index}.00`, monthly_contribution_eur: `${index % 1001}.00`,
      horizon_months: horizon, requested_probability: Math.min(0.99, 0.01 + 0.98 * index / Math.max(1, sampleCount - 1)),
      predicted_probability: perfect ? 1 : 0.25, observed_success: success, terminal_value_eur: success ? '16000.00' : '12000.00' };
  });
  const observed = forecasts.filter((forecast) => forecast.observed_success).length / sampleCount;
  const skipped = body.scenario_count - sampleCount;
  return {
    id: runId(), strategy_name: strategy.id === pair.id ? pair.name : buyAndHold.name,
    strategy, settings, seed: seed ?? 42, simulation_paths: 10000, method: 'walk_forward', calculated_at: '2026-09-23T12:00:00Z',
    data_as_of: '2026-09-23T11:00:00+00:00', history_first_date: '2021-01-01', history_last_date: '2026-09-22',
    history_fingerprint: 'saved-history-fingerprint', candidate_count: body.scenario_count, sample_count: sampleCount,
    available_origin_count: 36, origin_count: Math.min(30, sampleCount),
    generation: { method: 'training_quantiles_independent_streams', target_simulation_paths: 10000,
      capital_min_eur: '1000.00', capital_max_eur: '100000.00', monthly_min_eur: '0.00', monthly_max_eur: '1000.00',
      probability_min: 0.01, probability_max: 0.99 },
    skipped_counts: skipped ? { insufficient_training: skipped } : {},
    excluded_origins: Array.from({ length: skipped }, (_, index) => ({ scenario_id: `scenario-${sampleCount + index + 1}`, origin_date: '2022-12-01', reason: 'insufficient_training' })),
    forecasts, brier_score: perfect ? 0 : observed * 0.75 ** 2 + (1 - observed) * 0.25 ** 2,
    calibration_error: perfect ? 0 : Math.abs(observed - 0.25), mean_predicted_probability: perfect ? 1 : 0.25, observed_success_rate: observed,
    calibration_bins: Array.from({ length: 10 }, (_, index) => {
      const populated = index === (perfect ? 9 : 2);
      return { lower: index / 10, upper: (index + 1) / 10, count: populated ? sampleCount : 0,
        mean_predicted_probability: populated ? perfect ? 1 : 0.25 : null, observed_success_rate: populated ? observed : null };
    }),
    warnings: ['Scenarios sharing historical origins and overlapping outcomes are dependent.'],
  };
}
function summary(data) {
  const { id, strategy_name, strategy, settings, seed, calculated_at, brier_score, calibration_error, sample_count, origin_count } = data;
  return structuredClone({ id, strategy_name, strategy, settings, seed, calculated_at, brier_score, calibration_error, sample_count, origin_count });
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function mockServer() {
  let nextId = 1;
  const server = {
    definitions: [buyAndHold], runs: new Map(),
    acquiredItems: [{ instrument_id: A, name: 'World &amp; Europe ETF' }, { instrument_id: B, name: 'Second ETF' }],
  };
  // Each boundary returns a fresh object, as JSON would. Editing a clone must not mutate stored reports.
  server.save = (data = generatedCalibration()) => {
    while (server.runs.has(runId(nextId))) nextId += 1;
    const result = structuredClone({ ...data, id: runId(nextId++) });
    server.runs.set(result.id, structuredClone(result));
    return result;
  };
  server.strategies = async () => ({ items: structuredClone(server.definitions) });
  server.history = async (query) => {
    const offset = Number(query.get('offset')), limit = Number(query.get('limit'));
    const runs = [...server.runs.values()].sort((a, b) => b.calculated_at.localeCompare(a.calculated_at) || b.id.localeCompare(a.id));
    return { items: runs.slice(offset, offset + limit).map(summary), total: runs.length, offset, limit };
  };
  server.report = async (id) => {
    if (!server.runs.has(id)) throw new Error('Calibration run not found.');
    return { result: structuredClone(server.runs.get(id)) };
  };
  server.calibrate = async (body) => server.save({ ...generatedCalibration(body),
    strategy_name: server.definitions.find((item) => item.id === body.strategy.id)?.name || body.strategy.id });
  server.acquired = async (query) => {
    const offset = Number(query.get('offset')), limit = Number(query.get('limit'));
    return { items: structuredClone(server.acquiredItems.slice(offset, offset + limit)), total: server.acquiredItems.length, offset, limit };
  };
  request.mockImplementation(async (path, options = {}) => {
    if (path === '/customers?limit=20&offset=0') return { items: [], total: 0, limit: 20, offset: 0 };
    if (path === '/goals/strategies') return server.strategies(options);
    if (path.startsWith('/instruments/acquired?')) return server.acquired(new URL(path, 'http://localhost').searchParams, options);
    if (path === '/goals/calibrate') return server.calibrate(options.body, options);
    if (path.startsWith('/goals/calibrations?')) return server.history(new URL(path, 'http://localhost').searchParams, options);
    if (path.startsWith('/goals/calibrations/')) return server.report(decodeURIComponent(path.split('/')[3]), options);
    throw new Error(`Unexpected request: ${path}`);
  });
  return server;
}
const posts = () => request.mock.calls.filter(([, options]) => options?.method === 'POST');
const reads = (path) => request.mock.calls.filter(([url]) => url === path);
const button = () => screen.getByRole('button', { name: 'Run calibration' });
const result = () => screen.queryByRole('region', { name: 'Calibration report' });
const historyTable = () => screen.getByRole('table', { name: 'Calibration runs' });
const historyLinks = () => within(historyTable()).getAllByRole('link').map((link) => link.getAttribute('href'));
async function flush() { await act(async () => {}); }
async function tick() { await act(async () => { await vi.advanceTimersByTimeAsync(5); }); }
async function open({ route = '#/calibration/new', choose = false, props = null } = {}) {
  window.history.replaceState(null, '', route);
  const view = render(props ? <StrategyCalibration {...props} /> : <InstrumentWorkspace />);
  await flush();
  if (choose) { change('Instrument', A); await flush(); }
  return view;
}
async function submit() { fireEvent.click(button()); await flush(); await tick(); }
function change(label, value) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
async function navigate(hash) {
  await act(async () => { window.history.pushState(null, '', hash); window.dispatchEvent(new Event('hashchange')); });
}
async function follow(name) { fireEvent.click(screen.getByRole('link', { name })); await tick(); }
function metric(label) { return within(within(result()).getByText(label, { selector: 'dt' }).closest('div')).getByRole('definition'); }
function openSettings() { fireEvent.click(screen.getByText('Settings', { selector: 'summary' })); }
function openAudit() { fireEvent.click(screen.getByText('Calculation audit')); }
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-23T12:00:00Z')); request.mockReset();
  auth.user = { id: 'user-a' }; auth.status = 'authenticated'; delete auth.request;
  window.history.replaceState(null, '', '#/');
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); window.history.replaceState(null, '', '#/'); });

describe('immutable calibration history and routes', () => {
  it.each(['#/calibration', '#/strategies'])('lists saved runs, not strategies, at %s without catalogue or acquired reads', async (route) => {
    const server = mockServer();
    const first = server.save();
    const second = server.save({ ...generatedCalibration({ ...expectedBody, strategy: { ...selection, parameters: { instrument: B } }, seed: 7 }),
      calculated_at: '2026-09-23T13:00:00Z' });
    server.definitions = []; server.acquiredItems = [];
    await open({ route });
    expect(screen.getByRole('heading', { name: 'Calibration', exact: true })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Calibration', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'ETFs' })).not.toHaveAttribute('aria-current');
    const table = within(historyTable());
    expect(table.getAllByRole('row')).toHaveLength(3);
    for (const name of ['Run date', 'Strategy', 'Parameters', 'Brier score', 'Calibration error', 'Scored scenarios']) {
      expect(table.getByRole('columnheader', { name })).toBeInTheDocument();
    }
    expect(table.getAllByText(buyAndHold.name)).toHaveLength(2);
    expect(historyLinks()).toEqual([reportRoute(second.id), reportRoute(first.id)]);
    const row = table.getAllByRole('row')[1];
    expect(row).toHaveTextContent(`instrument: ${B}`); expect(row).toHaveTextContent('0.3125');
    expect(row).toHaveTextContent('25.0 pp'); expect(row).toHaveTextContent('1,000');
    expect(row.querySelector('time')).toHaveAttribute('datetime', second.calculated_at);
    expect(screen.getByRole('link', { name: 'New run' })).toHaveAttribute('href', '#/calibration/new');
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(request.mock.calls.map(([path]) => path)).toEqual([historyPath()]);
    expect(posts()).toHaveLength(0);
  });

  it('offers an explicit first run from empty history and preserves customer routing', async () => {
    mockServer(); await open({ route: '#/calibration' });
    expect(screen.getByText(/No calibration runs yet/)).toBeInTheDocument(); expect(screen.queryByRole('table')).not.toBeInTheDocument();
    await follow('New run'); expect(screen.getByRole('heading', { name: 'New calibration run' })).toBeInTheDocument();
    expect(screen.getByLabelText('Strategy')).toHaveValue(BUY_AND_HOLD_ID); expect(button()).toBeDisabled();
    await navigate('#/goal'); expect(screen.getByRole('article', { name: 'Customers' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add customer' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Customer name')).not.toBeInTheDocument();
    expect(screen.getByText(/No customers yet/)).toBeInTheDocument();
    await follow('Calibration'); expect(screen.getByText(/No calibration runs yet/)).toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it('keeps both identical submissions as distinct reports and refreshes/reopens without POST', async () => {
    const server = mockServer(); const view = await open({ choose: true }); change('Seed (optional)', '42');
    await submit(); const first = structuredClone([...server.runs.values()][0]);
    expect(window.location.hash).toBe(reportRoute(first.id)); expect(result()).toBeInTheDocument();
    await follow('Run again'); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('42'); expect(posts()).toHaveLength(1);
    await submit(); const second = [...server.runs.values()][1];
    expect(second.id).not.toBe(first.id); expect(second.strategy).toEqual(first.strategy); expect(second.settings).toEqual(first.settings);
    expect(posts()[0][1].body).toEqual(posts()[1][1].body); expect(server.runs.get(first.id)).toEqual(first);
    await follow(/Calibration history/); expect(historyLinks()).toEqual([reportRoute(second.id), reportRoute(first.id)]);
    const catalogueReads = reads('/goals/strategies').length;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await flush();
    expect(historyLinks()).toEqual([reportRoute(second.id), reportRoute(first.id)]);
    fireEvent.click(within(historyTable()).getAllByRole('link')[1]); await tick();
    expect(window.location.hash).toBe(reportRoute(first.id)); expect(result()).toHaveTextContent('Seed 42');
    view.unmount(); await open({ route: reportRoute(first.id) });
    expect(result()).toBeInTheDocument(); expect(reads(reportPath(first.id))).toHaveLength(4);
    expect(reads('/goals/strategies')).toHaveLength(catalogueReads); expect(posts()).toHaveLength(2);
  });

  it('paginates compact history by 20, refreshes to page one, and does not fetch audit reports', async () => {
    const server = mockServer();
    for (let index = 0; index < 41; index += 1) server.save(generatedCalibration({ ...expectedBody, scenario_count: 10, seed: index }));
    await open({ route: '#/calibration' });
    const pages = () => within(screen.getByRole('navigation', { name: 'Calibration history pagination' }));
    expect(within(historyTable()).getAllByRole('row')).toHaveLength(21); expect(pages().getByText('1–20 of 41')).toBeInTheDocument();
    expect(pages().getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(pages().getByRole('button', { name: 'Next' })); await flush();
    expect(pages().getByText('21–40 of 41')).toBeInTheDocument(); expect(historyLinks()[0]).toBe(reportRoute(runId(21)));
    fireEvent.click(pages().getByRole('button', { name: 'Next' })); await flush();
    expect(pages().getByText('41–41 of 41')).toBeInTheDocument(); expect(pages().getByRole('button', { name: 'Next' })).toBeDisabled();
    expect(historyLinks()).toEqual([reportRoute(runId(1))]);
    fireEvent.click(pages().getByRole('button', { name: 'Previous' })); await flush();
    expect(pages().getByText('21–40 of 41')).toBeInTheDocument();
    server.save(generatedCalibration({ ...expectedBody, scenario_count: 10 }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await flush();
    expect(pages().getByText('1–20 of 42')).toBeInTheDocument(); expect(historyLinks()[0]).toBe(reportRoute(runId(42)));
    expect(request.mock.calls.map(([path]) => path)).toEqual([historyPath(), historyPath(20), historyPath(40), historyPath(20), historyPath()]);
    expect(posts()).toHaveLength(0);
  });

  it('decodes a legacy strategy route as the initial new-run selection, with generic parameters', async () => {
    const server = mockServer(); server.definitions = [buyAndHold, pair];
    await open({ route: `#/strategies/${encodeURIComponent(pair.id)}` });
    expect(screen.getByRole('heading', { name: 'New calibration run' })).toBeInTheDocument();
    expect(screen.getByLabelText('Strategy')).toHaveValue(pair.id);
    expect(screen.getByLabelText('primary instrument')).toHaveValue(''); expect(screen.getByLabelText('secondary instrument')).toHaveValue('');
    change('primary instrument', A); await flush(); expect(button()).toBeDisabled();
    change('secondary instrument', B); await flush(); expect(button()).toBeEnabled(); expect(posts()).toHaveLength(0);
    await submit(); expect(posts()[0][1].body.strategy).toEqual({ id: pair.id, parameters: { primary: A, secondary: B } });
    expect(result()).toHaveTextContent(pair.name); expect(result()).toHaveTextContent(`primary: ${A}; secondary: ${B}`);
  });

  it('lets an unknown legacy strategy select an available replacement rather than silently submitting it', async () => {
    mockServer(); await open({ route: '#/strategies/retired' });
    expect(screen.getByRole('alert')).toHaveTextContent('no longer available'); expect(button()).toBeDisabled();
    expect(screen.getByLabelText('Strategy')).toHaveValue('');
    change('Strategy', BUY_AND_HOLD_ID); change('Instrument', A); await flush();
    expect(button()).toBeEnabled(); expect(posts()).toHaveLength(0);
    expect(request.mock.calls.map(([path]) => path)).toEqual(['/goals/strategies', '/instruments/acquired?limit=100&offset=0']);
  });
});

describe('read-only saved reports and explicit editable clones', () => {
  it.each([runId(), `legacy-${'ab'.repeat(32)}`])('opens/reloads saved run %s without catalogue, acquired data or POST', async (id) => {
    const server = mockServer();
    const saved = { ...generatedCalibration({ ...expectedBody, strategy: { ...selection, parameters: { instrument: B } },
      seed: 0, scenario_count: 20, min_horizon_months: 6, max_horizon_months: 36, lookback_months: 24, start_date: '2023-01-01', end_date: '2025-01-01' }), id };
    server.runs.set(id, saved); server.definitions = []; server.acquiredItems = [];
    const view = await open({ route: reportRoute(id) });
    expect(screen.getByRole('heading', { name: 'Calibration report' })).toBeInTheDocument();
    expect(result()).toHaveTextContent(`instrument: ${B}`);
    expect(result()).toHaveTextContent('20 scenarios · 6–36 month horizons · 24 month lookback · Seed 0');
    expect(result()).toHaveTextContent('Origins: 2023-01-01 – 2025-01-01');
    expect(screen.getByRole('link', { name: 'Run again' })).toHaveAttribute('href', cloneRoute(id));
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run calibration' })).not.toBeInTheDocument();
    openAudit(); expect(metric('Run ID')).toHaveTextContent(id); expect(metric('Strategy ID')).toHaveTextContent(BUY_AND_HOLD_ID);
    view.unmount(); await open({ route: reportRoute(id) });
    expect(result()).toBeInTheDocument(); expect(request.mock.calls.map(([path]) => path)).toEqual([reportPath(id), reportPath(id)]);
  });

  it('clones every saved setting without POST, permits edits, and leaves the original immutable after success', async () => {
    const server = mockServer();
    const original = server.save(generatedCalibration({ ...expectedBody, seed: 0, scenario_count: 20,
      min_horizon_months: 6, max_horizon_months: 36, lookback_months: 24, start_date: '2023-01-01', end_date: '2025-01-01' }));
    await open({ route: reportRoute(original.id) }); await follow('Run again');
    expect(window.location.hash).toBe(cloneRoute(original.id)); expect(result()).not.toBeInTheDocument();
    expect(screen.getByText(/original report will not change/)).toBeInTheDocument();
    for (const [label, value] of [['Strategy', BUY_AND_HOLD_ID], ['Instrument', A], ['Seed (optional)', '0'],
      ['Forecast-origin start date (optional)', '2023-01-01'], ['Forecast-origin end date (optional)', '2025-01-01']]) {
      expect(screen.getByLabelText(label)).toHaveValue(value);
    }
    for (const [label, value] of [['Number of scenarios', 20], ['Minimum horizon (months)', 6], ['Maximum horizon (months)', 36], ['Training lookback (months)', 24]]) {
      expect(screen.getByLabelText(label)).toHaveValue(value);
    }
    expect(button()).toBeEnabled(); expect(posts()).toHaveLength(0);
    openSettings(); change('Instrument', B); change('Seed (optional)', '7'); change('Number of scenarios', '10');
    change('Minimum horizon (months)', '3'); change('Maximum horizon (months)', '24'); change('Training lookback (months)', '12');
    change('Forecast-origin start date (optional)', ''); change('Forecast-origin end date (optional)', ''); await flush();
    expect(server.runs.get(original.id)).toEqual(original); expect(posts()).toHaveLength(0);
    await submit(); const next = [...server.runs.values()][1];
    expect(posts()[0][1].body).toEqual({ ...expectedBody, strategy: { ...selection, parameters: { instrument: B } }, scenario_count: 10, seed: 7 });
    expect(window.location.hash).toBe(reportRoute(next.id)); expect(result()).toHaveTextContent('Seed 7'); expect(result()).toHaveTextContent(B);
    expect(server.runs.get(original.id)).toEqual(original);
    await navigate(reportRoute(original.id)); expect(result()).toHaveTextContent('Seed 0'); expect(result()).toHaveTextContent(A);
    expect(result()).toHaveTextContent('20 scenarios · 6–36 month horizons · 24 month lookback'); expect(posts()).toHaveLength(1);
  });

  it('directly reopens a clone URL without calculating and retains its source link', async () => {
    const server = mockServer(); const saved = server.save();
    const view = await open({ route: cloneRoute(saved.id) });
    expect(screen.getByLabelText('Instrument')).toHaveValue(A); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('42');
    expect(screen.getByRole('link', { name: 'saved run' })).toHaveAttribute('href', reportRoute(saved.id));
    view.unmount(); await open({ route: cloneRoute(saved.id) });
    expect(screen.getByLabelText('Seed (optional)')).toHaveValue('42'); expect(reads(reportPath(saved.id))).toHaveLength(2); expect(posts()).toHaveLength(0);
  });

  it('renders retired strategy snapshots and requires a current strategy only when cloning', async () => {
    const server = mockServer(); const retired = server.save({ ...generatedCalibration({ ...expectedBody,
      strategy: { id: 'retired/strategy', parameters: { former_instrument: A } }, seed: 77 }), strategy_name: 'Retired strategy snapshot' });
    server.acquiredItems = [];
    await open({ route: reportRoute(retired.id) });
    expect(result()).toHaveTextContent('Retired strategy snapshot'); expect(result()).toHaveTextContent(`former instrument: ${A}`);
    expect(request.mock.calls.map(([path]) => path)).toEqual([reportPath(retired.id)]);
    await follow('Run again'); expect(screen.getByRole('alert')).toHaveTextContent('no longer available'); expect(button()).toBeDisabled();
    expect(screen.getByLabelText('Seed (optional)')).toHaveValue('77'); expect(screen.getByLabelText('Strategy')).toHaveValue('');
    change('Strategy', BUY_AND_HOLD_ID); await flush();
    expect(screen.getByLabelText('Instrument')).toHaveValue(''); expect(screen.getByText(/No acquired instruments yet/)).toBeInTheDocument();
    expect(button()).toBeDisabled(); expect(server.runs.get(retired.id)).toEqual(retired); expect(posts()).toHaveLength(0);
  });

  it('uses the stored strategy name instead of a renamed catalogue entry', async () => {
    const server = mockServer(); const original = server.save(); server.definitions = [{ ...buyAndHold, name: 'Renamed strategy' }];
    await open({ route: reportRoute(original.id) }); expect(result()).toHaveTextContent(buyAndHold.name);
    await follow('Run again'); expect(screen.getByRole('option', { name: 'Renamed strategy' })).toBeInTheDocument();
    await submit(); expect(result()).toHaveTextContent('Renamed strategy'); expect(server.runs.get(original.id).strategy_name).toBe(buyAndHold.name);
  });
});

describe('new-run readiness, parameters, seeds and failures', () => {
  it('starts blank with generated defaults and loads no saved report or full-history statistics', async () => {
    mockServer(); await open(); expect(button()).toBeDisabled(); expect(screen.getByLabelText('Instrument')).toHaveValue('');
    expect(screen.getByLabelText('Strategy')).toHaveValue(BUY_AND_HOLD_ID);
    expect(screen.getByLabelText('Number of scenarios')).toHaveValue(1000); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('');
    for (const label of ['Evaluation mode', 'Target (EUR)', 'Starting capital (EUR)', 'Monthly contribution (EUR)', 'Forecast horizon (months)']) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }
    expect(screen.queryByText('Instrument parameter · acquired universe only')).not.toBeInTheDocument();
    expect(screen.getByText('Settings', { selector: 'summary' }).closest('details')).not.toHaveAttribute('open');
    change('Instrument', A); await flush(); expect(button()).toBeEnabled(); expect(posts()).toHaveLength(0);
    expect(screen.getByRole('option', { name: `World & Europe ETF · ${A}` })).toBeInTheDocument();
    expect(request.mock.calls.map(([path]) => path)).toEqual(['/goals/strategies', '/instruments/acquired?limit=100&offset=0']);
    await submit(); expect(posts()[0]).toEqual(['/goals/calibrate', expect.objectContaining({ method: 'POST', body: expectedBody, signal: expect.any(AbortSignal) })]);
    expect(window.location.hash).toBe(reportRoute()); expect(result()).toHaveTextContent('Seed 42');
    await follow('Run again'); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('42');
    await submit(); expect(posts()[1][1].body).toEqual({ ...expectedBody, seed: 42 });
  });

  it('resets all parameters/readiness on strategy changes but keeps common settings', async () => {
    const server = mockServer(); server.definitions = [buyAndHold, pair]; await open({ choose: true });
    change('Number of scenarios', '10'); change('Seed (optional)', '9'); change('Strategy', pair.id); await flush();
    expect(screen.queryByLabelText('Instrument')).not.toBeInTheDocument(); expect(button()).toBeDisabled();
    expect(screen.getByLabelText('primary instrument')).toHaveValue(''); expect(screen.getByLabelText('secondary instrument')).toHaveValue('');
    change('primary instrument', A); await flush(); expect(button()).toBeDisabled(); change('secondary instrument', B); await flush();
    expect(button()).toBeEnabled(); change('Strategy', BUY_AND_HOLD_ID); await flush();
    expect(screen.getByLabelText('Instrument')).toHaveValue(''); expect(button()).toBeDisabled();
    change('Strategy', pair.id); await flush(); expect(screen.getByLabelText('primary instrument')).toHaveValue('');
    expect(screen.getByLabelText('Number of scenarios')).toHaveValue(10); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('9');
    change('primary instrument', B); change('secondary instrument', A); await flush(); await submit();
    expect(posts()[0][1].body.strategy).toEqual({ id: pair.id, parameters: { primary: B, secondary: A } }); expect(posts()).toHaveLength(1);
  });

  it('disables every editable field while busy and blocks duplicate click/form submissions', async () => {
    const server = mockServer(), pending = deferred(); server.calibrate = () => pending.promise;
    await open({ choose: true }); openSettings(); await submit();
    const form = screen.getByRole('form', { name: 'Calibration inputs' });
    for (const field of form.querySelectorAll('input, select, button')) expect(field).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('run may still finish and appear in history');
    expect(result()).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Calculating…' })); fireEvent.submit(form); await flush(); expect(posts()).toHaveLength(1);
    await act(async () => pending.resolve(server.save(generatedCalibration(posts()[0][1].body)))); await tick();
    expect(result()).toBeInTheDocument(); expect(window.location.hash).toBe(reportRoute()); expect(server.runs.size).toBe(1);
  });

  it.each(['offline', 'Insufficient historical goal outcomes.', 'Session expired.', 'Not authorized.'])('keeps the original and creates no history after %s; retries only explicitly', async (message) => {
    const server = mockServer(); const original = server.save(); server.calibrate = async () => { throw new Error(message); };
    await open({ route: cloneRoute(original.id) }); change('Instrument', B); change('Seed (optional)', '7'); await flush(); await submit();
    expect(screen.getByRole('alert')).toHaveTextContent(message); expect(button()).toBeEnabled(); expect(result()).not.toBeInTheDocument();
    expect(screen.getByLabelText('Instrument')).toHaveValue(B); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('7');
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); }); expect(posts()).toHaveLength(1); expect(server.runs.size).toBe(1);
    await follow(/Calibration history/); expect(historyLinks()).toEqual([reportRoute(original.id)]);
    await navigate(cloneRoute(original.id)); server.calibrate = async (body) => server.save(generatedCalibration(body, body.scenario_count, true));
    await submit(); expect(metric('Calibration error')).toHaveTextContent('0.0 pp'); expect(server.runs.size).toBe(2);
    expect(server.runs.get(original.id)).toEqual(original); expect(posts()).toHaveLength(2);
  });

  it.each([false, undefined])('blocks submission without calibration support (%s)', async (supports_calibration) => {
    const server = mockServer(); server.definitions = [{ ...buyAndHold, supports_calibration }]; await open({ choose: true });
    expect(screen.getByText('Calibration is not available for this strategy.')).toBeInTheDocument(); expect(button()).toBeDisabled();
    fireEvent.submit(screen.getByRole('form', { name: 'Calibration inputs' })); expect(posts()).toHaveLength(0);
  });

  it('chooses the first calibration-capable strategy by default and blocks unsupported parameter editors', async () => {
    const server = mockServer(); server.definitions = [{ ...buyAndHold, supports_calibration: false }, { ...pair,
      parameters: [{ key: 'allocation', type: 'number', label: 'Allocation', required: true }] }];
    await open(); expect(screen.getByLabelText('Strategy')).toHaveValue(pair.id);
    expect(screen.getByRole('alert')).toHaveTextContent('not supported'); expect(button()).toBeDisabled(); expect(posts()).toHaveLength(0);
  });

  it('blocks an empty acquired universe and removed clone instruments without preventing report reads', async () => {
    const server = mockServer(); const original = server.save(); server.acquiredItems = [];
    await open(); expect(screen.getByText(/No acquired instruments yet/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add an ETF' })).toHaveAttribute('href', '#/add'); expect(button()).toBeDisabled();
    await navigate(reportRoute(original.id)); expect(result()).toHaveTextContent(A);
    await follow('Run again'); expect(screen.getByRole('alert')).toHaveTextContent('not in the acquired universe'); expect(button()).toBeDisabled();
    expect(screen.getByLabelText('Instrument')).toHaveValue(''); expect(server.runs.get(original.id)).toEqual(original); expect(posts()).toHaveLength(0);
  });

  it('loads every acquired instrument page without requesting full-history statistics', async () => {
    const server = mockServer(); server.acquiredItems = Array.from({ length: 101 }, (_, index) => ({ instrument_id: `IE${String(index).padStart(10, '0')}`, name: `Fund ${index}` }));
    await open(); expect(within(screen.getByLabelText('Instrument')).getAllByRole('option')).toHaveLength(102);
    expect(reads('/instruments/acquired?limit=100&offset=100')).toHaveLength(1);
    change('Instrument', server.acquiredItems[100].instrument_id); await flush(); await submit();
    expect(posts()[0][1].body.strategy.parameters.instrument).toBe(server.acquiredItems[100].instrument_id); expect(result()).toBeInTheDocument();
    expect(request.mock.calls.some(([path]) => path.endsWith('/statistics'))).toBe(false);
  });

  it.each(['0', '4294967295'])('accepts and clones custom uint32 seed %s', async (seed) => {
    mockServer(); await open({ choose: true }); change('Seed (optional)', seed); await submit();
    expect(posts()[0][1].body.seed).toBe(Number(seed)); await follow('Run again');
    expect(screen.getByLabelText('Seed (optional)')).toHaveValue(seed); await submit(); expect(posts()[1][1].body.seed).toBe(Number(seed));
  });

  it('generates zero without changing the source, and blank seed requests server generation', async () => {
    const server = mockServer(); const original = server.save(); await open({ route: cloneRoute(original.id) });
    const random = vi.spyOn(crypto, 'getRandomValues').mockImplementation((array) => { array[0] = 0; return array; });
    fireEvent.click(screen.getByRole('button', { name: 'Generate seed' })); expect(random).toHaveBeenCalledWith(expect.any(Uint32Array));
    expect(random.mock.calls[0][0]).toHaveLength(1); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('0');
    expect(server.runs.get(original.id)).toEqual(original); expect(posts()).toHaveLength(0); await submit(); expect(posts()[0][1].body.seed).toBe(0);
    await follow('Run again'); change('Seed (optional)', ''); await submit(); expect(posts()[1][1].body.seed).toBeNull(); expect(result()).toHaveTextContent('Seed 42');
  });

  it('reports crypto failure without modifying clone inputs or its saved source', async () => {
    const server = mockServer(); const original = server.save(); await open({ route: cloneRoute(original.id) });
    vi.spyOn(crypto, 'getRandomValues').mockImplementation(() => { throw new Error('unavailable'); });
    fireEvent.click(screen.getByRole('button', { name: 'Generate seed' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not generate a seed'); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('42');
    expect(server.runs.get(original.id)).toEqual(original); expect(posts()).toHaveLength(0);
  });
});

describe('read failures and strict API reply validation', () => {
  it('loads history, retries a read failure explicitly, and never converts an error to empty history', async () => {
    const server = mockServer(), pending = deferred(); server.history = () => pending.promise;
    await open({ route: '#/calibration' }); expect(screen.getByRole('status')).toHaveTextContent('Loading calibration history');
    await act(async () => pending.reject(new Error('offline')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load calibration history: offline');
    expect(screen.queryByText(/No calibration runs yet/)).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); }); expect(reads(historyPath())).toHaveLength(1);
    server.history = async () => ({ items: [], total: 0, limit: 20, offset: 0 });
    fireEvent.click(screen.getByRole('button', { name: 'Retry history' })); await flush();
    expect(screen.getByText(/No calibration runs yet/)).toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it('retries a failed later history page at its original offset without retaining misleading rows', async () => {
    const server = mockServer(); for (let i = 0; i < 21; i += 1) server.save(generatedCalibration({ ...expectedBody, scenario_count: 10 }));
    const history = server.history; server.history = (query) => query.get('offset') === '20' ? Promise.reject(new Error('page offline')) : history(query);
    await open({ route: '#/calibration' }); fireEvent.click(screen.getByRole('button', { name: 'Next' })); await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('page offline'); expect(screen.queryByRole('table')).not.toBeInTheDocument();
    server.history = history; fireEvent.click(screen.getByRole('button', { name: 'Retry history' })); await flush();
    expect(screen.getByText('21–21 of 21')).toBeInTheDocument(); expect(reads(historyPath(20))).toHaveLength(2);
    expect(historyLinks()).toEqual([reportRoute(runId(1))]); expect(posts()).toHaveLength(0);
  });

  it.each([
    ['missing envelope', () => null], ['old array response', (data) => data.items],
    ['missing total', (data) => { delete data.total; return data; }],
    ['wrong limit', (data) => ({ ...data, limit: 100 })], ['wrong offset', (data) => ({ ...data, offset: 20 })],
    ['incomplete page', (data) => ({ ...data, total: 2 })], ['duplicate IDs', (data) => ({ ...data, total: 2, items: [data.items[0], data.items[0]] })],
    ['missing ID', (data) => { delete data.items[0].id; return data; }],
    ['invalid ID', (data) => { data.items[0].id = 'run-1'; return data; }],
    ['missing name snapshot', (data) => { delete data.items[0].strategy_name; return data; }],
    ['missing ECE', (data) => { delete data.items[0].calibration_error; return data; }],
  ])('rejects history with %s and offers retry', async (_, mutate) => {
    const server = mockServer(); server.history = async () => mutate({ items: [summary(generatedCalibration())], total: 1, limit: 20, offset: 0 });
    await open({ route: '#/calibration' }); expect(screen.getByRole('alert')).toHaveTextContent('unexpected calibration history');
    expect(screen.queryByRole('table')).not.toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Retry history' })).toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });

  it.each(['report', 'clone'])('shows a retryable saved-read failure in the %s route, not an empty report or fresh form', async (route) => {
    const server = mockServer(), pending = deferred(); const saved = server.save(); server.report = () => pending.promise;
    await open({ route: route === 'report' ? reportRoute(saved.id) : cloneRoute(saved.id) });
    expect(screen.getByRole('status')).toHaveTextContent(route === 'report' ? 'Loading calibration run' : 'Loading calibration settings');
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
    await act(async () => pending.reject(new Error('Calibration run not found.')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load calibration run: Calibration run not found.');
    expect(result()).not.toBeInTheDocument(); expect(screen.queryByRole('form')).not.toBeInTheDocument();
    server.report = async () => ({ result: structuredClone(saved) });
    fireEvent.click(screen.getByRole('button', { name: 'Retry run' })); await flush();
    if (route === 'report') expect(result()).toBeInTheDocument();
    else expect(screen.getByLabelText('Instrument')).toHaveValue(A);
    expect(posts()).toHaveLength(0);
  });

  it.each([
    ['missing wrapper', () => generatedCalibration()], ['null envelope', () => null], ['missing result', () => ({})],
    ['null result', () => ({ result: null })], ['undefined result', () => ({ result: undefined })], ['empty result', () => ({ result: {} })],
    ['mismatched run ID', () => ({ result: { ...generatedCalibration(), id: runId(2) } })],
    ['missing run ID', () => { const result = generatedCalibration(); delete result.id; return { result }; }],
    ['blank snapshot name', () => ({ result: { ...generatedCalibration(), strategy_name: ' ' } })],
    ['null ECE', () => ({ result: { ...generatedCalibration(), calibration_error: null } })],
    ['inconsistent audit', () => ({ result: { ...generatedCalibration(), forecasts: [] } })],
  ])('rejects a saved report with %s and allows retry', async (_, response) => {
    const server = mockServer(); server.report = async () => response(); await open({ route: reportRoute() });
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected saved run'); expect(result()).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry run' })).toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it('never hydrates clone inputs from a mismatched saved run ID', async () => {
    const server = mockServer(); server.report = async () => ({ result: { ...generatedCalibration(), id: runId(2) } });
    await open({ route: cloneRoute() }); expect(screen.getByRole('alert')).toHaveTextContent('unexpected saved run');
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['catalogue', 'acquired'])('keeps the form unavailable until %s loads, then allows an explicit retry', async (resource) => {
    const server = mockServer(), pending = deferred();
    const method = resource === 'catalogue' ? 'strategies' : 'acquired', load = server[method]; server[method] = () => pending.promise;
    await open(); expect(screen.getByRole('status')).toHaveTextContent('Loading calibration settings');
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); await act(async () => pending.reject(new Error('offline')));
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load strategies: offline');
    server[method] = load; fireEvent.click(screen.getByRole('button', { name: 'Retry strategies' })); await flush();
    expect(button()).toBeDisabled(); expect(posts()).toHaveLength(0);
  });

  it.each([
    ['invalid capability', (item) => { item.supports_calibration = 'yes'; }],
    ['duplicate parameter keys', (item) => { item.parameters.push({ ...item.parameters[0] }); }],
    ['missing parameter type', (item) => { delete item.parameters[0].type; }],
    ['malformed optional latest summary', (item) => { item.latest_calibration = { ...summary(generatedCalibration()), brier_score: '0' }; }],
  ])('fails closed for a new-run catalogue with %s', async (_, mutate) => {
    const server = mockServer(), item = structuredClone(buyAndHold); mutate(item); server.definitions = [item];
    await open(); expect(screen.getByRole('alert')).toHaveTextContent('unexpected strategy catalogue');
    expect(screen.queryByRole('form')).not.toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Retry strategies' })).toBeInTheDocument();
  });

  it.each([
    ['ECE mismatch', (data) => { data.calibration_error = 0; }], ['Brier mismatch', (data) => { data.brier_score = 0; }],
    ['seed mismatch', (data) => { data.seed = 99; }], ['settings mismatch', (data) => { data.settings.lookback_months = 99; }],
    ['instrument mismatch', (data) => { data.strategy = { ...selection, parameters: { instrument: B } }; }],
    ['strategy mismatch', (data) => { data.strategy.id = pair.id; }],
    ['missing timestamp', (data) => { delete data.calculated_at; }], ['missing audit', (data) => { data.forecasts = []; }],
    ['missing ID', (data) => { delete data.id; }], ['invalid ID', (data) => { data.id = 'saved-run'; }],
    ['missing snapshot name', (data) => { delete data.strategy_name; }], ['blank snapshot name', (data) => { data.strategy_name = ' '; }],
  ])('does not navigate or fabricate history when POST returns %s', async (_, mutate) => {
    const server = mockServer(); const original = server.save();
    server.calibrate = async (body) => { const data = generatedCalibration(body); mutate(data); return data; };
    await open({ route: cloneRoute(original.id) }); await submit();
    expect(screen.getByRole('alert')).toHaveTextContent('unexpected response. Check history before trying again');
    expect(window.location.hash).toBe(cloneRoute(original.id)); expect(button()).toBeEnabled(); expect(result()).not.toBeInTheDocument();
    expect(server.runs.get(original.id)).toEqual(original); expect(server.runs.size).toBe(1);
    await follow(/Calibration history/); expect(historyLinks()).toEqual([reportRoute(original.id)]); expect(posts()).toHaveLength(1);
  });

  it('lets a saved but malformed POST reply be reconciled from history rather than automatically resubmitted', async () => {
    const server = mockServer(); server.calibrate = async (body) => { const saved = server.save(generatedCalibration(body)); return { ...saved, forecasts: [] }; };
    await open({ choose: true }); await submit(); expect(screen.getByRole('alert')).toHaveTextContent('Check history before trying again');
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); }); expect(posts()).toHaveLength(1);
    await follow(/Calibration history/); expect(historyLinks()).toEqual([reportRoute()]);
    fireEvent.click(within(historyTable()).getByRole('link')); await tick(); expect(result()).toBeInTheDocument(); expect(posts()).toHaveLength(1);
  });
});

describe('input and pure contract validation', () => {
  it.each([
    ['Number of scenarios', '9', /10 to 5000/], ['Number of scenarios', '5001', /10 to 5000/],
    ['Number of scenarios', '10.5', /whole number/], ['Number of scenarios', '1e3', /whole number/], ['Number of scenarios', '', /whole number/],
    ['Minimum horizon (months)', '0', /1 to 120/], ['Minimum horizon (months)', '121', /1 to 120/],
    ['Minimum horizon (months)', '1.5', /whole number/], ['Maximum horizon (months)', '0', /1 to 120/],
    ['Maximum horizon (months)', '121', /1 to 120/], ['Maximum horizon (months)', '1e2', /whole number/],
    ['Training lookback (months)', '2', /3 to 120/], ['Training lookback (months)', '121', /3 to 120/],
    ['Seed (optional)', '-1', /0 to 4294967295/], ['Seed (optional)', '4294967296', /0 to 4294967295/],
    ['Seed (optional)', '1.5', /whole number/], ['Seed (optional)', '1e3', /whole number/], ['Seed (optional)', 'abc', /whole number/],
  ])('rejects %s = %s before POST', async (label, value, message) => {
    const server = mockServer(); await open({ choose: true }); openSettings(); change(label, value); await submit();
    expect(screen.getByRole('alert')).toHaveTextContent(message); expect(posts()).toHaveLength(0); expect(server.runs.size).toBe(0);
  });

  it('rejects reversed horizons/dates and accepts custom inclusive bounds', async () => {
    mockServer(); await open({ choose: true }); openSettings();
    change('Minimum horizon (months)', '25'); await submit(); expect(screen.getByRole('alert')).toHaveTextContent('on or below');
    change('Maximum horizon (months)', '120'); change('Training lookback (months)', '120'); change('Number of scenarios', '10');
    change('Forecast-origin start date (optional)', '2023-01-02'); change('Forecast-origin end date (optional)', '2023-01-01');
    await submit(); expect(screen.getByRole('alert')).toHaveTextContent('on or before'); expect(posts()).toHaveLength(0);
    change('Forecast-origin start date (optional)', '2023-01-01'); change('Seed (optional)', '0'); await submit();
    expect(posts()[0][1].body).toEqual({ ...expectedBody, scenario_count: 10, min_horizon_months: 25, max_horizon_months: 120,
      lookback_months: 120, start_date: '2023-01-01', end_date: '2023-01-01', seed: 0 }); expect(result()).toBeInTheDocument();
  });

  it('validates real calendar dates and refuses single mode independently of native inputs', () => {
    for (const start of ['2023-02-29', '2024-13-01', '0000-01-01', 'not a date']) {
      expect(() => calibrationRequest({ ...DEFAULT_CALIBRATION_INPUTS, start }, selection)).toThrow('valid forecast-origin dates');
    }
    expect(calibrationRequest({ ...DEFAULT_CALIBRATION_INPUTS, start: '2024-02-29' }, selection).start_date).toBe('2024-02-29');
    expect(() => calibrationRequest({ ...DEFAULT_CALIBRATION_INPUTS, mode: 'single' }, selection)).toThrow('Only generated');
  });

  it('round-trips saved zero seed and settings, keeping nullable date fields editable', () => {
    const saved = generatedCalibration({ ...expectedBody, seed: 0 });
    expect(savedCalibrationInputs(saved)).toEqual({ ...DEFAULT_CALIBRATION_INPUTS, seed: '0' });
    expect(calibrationRequest(savedCalibrationInputs(saved), saved.strategy)).toEqual({ ...expectedBody, seed: 0 });
    const dated = generatedCalibration({ ...expectedBody, seed: 4294967295, scenario_count: 10, start_date: '2024-02-29', end_date: '2025-01-01' });
    expect(calibrationRequest(savedCalibrationInputs(dated), dated.strategy)).toEqual({ ...expectedBody, seed: 4294967295,
      scenario_count: 10, start_date: '2024-02-29', end_date: '2025-01-01' });
  });

  it('validates ECE as the count-weighted bin gap, not an unweighted mean', () => {
    const data = generatedCalibration({ ...expectedBody, scenario_count: 10 });
    data.forecasts.forEach((forecast, index) => { forecast.predicted_probability = index === 0 ? 0.05 : 0.25; });
    data.calibration_bins[0] = { lower: 0, upper: 0.1, count: 1, mean_predicted_probability: 0.05, observed_success_rate: 1 };
    data.calibration_bins[2] = { lower: 0.2, upper: 0.3, count: 9, mean_predicted_probability: 0.25, observed_success_rate: 4 / 9 };
    data.mean_predicted_probability = 0.23; data.calibration_error = 0.1 * 0.95 + 0.9 * Math.abs(0.25 - 4 / 9);
    data.brier_score = data.forecasts.reduce((sum, row) => sum + (row.predicted_probability - Number(row.observed_success)) ** 2, 0) / 10;
    expect(validCalibrationResult(data)).toBe(true);
    data.calibration_error = (0.95 + Math.abs(0.25 - 4 / 9)) / 2; expect(validCalibrationResult(data)).toBe(false);
  });

  it('fails closed for malformed summaries and generated result fields/counts/metrics', () => {
    const body = { ...expectedBody, scenario_count: 10, seed: 42 }, valid = generatedCalibration(body, 9);
    expect(validCalibrationResult(valid, body, buyAndHold)).toBe(true); expect(validCalibrationSummary(summary(valid), buyAndHold)).toBe(true);
    const mutations = [
      ...Object.keys(valid.generation).map((key) => (data) => { data.generation[key] = null; }),
      ...Object.keys(valid.settings).map((key) => (data) => { data.settings[key] = 'mismatched'; }),
      (data) => { data.settings.goal_eur = '15000.00'; }, (data) => { data.seed = 43; }, (data) => { data.seed = -1; },
      (data) => { data.history_fingerprint = ''; }, (data) => { delete data.history_fingerprint; },
      (data) => { data.available_origin_count = 1; }, (data) => { data.available_origin_count = 10.5; },
      (data) => { data.origin_count = 8; }, (data) => { data.origin_count = '9'; },
      (data) => { data.candidate_count = 11; }, (data) => { data.skipped_counts.insufficient_training = 2; },
      (data) => { data.excluded_origins = []; }, (data) => { delete data.excluded_origins[0].scenario_id; },
      (data) => { data.excluded_origins[0].scenario_id = data.forecasts[0].scenario_id; },
      (data) => { data.excluded_origins[0].reason = 'unaccounted'; },
      ...['', 1, null].map((value) => (data) => { data.forecasts[0].scenario_id = value; }),
      (data) => { data.forecasts[1].scenario_id = data.forecasts[0].scenario_id; },
      ...['0.00', '-1', 'NaN', 15000, null].map((value) => (data) => { data.forecasts[0].goal_eur = value; }),
      ...['999.99', '100000.01', 1000].map((value) => (data) => { data.forecasts[0].starting_capital_eur = value; }),
      ...['-0.01', '1000.01', 0].map((value) => (data) => { data.forecasts[0].monthly_contribution_eur = value; }),
      ...[2, 25, 3.5, '3'].map((value) => (data) => { data.forecasts[0].horizon_months = value; }),
      ...[0, 1, '0.5', NaN].map((value) => (data) => { data.forecasts[0].requested_probability = value; }),
      (data) => { data.forecasts[0].predicted_probability = 1.1; }, (data) => { data.forecasts[0].terminal_value_eur = 'NaN'; },
      (data) => { data.forecasts[0].training_start_date = '2024-02-30'; }, (data) => { data.forecasts[0].price_basis = 'wrong'; },
      (data) => { data.strategy.parameters.instrument = B; }, (data) => { data.strategy = []; }, (data) => { data.method = 'in_sample'; },
      (data) => { data.simulation_paths = 3; }, (data) => { data.warnings = [{}]; }, (data) => { data.skipped_counts = { missing: -1 }; },
      ...[undefined, null, NaN, -1, 1.1, '0', 0].map((value) => (data) => { data.calibration_error = value; }),
      (data) => { data.brier_score = 0; }, (data) => { data.mean_predicted_probability = 0; }, (data) => { data.observed_success_rate = 0; },
      (data) => { data.calibration_bins.pop(); }, (data) => { data.calibration_bins[0].count = 2; },
      (data) => { data.calibration_bins[0].mean_predicted_probability = 0; }, (data) => { data.calibration_bins[2].lower = 0; },
      (data) => { data.calibration_bins[2].observed_success_rate = 2; }, (data) => { data.calibration_bins[2].mean_predicted_probability = 0.7; },
      (data) => { data.sample_count = 0; }, (data) => { data.forecasts = []; },
      ...[undefined, 'yesterday', '2026-02-30T00:00:00Z', '2026-01-01T25:00:00Z', '2026-01-01T01:00:00+01:00'].map((value) => (data) => { data.calculated_at = value; }),
    ];
    for (const mutate of mutations) { const data = structuredClone(valid); mutate(data); expect(validCalibrationResult(data, body, buyAndHold)).toBe(false); }
    for (const key of Object.keys(summary(valid)).filter((key) => !['id', 'strategy_name'].includes(key))) {
      const data = summary(valid); delete data[key]; expect(validCalibrationSummary(data, buyAndHold)).toBe(false);
    }
    const perfect = generatedCalibration(body, 10, true); expect(validCalibrationResult(perfect, body)).toBe(true);
    expect(validCalibrationSummary(summary(perfect), buyAndHold)).toBe(true);
  });

  it('accepts real run identifiers, snapshots and retired selections but rejects malformed summary fields', () => {
    const valid = summary(generatedCalibration()); expect(validCalibrationRunSummary(valid)).toBe(true);
    expect(validCalibrationRunSummary({ ...valid, id: `legacy-${'af'.repeat(32)}`, strategy: { id: 'retired', parameters: { old_parameter: A } } })).toBe(true);
    for (const id of [undefined, null, 1, '', 'run-1', 'A'.repeat(24), 'a'.repeat(23), 'a'.repeat(25), 'a'.repeat(64),
      `legacy-${'a'.repeat(63)}`, `legacy-${'A'.repeat(64)}`, '../secret', `${runId()}?from=x`]) {
      expect(validCalibrationRunSummary({ ...valid, id })).toBe(false);
    }
    for (const strategy_name of [undefined, null, 1, '', ' \t ']) expect(validCalibrationRunSummary({ ...valid, strategy_name })).toBe(false);
    for (const key of Object.keys(valid)) { const data = structuredClone(valid); delete data[key]; expect(validCalibrationRunSummary(data)).toBe(false); }
    for (const strategy of [null, [], { id: '', parameters: {} }, { id: 'retired', parameters: { instrument: null } }]) {
      expect(validCalibrationRunSummary({ ...valid, strategy })).toBe(false);
    }
  });

  it('validates exact paged history metadata, compact entries and uniqueness without requiring audit arrays', () => {
    const items = Array.from({ length: 20 }, (_, index) => ({ ...summary(generatedCalibration({ ...expectedBody, scenario_count: 10 })), id: runId(index + 1) }));
    const valid = { items, total: 21, offset: 0, limit: 20 };
    expect(items[0]).not.toHaveProperty('forecasts'); expect(items[0]).not.toHaveProperty('calibration_bins');
    expect(validCalibrationHistory(valid, 20, 0)).toBe(true);
    expect(validCalibrationHistory({ ...valid, items: [items[0]], offset: 20 }, 20, 20)).toBe(true);
    expect(validCalibrationHistory({ items: [], total: 0, offset: 0, limit: 20 }, 20, 0)).toBe(true);
    expect(validCalibrationHistory({ items: [], total: 21, offset: 40, limit: 20 }, 20, 40)).toBe(true);
    for (const data of [null, [], {}, { ...valid, items: {} }, { ...valid, items: items.slice(0, 19) }, { ...valid, total: 0 },
      { ...valid, items: [...items, items[0]] }, { ...valid, items: [items[0], ...items.slice(0, 19)] },
      ...[-1, 1.5, '21', undefined, NaN].map((total) => ({ ...valid, total })),
      { ...valid, limit: 10 }, { ...valid, limit: '20' }, { ...valid, offset: 20 }, { ...valid, offset: '0' }]) {
      expect(validCalibrationHistory(data, 20, 0)).toBe(false);
    }
  });
});

describe('saved report chart, metrics and bounded audit', () => {
  it('renders actual nonempty bins, scores and distinct origins; keeps audit/bins collapsed', async () => {
    const server = mockServer(); server.save(); await open({ route: reportRoute() });
    expect(metric('Brier score')).toHaveTextContent('0.3125'); expect(metric('Calibration error')).toHaveTextContent('25.0 pp');
    expect(metric('Scored scenarios')).toHaveTextContent('1,000'); expect(metric('Historical origins')).toHaveTextContent('30');
    const svg = screen.getByRole('img', { name: 'Financial goal success calibration curve' });
    expect(svg).toHaveAccessibleDescription(expect.stringContaining('0% to 100%'));
    expect(svg.querySelectorAll('circle')).toHaveLength(1); expect(svg.querySelector('circle')).toHaveTextContent('1000 scenarios; predicted 25%, observed 50%');
    expect(svg.querySelector('circle')).toHaveAttribute('cx', '211.5'); expect(svg.querySelector('circle')).toHaveAttribute('cy', '194');
    expect(svg.querySelector('.calibration-ideal')).toHaveAttribute('stroke-dasharray', '7 5');
    expect(screen.getByText('Calibration bins', { selector: 'summary' }).closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText('Calculation audit').closest('details')).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('Calibration bins', { selector: 'summary' }));
    const rows = within(screen.getByRole('table', { name: 'Calibration bins' })).getAllByRole('row');
    expect(rows).toHaveLength(11); expect(rows[1]).toHaveTextContent('0%–10%0No scenariosNo scenarios');
    expect(rows[3]).toHaveTextContent('20%–30%100025%50%'); expect(posts()).toHaveLength(0);
  });

  it('plots 100% in the final bin and preserves zero Brier and ECE values', async () => {
    const server = mockServer(); server.save(generatedCalibration(expectedBody, 1000, true)); await open({ route: reportRoute() });
    expect(metric('Brier score')).toHaveTextContent('0.0000'); expect(metric('Calibration error')).toHaveTextContent('0.0 pp');
    const circle = screen.getByRole('img', { name: 'Financial goal success calibration curve' }).querySelector('circle');
    expect(circle).toHaveAttribute('cx', '612'); expect(circle).toHaveAttribute('cy', '24');
  });

  it('accepts and labels split-adjusted saved calibration audits', async () => {
    const server = mockServer(); const saved = generatedCalibration({ ...expectedBody, scenario_count: 10 }, 9);
    saved.forecasts.forEach((forecast) => { forecast.price_basis = 'split_adjusted_close'; });
    server.save(saved); await open({ route: reportRoute() }); openAudit();
    expect(within(screen.getByRole('table', { name: 'Per-scenario audit' })).getAllByText('Split-adjusted close')).toHaveLength(9);
    expect(posts()).toHaveLength(0);
  });

  it('keeps audit identities, monetary values, origin dates and actual/requested probabilities distinct', async () => {
    const server = mockServer(); const saved = generatedCalibration({ ...expectedBody, scenario_count: 10 }, 9);
    saved.forecasts[0].price_basis = 'close'; server.save(saved); await open({ route: reportRoute() }); openAudit();
    for (const [label, value] of [['Mean predicted probability', '25%'], ['Observed success rate', '56%'],
      ['Monte Carlo paths per scenario', '10,000'], ['Available monthly origins', '36'],
      ['Saved history', '2021-01-01 – 2026-09-22'], ['Data snapshot (UTC)', saved.data_as_of],
      ['Target-generation paths per scenario', '10,000'], ['History fingerprint', saved.history_fingerprint]]) {
      expect(metric(label)).toHaveTextContent(value);
    }
    const table = within(screen.getByRole('table', { name: 'Per-scenario audit' }));
    expect(table.getAllByRole('columnheader').map((item) => item.textContent)).toEqual(['Scenario ID', 'Target (EUR)', 'Starting capital (EUR)',
      'Monthly contribution (EUR)', 'Horizon (months)', 'Requested probability', 'Forecast origin', 'Goal date', 'Training start', 'Training end',
      'Training returns', 'Price basis', 'Actual predicted probability', 'Observed success', 'Terminal value (EUR)', 'Valuation date']);
    const row = table.getByRole('rowheader', { name: 'scenario-1' }).closest('tr');
    const cells = within(row).getAllByRole('cell').map((cell) => cell.textContent);
    expect(cells.slice(0, 5)).toEqual(['€15,000.00', '€10,000.00', '€0.00', '3', '1%']);
    expect(cells.slice(5, 10)).toEqual(['2023-01-01', '2023-04-01', '2022-01-01', '2023-01-01', '252']);
    expect(cells.slice(10)).toEqual(['Unadjusted close', '25%', 'Yes', '€16,000.00', '2023-04-01']);
    expect(table.getByRole('rowheader', { name: 'scenario-2' }).closest('tr')).toHaveTextContent('Adjusted close');
    expect(screen.getByRole('list', { name: 'Skipped scenario counts' })).toHaveTextContent('insufficient_training: 1');
    expect(within(screen.getByRole('table', { name: 'Excluded scenarios' })).getByRole('rowheader')).toHaveTextContent('scenario-10');
  });

  it('paginates scored and excluded scenarios independently by 50 and resets when opening another report', async () => {
    const server = mockServer(); const first = server.save(generatedCalibration({ ...expectedBody, scenario_count: 154 }, 103));
    const second = server.save(generatedCalibration({ ...expectedBody, scenario_count: 154, seed: 99 }, 103));
    await open({ route: reportRoute(first.id) }); openAudit();
    const table = () => within(screen.getByRole('table', { name: 'Per-scenario audit' }));
    const pages = within(screen.getByRole('navigation', { name: 'Per-scenario audit pagination' }));
    expect(table().getAllByRole('row')).toHaveLength(51); expect(pages.getByText('1–50 of 103')).toBeInTheDocument();
    expect(pages.getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(pages.getByRole('button', { name: 'Next' })); expect(table().getByRole('rowheader', { name: 'scenario-51' })).toBeInTheDocument();
    fireEvent.click(pages.getByRole('button', { name: 'Next' })); expect(table().getAllByRole('row')).toHaveLength(4);
    expect(pages.getByText('101–103 of 103')).toBeInTheDocument(); expect(pages.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(pages.getByRole('button', { name: 'Previous' })); expect(pages.getByText('51–100 of 103')).toBeInTheDocument();
    const excluded = within(screen.getByRole('navigation', { name: 'Excluded scenarios pagination' }));
    expect(excluded.getByText('1–50 of 51')).toBeInTheDocument(); fireEvent.click(excluded.getByRole('button', { name: 'Next' }));
    expect(excluded.getByText('51–51 of 51')).toBeInTheDocument(); expect(excluded.getByRole('button', { name: 'Next' })).toBeDisabled();
    expect(within(screen.getByRole('table', { name: 'Excluded scenarios' })).getByRole('rowheader')).toHaveTextContent('scenario-154');
    expect(pages.getByText('51–100 of 103')).toBeInTheDocument();
    await navigate(reportRoute(second.id)); expect(result()).toHaveTextContent('Seed 99');
    expect(screen.getByText('Calculation audit').closest('details')).not.toHaveAttribute('open'); openAudit();
    expect(screen.getByText('1–50 of 103')).toBeInTheDocument(); expect(screen.getByText('1–50 of 51')).toBeInTheDocument();
    expect(request.mock.calls.map(([path]) => path)).toEqual([reportPath(first.id), reportPath(second.id)]); expect(posts()).toHaveLength(0);
  });

  it('keeps the maximum 5000-scenario audit bounded to 50 data rows', async () => {
    const server = mockServer(); server.save(generatedCalibration({ ...expectedBody, scenario_count: 5000 }));
    await open({ route: reportRoute() }); openAudit();
    expect(within(screen.getByRole('table', { name: 'Per-scenario audit' })).getAllByRole('row')).toHaveLength(51);
    expect(screen.getByText('1–50 of 5,000')).toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });
});

describe('route, auth and request race protection', () => {
  it.each(['resolve', 'reject'])('aborts the old report on route change and ignores a late %s for the same strategy', async (outcome) => {
    const server = mockServer(), pending = deferred(); const first = server.save();
    const second = server.save(generatedCalibration({ ...expectedBody, seed: 7 }, 1000, true));
    const report = server.report; server.report = (id) => id === first.id ? pending.promise : report(id);
    await open({ route: reportRoute(first.id) }); const signal = reads(reportPath(first.id))[0][1].signal;
    await navigate(reportRoute(second.id)); expect(signal.aborted).toBe(true); expect(result()).toHaveTextContent('Seed 7');
    await act(async () => outcome === 'resolve' ? pending.resolve({ result: first }) : pending.reject(new Error('obsolete report')));
    expect(result()).toHaveTextContent('Seed 7'); expect(metric('Brier score')).toHaveTextContent('0.0000');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(window.location.hash).toBe(reportRoute(second.id)); expect(posts()).toHaveLength(0);
  });

  it.each(['resolve', 'reject'])('ignores a late clone-source %s after navigating to a different source', async (outcome) => {
    const server = mockServer(), pending = deferred(); const first = server.save();
    const second = server.save(generatedCalibration({ ...expectedBody, strategy: { ...selection, parameters: { instrument: B } }, seed: 7, scenario_count: 10 }));
    const report = server.report; server.report = (id) => id === first.id ? pending.promise : report(id);
    await open({ route: cloneRoute(first.id) }); const signal = reads(reportPath(first.id))[0][1].signal;
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
    await navigate(cloneRoute(second.id)); expect(signal.aborted).toBe(true);
    await act(async () => outcome === 'resolve' ? pending.resolve({ result: first }) : pending.reject(new Error('obsolete clone')));
    expect(screen.getByLabelText('Instrument')).toHaveValue(B); expect(screen.getByLabelText('Seed (optional)')).toHaveValue('7');
    expect(screen.getByLabelText('Number of scenarios')).toHaveValue(10); expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['resolve', 'reject'])('ignores late history-page %s after refresh resets the offset', async (outcome) => {
    const server = mockServer(), pending = deferred();
    for (let i = 0; i < 21; i += 1) server.save(generatedCalibration({ ...expectedBody, scenario_count: 10 }));
    const history = server.history;
    const oldPage = await history(new URLSearchParams('limit=20&offset=20'));
    server.history = (query) => query.get('offset') === '20' ? pending.promise : history(query);
    await open({ route: '#/calibration' }); fireEvent.click(screen.getByRole('button', { name: 'Next' })); await flush();
    const signal = reads(historyPath(20))[0][1].signal; expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Loading calibration history');
    const newest = server.save(generatedCalibration({ ...expectedBody, scenario_count: 10 }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await flush(); expect(signal.aborted).toBe(true);
    expect(screen.getByText('1–20 of 22')).toBeInTheDocument(); expect(historyLinks()[0]).toBe(reportRoute(newest.id));
    await act(async () => outcome === 'resolve' ? pending.resolve(oldPage) : pending.reject(new Error('obsolete page')));
    expect(screen.getByText('1–20 of 22')).toBeInTheDocument(); expect(historyLinks()[0]).toBe(reportRoute(newest.id));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['resolve', 'reject'])('ignores history %s from an older refresh of the same page', async (outcome) => {
    const server = mockServer(), pending = deferred(); server.save(); await open({ route: '#/calibration' });
    const history = server.history, oldPage = await history(new URLSearchParams('limit=20&offset=0'));
    server.history = () => pending.promise;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await flush(); const signal = reads(historyPath()).at(-1)[1].signal;
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    const newest = server.save(); server.history = history;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await flush(); expect(signal.aborted).toBe(true);
    await act(async () => outcome === 'resolve' ? pending.resolve(oldPage) : pending.reject(new Error('obsolete refresh')));
    expect(historyLinks()).toEqual([reportRoute(newest.id), reportRoute()]); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['resolve', 'reject'])('ignores history %s after leaving for a report and reopening history', async (outcome) => {
    const server = mockServer(), pending = deferred(); const first = server.save(); const history = server.history;
    const oldPage = await history(new URLSearchParams('limit=20&offset=0')); server.history = () => pending.promise;
    await open({ route: '#/calibration' }); const signal = reads(historyPath())[0][1].signal;
    await navigate(reportRoute(first.id)); expect(signal.aborted).toBe(true); expect(result()).toHaveTextContent('Seed 42');
    const second = server.save(); server.history = history;
    await follow(/Calibration history/); expect(historyLinks()).toEqual([reportRoute(second.id), reportRoute(first.id)]);
    await act(async () => outcome === 'resolve' ? pending.resolve(oldPage) : pending.reject(new Error('obsolete list')));
    expect(historyLinks()).toEqual([reportRoute(second.id), reportRoute(first.id)]);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['catalogue', 'acquired'])('aborts a pending %s load on navigation without leaking errors onto a report', async (resource) => {
    const server = mockServer(), pending = deferred(); const saved = server.save();
    const method = resource === 'catalogue' ? 'strategies' : 'acquired'; server[method] = () => pending.promise;
    await open(); const signal = reads(resource === 'catalogue' ? '/goals/strategies' : '/instruments/acquired?limit=100&offset=0')[0][1].signal;
    await navigate(reportRoute(saved.id)); expect(signal.aborted).toBe(true); expect(result()).toBeInTheDocument();
    await act(async () => pending.reject(new Error('obsolete form data')));
    expect(result()).toBeInTheDocument(); expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['identity', 'status'].flatMap((change) => ['report', 'history', 'catalogue', 'calculate'].map((operation) => [change, operation])))
  ('aborts %s-stale %s work even when the request function is stable', async (authChange, operation) => {
    const server = mockServer(), pending = deferred();
    const old = server.save({ ...generatedCalibration(), strategy_name: 'Old account snapshot' });
    const report = server.report, history = server.history, strategies = server.strategies;
    if (operation === 'report') server.report = () => pending.promise;
    if (operation === 'history') server.history = () => pending.promise;
    if (operation === 'catalogue') server.strategies = () => pending.promise;
    if (operation === 'calculate') server.calibrate = () => pending.promise;
    const route = operation === 'report' ? reportRoute(old.id) : operation === 'history' ? '#/calibration' : '#/calibration/new';
    const view = await open({ route, choose: operation === 'calculate' }); if (operation === 'calculate') await submit();
    const options = operation === 'calculate' ? posts().at(-1)[1] : reads(operation === 'report' ? reportPath(old.id)
      : operation === 'history' ? historyPath() : '/goals/strategies').at(-1)[1];
    if (authChange === 'identity') auth.user = { id: 'user-b' }; else auth.status = 'checking';
    const replacement = { ...generatedCalibration({ ...expectedBody, seed: 99 }, 1000, true), id: old.id, strategy_name: 'Current account snapshot' };
    server.runs.set(old.id, replacement); server.report = report; server.history = history; server.strategies = strategies;
    view.rerender(<InstrumentWorkspace />); await flush(); expect(options.signal.aborted).toBe(true);
    await act(async () => pending.resolve(operation === 'report' ? { result: old }
      : operation === 'history' ? { items: [summary(old)], total: 1, limit: 20, offset: 0 }
        : operation === 'catalogue' ? { items: [{ ...buyAndHold, name: 'Old account strategy' }] } : generatedCalibration(options.body)));
    await tick();
    expect(screen.queryByText('Old account snapshot')).not.toBeInTheDocument(); expect(screen.queryByText('Old account strategy')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(window.location.hash).toBe(route);
    if (operation === 'report') { expect(result()).toHaveTextContent('Seed 99'); expect(metric('Brier score')).toHaveTextContent('0.0000'); }
    else if (operation === 'history') { expect(historyTable()).toHaveTextContent('Current account snapshot'); expect(historyTable()).toHaveTextContent('0.0000'); }
    else { expect(result()).not.toBeInTheDocument(); expect(screen.getByLabelText('Instrument')).toHaveValue(''); expect(screen.getByLabelText('Seed (optional)')).toHaveValue(''); }
  });

  it.each(['report', 'history'])('immediately hides an already loaded %s when the request function changes', async (operation) => {
    const server = mockServer(), pending = deferred(); const saved = server.save();
    const props = operation === 'report' ? { runId: saved.id } : {};
    const view = await open({ route: operation === 'report' ? reportRoute(saved.id) : '#/calibration', props });
    if (operation === 'report') expect(result()).toBeInTheDocument(); else expect(historyTable()).toBeInTheDocument();
    const path = operation === 'report' ? reportPath(saved.id) : historyPath();
    const oldSignal = reads(path)[0][1].signal;
    auth.request = vi.fn(() => pending.promise); view.rerender(<StrategyCalibration {...props} />);
    expect(oldSignal.aborted).toBe(true); expect(screen.getByRole('status')).toHaveTextContent('Loading calibration');
    expect(result()).not.toBeInTheDocument(); expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(auth.request).toHaveBeenCalledWith(path, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    await act(async () => pending.reject(new Error('New session unavailable')));
    expect(screen.getByRole('alert')).toHaveTextContent('New session unavailable');
    expect(result()).not.toBeInTheDocument(); expect(screen.queryByRole('table')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['report', 'history'])('aborts a pending %s and rejects its late response when the request function changes', async (operation) => {
    const server = mockServer(), pending = deferred(); const saved = server.save();
    if (operation === 'report') server.report = () => pending.promise; else server.history = () => pending.promise;
    const props = operation === 'report' ? { runId: saved.id } : {};
    const view = await open({ props }); const signal = reads(operation === 'report' ? reportPath(saved.id) : historyPath())[0][1].signal;
    const replacement = { ...generatedCalibration({ ...expectedBody, seed: 99 }, 1000, true), id: saved.id };
    auth.request = vi.fn(async () => operation === 'report' ? { result: replacement } : { items: [], total: 0, limit: 20, offset: 0 });
    view.rerender(<StrategyCalibration {...props} />); await flush(); expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(operation === 'report' ? { result: saved } : { items: [summary(saved)], total: 1, limit: 20, offset: 0 }));
    if (operation === 'report') expect(result()).toHaveTextContent('Seed 99');
    else { expect(screen.getByText(/No calibration runs yet/)).toBeInTheDocument(); expect(screen.queryByRole('table')).not.toBeInTheDocument(); }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(posts()).toHaveLength(0);
  });

  it.each(['resolve', 'reject'])('aborts a pending POST on request-function change and ignores its late %s', async (outcome) => {
    const server = mockServer(), pending = deferred(); server.calibrate = () => pending.promise;
    const view = await open({ choose: true }); await submit(); const options = posts()[0][1];
    const nextCatalogue = deferred(); server.strategies = () => nextCatalogue.promise;
    auth.request = (path, options) => request(path, options); view.rerender(<InstrumentWorkspace />); await flush();
    expect(options.signal.aborted).toBe(true); expect(screen.queryByRole('form')).not.toBeInTheDocument();
    await act(async () => pending[outcome](outcome === 'resolve' ? generatedCalibration(options.body) : new Error('obsolete POST')));
    await tick(); expect(window.location.hash).toBe('#/calibration/new'); expect(result()).not.toBeInTheDocument();
    await act(async () => nextCatalogue.resolve({ items: [buyAndHold] }));
    expect(screen.getByLabelText('Instrument')).toHaveValue(''); expect(button()).toBeDisabled(); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(posts()).toHaveLength(1);
  });

  it.each(['resolve', 'reject'])('ignores an abandoned calculation %s after a newer run succeeds even if fetch ignores abort', async (outcome) => {
    const server = mockServer(), pending = deferred(); server.calibrate = () => pending.promise;
    await open({ choose: true }); await submit(); const first = posts()[0][1];
    await follow(/Calibration history/); expect(first.signal.aborted).toBe(true); expect(screen.getByText(/No calibration runs yet/)).toBeInTheDocument();
    await follow('New run'); change('Instrument', B); change('Seed (optional)', '99'); await flush();
    server.calibrate = async (body) => server.save(generatedCalibration(body, body.scenario_count, true)); await submit();
    const savedRoute = window.location.hash;
    await act(async () => pending[outcome](outcome === 'resolve' ? { ...generatedCalibration(first.body), id: runId(100) } : new Error('obsolete calculation')));
    await tick(); expect(window.location.hash).toBe(savedRoute); expect(result()).toHaveTextContent('Seed 99'); expect(result()).toHaveTextContent(B);
    expect(metric('Brier score')).toHaveTextContent('0.0000'); expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(posts()).toHaveLength(2);
  });

  it('allows an abandoned server-side calculation to appear only when history is explicitly refreshed', async () => {
    const server = mockServer(), pending = deferred(); server.calibrate = () => pending.promise;
    await open({ choose: true }); await submit(); const options = posts()[0][1];
    await follow(/Calibration history/); expect(options.signal.aborted).toBe(true);
    const saved = server.save(generatedCalibration(options.body)); await act(async () => pending.resolve(saved)); await tick();
    expect(window.location.hash).toBe('#/calibration'); expect(screen.getByText(/No calibration runs yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await flush(); expect(historyLinks()).toEqual([reportRoute(saved.id)]);
    expect(posts()).toHaveLength(1);
  });

  it.each(['report', 'history', 'clone', 'calculate'])('aborts %s on unmount and ignores late errors', async (operation) => {
    const server = mockServer(), pending = deferred();
    const route = operation === 'report' ? reportRoute() : operation === 'history' ? '#/calibration' : operation === 'clone' ? cloneRoute() : '#/calibration/new';
    if (operation === 'history') server.history = () => pending.promise;
    else if (operation === 'calculate') server.calibrate = () => pending.promise;
    else server.report = () => pending.promise;
    const view = await open({ route, choose: operation === 'calculate' }); if (operation === 'calculate') await submit();
    const options = operation === 'calculate' ? posts()[0][1] : reads(operation === 'history' ? historyPath() : reportPath())[0][1];
    view.unmount(); expect(options.signal.aborted).toBe(true); const count = request.mock.calls.length;
    await act(async () => pending.reject(new Error('late unmount error'))); await tick();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(request).toHaveBeenCalledTimes(count); expect(window.location.hash).toBe(route);
  });
});
