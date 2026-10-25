import PhoneTable from './PhoneTable.jsx';
import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import Icon from './Icon.jsx';

const PAGE_SIZE = 20;
const VARIANTS = { unknown: 'Unknown', price: 'Price', net_total_return: 'Net total return', gross_total_return: 'Gross total return' };
const pending = (job) => ['queued', 'running'].includes(job?.status);

function ManualIndex({ busy, onSave, onCancel }) {
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

function AddIndex({ busy, onSave, onCancel }) {
  const { request } = useAuth();
  const [manual, setManual] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [active, setActive] = useState(-1);
  const [expanded, setExpanded] = useState(true);
  const version = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const current = version.current;
    if (manual || selected || query.trim().length < 2) return () => controller.abort();
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const data = await request(`/indices/search?q=${encodeURIComponent(query.trim())}`, { signal: controller.signal });
        if (controller.signal.aborted || current !== version.current) return;
        if (!Array.isArray(data?.items) || data.items.some((item) => typeof item.name !== 'string' || typeof item.symbol !== 'string')) {
          throw new Error('The backend returned unexpected index search results. Please retry.');
        }
        setResults(data.items); setSearched(true);
      } catch (err) {
        if (!controller.signal.aborted && current === version.current) setError(err.message);
      } finally {
        if (!controller.signal.aborted && current === version.current) setLoading(false);
      }
    }, 350);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [query, manual, selected, request, attempt]);

  function reset() {
    version.current += 1;
    setResults([]); setSelected(null); setError(null); setSearched(false); setLoading(false); setActive(-1); setExpanded(true);
  }
  function choose(item) {
    version.current += 1;
    setSelected(item); setQuery(item.name); setResults([]); setLoading(false); setExpanded(false); setActive(-1);
  }
  if (manual) return <>
    <button type="button" className="secondary" disabled={busy} onClick={() => { reset(); setManual(false); }}>Search instead</button>
    <ManualIndex busy={busy} onSave={onSave} onCancel={onCancel} />
  </>;
  const visible = expanded && results.length > 0;
  return <form className="acquisition-form index-form" aria-label="Add index" onSubmit={(event) => {
    event.preventDefault();
    if (selected && !busy) onSave({ name: selected.name, symbol: selected.symbol, currency: selected.currency || null, return_variant: 'unknown' });
  }}>
    <h3>Find an index</h3>
    <p>Search Yahoo Finance by name or symbol. Only indices are shown, never ETFs. Saving does not download market data.</p>
    <label htmlFor="index-search">Search indices</label>
    <input id="index-search" role="combobox" autoComplete="off" aria-autocomplete="list" aria-controls="index-suggestions"
      aria-expanded={Boolean(visible)} aria-activedescendant={visible && active >= 0 ? `index-suggestion-${active}` : undefined}
      placeholder="MSCI World net" maxLength={100} value={query} disabled={busy}
      onChange={(event) => { reset(); setQuery(event.target.value); }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') { event.preventDefault(); setExpanded(false); setActive(-1); }
        if (results.length && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
          event.preventDefault(); setExpanded(true);
          setActive((value) => event.key === 'ArrowDown' ? (value + 1) % results.length : (value <= 0 ? results.length - 1 : value - 1));
        }
        if (event.key === 'Enter' && !selected) { event.preventDefault(); if (visible && active >= 0) choose(results[active]); }
      }} />
    <ul id="index-suggestions" role="listbox" aria-label="Matching indices" hidden={!visible}>
      {results.map((item, i) => <li key={item.symbol} id={`index-suggestion-${i}`} role="option" aria-selected={active === i}
        onMouseDown={(event) => event.preventDefault()} onClick={() => { if (!busy) choose(item); }}>
        {item.name} · {item.symbol} · {item.currency || 'Unknown currency'} · Unknown return variant
      </li>)}
    </ul>
    {loading && <p role="status">Searching indices…</p>}
    {error && <><p role="alert">{error}</p><button type="button" className="secondary" onClick={() => { reset(); setAttempt((value) => value + 1); }}>Retry search</button></>}
    {searched && !loading && !error && !results.length && !selected && <p role="status">No matching indices found. Yahoo may not carry this exact series; try another name or use manual entry.</p>}
    {selected && <p role="status">Selected: {selected.name} ({selected.symbol}) · {selected.currency || 'Unknown currency'}</p>}
    <p className="muted">Check the exact series before adding. Search does not verify price, net or gross total-return semantics; the return variant remains unknown.</p>
    <div className="button-group"><button disabled={busy || !selected}>Save index</button>
      <button type="button" className="secondary" disabled={busy} onClick={() => { reset(); setManual(true); }}>Manual entry (advanced)</button>
      <button type="button" className="secondary" disabled={busy} onClick={onCancel}>Cancel</button></div>
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

