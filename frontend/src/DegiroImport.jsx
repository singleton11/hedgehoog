import { useEffect, useRef, useState } from 'react';
import { formatMoney } from './money.js';

const MAX_BYTES = 1_000_000;
const decimal = (value) => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value);
const count = (value) => Number.isInteger(value) && value >= 0;
function validPreview(data) {
  return data && typeof data.can_import === 'boolean' && typeof data.preview_token === 'string'
    && (!data.can_import || data.preview_token.length > 0)
    && Array.isArray(data.transactions) && data.transactions.every((item) => item && typeof item.date === 'string'
      && ['new', 'duplicate', 'removed'].includes(item.status) && Array.isArray(item.source_rows)
      && (['deposit', 'withdrawal'].includes(item.type) ? decimal(item.amount_eur)
        : ['buy', 'sell'].includes(item.type) && typeof item.instrument_id === 'string'
          && ['quantity', 'price_eur', 'commission_eur'].every((key) => decimal(item[key]))))
    && Array.isArray(data.errors) && data.errors.every((item) => item && typeof item.message === 'string')
    && Array.isArray(data.ignored_rows) && data.ignored_rows.every((item) => item && typeof item.description === 'string' && typeof item.reason === 'string')
    && Array.isArray(data.warnings) && data.warnings.every((item) => typeof item === 'string')
    && data.summary && count(data.summary.new_count) && count(data.summary.duplicate_count)
    && (data.summary.cash_eur === null || decimal(data.summary.cash_eur))
    && ['deposits_eur', 'withdrawals_eur', 'commission_eur'].every((key) => decimal(data.summary[key]))
    && Array.isArray(data.summary.positions) && data.summary.positions.every((item) => item && typeof item.instrument_id === 'string' && decimal(item.quantity) && decimal(item.cost_basis_eur));
}
const euros = (value) => value == null ? 'Unavailable' : `${formatMoney(value)} EUR`;

