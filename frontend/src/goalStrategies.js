import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { loadAcquiredInstruments } from './instruments.js';

// Only the legacy #/goal/{isin} shortcut needs to know a concrete strategy.
export const BUY_AND_HOLD_ID = 'buy_and_hold_single_instrument';

function validCatalogue(items) {
  const text = (value) => typeof value === 'string' && value.length > 0;
  return Array.isArray(items) && items.length > 0 && new Set(items.map((item) => item?.id)).size === items.length
    && items.every((item) => text(item?.id) && text(item.name) && typeof item.description === 'string'
      && Array.isArray(item.parameters) && new Set(item.parameters.map((parameter) => parameter?.key)).size === item.parameters.length
      && item.parameters.every((parameter) => text(parameter?.key) && text(parameter.type) && text(parameter.label)
        && typeof parameter.required === 'boolean'));
}

export function sameStrategy(left, right) {
  if (!left || !right || left.id !== right.id || !left.parameters || !right.parameters) return false;
  const keys = Object.keys(left.parameters);
  return keys.length === Object.keys(right.parameters).length
    && keys.every((key) => Object.hasOwn(right.parameters, key) && left.parameters[key] === right.parameters[key]);
}

export function useGoalStrategies() {
  const { request } = useAuth();
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
        if (!validCatalogue(catalogue?.items)) throw new Error('The backend returned an unexpected strategy catalogue.');
        setState({ attempt, request, strategies: catalogue.items, instruments, error: null });
      } catch (error) {
        if (abort.signal.aborted) return;
        setState({ attempt, request, strategies: [], instruments: [], error: error.message || 'Planner data is unavailable.' });
        abort.abort();
      }
    }
    load();
    return () => abort.abort();
  }, [attempt, request]);
  const current = state?.attempt === attempt && state.request === request;
  return {
    strategies: current ? state.strategies : [], instruments: current ? state.instruments : [],
    error: current ? state.error : null, loading: !current,
    retry: () => setAttempt((value) => value + 1),
  };
}
