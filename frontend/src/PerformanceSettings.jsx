import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';

export default function PerformanceSettings() {
  const { request, user } = useAuth();
  const [identity, setIdentity] = useState({ request, version: 0 });
  if (identity.request !== request) setIdentity({ request, version: identity.version + 1 });
  return <Settings key={`${user.id}:${identity.version}`} request={request} admin={user.role === 'admin'} />;
}

function Settings({ request, admin }) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState(null);
  const [selection, setSelection] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    setResult(null);
    (async () => {
      try {
        const settings = await request('/settings/performance', { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!settings || !Object.hasOwn(settings, 'proxy') || (settings.proxy !== null && (typeof settings.proxy?.instrument_id !== 'string' || typeof settings.proxy?.name !== 'string' || typeof settings.proxy?.symbol !== 'string'))) throw new Error('Invalid performance settings.');
        const items = [], seen = new Set();
        let offset = 0, total;
        do {
          const page = await request(`/settings/performance/indices?limit=100&offset=${offset}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          if (!Array.isArray(page?.items) || !Number.isInteger(page.total) || page.total < 0 || page.offset !== offset || page.limit !== 100
            || (total !== undefined && total !== page.total) || page.items.length > 100 || page.items.length > page.total - offset
            || (offset < page.total && !page.items.length)) throw new Error('Could not load the complete saved instrument list.');
          total = page.total;
          for (const item of page.items) {
            if (!item.instrument_id || seen.has(item.instrument_id) || typeof item.name !== 'string' || typeof item.symbol !== 'string') throw new Error('Invalid saved instrument list.');
            seen.add(item.instrument_id); items.push(item);
          }
          offset += page.items.length;
        } while (offset < total);
        if (!controller.signal.aborted) {
          setSelection(settings.proxy?.instrument_id || '');
          setResult({ request, settings, items });
        }
      } catch (error) { if (!controller.signal.aborted) setResult({ request, error: error.message }); }
    })();
    return () => controller.abort();
  }, [request, attempt]);
  // Unmount/auth changes fence mutation responses too.
  const [lifetime] = useState(() => ({ active: false }));
  useEffect(() => { lifetime.active = true; return () => { lifetime.active = false; }; }, [lifetime]);
  const current = result?.request === request ? result : null;
  async function save(event) {
    event.preventDefault(); setBusy(true); setMessage(null);
    try {
      await request('/settings/performance', { method: 'PUT', body: { instrument_id: selection || null } });
      if (lifetime.active) setMessage('Saved for all users and portfolios.');
    } catch (error) { if (lifetime.active) setMessage(error.message); }
    finally { if (lifetime.active) setBusy(false); }
  }
  return <article className="customer-planner" aria-label="Settings">
    <h2>Settings</h2><h3>Portfolio risk-return proxy</h3>
    <p>This app-wide setting applies to all users and portfolios. Only administrators can change it.</p>
    <p>Choose an acquired EUR-denominated index whose saved level returns will serve as both Sharpe’s risk-free proxy and Sortino’s minimum-return target. Use an appropriate overnight-rate or money-market return index; an equity index is not risk-free. Inflation indices and annual yield quotes are not return proxies.</p>
    <p>For index proxies, missing currency metadata is assumed to be EUR, not converted to EUR. Explicit non-EUR currencies remain excluded. Check the index’s denomination before selecting it.</p>
    <p>Missing history makes ratios unavailable; there is no zero-rate fallback. Returns follow the saved index levels and their return variant. Changing this setting recalculates historical ratios when portfolios are reopened or refreshed.</p>
    {!current && <p role="status">Loading settings…</p>}
    {current?.error && <><p role="alert">{current.error}</p><button onClick={() => setAttempt(attempt + 1)}>Retry settings</button></>}
    {current?.items && <form onSubmit={save}>
      <label htmlFor="risk-proxy">Saved EUR index</label>
      <select id="risk-proxy" value={selection} disabled={!admin || busy} onChange={(event) => setSelection(event.target.value)}>
        <option value="">Not configured — ratios unavailable</option>
        {selection && !current.items.some((item) => item.instrument_id === selection) && <option value={selection}>Previous selection (unavailable — choose an index)</option>}
        {current.items.map((item) => <option key={item.instrument_id} value={item.instrument_id}>{item.name} ({item.symbol})</option>)}
      </select>
      <p><a href="#/indices">Acquire an index</a></p>
      {admin && <button disabled={busy}>{busy ? 'Saving…' : 'Save settings'}</button>}
    </form>}
    {message && <p role="status">{message}</p>}
  </article>;
}
