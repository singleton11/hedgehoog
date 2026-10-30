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
        if (!controller.signal.aborted) setBudget(data?.provider === 'yfinance' && data.metered === false ? data : null);
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

  return <div className="budget" role="group" aria-label="Provider status">
    {budget ? <p title={`${budget.month} · unmetered experimental provider.`}>
      <strong>Yahoo Finance · experimental</strong>
    </p> : <p className="muted">{loading ? 'Loading provider status…' : 'Provider status unavailable.'}</p>}
  </div>;
}