// The tracker remounts this panel at its route/auth/request identity boundary.
export default function DegiroImport({ request, portfolioPath, onImported, disabled = false }) {
  const [open, setOpen] = useState(false);
  const [csv, setCsv] = useState(null);
  const [preview, setPreview] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [phase, setPhase] = useState('idle');
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [uncertain, setUncertain] = useState(false);
  const active = useRef(null), reader = useRef(null), generation = useRef(0), trigger = useRef(null), fileInput = useRef(null);
  function invalidate() {
    generation.current += 1;
    active.current?.abort(); active.current = null;
    reader.current?.abort(); reader.current = null;
  }
  useEffect(() => () => invalidate(), [request, portfolioPath]);
  function reset() {
    invalidate(); setCsv(null); setPreview(null); setConfirmed(false); setError(null); setMessage(null); setUncertain(false); setPhase('idle');
  }
  function select(event) {
    if (phase === 'saving') return;
    reset();
    const file = event.target.files?.[0];
    if (!file) return;
    if (!/\.csv$/i.test(file.name)) { setError('Choose a .csv file.'); return; }
    if (file.size > MAX_BYTES) { setError('CSV must be at most 1,000,000 UTF-8 bytes.'); return; }
    const version = generation.current;
    const next = new FileReader(); reader.current = next; setPhase('reading');
    next.onload = () => {
      if (version !== generation.current) return;
      reader.current = null; setPhase('idle');
      const content = next.result;
      if (typeof content !== 'string' || !content.trim()) { setError('The CSV file is empty or unreadable.'); return; }
      if (new TextEncoder().encode(content).byteLength > MAX_BYTES) { setError('CSV must be at most 1,000,000 UTF-8 bytes.'); return; }
      setCsv(content);
    };
    next.onerror = () => {
      if (version !== generation.current) return;
      reader.current = null; setPhase('idle'); setError('Unable to read the CSV. Select the file again to retry.');
    };
    next.readAsText(file, 'UTF-8');
  }
  async function run(save) {
    if (active.current || csv === null || disabled || (save && (!preview?.can_import || preview.errors.length || !confirmed)) || (!save && uncertain)) return;
    const version = generation.current;
    const abort = new AbortController(); active.current = abort;
    setPhase(save ? 'saving' : 'previewing'); setError(null); setMessage(null);
    if (!save) { setPreview(null); setConfirmed(false); }
    try {
      const result = await request(`${portfolioPath}/imports/degiro${save ? '' : '/preview'}`, {
        method: 'POST', body: save ? { csv, preview_token: preview.preview_token } : { csv }, signal: abort.signal,
      });
      if (abort.signal.aborted || version !== generation.current) return;
      if (save) {
        if (!count(result?.imported_count) || !count(result?.duplicate_count)) throw new Error('Unexpected import response.');
        setMessage(`Imported ${result.imported_count} transactions; ${result.duplicate_count} duplicates skipped.`);
        setPreview(null); setConfirmed(false); setUncertain(false); setCsv(null);
        if (fileInput.current) fileInput.current.value = '';
        onImported();
      } else {
        if (!validPreview(result)) throw new Error('Unexpected preview response. Please retry.');
        setPreview(result);
      }
    } catch (failure) {
      if (abort.signal.aborted || version !== generation.current) return;
      const rejected = save && failure.status >= 400 && failure.status < 500;
      setError(rejected
        ? `${failure.message || 'Import rejected.'} Nothing was imported by this request. Preview the statement again before importing.`
        : failure.message || 'Unable to complete the request. Please retry.');
      if (rejected) { setPreview(null); setConfirmed(false); setUncertain(false); }
      else if (save) setUncertain(true);
    } finally {
      if (!abort.signal.aborted && version === generation.current) { active.current = null; setPhase('idle'); }
    }
  }
  const saving = phase === 'saving';
  return <section className="degiro-import" aria-label="DEGIRO statement import">
    <button ref={trigger} type="button" className="secondary" disabled={disabled || open} aria-expanded={open} onClick={() => { reset(); setOpen(true); }}>Import DEGIRO CSV</button>
    {open && <div className="planner-create">
      <h3>Import DEGIRO account statement</h3>
      <p>Use a DEGIRO Account CSV with these exact English headers: <code>Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id</code> (including the two empty headers). Comma and semicolon separators are supported; descriptions may be Dutch or English. EUR only.</p>
      <p>Include the complete funding history so every purchase is funded. iDEAL reservations and reversals are ignored only when matched to a final deposit, which is counted once at funding time. Unmatched or ambiguous reservations block import. Cash sweeps are ignored.</p>
      <p>The CSV contains ISINs, not exchanges. Check the chosen acquired EUR listing and all transactions before importing.</p>
      <label htmlFor="degiro-csv">DEGIRO CSV file (maximum 1,000,000 UTF-8 bytes)</label>
      <input ref={fileInput} id="degiro-csv" type="file" accept=".csv,text/csv" disabled={saving} onChange={select} />
      <div className="button-group">
        <button type="button" disabled={disabled || csv === null || phase !== 'idle' || uncertain} onClick={() => run(false)}>Preview statement</button>
        <button type="button" className="secondary" disabled={saving} onClick={() => { reset(); setOpen(false); trigger.current?.focus(); }}>Cancel import</button>
      </div>
      {phase !== 'idle' && <p role="status">{phase === 'reading' ? 'Reading CSV…' : saving ? 'Importing statement…' : 'Previewing statement…'}</p>}
      {error && <p role="alert">{error}</p>}
      {uncertain && <p role="alert">The import may have been saved. Retry import uses the exact same CSV and preview token; do not create a new preview to retry a lost response.</p>}
      {message && <p role="status">{message}</p>}
      {preview && <>
        <h4>Statement preview</h4>
        <p>{preview.summary.new_count} new transactions · {preview.summary.duplicate_count} duplicates (skipped)</p>
        <p>Resulting cash: {euros(preview.summary.cash_eur)} · Deposits: {euros(preview.summary.deposits_eur)} · Withdrawals: {euros(preview.summary.withdrawals_eur)} · Commission: {euros(preview.summary.commission_eur)}</p>
        {preview.summary.positions.length > 0 && <ul aria-label="Resulting positions">{preview.summary.positions.map((position, index) => <li key={index}>{position.instrument_id}: {position.quantity} units · cost basis {euros(position.cost_basis_eur)}</li>)}</ul>}
        {preview.warnings.length > 0 && <ul aria-label="Import warnings">{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
        {preview.errors.length > 0 && <div role="alert"><h4>Import errors</h4><ul>{preview.errors.map((item, index) => <li key={index}>{item.row == null ? 'Statement' : `Line ${item.row}`}: {item.message}</li>)}</ul></div>}
        {!preview.can_import && <p role="alert">This statement cannot be imported. Resolve the errors and preview again.</p>}
        <p id="degiro-transactions-scroll" className="portfolio-scroll-hint">Scroll horizontally to view all columns.</p>
        <div className="planner-table-scroll" tabIndex={0} role="region" aria-label="DEGIRO preview transactions" aria-describedby="degiro-transactions-scroll"><table className="planner-table portfolio-table"><caption>Proposed transactions and selected EUR listings</caption><thead><tr>{['Lines', 'Date', 'Type', 'Status', 'Chosen listing / ISIN', 'Quantity', 'Price (EUR)', 'Commission (EUR)', 'Amount (EUR)'].map((label) => <th key={label} scope="col">{label}</th>)}</tr></thead>
          <tbody>{preview.transactions.map((item, index) => <tr key={index}><td>{item.source_rows.join(', ')}</td><td>{item.date}</td><td>{item.type}</td><td>{item.status}</td><td className="portfolio-instrument">{[item.instrument?.name, item.instrument?.symbol, item.instrument?.exchange, item.instrument_id].filter(Boolean).join(' · ') || '—'}</td><td>{item.quantity ?? '—'}</td><td>{formatMoney(item.price_eur, { unavailable: '—' })}</td><td>{formatMoney(item.commission_eur, { unavailable: '—' })}</td><td>{formatMoney(item.amount_eur, { unavailable: '—' })}</td></tr>)}</tbody></table></div>
        {preview.ignored_rows.length > 0 && <><h4>Ignored rows</h4><ul>{preview.ignored_rows.map((item, index) => <li key={index}>Line {item.row}: {item.description} — {item.reason}</li>)}</ul></>}
        <label className="checkbox-label"><input type="checkbox" checked={confirmed} disabled={saving || !preview.can_import || preview.errors.length > 0} onChange={(event) => setConfirmed(event.target.checked)} /> I checked the chosen EUR listings and all transactions.</label>
        <button type="button" disabled={disabled || phase !== 'idle' || !confirmed || !preview.can_import || preview.errors.length > 0} onClick={() => run(true)}>{uncertain ? 'Retry import' : 'Import transactions'}</button>
      </>}
    </div>}
  </section>;
}
