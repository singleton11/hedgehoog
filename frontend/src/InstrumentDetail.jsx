import { useEffect, useRef, useState } from 'react';
import CandlestickChart, { Metadata } from './CandlestickChart.jsx';
import { acquisitionEstimate, displayText, pendingJob } from './instruments.js';
import useInstrument from './useInstrument.js';

function AcquisitionForm({ detail, mode, busy, onCancel, onConfirm }) {
  const [symbol, setSymbol] = useState(detail.listing?.symbol || '');
  const [exchange, setExchange] = useState(detail.listing?.exchange || '');
  const [manualOverride, setManualOverride] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const fixed = detail.summary.quote_count > 0;
  const manual = manualOverride && !fixed;
  const estimate = acquisitionEstimate(detail, mode, manual);
  const insufficientBudget = detail.budget && detail.budget.remaining < estimate.minimumCalls;
  const invalidListing = manual && (!symbol.trim() || !/^[A-Za-z0-9]{4}$/.test(exchange.trim()));
  const blocked = busy || !detail.provider_configured || insufficientBudget || invalidListing;
  const title = mode === 'backfill' ? 'Confirm older history' : fixed ? 'Confirm update' : 'Confirm request';
  const formHeading = useRef(null);
  useEffect(() => { formHeading.current?.focus(); }, []);

  function submit(event) {
    event.preventDefault();
    if (!confirmed || blocked) return;
    onConfirm(manual
      ? { symbol: symbol.trim().toUpperCase(), exchange: exchange.trim().toUpperCase(), confirmed: true, mode }
      : { confirmed: true, mode });
  }

  return <form className="acquisition-form" aria-label={title} onSubmit={submit}>
    <h3 ref={formHeading} tabIndex={-1}>{title}</h3>
    {!manual && (detail.listing
      ? <p>The saved listing <strong>{detail.listing.symbol} / {detail.listing.exchange}</strong> will be reused. No ticker-list lookup is needed.</p>
      : <p>After you authorize this request, the server looks up the catalogue ticker <strong>{displayText(detail.catalogue.ticker) || '(not supplied)'}</strong> in Marketstack's ticker list. It automatically selects the first named, EOD-enabled exact ticker.exchange match (the ticker followed by a dot and an uppercase alphanumeric exchange suffix) with a valid four-character exchange MIC, then caches that listing for this instrument and reuses it for future requests.</p>)}
    <p className="muted">Listing selection is not ISIN verification. You do not need to enter or confirm a provider symbol or MIC for automatic acquisition.</p>
    {!fixed && <label className="checkbox-label"><input type="checkbox" checked={manualOverride} disabled={busy}
      onChange={(event) => { setManualOverride(event.target.checked); setConfirmed(false); }} />
      Manual listing override (advanced / lookup failures)</label>}
    {manual && <>
      <p>Override automatic selection or the saved listing before quotes exist. Choose a provider symbol and four-character MIC using the <a href="https://marketstack.com/download/v2_eod_tickers_finnworlds_tiingo.xlsx" target="_blank" rel="noopener noreferrer">public Marketstack ticker workbook (XLSX)</a>. This skips ticker-list lookup.</p>
      <div className="listing-fields">
        <div><label htmlFor="provider-symbol">Provider symbol</label><input id="provider-symbol" value={symbol}
          onChange={(event) => { setSymbol(event.target.value); setConfirmed(false); }} required maxLength={64} disabled={busy} autoComplete="off" /></div>
        <div><label htmlFor="exchange-mic">Exchange MIC</label><input id="exchange-mic" value={exchange}
          onChange={(event) => { setExchange(event.target.value); setConfirmed(false); }} required minLength={4} maxLength={4} pattern="[A-Za-z0-9]{4}" disabled={busy} autoComplete="off" /></div>
      </div>
    </>}
    {fixed && <p className="muted">This saved listing is locked because quotes already exist. Updates and older-history requests cannot switch listings.</p>}
    <p><strong>Requested range:</strong> {estimate.range}. {mode === 'backfill' ? 'This explicitly retries older history, rather than only newer dates.' : 'The first acquisition requests history from catalogue inception.'}</p>
    <p><strong>Estimated provider calls:</strong> {estimate.calls === null ? `At least ${estimate.minimumCalls}` : `About ${estimate.calls}`} = {estimate.lookupCalls > 0 && 'at least 1 tickerslist lookup + '}1 tickerinfo + {estimate.pages === null ? 'at least 1 EOD page' : `${estimate.pages} EOD page(s)`}.
      {estimate.lookupCalls > 0 && ' Ticker-list lookup may require multiple pages, adding calls beyond this estimate.'}
      {' '}EOD is paginated at up to 1,000 rows per page. The calendar-day estimate is conservative; actual pages, failures and provider access can change usage.</p>
    <Budget budget={detail.budget} />
    {insufficientBudget && <p className="notice">This request needs at least {estimate.minimumCalls} provider calls; the remaining local budget is insufficient.</p>}
    <p className="notice">Your plan and the selected listing determine available history; responses may omit older dates. A successful job does not mean complete since-inception coverage. Retry Older History also consumes quota and may return no additional history.</p>
    <label className="checkbox-label"><input type="checkbox" checked={confirmed} disabled={busy}
      onChange={(event) => setConfirmed(event.target.checked)} required />
      I authorize this quota-consuming acquisition.</label>
    <div className="button-group"><button type="submit" disabled={blocked || !confirmed}>{busy ? 'Submitting…' : title}</button>
      <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button></div>
  </form>;
}

function Budget({ budget }) {
  return <div className="budget">
    {budget ? <p><strong>Local monthly budget ({budget.month}):</strong> {budget.used} used / {budget.limit} calls · {budget.remaining} remaining.</p>
      : <p>Local budget unavailable.</p>}
    <p className="muted">Conservative local accounting, not the provider dashboard balance. Requests made outside Hedgehoog are not included.</p>
  </div>;
}

function JobStatus({ job, coverageWarning }) {
  if (!job) return null;
  return <div className="job-status">
    <p role="status"><strong>Acquisition: {job.status}</strong>{job.phase ? ` · ${displayText(job.phase)}` : ''}
      {' '}· {job.pages ?? 0} pages · {job.rows ?? 0} rows · {job.calls ?? 0} calls</p>
    {pendingJob(job) && <p className="muted">This job continues on the server if you leave this page. Polling only reads saved status; it does not consume provider quota.</p>}
    {job.error && <p role="alert">Acquisition failed: {job.error}. Previously saved quotes remain available. Review the cause before explicitly requesting again.</p>}
    {job.warning && job.warning !== coverageWarning && <p className="notice">{job.warning}</p>}
  </div>;
}

export default function InstrumentDetail({ isin }) {
  const { detail, loading, error, pollError, postError, submitting, quotes, quotesLoading, quotesError, acquire, retry, retryQuotes } = useInstrument(isin);
  const [reviewMode, setReviewMode] = useState(null);
  const heading = useRef(null);
  const reviewButton = useRef(null);
  const previousReview = useRef(null);
  const ready = Boolean(detail);
  useEffect(() => { if (ready) heading.current?.focus(); }, [ready]);
  useEffect(() => {
    if (previousReview.current && !reviewMode) reviewButton.current?.focus();
    previousReview.current = reviewMode;
  }, [reviewMode]);
  if (!detail) return <section aria-label="Instrument detail">
    {loading && <p role="status">Loading instrument…</p>}
    {error && <><p role="alert">{error}</p><button onClick={retry}>Retry details</button></>}
  </section>;

  const { catalogue, summary, listing, listing_resolution, acquisition, profile, budget } = detail;
  const pending = pendingJob(acquisition);
  const hasData = summary.quote_count > 0 || quotes.length > 0;
  const minimumCalls = acquisitionEstimate(detail, 'update').minimumCalls;
  // Keep review accessible with two calls remaining: a manual override can
  // skip lookup. The form enforces the minimum for the chosen acquisition.
  const blocked = pending || submitting || !detail.provider_configured || (budget && budget.remaining < 2);

  async function confirm(body) {
    if (await acquire(body)) setReviewMode(null);
  }

  return <article aria-label="Instrument detail">
    <section className="instrument-heading">
      <p className="eyebrow">{isin}</p>
      <h2 className="instrument-name" ref={heading} tabIndex={-1}>{displayText(catalogue.name) || isin}</h2>
      <p>{displayText(catalogue.ticker) || 'No catalogue ticker'} · {displayText(catalogue.asset_class) || 'ETF'}
        {' '}· Fund currency: {catalogue.currency || 'unknown'} (not necessarily the listing price currency)</p>
      <p><strong>Marketstack listing:</strong> {listing ? `${listing.symbol} / ${listing.exchange}` : 'Not selected yet; resolved after authorization'}</p>
      {listing && listing_resolution?.source === 'tickerslist' && <p className="muted">Automatically selected from Marketstack's ticker list: {displayText(listing_resolution.name) || listing.symbol}. This is a listing selection, not ISIN verification.</p>}
    </section>
    <section aria-label="Market data acquisition">
      <h2>Market data</h2>
      <p>Only a confirmed request fetches provider data. Opening this page, searching, polling and viewing charts read the database.</p>
      <Budget budget={budget} />
      {!detail.provider_configured && <p className="notice">Marketstack is not configured on the server. Saved data is still available.</p>}
      {budget && budget.remaining < minimumCalls && <p className="notice">Insufficient local budget for the minimum {minimumCalls} provider calls{!listing ? ' with automatic lookup' : ''}. {budget.remaining >= 2 && !hasData ? 'You can review a manual override, which needs at least 2 calls.' : 'Acquisition is unavailable until budget is available.'}</p>}
      <JobStatus job={acquisition} coverageWarning={summary.warning} />
      {pollError && <div><p role="alert">Could not refresh acquisition status: {pollError} {pending ? 'Polling will retry automatically.' : 'You can retry without starting another acquisition.'}</p>
        <button className="secondary" onClick={retry} disabled={submitting}>Retry polling</button></div>}
      {postError && <p role="alert">Request could not be confirmed: {postError} Status is being refreshed; no acquisition is retried automatically.</p>}
      {!reviewMode && <div className="button-group">
        <button ref={reviewButton} disabled={blocked} onClick={() => setReviewMode('update')}>{hasData ? 'Update Data' : 'Request Data'}</button>
        {summary.history_status === 'partial' && <button className="secondary" disabled={blocked} onClick={() => setReviewMode('backfill')}>Retry Older History</button>}
      </div>}
      {reviewMode && !pending && <AcquisitionForm key={reviewMode} detail={detail} mode={reviewMode} busy={submitting}
        onCancel={() => setReviewMode(null)} onConfirm={confirm} />}
    </section>
    <section aria-label="History coverage">
      <h2>History coverage</h2>
      <dl className="facts">
        <div><dt>Coverage status</dt><dd>{summary.history_status}</dd></div>
        <div><dt>Saved quotes</dt><dd>{summary.quote_count}</dd></div>
        <div><dt>Requested from</dt><dd>{summary.requested_from || 'Not requested'}</dd></div>
        <div><dt>Returned range</dt><dd>{summary.first_date && summary.last_date ? `${summary.first_date} – ${summary.last_date}` : 'No quotes returned'}</dd></div>
        <div><dt>Profile collected at (UTC)</dt><dd>{profile?.collected_at || 'Not collected'}</dd></div>
        <div><dt>Latest quote observation (UTC)</dt><dd>{quotes.length ? quotes.reduce((latest, quote) => quote.collected_at > latest ? quote.collected_at : latest, '') || 'Unknown' : 'Not loaded'}</dd></div>
      </dl>
      {summary.warning && <p className="notice">{summary.warning}</p>}
      {summary.history_status === 'partial' && !summary.warning && <p className="notice">Only partial history is available. Plan access and listing coverage may limit the requested range; success is not proof of a complete since-inception download.</p>}
      {summary.history_status === 'available' && <p className="muted">Returned coverage starts near inception; this does not prove that every historical trading session is present.</p>}
      {summary.history_status === 'empty' && <p>No quotes were returned. Check the selected listing and provider access before requesting again.</p>}
    </section>
    {quotesLoading && <p role="status">Loading all quote pages…{quotes.length ? ' Showing previously loaded quotes meanwhile.' : ''}</p>}
    {quotesError && <div><p role="alert">Could not load the complete chart: {quotesError} Previously loaded quotes are retained.</p><button className="secondary" onClick={retryQuotes}>Retry chart</button></div>}
    {quotes.length > 0 ? <CandlestickChart quotes={quotes} /> : !quotesLoading && <p className="empty-state">No saved quotes to chart yet. {pending ? 'Acquisition is in progress.' : 'Use Request Data to explicitly acquire market data.'}</p>}
    <section aria-label="All instrument metadata">
      <h2>All instrument metadata</h2>
      <p className="muted">Complete saved fields, including nested and unfamiliar provider attributes. Collection time is when data was learned, not a historical fundamentals date. Null means not supplied; adjusted fields are preserved but are not charted. Incremental updates do not refresh earlier corporate-action adjustments.</p>
      <Metadata title="Catalogue metadata" value={catalogue} />
      <Metadata title="Tickerinfo profile and collection metadata" value={profile} />
      <Metadata title="Listing, acquisition and coverage metadata" value={{ instrument_id: detail.instrument_id, listing, listing_resolution, acquisition, summary, provider_configured: detail.provider_configured, budget }} />
    </section>
  </article>;
}
