import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import Icon from './Icon.jsx';

const PAGE_SIZE = 20;
const VARIANTS = { unknown: 'Unknown', price: 'Price', net_total_return: 'Net total return', gross_total_return: 'Gross total return' };
const pending = (job) => ['queued', 'running'].includes(job?.status);

function AddIndex({ busy, onSave, onCancel }) {
  const [name, setName] = useState('');
  const [symbol, setSymbol] = useState('');
  const [currency, setCurrency] = useState('');
  const [variant, setVariant] = useState('unknown');
  return <form className="acquisition-form index-form" aria-label="Add index" onSubmit={(event) => {
    event.preventDefault();
    onSave({ name: name.trim(), symbol: symbol.trim().toUpperCase(), currency: currency.trim().toUpperCase() || null, return_variant: variant });
  }}>
    <h3>Add an index</h3>
    <p>Save catalogue metadata only. Market data is fetched separately, after confirmation. The symbol must identify a Yahoo Finance index, not an ETF.</p>
    <label htmlFor="index-name">Index name</label>
    <input id="index-name" required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
    <label htmlFor="index-symbol">Yahoo symbol</label>
    <input id="index-symbol" required maxLength={64} placeholder="^GSPC" value={symbol} onChange={(e) => setSymbol(e.target.value)} disabled={busy} />
    <label htmlFor="index-currency">Currency (optional)</label>
    <input id="index-currency" maxLength={3} pattern="[A-Za-z]{3}" placeholder="USD" value={currency} onChange={(e) => setCurrency(e.target.value)} disabled={busy} />
    <label htmlFor="index-variant">Return variant</label>
    <select id="index-variant" value={variant} onChange={(e) => setVariant(e.target.value)} disabled={busy}>
      {Object.entries(VARIANTS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select>
    <p className="muted">Leave currency and return variant unknown unless you have verified the exact index series. User-supplied metadata is not provider verification.</p>
    <div className="button-group"><button disabled={busy || !name.trim() || !symbol.trim()}>Save index</button>
      <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button></div>
  </form>;
}

function ConfirmDownload({ item, busy, onConfirm, onCancel }) {
  const [confirmed, setConfirmed] = useState(false);
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <form className="acquisition-form" aria-label="Confirm index download" onSubmit={(event) => {
    event.preventDefault();
    if (confirmed && !busy) onConfirm();
  }}>
    <h3 tabIndex={-1} ref={heading}>Download market data for {item.name}?</h3>
    <p>Fetch available Yahoo Finance history for {item.symbol}. A refresh replaces the published history only after a successful download; a failed download keeps the previous data.</p>
    <label className="checkbox-label"><input type="checkbox" checked={confirmed} required disabled={busy}
      onChange={(event) => setConfirmed(event.target.checked)} />Allow this request to fetch Yahoo Finance data.</label>
    <div className="button-group"><button disabled={!confirmed || busy}>Confirm download</button>
      <button type="button" className="secondary" disabled={busy} onClick={onCancel}>Cancel</button></div>
  </form>;
}

function ConfirmRemoval({ item, busy, onConfirm, onCancel }) {
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <form className="acquisition-form" aria-label="Confirm index removal" onSubmit={(event) => {
    event.preventDefault();
    if (!busy) onConfirm();
  }}>
    <h3 tabIndex={-1} ref={heading}>Remove {item.name} ({item.symbol}) from the catalogue?</h3>
    <p>This removes the index from the shared catalogue for all users. Downloaded data is preserved. Existing downloads are not cancelled. Add the same symbol again to restore the index and its saved history.</p>
    <div className="button-group"><button disabled={busy}>Confirm removal</button>
      <button type="button" className="secondary" disabled={busy} onClick={onCancel}>Cancel</button></div>
  </form>;
}

export default function Indices() {
  const { request } = useAuth();
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [selection, setSelection] = useState(null);
  const [removing, setRemoving] = useState(false);
  const [notice, setNotice] = useState(null);
  const readController = useRef(null);
  const mutationController = useRef(null);
  const timer = useRef(null);
  const trigger = useRef(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; mutationController.current?.abort(); };
  }, []);

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    readController.current = controller;
    setLoading(true);
    setLoadError(null);
    async function load() {
      try {
        const data = await request(`/indices?limit=${PAGE_SIZE}&offset=${offset}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!Array.isArray(data?.items) || !Number.isInteger(data.total) || data.total < 0 || data.limit !== PAGE_SIZE || data.offset !== offset
          || data.items.some((item) => typeof item.instrument_id !== 'string' || typeof item.name !== 'string' || typeof item.symbol !== 'string')) {
          throw new Error('The backend returned an unexpected index list. Please retry.');
        }
        if (!data.items.length && offset > 0) {
          setPage(null);
          setOffset(Math.max(0, Math.ceil(data.total / PAGE_SIZE) - 1) * PAGE_SIZE);
          return;
        }
        setPage(data);
        if (data.items.some((item) => pending(item.acquisition))) {
          timer.current = setTimeout(() => { if (!controller.signal.aborted) setAttempt((value) => value + 1); }, 2000);
        }
      } catch (err) {
        if (!controller.signal.aborted) setLoadError(err.message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    load();
    return () => { controller.abort(); clearTimeout(timer.current); };
  }, [request, offset, attempt, busy]);

  function closeForms() {
    setAdding(false);
    setSelection(null);
    setRemoving(false);
    setError(null);
    trigger.current?.focus();
  }

  async function mutate(path, body, message, method = 'POST') {
    if (mutationController.current) return;
    readController.current?.abort();
    clearTimeout(timer.current);
    const controller = new AbortController();
    mutationController.current = controller;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await request(path, { method, body, signal: controller.signal });
      if (!alive.current || controller.signal.aborted) return;
      closeForms();
      setNotice(message);
      if (path === '/indices') { setOffset(0); setPage(null); }
      if (method === 'DELETE') setPage(null);
    } catch (err) {
      if (alive.current && !controller.signal.aborted) {
        setError(`${err.message} Check the refreshed catalogue before retrying; the request may already have been accepted.`);
      }
    } finally {
      if (alive.current && !controller.signal.aborted) {
        mutationController.current = null;
        setBusy(false);
        setAttempt((value) => value + 1);
      }
    }
  }

  function changePage(next) {
    readController.current?.abort();
    clearTimeout(timer.current);
    setPage(null);
    setOffset(next);
    closeForms();
  }

  return <section className="acquired-page" aria-labelledby="indices-heading">
    <div className="section-heading page-heading"><div><p className="eyebrow">Market reference data</p>
      <h2 id="indices-heading">Indices{page ? ` (${page.total})` : ''}</h2>
      <p className="muted">A small, curated catalogue — not a complete global directory. Downloads happen only on request.</p>
    </div><button disabled={busy || adding || Boolean(selection)} onClick={(event) => {
      trigger.current = event.currentTarget; setAdding(true); setError(null); setNotice(null);
    }}><Icon name="plus" />Add index</button></div>
    <p className="muted">Benchmark comparisons and ETF proxy assignments are not enabled. Index identity checks do not verify currency or dividend-reinvestment semantics.</p>
    {adding && <AddIndex busy={busy} onCancel={closeForms} onSave={(body) => mutate('/indices', body, 'Index saved. No market data was downloaded.')} />}
    {selection && removing && <ConfirmRemoval key={selection.instrument_id} item={selection} busy={busy} onCancel={closeForms}
      onConfirm={() => mutate(`/indices/${encodeURIComponent(selection.instrument_id)}`, undefined, 'Index removed from the catalogue. Downloaded data was preserved.', 'DELETE')} />}
    {selection && !removing && <ConfirmDownload key={selection.instrument_id} item={selection} busy={busy} onCancel={closeForms}
      onConfirm={() => mutate(`/indices/${encodeURIComponent(selection.instrument_id)}/acquisitions`, { confirmed: true }, 'Download requested. Status will update automatically.')} />}
    {busy && <p role="status">Submitting request…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert">{error}</p>}
    {loading && !busy && <p role="status">Loading indices…</p>}
    {loadError && <><p role="alert">{loadError}</p><button className="secondary" disabled={busy} onClick={() => setAttempt((value) => value + 1)}>Retry list</button></>}
    {page && <div className="collection-table-card">
      <div className="table-heading"><h3>Index catalogue</h3><span className="table-scroll-hint">Scroll for more →</span></div>
      <div className="acquired-table-scroll" role="region" aria-label="Index market data" tabIndex={0}>
        <table className="acquired-table"><caption className="sr-only">Indices, metadata, downloaded history and acquisition status</caption>
          <thead><tr><th scope="col">Index</th><th scope="col">Currency / variant</th><th scope="col">Identity</th><th scope="col">Saved quotes</th><th scope="col">Market data</th></tr></thead>
          <tbody>{page.items.map((item) => <tr key={item.instrument_id}>
            <th scope="row">{item.name}<span className="muted">{item.symbol} · Yahoo Finance</span></th>
            <td>{item.currency || 'Unknown currency'}<span className="muted">{VARIANTS[item.return_variant] || 'Unknown'}{item.return_variant !== 'unknown' ? ' (catalogue metadata)' : ''}</span></td>
            <td>{item.verification_status === 'verified' ? 'Verified index' : item.verification_status === 'unverified' ? 'Unverified' : item.verification_status?.replaceAll('_', ' ') || 'Unverified'}</td>
            <td>{item.summary?.quote_count > 0 ? <>{item.summary.quote_count.toLocaleString()} quotes<span className="muted">{item.summary.first_date} – {item.summary.last_date}</span></> : 'No saved data'}</td>
            <td className="market-data-actions"><button className="secondary" disabled={busy || pending(item.acquisition) || adding || Boolean(selection)}
              aria-label={`Download market data for ${item.name}`} onClick={(event) => {
                trigger.current = event.currentTarget; setSelection(item); setError(null); setNotice(null);
              }}>{item.summary?.quote_count > 0 ? 'Refresh data' : 'Download data'}</button>
              <button className="secondary" disabled={busy || adding || Boolean(selection)}
                aria-label={`Remove ${item.name} from catalogue`} onClick={(event) => {
                  trigger.current = event.currentTarget; setSelection(item); setRemoving(true); setError(null); setNotice(null);
                }}>Remove from catalogue</button>
              <span className="muted" role="status">{item.acquisition?.status || 'Not requested'}{pending(item.acquisition) && item.acquisition?.phase ? ` · ${item.acquisition.phase}` : ''}</span>
              {item.acquisition?.error && <span className="update-error" role="alert">{item.acquisition.error}</span>}
              {item.acquisition?.warning && <span className="muted">{item.acquisition.warning}</span>}
            </td>
          </tr>)}</tbody>
        </table>
      </div>
      {!page.items.length && <p>No indices on this page. Add an index to get started.</p>}
      <div className="section-heading acquired-pagination"><p>{page.total} indices · page {Math.floor(offset / PAGE_SIZE) + 1}</p>
        <div className="button-group"><button className="secondary" disabled={busy || offset === 0} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}>Previous indices</button>
          <button className="secondary" disabled={busy || !page.items.length || offset + page.items.length >= page.total} onClick={() => changePage(offset + PAGE_SIZE)}>Next indices</button></div>
      </div>
    </div>}
  </section>;
}
