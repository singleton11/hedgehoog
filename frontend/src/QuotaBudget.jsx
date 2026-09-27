import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';

export const BUDGET_POLL_INTERVAL = 5000;

export default function QuotaBudget() {
  const { request } = useAuth();
  const [budget, setBudget] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function refresh() {
      try {
        const data = await request('/instruments/budget', { signal: controller.signal });
        if (!controller.signal.aborted) setBudget(data);
      } catch {
        if (!controller.signal.aborted) setBudget(null);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = setTimeout(refresh, BUDGET_POLL_INTERVAL);
        }
      }
    }
    refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [request]);

  const unlimited = budget && budget.metered === false;
  const label = budget?.provider === 'yfinance' ? 'Yahoo Finance · experimental' : 'Local budget';
  return <div className="budget" role="group" aria-label="Provider status">
    {budget ? <>{unlimited ? <p title={`${budget.month} · unmetered experimental provider.`}>
      <strong>{label}</strong>
    </p> : <><p title={`${budget.month} · ${budget.used} used. Excludes requests outside Hedgehoog.`}>
      <strong>{label}:</strong> {budget.remaining.toLocaleString()} / {budget.limit.toLocaleString()} calls left
    </p><span className="budget-track" aria-hidden="true"><span style={{ width: `${budget.limit > 0 ? Math.max(0, Math.min(100, budget.remaining / budget.limit * 100)) : 0}%` }} /></span></>}</> : <p className="muted">{loading ? 'Loading provider status…' : 'Provider status unavailable.'}</p>}
  </div>;
}
