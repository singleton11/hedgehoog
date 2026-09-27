import { useEffect, useRef, useState } from 'react';
import CandlestickChart, { Metadata } from './CandlestickChart.jsx';
import { acquisitionEstimate, displayText, listingPriceCurrency, pendingJob } from './instruments.js';
import useInstrument from './useInstrument.js';
import ReturnStatistics from './ReturnStatistics.jsx';
import ManualSplits from './ManualSplits.jsx';

function AcquisitionForm({ detail, mode, busy, onCancel, onConfirm }) {
  const [symbol, setSymbol] = useState(detail.listing?.symbol || '');
  const [exchange, setExchange] = useState(detail.listing?.exchange || '');
  const [manualOverride, setManualOverride] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const fixed = detail.summary.quote_count > 0;
  const manual = manualOverride && !fixed;
  const yahoo = detail.provider === 'yfinance';
  const estimate = acquisitionEstimate(detail, mode, manual);
  const metered = detail.budget?.metered !== false && detail.budget?.remaining != null;
  const insufficientBudget = metered && detail.budget.remaining < estimate.minimumCalls;
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
      {yahoo
        ? <p>Enter a Yahoo Finance symbol and exchange MIC. Examples: <a href="https://finance.yahoo.com/quote/EUNL.DE" target="_blank" rel="noopener noreferrer">EUNL.DE</a> / XETR, <a href="https://finance.yahoo.com/quote/IWDA.AS" target="_blank" rel="noopener noreferrer">IWDA.AS</a> / XAMS.</p>
        : <p>Find a symbol and MIC in the provider ticker workbook.</p>}
      <div className="listing-fields">
        <div><label htmlFor="provider-symbol">Provider symbol</label><input id="provider-symbol" value={symbol}
          onChange={(event) => { setSymbol(event.target.value); setConfirmed(false); }} required maxLength={64} disabled={busy} autoComplete="off" /></div>
        <div><label htmlFor="exchange-mic">Exchange MIC</label><input id="exchange-mic" value={exchange}
          onChange={(event) => { setExchange(event.target.value); setConfirmed(false); }} required minLength={4} maxLength={4} pattern="[A-Za-z0-9]{4}" disabled={busy} autoComplete="off" /></div>
      </div>
    </>}
    <p><strong>Range:</strong> {estimate.range}.</p>
    {yahoo
      ? <p><strong>Provider requests:</strong> Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.</p>
      : <p><strong>Estimated provider calls:</strong> {estimate.calls === null ? `At least ${estimate.minimumCalls}` : `About ${estimate.calls}`}. Actual usage may vary.</p>}
    {insufficientBudget && <p className="muted">This request needs at least {estimate.minimumCalls} calls. Not enough local budget.</p>}
    <details className="disclosure"><summary>Request details</summary>
      <div className="disclosure-content">
        <p>{yahoo
          ? (manual ? 'Manual selection skips Yahoo Search.' : detail.listing
            ? 'The saved listing is reused without a Yahoo Search lookup.'
            : 'We select a Yahoo Finance listing from Yahoo Search and cache its symbol and exchange MIC for future requests.')
          : (manual ? 'Manual selection skips ticker lookup.' : detail.listing
            ? 'The saved listing is reused without a ticker lookup.'
            : 'We select the first named, EOD-enabled exact ticker.exchange match with a valid MIC from the provider and cache it for future requests.')}</p>
        {fixed && <p>The listing cannot change once quotes are saved.</p>}
        <p>{yahoo
          ? 'Each explicit request refreshes the full Yahoo Finance price history from inception to the latest available trading day, avoiding mixed adjusted-data vintages. Saved prices use provider split-adjusted OHLC; Adj Close is kept separately, and distributions are not reinvested in analytics.'
          : <>{estimate.lookupCalls > 0 && 'At least 1 ticker-list lookup + '}1 profile call + at least 1 split-history page + {estimate.pages === null ? 'at least 1 history page' : `${estimate.pages} history page(s)`}.
            {estimate.lookupCalls > 0 && ' Ticker lookup may need extra pages.'} Split history is refreshed from inception for derived calculations only; candles remain unadjusted. Split and price pages contain up to 1,000 rows and may need extra calls; price estimates use calendar days.</>}</p>
        {!yahoo && <p>The local budget excludes requests made outside Hedgehoog.</p>}
      </div>
    </details>
    <label className="checkbox-label"><input type="checkbox" checked={confirmed} disabled={busy}
      onChange={(event) => setConfirmed(event.target.checked)} required />
      {yahoo ? 'Allow this request to fetch Yahoo Finance data.' : 'Allow this request to use provider quota.'}</label>
    <div className="button-group"><button type="submit" disabled={blocked || !confirmed}>{busy ? 'Submitting…' : title}</button>
      <button type="button" className="secondary" onClick={onCancel} disabled={busy}>Cancel</button></div>
  </form>;
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
  const [splitRevision, setSplitRevision] = useState(0);
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
  const metered = budget?.metered !== false && budget?.remaining != null;
  const pending = pendingJob(acquisition);
  const hasData = summary.quote_count > 0 || quotes.length > 0;
  const priceCurrency = detail.price_currency_override || listingPriceCurrency(quotes);
  const minimumCalls = acquisitionEstimate(detail, 'update').minimumCalls;
  // Keep review accessible with three calls remaining: a manual override can
  // skip lookup. The form enforces the minimum for the chosen acquisition.
  const blocked = pending || submitting || !detail.provider_configured || (metered && budget.remaining < 3);

  async function confirm(body) {
    if (await acquire(body)) setReviewMode(null);
  }

  return <article aria-label="Instrument detail">
    <section className="instrument-heading">
      <p className="eyebrow">{isin}</p>
      <h2 className="instrument-name" ref={heading} tabIndex={-1}>{displayText(catalogue.name) || isin}</h2>
      <p className="muted">{[displayText(catalogue.ticker), displayText(catalogue.asset_class) || 'ETF', `Fund currency: ${catalogue.currency || 'unknown'}`].filter(Boolean).join(' · ')}
        {listing && <> · {listing.symbol} / {listing.exchange}</>}</p>
      <p><a href={`#/goal/${encodeURIComponent(isin)}`}>Plan a goal with this ETF</a></p>
    </section>
    <ReturnStatistics isin={isin} generation={detail.generation} refreshKey={JSON.stringify([acquisition?.id, acquisition?.status, detail.price_currency_override, splitRevision])} />
    {!quotes.length && priceCurrency && <p className="muted">Price currency: {priceCurrency}</p>}
    {quotesLoading && <p role="status">Loading all quote pages…{quotes.length ? ' Showing previously loaded quotes meanwhile.' : ''}</p>}
    {quotesError && <div><p role="alert">Could not load the complete chart: {quotesError} Previously loaded quotes are retained.</p><button className="secondary" onClick={retryQuotes}>Retry chart</button></div>}
    {quotes.length > 0 ? <CandlestickChart quotes={quotes} priceCurrencyOverride={detail.price_currency_override} priceBasis={detail.price_basis} /> : !quotesLoading && <p className="empty-state">No saved quotes to chart yet. {pending ? 'Acquisition is in progress.' : 'Choose Request Data to begin.'}</p>}
    <section aria-label="Market data acquisition">
      <h2>Market data</h2>
      {!detail.provider_configured && <p className="muted">Market data provider is not configured.</p>}
      {!reviewMode && metered && budget.remaining < minimumCalls && <p className="muted">Insufficient local budget. Requires {minimumCalls} calls{!listing ? ' with automatic lookup' : ''}.</p>}
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
      {detail.price_basis === 'provider_split_adjusted'
        ? <details className="disclosure manual-splits"><summary>Splits</summary><p>Yahoo Finance prices are already provider split-adjusted. Manual split corrections are not available for this price basis.</p></details>
        : <ManualSplits isin={isin} listing={listing} inception={catalogue.inception_date} hasQuotes={hasData}
          disabled={pending || submitting} refreshKey={JSON.stringify([acquisition?.id, acquisition?.status])}
          onChanged={() => setSplitRevision((value) => value + 1)} />}
    </section>
    <details className="disclosure instrument-metadata"><summary>Data details</summary>
      <div className="disclosure-content">
        {listing && ['tickerslist', 'yahoo_search'].includes(listing_resolution?.source) && <p>Auto-selected listing: {displayText(listing_resolution.name) || listing.symbol}.</p>}
        <dl className="facts">
          <div><dt>Profile collected (UTC)</dt><dd>{profile?.collected_at || 'Not collected'}</dd></div>
          <div><dt>Quotes collected (UTC)</dt><dd>{quotes.length ? quotes.reduce((latest, quote) => quote.collected_at > latest ? quote.collected_at : latest, '') || 'Unknown' : 'Not loaded'}</dd></div>
        </dl>
        <p className="muted">Collection time records when data was learned. Null means not supplied. {detail.provider === 'yfinance' ? 'Yahoo charts use provider split-adjusted OHLC, with Adj Close kept separately; distributions are not reinvested in analytics.' : 'Charts use the saved provider price basis.'}</p>
        <Metadata title="Catalogue metadata" label="Catalogue" value={catalogue} />
        <Metadata title="Tickerinfo profile and collection metadata" label="Provider profile" value={profile} />
        <Metadata title="Listing, acquisition and coverage metadata" label="Acquisition & coverage" value={{ instrument_id: detail.instrument_id, provider: detail.provider, price_basis: detail.price_basis, listing, listing_resolution, price_currency_override: detail.price_currency_override, acquisition, summary, provider_configured: detail.provider_configured, budget }} />
      </div>
    </details>
  </article>;
}
