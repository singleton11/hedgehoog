import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';

const factorPattern = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const validFactor = (value) => {
  if (typeof value !== 'string' || value.length > 32 || !factorPattern.test(value)) return false;
  const [whole, fraction = ''] = value.split('.');
  const numerator = BigInt(whole + fraction);
  const unit = 10n ** BigInt(fraction.length);
  return numerator * 1000000000n >= unit && numerator <= 1000000000n * unit && numerator !== unit;
};
const validDate = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;

function SplitEditor({ isin, listing, inception, disabled, refreshKey, onChanged }) {
  const { request } = useAuth();
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [writeError, setWriteError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState(null);
  const [day, setDay] = useState('');
  const [factor, setFactor] = useState('');
  const [note, setNote] = useState('');
  const readController = useRef(null);
  const writeController = useRef(null);
  const pendingAdd = useRef(null);
  const path = `/instruments/${encodeURIComponent(isin)}/splits`;
  const today = new Date().toISOString().slice(0, 10);
  const blocked = busy || disabled;
  const valid = validDate(day) && day <= today && (!inception || day >= inception) && validFactor(factor.trim());

  useEffect(() => () => { writeController.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    readController.current = controller;
    setLoading(true);
    setLoadError(null);
    request(path, { signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return;
      if (result?.instrument_id !== isin || result.listing?.symbol !== listing.symbol || result.listing?.exchange !== listing.exchange
        || !Array.isArray(result.manual) || !Array.isArray(result.provider)
        || !result.manual.every((entry) => typeof entry.id === 'string' && validDate(entry.date) && validFactor(entry.factor) && typeof entry.note === 'string')
        || !result.provider.every((entry) => validDate(entry.date) && typeof entry.factor === 'string' && Number(entry.factor) > 0)
        || (result.error !== null && typeof result.error !== 'string')) {
        throw new Error('Split history returned an unexpected response.');
      }
      setData(result);
    }).catch((error) => {
      if (!controller.signal.aborted) setLoadError(error.message);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [isin, listing.symbol, listing.exchange, path, request, attempt, refreshKey]);

  async function mutate(id, body) {
    if (writeController.current || disabled) return;
    readController.current?.abort();
    const controller = new AbortController();
    writeController.current = controller;
    setBusy(true);
    setWriteError(null);
    setNotice(null);
    try {
      const result = await request(id ? `${path}/${encodeURIComponent(id)}` : path, {
        method: id ? 'DELETE' : 'POST', ...(body ? { body } : {}), signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!id && (result?.item?.id !== body.id || result.item.date !== body.date || !validFactor(result.item.factor))) {
        throw new Error('Saving the split returned an unexpected response. Refresh splits before retrying.');
      }
      setNotice(id ? 'Manual split removed.' : 'Manual split saved.');
      setRemoving(null);
      if (!id) {
        pendingAdd.current = null;
        setAdding(false);
        setDay(''); setFactor(''); setNote('');
      }
    } catch (error) {
      if (!controller.signal.aborted) setWriteError(error.message);
    } finally {
      if (!controller.signal.aborted) {
        writeController.current = null;
        setBusy(false);
        // Reconcile even an uncertain network failure; never retry a write automatically.
        setAttempt((value) => value + 1);
        onChanged();
      }
    }
  }

  function submit(event) {
    event.preventDefault();
    if (!valid || blocked || loading || loadError) return;
    const payload = { ...listing, date: day, factor: factor.trim(), note: note.trim() };
    const key = JSON.stringify(payload);
    if (pendingAdd.current?.key !== key) pendingAdd.current = { key, id: crypto.randomUUID() };
    mutate(null, { id: pendingAdd.current.id, ...payload });
  }

  return <div className="disclosure-content">
    <p>Splits for <strong>{listing.symbol} / {listing.exchange}</strong>. Manual entries are shared by all users and survive provider updates. No provider quota is used.</p>
    <p className="muted">Affects returns and new simulations, not raw candles, portfolio share balances or saved reports. Recalculate saved results to use corrections.</p>
    {loading && <p role="status">Loading split history…</p>}
    {loadError && <p>Could not load splits: {loadError} <button className="secondary" disabled={blocked} onClick={() => setAttempt((value) => value + 1)}>Retry splits</button></p>}
    {data?.error && <p role="alert">{data.error}</p>}
    {writeError && <p role="alert">{writeError}</p>}
    {notice && <p role="status">{notice}</p>}
    {data && <>
      <h3>Provider splits</h3>
      {data.provider.length ? <ul>{data.provider.map((entry) => <li key={entry.date}>{entry.date} · factor {entry.factor}</li>)}</ul>
        : <p>No provider splits saved. This does not prove there were no splits.</p>}
      <h3>Manual splits</h3>
      {data.manual.length ? <ul className="manual-split-list">{data.manual.map((entry) => <li key={entry.id}>
        <span>{entry.date} · factor {entry.factor}{entry.note && <> · {entry.note}</>}</span>{' '}
        <button className="secondary" disabled={blocked || loading || Boolean(loadError)} onClick={() => { setRemoving(entry.id); setWriteError(null); }}
          aria-label={`Remove split on ${entry.date}`}>Remove</button>
        {removing === entry.id && <div>
          <p>Remove this manual split? Derived returns will be recalculated. Any matching provider split still applies.</p>
          <div className="button-group"><button disabled={blocked} onClick={() => mutate(entry.id)}>Confirm removal</button>
            <button className="secondary" disabled={blocked} onClick={() => setRemoving(null)}>Cancel removal</button></div>
        </div>}
      </li>)}</ul> : <p>No manual splits saved.</p>}
      {!adding && <button className="secondary" disabled={blocked || loading || Boolean(loadError)} onClick={() => { setAdding(true); setNotice(null); }}>Add manual split</button>}
      {adding && <form className="acquisition-form" aria-label="Add manual split" onSubmit={submit}>
        <div className="listing-fields">
          <div><label htmlFor="split-date">Effective date (ex-date)</label><input id="split-date" type="date" required value={day}
            min={inception || undefined} max={today} disabled={blocked} onChange={(event) => setDay(event.target.value)} /></div>
          <div><label htmlFor="split-factor">Split factor (new shares per old share)</label><input id="split-factor" inputMode="decimal" required value={factor}
            maxLength={32} disabled={blocked} onChange={(event) => setFactor(event.target.value)} aria-describedby="split-factor-help" /></div>
        </div>
        <p id="split-factor-help">25 for a 25-for-1 split; 0.5 for a 1-for-2 reverse split. Use the issuer's effective date and ratio, not a price-move estimate.</p>
        <label htmlFor="split-note">Source or note (optional)</label><input id="split-note" value={note} maxLength={500} disabled={blocked} onChange={(event) => setNote(event.target.value)} />
        <div className="button-group"><button type="submit" disabled={blocked || loading || Boolean(loadError) || !valid}>{busy ? 'Saving…' : 'Save split'}</button>
          <button type="button" className="secondary" disabled={blocked} onClick={() => setAdding(false)}>Cancel</button></div>
      </form>}
    </>}
  </div>;
}

export default function ManualSplits({ isin, listing, inception, hasQuotes, disabled, refreshKey, onChanged }) {
  // Lazy mount, but keep mutation ownership alive when the disclosure closes.
  // Aborting fetch cannot undo a server commit; reconciliation must still run.
  const [opened, setOpened] = useState(false);
  return <details className="disclosure manual-splits" onToggle={(event) => { if (event.currentTarget.open) setOpened(true); }}>
    <summary>Splits</summary>
    {opened && (listing && hasQuotes
      ? <SplitEditor key={`${isin}:${listing.symbol}:${listing.exchange}`} isin={isin} listing={listing} inception={inception}
        disabled={disabled} refreshKey={refreshKey} onChanged={onChanged} />
      : <p>Save quotes for this ETF before adding manual splits.</p>)}
  </details>;
}
