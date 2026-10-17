import { useEffect, useRef, useState } from 'react';

function valid(value) {
  return value?.version === 1 && ['smooth', 'normal'].includes(value.method)
    && Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295;
}

export default function MarketDataSettings({ request, admin }) {
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [method, setMethod] = useState('smooth');
  const [seed, setSeed] = useState('0');
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const lifetime = useRef(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    setLoaded(false); setBusy(false); setError(null); setMessage(null);
    (async () => {
      try {
        const result = await request('/settings/market-data', { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!valid(result)) throw new Error('Invalid missing-quote settings.');
        setMethod(result.method); setSeed(String(result.seed)); setLoaded(true);
      } catch (failure) { if (!controller.signal.aborted) setError(failure.message); }
    })();
    return () => controller.abort();
  }, [request, attempt]);
  const seedValid = /^\d+$/.test(seed) && Number.isSafeInteger(Number(seed)) && Number(seed) <= 4294967295;
  async function save(event) {
    event.preventDefault();
    if (!admin || busy || !seedValid) return;
    const controller = lifetime.current;
    setBusy(true); setError(null); setMessage(null);
    try {
      const result = await request('/settings/market-data', {
        method: 'PUT', body: { method, seed: Number(seed) }, signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!valid(result)) throw new Error('Invalid missing-quote settings response. Reload before trying again.');
      setMethod(result.method); setSeed(String(result.seed));
      setMessage('Missing-quote policy saved for all calculations. Existing saved reports are unchanged.');
    } catch (failure) { if (!controller.signal.aborted) setError(failure.message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  function generate() {
    try {
      const values = new Uint32Array(1);
      crypto.getRandomValues(values);
      setSeed(String(values[0])); setMessage(null); setError(null);
    } catch { setError('Secure random generation is unavailable. Enter a seed manually.'); }
  }
  return <section aria-label="Missing quote policy">
    <h3>Missing quotes</h3>
    <p>Applies to all new calculations, without changing downloaded prices or existing saved reports. Only administrators can save changes.</p>
    <p>Only explicit missing-price rows are filled; no weekend or holiday rows are created. Arithmetic smoothing needs valid prices on both sides within the calculation cutoff. Normal sampling uses only earlier observed daily log returns; insufficient history remains missing.</p>
    {!loaded && !error && <p role="status">Loading missing-quote settings...</p>}
    {error && <p role="alert">{error}</p>}
    {!loaded && error && <button onClick={() => setAttempt(attempt + 1)}>Retry missing-quote settings</button>}
    {loaded && <form onSubmit={save}>
      <label htmlFor="quote-fill-method">Missing quote method</label>
      <select id="quote-fill-method" value={method} disabled={!admin || busy} onChange={(event) => { setMethod(event.target.value); setMessage(null); }}>
        <option value="smooth">Arithmetic smoothing</option>
        <option value="normal">Normally sampled log returns</option>
      </select>
      <label htmlFor="quote-fill-seed">Global missing-quote seed</label>
      <input id="quote-fill-seed" type="text" inputMode="numeric" value={seed} disabled={!admin || busy}
        aria-invalid={!seedValid} onChange={(event) => { setSeed(event.target.value); setMessage(null); }} />
      <p>Integer from 0 to 4294967295. The same seed, instrument, dates and observed history reproduce sampled prices. The seed does not affect smoothing.</p>
      {!seedValid && <p role="alert">Enter an integer from 0 to 4294967295.</p>}
      {admin && <><button type="button" disabled={busy} onClick={generate}>Generate / Regenerate seed</button>
        <button disabled={busy || !seedValid}>{busy ? 'Saving missing-quote settings...' : 'Save missing-quote settings'}</button></>}
    </form>}
    {message && <p role="status">{message}</p>}
  </section>;
}