function ConfirmAllDownload({ count, busy, onConfirm, onCancel }) {
  const [confirmed, setConfirmed] = useState(false);
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <form className="acquisition-form" aria-label="Confirm all index downloads" onSubmit={(event) => {
    event.preventDefault();
    if (confirmed && !busy) onConfirm();
  }}>
    <h3 tabIndex={-1} ref={heading}>Download market data for all indexes?</h3>
    <p>Queue available Yahoo Finance history for all {count} indexes in the catalogue. Each download will require explicit provider access.</p>
    <label className="checkbox-label"><input type="checkbox" checked={confirmed} required disabled={busy}
      onChange={(event) => setConfirmed(event.target.checked)} />Allow these requests to fetch Yahoo Finance data.</label>
    <div className="button-group"><button disabled={!confirmed || busy}>Confirm all downloads</button>
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
  const [allDownloads, setAllDownloads] = useState(false);
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
    setAllDownloads(false);
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
    </div><div className="button-group"><button className="secondary" disabled={busy || adding || Boolean(selection) || allDownloads || !page?.total} onClick={(event) => {
      trigger.current = event.currentTarget; setAllDownloads(true); setError(null); setNotice(null);
    }}>Download all market data</button><button disabled={busy || adding || Boolean(selection) || allDownloads} onClick={(event) => {
      trigger.current = event.currentTarget; setAdding(true); setError(null); setNotice(null);
    }}><Icon name="plus" />Add index</button></div></div>
    <p className="muted">Benchmark comparisons and ETF proxy assignments are not enabled. Index identity checks do not verify currency or dividend-reinvestment semantics.</p>
    {adding && <AddIndex busy={busy} onCancel={closeForms} onSave={(body) => mutate('/indices', body, 'Index saved. No market data was downloaded.')} />}
    {allDownloads && <ConfirmAllDownload count={page?.total || 0} busy={busy} onCancel={closeForms}
      onConfirm={() => mutate('/indices/acquisitions', { confirmed: true }, 'Downloads requested for all indexes. Status will update automatically.')} />}
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
        <PhoneTable className="acquired-table"><caption className="sr-only">Indices, metadata, downloaded history and acquisition status</caption>
          <thead><tr><th scope="col">Index</th><th scope="col">Currency / variant</th><th scope="col">Identity</th><th scope="col">Saved quotes</th><th scope="col">Market data</th></tr></thead>
          <tbody>{page.items.map((item) => <tr key={item.instrument_id}>
            <th scope="row"><a href={`#/indices/${encodeURIComponent(item.instrument_id)}`}>{item.name}</a><span className="muted">{item.symbol} · Yahoo Finance</span></th>
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
        </PhoneTable>
      </div>
      {!page.items.length && <p>No indices on this page. Add an index to get started.</p>}
      <div className="section-heading acquired-pagination"><p>{page.total} indices · page {Math.floor(offset / PAGE_SIZE) + 1}</p>
        <div className="button-group"><button className="secondary" disabled={busy || offset === 0} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}>Previous indices</button>
          <button className="secondary" disabled={busy || !page.items.length || offset + page.items.length >= page.total} onClick={() => changePage(offset + PAGE_SIZE)}>Next indices</button></div>
      </div>
    </div>}
  </section>;
}
