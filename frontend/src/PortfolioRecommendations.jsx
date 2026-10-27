import { useEffect, useRef, useState } from 'react';
import { formatEuros } from './goalProjection.js';
import { secureUuid } from './secureUuid.js';

const PREFIX = 'portfolio-recommendation:';
export function clearRecommendationRecovery() {
  try {
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const key = sessionStorage.key(i);
      if (key?.startsWith(PREFIX)) sessionStorage.removeItem(key);
    }
  } catch { /* Storage may be disabled. Submission requires durable storage. */ }
}
const reasons = (items = []) => items.map((item) => typeof item === 'string' ? item : item.message || item.reason || item.code).filter(Boolean).join('; ');
const name = (item) => item.label || item.instrument?.name || item.instrument_id || 'Cash';
const title = (action) => `${({ deposit: 'Deposit', buy: 'Buy', sell: 'Sell' })[action.type] || action.type}${action.type === 'deposit' ? '' : ` ${name(action)}`}`;
const percent = (value) => value == null ? 'Unavailable' : `${(Number(value) * 100).toFixed(1)}%`;
const eligible = (action) => action.eligible === true && !action.blocked;
const listingName = (listing) => typeof listing === 'string' ? listing : listing ? [listing.symbol, listing.exchange, listing.currency].filter(Boolean).join(' / ') : 'Listing unavailable';
function formatUnitPrice(value) {
  // Keep exact ledger decimals, including prices below one cent, without Number rounding.
  const match = typeof value === 'string' && value.match(/^(\d{1,13})(?:\.(\d{1,20}))?(?:e([+-]?\d{1,2}))?$/i);
  if (!match) return 'Unavailable';
  const [, integer, fraction = '', exponent = '0'] = match;
  const shift = Number(exponent);
  if (shift < -8 || shift > 12) return 'Unavailable';
  const digits = integer + fraction;
  const point = integer.length + shift;
  const whole = point <= 0 ? '0' : digits.padEnd(point, '0').slice(0, point);
  const decimals = (point <= 0 ? '0'.repeat(-point) + digits : digits.slice(point)).replace(/0+$/, '');
  if (decimals.length > 8) return 'Unavailable';
  return `€${BigInt(whole).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${decimals.padEnd(2, '0')}`;
}
function Balance({ label, value }) {
  return <div><dt>{label}</dt><dd>{formatEuros(value)}</dd></div>;
}
function Projection({ label, data }) {
  if (!data) return null;
  return <section aria-label={label}><h4>{label}</h4><dl className="facts"><Balance label="Cash" value={data.cash_eur} />
    {data.equity_eur != null && <Balance label="Equity" value={data.equity_eur} />}</dl>
    {(Array.isArray(data.holdings) ? data.holdings : data.holdings ? Object.entries(data.holdings).map(([instrument_id, quantity]) => ({ instrument_id, quantity })) : data.positions || []).map((holding) => <p key={holding.instrument_id || holding.instrument?.instrument_id}>{name(holding)}: {holding.quantity}</p>)}
  </section>;
}
export default function PortfolioRecommendations({ request, portfolioPath, planId, revision, recoveryScope, onRefresh }) {
  // Keying the inner component also fences unresolved responses on identity changes.
  const [identity, setIdentity] = useState({ request, version: 0 });
  if (identity.request !== request) {
    clearRecommendationRecovery();
    setIdentity({ request, version: identity.version + 1 });
  }
  return <Recommendations key={JSON.stringify([portfolioPath, planId, revision, recoveryScope, identity.version])}
    {...{ request, portfolioPath, planId, revision, recoveryScope, onRefresh }} />;
}
function Recommendations({ request, portfolioPath, planId, revision, recoveryScope, onRefresh }) {
  const storageKey = `${PREFIX}${JSON.stringify([recoveryScope, portfolioPath])}`;
  const [data, setData] = useState(null), [error, setError] = useState(null), [attempt, setAttempt] = useState(0);
  const [selection, setSelection] = useState(null), [overrides, setOverrides] = useState({});
  const [preview, setPreview] = useState(null), [busy, setBusy] = useState(false), [message, setMessage] = useState(null);
  const [recoveryHidden, setRecoveryHidden] = useState(false);
  const [recovery, setRecovery] = useState(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey));
      return saved?.body?.operation_id && saved.body.preview_token && Array.isArray(saved.body.action_ids) ? saved : null;
    } catch { return null; }
  });
  const alive = useRef(true), pending = useRef(false), generation = useRef(0), dialog = useRef(null), opener = useRef(null);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; generation.current++; };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    if (!planId) return () => abort.abort();
    request(`${portfolioPath}/recommendations`, { signal: abort.signal }).then((result) => {
      if (abort.signal.aborted) return;
      if (result?.plan_id !== planId || !Array.isArray(result.actions) || !Array.isArray(result.targets)
        || typeof result.snapshot_token !== 'string') throw new Error('Unexpected recommendations response.');
      setData(result);
    }).catch((err) => { if (!abort.signal.aborted) setError(err.message || 'Unable to load recommendations.'); });
    return () => abort.abort();
  }, [request, portfolioPath, planId, revision, attempt]);
  useEffect(() => {
    if (selection || (recovery && !recoveryHidden)) dialog.current?.focus();
    else if (opener.current) { opener.current.focus(); opener.current = null; }
  }, [selection, recovery, recoveryHidden]);
  const actions = data?.actions || [];
  const available = actions.filter(eligible);
  function clearSaved() { sessionStorage.removeItem(storageKey); setRecovery(null); }
  function close() {
    setSelection(null); setPreview(null); setError(null);
    if (recovery) setRecoveryHidden(true);
  }
  function open(ids, target) {
    if (pending.current || recovery) return;
    opener.current = target; setSelection(ids); setOverrides({}); setPreview(null); setError(null);
  }
  function edit(id, key, value) {
    generation.current++; setPreview(null); setError(null);
    setOverrides((previous) => ({ ...previous, [id]: { ...previous[id], [key]: value } }));
  }
  const payload = () => ({ snapshot_token: data.snapshot_token, action_ids: selection, date: data.date, overrides });
  async function review() {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(null);
    const version = ++generation.current;
    try {
      const result = await request(`${portfolioPath}/recommendations/preview`, { method: 'POST', body: payload() });
      if (!alive.current || version !== generation.current) return;
      if (!result?.preview_token || !Array.isArray(result.entries)) throw new Error('Unexpected preview response.');
      setPreview(result);
    } catch (err) {
      if (alive.current && version === generation.current) {
        setError(err.status === 409 ? 'Recommendations changed. Refresh recommendations and review again.' : err.message);
        if (err.status === 409) { setData(null); setSelection(null); setAttempt((value) => value + 1); }
      }
    } finally { if (alive.current) { pending.current = false; setBusy(false); } }
  }
  async function fulfill() {
    if (pending.current || (!recovery && !preview)) return;
    let saved = recovery;
    try {
      if (!saved) {
        saved = { body: { ...payload(), preview_token: preview.preview_token, operation_id: secureUuid() }, entries: preview.entries,
          selection: preview.selection, before: preview.before, after: preview.after };
        // Persist the exact reviewed request before sending: a lost reply must never create a new operation.
        sessionStorage.setItem(storageKey, JSON.stringify(saved));
        if (sessionStorage.getItem(storageKey) !== JSON.stringify(saved)) throw new Error('Storage unavailable');
        setRecovery(saved);
      }
    } catch { setError('Secure retry storage is unavailable. Enable session storage and Web Crypto before confirming.'); return; }
    pending.current = true; setBusy(true); setError(null);
    try {
      const result = await request(`${portfolioPath}/recommendations/fulfill`, { method: 'POST', body: saved.body });
      if (!alive.current) return;
      if (result?.operation_id !== saved.body.operation_id || !Array.isArray(result.event_ids)) throw new Error('Unexpected fulfillment response.');
      clearSaved(); setSelection(null); setPreview(null); setMessage('Recommendations fulfilled.'); onRefresh();
    } catch (err) {
      if (!alive.current) return;
      if (err.status === 409 || err.status === 422) {
        clearSaved(); setPreview(null); setSelection(null); setData(null); setAttempt((value) => value + 1);
        setError(`${err.message || 'Recommendations changed.'} Refresh and review again.`);
      } else setError('The result is uncertain. Retry the saved operation to reconcile it without duplicating entries.');
    } finally { if (alive.current) { pending.current = false; setBusy(false); } }
  }
  const selectedActions = actions.filter((action) => selection?.includes(action.id));
  const missing = selectedActions.flatMap((action) => [...(action.dependencies || []), ...(action.group || [])].filter((id) => !selection.includes(id)));
  const modal = Boolean(selection || (recovery && !recoveryHidden));
  function dialogKey(event) {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const nodes = [...dialog.current.querySelectorAll('button:not(:disabled), input:not(:disabled)')];
      if (!nodes.length) {
        event.preventDefault(); dialog.current.focus(); return;
      }
      const first = nodes[0], last = nodes.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }
  return <section aria-label="Portfolio recommendations" className="portfolio-recommendations">
    <div className="section-heading"><h3>Next recommendations</h3>
      <button type="button" disabled={!available.length || modal || busy || Boolean(recovery)} onClick={(event) => open(available.map((action) => action.id), event.currentTarget)}>Fulfill all</button></div>
    {message && <p role="status">{message}</p>}
    {recovery && recoveryHidden && <p role="status">A saved operation needs reconciliation before new fulfillment.
      <button type="button" className="secondary" onClick={(event) => { opener.current = event.currentTarget; setRecoveryHidden(false); }}>Resume saved operation</button></p>}
    {!modal && error && <p role="alert">{error}</p>}
    {!planId ? <p className="muted">Assign a saved plan to see recommendations.</p> : !data ? <><p role="status">{error ? 'Recommendations unavailable.' : 'Loading recommendations…'}</p>
      {error && <button className="secondary" onClick={() => { setError(null); setAttempt((value) => value + 1); }}>Refresh recommendations</button>}</> : <>
      <p>Evaluated {data.date} · {data.status}</p>
      {data.funding && <><h4>Scheduled funding</h4><dl className="facts">
        <Balance label="Capital due" value={data.funding.scheduled_due_eur} /><Balance label="Net external contributions" value={data.funding.net_funded_eur} />
        <Balance label="Funding shortfall" value={data.funding.shortfall_eur} /><Balance label="Funding excess" value={data.funding.excess_eur} />
      </dl><p>Scheduled payments due: {data.funding.due_count ?? 'Unavailable'}</p>{data.funding.next_payment && <p>Next scheduled payment: {typeof data.funding.next_payment === 'string' ? data.funding.next_payment : `${data.funding.next_payment.date} · ${formatEuros(data.funding.next_payment.amount_eur)}`}</p>}</>}
      <h4>Allocation targets</h4><ul className="recommendation-list">{data.targets.map((target, index) => <li key={target.instrument_id || index}>
        <strong>{name(target)}</strong><span>Actual {formatEuros(target.actual_eur)} · Target {formatEuros(target.target_eur)} ({percent(target.weight)}) · Weight deviation {percent(target.deviation)}</span>
      </li>)}</ul>
      <p className="muted">Funding and allocation are separate from the probability forecast in Plan progress. Rebalancing does not guarantee the goal or repair a forecast shortfall.</p>
      {data.policy && <><h4>Cost and frequency policy</h4><dl className="facts">{Object.entries(data.policy).filter(([, value]) => value != null && typeof value !== 'object').map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}</dl></>}
      {data.next_eligible_date && <p>Next rebalance eligibility: {data.next_eligible_date}</p>}
      {data.blockers?.length > 0 && <p>{reasons(data.blockers)}</p>}
      {!actions.length && <p>{data.status === 'aligned' ? 'Funding and allocation are aligned.' : 'No eligible corrective actions.'}</p>}
      <ul className="recommendation-list">{actions.map((action) => <li key={action.id}>
        <strong>{title(action)}</strong><span>{formatEuros(action.amount_eur)}{action.quantity && ` · ${action.quantity} units`}</span>
        {action.type !== 'deposit' && <span>Price {formatUnitPrice(action.price_eur)} · {action.price_date || 'Date unavailable'} · {action.price_source || 'Source unavailable'} · {listingName(action.listing)} · Fee {formatEuros(action.commission_eur ?? '0.00')}</span>}
        {action.dependencies?.length > 0 && <span>Requires: {action.dependencies.map((id) => title(actions.find((item) => item.id === id) || { type: id })).join(', ')}. Select prerequisites explicitly in Fulfill all.</span>}
        {action.group?.length > 1 && <span>Rebalance group: {action.group.map((id) => title(actions.find((item) => item.id === id) || { type: id })).join(', ')}. Review the whole group together.</span>}
        {(action.reason || action.blockers?.length > 0) && <span>{action.reason || reasons(action.blockers)}</span>}
        <button type="button" className="secondary" disabled={!eligible(action) || modal || busy || Boolean(recovery)} onClick={(event) => open([action.id], event.currentTarget)} aria-label={`Fulfill ${title(action)}`}>Fulfill</button>
      </li>)}</ul>
    </>}
    {modal && <div className="recommendation-backdrop"><div role="dialog" aria-modal="true" aria-labelledby="recommendation-review-title" tabIndex={-1} ref={dialog} className="recommendation-dialog goal-form" onKeyDown={dialogKey}>
      <h3 id="recommendation-review-title">Review recommendations</h3>
      {recovery ? <><p>Saved operation awaiting reconciliation. Retry uses the same operation ID and reviewed details.</p>
        <EntryList entries={recovery.entries || []} /><ReviewedPrices actions={recovery.selection || []} />
        <Projection label="Before" data={recovery.before} /><Projection label="After" data={recovery.after} /><div className="button-group"><button type="button" disabled={busy} onClick={fulfill}>{busy ? 'Saving…' : 'Retry saved operation'}</button>
        <button type="button" className="secondary" disabled={busy} onClick={close}>Close review</button></div></> : <>
        <p>Transaction date (UTC): {data.date}</p>
        <fieldset disabled={busy}>{selectedActions.map((action) => <div key={action.id} className="recommendation-review-action"><h4>{title(action)}</h4>
          {action.type === 'deposit' ? <p>{formatEuros(action.amount_eur)}</p> : <>
            <p>Saved price: {formatUnitPrice(action.price_eur)} · {action.price_date || 'Date unavailable'} · {action.price_source || 'Source unavailable'} · {listingName(action.listing)}</p>
            <div className="goal-fields">{[['price_eur', 'Price (EUR)', action.price_eur], ['commission_eur', 'Fee (EUR)', action.commission_eur ?? '0.00'], ['quantity', 'Quantity', action.quantity]].map(([key, label, value]) => <label key={key}>{label} — {name(action)}<input inputMode="decimal" maxLength={100} value={overrides[action.id]?.[key] ?? value ?? ''} onChange={(event) => edit(action.id, key, event.target.value)} /></label>)}</div>
          </>}
        </div>)}</fieldset>
        {missing.length > 0 && <p role="alert">Missing prerequisites: {[...new Set(missing)].join(', ')}. Cancel and choose Fulfill all, or fulfill the prerequisite first.</p>}
        {preview && <><EntryList entries={preview.entries} /><ReviewedPrices actions={preview.selection || []} /><Projection label="Before" data={preview.before} /><Projection label="After" data={preview.after} />
          {preview.total_commission_eur != null && <p>Total fees: {formatEuros(preview.total_commission_eur)}</p>}
          {preview.residual_eur != null && <p>Residual cash: {formatEuros(preview.residual_eur)}</p>}</>}
        <div className="button-group"><button type="button" disabled={busy || missing.length > 0} onClick={preview ? fulfill : review}>{busy ? 'Preparing…' : preview ? 'Confirm fulfillment' : 'Preview transactions'}</button>
          <button type="button" className="secondary" disabled={busy} onClick={close}>Cancel</button></div>
      </>}
      {error && <p role="alert">{error}</p>}
    </div></div>}
  </section>;
}
function ReviewedPrices({ actions }) {
  return actions.filter((action) => action.type !== 'deposit').map((action) => <p key={action.id}>
    {title(action)}: {action.price_source === 'override' ? 'Edited price' : 'Saved last/close'} {formatUnitPrice(action.price_eur)} · {action.price_date || 'Date unavailable'} · {action.price_source || 'Source unavailable'} · {listingName(action.listing)}
  </p>);
}
function EntryList({ entries }) {
  const cents = entries.reduce((sum, entry) => {
    const value = entry.commission_eur ?? '0.00';
    return /^\d+\.\d{2}$/.test(value) ? sum + BigInt(value.replace('.', '')) : sum;
  }, 0n);
  const fees = `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`;
  return <section aria-label="Reviewed transactions"><h4>Transactions</h4><ol>{entries.map((entry, index) => <li key={entry.id || index}>{title(entry)} · {entry.date} · {entry.type === 'deposit' ? formatEuros(entry.amount_eur) : `${entry.quantity} at ${formatUnitPrice(entry.price_eur)} · Fee ${formatEuros(entry.commission_eur ?? '0.00')}`}</li>)}</ol><p>Total fees: {formatEuros(fees)}</p></section>;
}
