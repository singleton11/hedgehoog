import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { loadAcquiredInstruments } from './instruments.js';
import { validCalibrationSummary } from './strategyCalibration.js';
export { sameStrategy } from './strategySelection.js';

// Only the legacy #/goal/{isin} shortcut needs to know a concrete strategy.
export const BUY_AND_HOLD_ID = 'buy_and_hold_single_instrument';

function validCatalogue(items, requireCalibrationSummaries) {
  const text = (value) => typeof value === 'string' && value.length > 0;
  return Array.isArray(items) && items.length > 0 && new Set(items.map((item) => item?.id)).size === items.length
    && items.every((item) => text(item?.id) && text(item.name) && typeof item.description === 'string'
      && (item.supports_calibration === undefined || typeof item.supports_calibration === 'boolean')
      && (!requireCalibrationSummaries || Object.hasOwn(item, 'latest_calibration'))
      && Array.isArray(item.parameters) && new Set(item.parameters.map((parameter) => parameter?.key)).size === item.parameters.length
      && item.parameters.every((parameter) => text(parameter?.key) && text(parameter.type) && text(parameter.label)
        && typeof parameter.required === 'boolean')
      && ((!requireCalibrationSummaries && item.latest_calibration === undefined) || item.latest_calibration === null || validCalibrationSummary(item.latest_calibration, item)));
}

export function useGoalStrategies({ requireCalibrationSummaries = false } = {}) {
  const { request, user, status } = useAuth();
  const authKey = JSON.stringify([user?.id, status]);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState(null);
  useEffect(() => {
    const abort = new AbortController();
    async function load() {
      try {
        const [catalogue, instruments] = await Promise.all([
          request('/goals/strategies', { signal: abort.signal }),
          loadAcquiredInstruments(request, abort.signal),
        ]);
        if (abort.signal.aborted) return;
        if (!validCatalogue(catalogue?.items, requireCalibrationSummaries)) throw new Error('The backend returned an unexpected strategy catalogue.');
        setState({ attempt, request, authKey, strategies: catalogue.items, instruments, error: null });
      } catch (error) {
        if (abort.signal.aborted) return;
        setState({ attempt, request, authKey, strategies: [], instruments: [], error: error.message || 'Planner data is unavailable.' });
        abort.abort();
      }
    }
    load();
    return () => abort.abort();
  }, [attempt, request, authKey, requireCalibrationSummaries]);
  const current = state?.attempt === attempt && state.request === request && state.authKey === authKey;
  return {
    strategies: current ? state.strategies : [], instruments: current ? state.instruments : [],
    error: current ? state.error : null, loading: !current,
    retry: () => setAttempt((value) => value + 1),
  };
}
