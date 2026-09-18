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
      ? <p><strong>Listing:</strong> {detail.listing.symbol} / {detail.listing.exchange}{fixed ? ' · Locked' : ' · Saved'}</p>
      : <p><strong>Listing:</strong> Auto-select for {displayText(detail.catalogue.ticker) || 'catalogue ticker'}.</p>)}
    {!fixed && <label className="checkbox-label"><input type="checkbox" checked={manualOverride} disabled={busy}
      onChange={(event) => { setManualOverride(event.target.checked); setConfirmed(false); }} />
      Manual listing override</label>}
    {manual && <>
      <p>Find a symbol and MIC in the <a href="https://marketstack.com/download/v2_eod_tickers_finnworlds_tiingo.xlsx" target="_blank" rel="noopener noreferrer">Marketstack ticker workbook (XLSX)</a>.</p>
      <div className="listing-fields">
        <div><label htmlFor="provider-symbol">Provider symbol</label><input id="provider-symbol" value={symbol}
          onChange={(event) => { setSymbol(event.target.value); setConfirmed(false); }} required maxLength={64} disabled={busy} autoComplete="off" /></div>
        <div><label htmlFor="exchange-mic">Exchange MIC</label><input id="exchange-mic" value={exchange}
          onChange={(event) => { setExchange(event.target.value); setConfirmed(false); }} required minLength={4} maxLength={4} pattern="[A-Za-z0-9]{4}" disabled={busy} autoComplete="off" /></div>
      </div>
    </>}
    <p><strong>Range:</strong> {estimate.range}.</p>
    <p><strong>Estimated provider calls:</strong> {estimate.calls === null ? `At least ${estimate.minimumCalls}` : `About ${estimate.calls}`}. Actual usage may vary.</p>
    <Budget budget={detail.budget} />
    {insufficientBudget && <p className="muted">This request needs at least {estimate.minimumCalls} calls. Not enough local budget.</p>}
    <details className="disclosure"><summary>Request details</summary>
      <div className="disclosure-content">
        <p>{manual ? 'Manual selection skips ticker lookup.' : detail.listing
          ? 'The saved listing is reused without a ticker lookup.'
          : 'We select the first named, EOD-enabled exact ticker.exchange match with a valid MIC from Marketstack and cache it for future requests.'}</p>
        {fixed && <p>The listing cannot change once quotes are saved.</p>}
        <p>{estimate.lookupCalls > 0 && 'At least 1 ticker-list lookup + '}1 profile call + {estimate.pages === null ? 'at least 1 history page' : `${estimate.pages} history page(s)`}.
          {estimate.lookupCalls > 0 && ' Ticker lookup may need extra pages.'} History pages contain up to 1,000 rows; estimates use calendar days.</p>
        <p>The local budget excludes requests made outside Hedgehoog.</p>
      </div>
    </details>
    <label className="checkbox-label"><input type="checkbox" checked={confirmed} disabled={busy}
      onChange={(event) => setConfirmed(event.target.checked)} required />
      Allow this request to use provider quota.</label>
    <div className="button-group"><button type="submit" disabled={blocked || !confirmed}>{busy ? 'Submitting…' : title}</button>
      <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button></div>
  </form>;
}

function Budget({ budget }) {
  return <div className="budget">
    {budget ? <p title={`${budget.month} · ${budget.used} used. Excludes requests outside Hedgehoog.`}><strong>Local budget:</strong> {budget.remaining.toLocaleString()} / {budget.limit.toLocaleString()} calls left</p>
      : <p>Local budget unavailable.</p>}
  </div>;
}

function JobStatus({ job }) {
  if (!job) return null;
  return <div className="job-status">
    <p role="status"><strong>Acquisition: {job.status}</strong>{pendingJob(job) && ` · ${job.rows ?? 0} rows saved`}</p>
    {job.error && <p role="alert">Acquisition failed: {job.error}. Saved quotes remain available.</p>}
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
      <p className="muted">{[displayText(catalogue.ticker), displayText(catalogue.asset_class) || 'ETF', `Fund currency: ${catalogue.currency || 'unknown'}`].filter(Boolean).join(' · ')}
        {listing && <> · {listing.symbol} / {listing.exchange}</>}</p>
    </section>
    <section aria-label="Market data acquisition">
      <h2>Market data</h2>
      {!reviewMode && <Budget budget={budget} />}
      {!detail.provider_configured && <p className="muted">Marketstack is not configured.</p>}
      {!reviewMode && budget && budget.remaining < minimumCalls && <p className="muted">Insufficient local budget. Requires {minimumCalls} calls{!listing ? ' with automatic lookup' : ''}.</p>}
      <JobStatus job={acquisition} />
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
      <h2>Coverage</h2>
      <dl className="facts">
        <div><dt>Coverage status</dt><dd>{summary.history_status}</dd></div>
        <div><dt>Saved quotes</dt><dd>{summary.quote_count}</dd></div>
        <div><dt>Requested from</dt><dd>{summary.requested_from || 'Not requested'}</dd></div>
        <div><dt>Returned range</dt><dd>{summary.first_date && summary.last_date ? `${summary.first_date} – ${summary.last_date}` : 'No quotes returned'}</dd></div>
      </dl>
      {summary.history_status === 'empty' && <p>No quotes returned. Check the listing and plan access.</p>}
    </section>
    {quotesLoading && <p role="status">Loading all quote pages…{quotes.length ? ' Showing previously loaded quotes meanwhile.' : ''}</p>}
    {quotesError && <div><p role="alert">Could not load the complete chart: {quotesError} Previously loaded quotes are retained.</p><button className="secondary" onClick={retryQuotes}>Retry chart</button></div>}
    {quotes.length > 0 ? <CandlestickChart quotes={quotes} /> : !quotesLoading && <p className="empty-state">No saved quotes to chart yet. {pending ? 'Acquisition is in progress.' : 'Choose Request Data to begin.'}</p>}
    <details className="disclosure instrument-metadata"><summary>Data details</summary>
      <div className="disclosure-content">
        {listing && listing_resolution?.source === 'tickerslist' && <p>Auto-selected listing: {displayText(listing_resolution.name) || listing.symbol}.</p>}
        <dl className="facts">
          <div><dt>Profile collected (UTC)</dt><dd>{profile?.collected_at || 'Not collected'}</dd></div>
          <div><dt>Quotes collected (UTC)</dt><dd>{quotes.length ? quotes.reduce((latest, quote) => quote.collected_at > latest ? quote.collected_at : latest, '') || 'Unknown' : 'Not loaded'}</dd></div>
        </dl>
        <p className="muted">Collection time records when data was learned. Null means not supplied. Charts use unadjusted prices; updates do not refresh older adjustments.</p>
        <Metadata title="Catalogue metadata" label="Catalogue" value={catalogue} />
        <Metadata title="Tickerinfo profile and collection metadata" label="Provider profile" value={profile} />
        <Metadata title="Listing, acquisition and coverage metadata" label="Acquisition & coverage" value={{ instrument_id: detail.instrument_id, listing, listing_resolution, acquisition, summary, provider_configured: detail.provider_configured, budget }} />
      </div>
    </details>
  </article>;
}
