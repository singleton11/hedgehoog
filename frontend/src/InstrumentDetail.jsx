import { useEffect, useRef, useState } from 'react';
import CandlestickChart, { Metadata } from './CandlestickChart.jsx';
import { acquisitionEstimate, displayText, listingPriceCurrency, pendingJob } from './instruments.js';
import useInstrument from './useInstrument.js';
import ReturnStatistics from './ReturnStatistics.jsx';

function AcquisitionForm({ detail, mode, busy, onCancel, onConfirm }) {
  const [symbol, setSymbol] = useState(detail.listing?.symbol || '');
  // A saved MIC may belong to the failed listing the user is replacing.
  // Yahoo can resolve the exchange for the exact new symbol instead.
  const [exchange, setExchange] = useState('');
  const [manualOverride, setManualOverride] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const fixed = detail.summary.quote_count > 0;
  const manual = manualOverride && !fixed;
  const estimate = acquisitionEstimate(detail, mode, manual);
  const symbolError = manual && (!symbol.trim() ? 'Enter a provider symbol.'
    : symbol.trim().length > 64 || !/^[A-Z0-9^][A-Z0-9._^=\-]*$/i.test(symbol.trim())
      ? 'Enter a valid provider symbol, for example SPYI.DE.' : null);
  const exchangeError = manual && exchange.trim() && !/^[A-Za-z0-9]{4}$/.test(exchange.trim())
    ? 'Enter a four-character exchange MIC, or leave it blank to detect it from Yahoo.' : null;
  const blocked = busy || !detail.provider_configured || Boolean(symbolError || exchangeError);
  const title = mode === 'backfill' ? 'Confirm older history' : fixed ? 'Confirm update' : 'Confirm request';
  const formHeading = useRef(null);
  useEffect(() => { formHeading.current?.focus(); }, []);

  function submit(event) {
    event.preventDefault();
    if (!confirmed || blocked) return;
    onConfirm(manual
      ? { symbol: symbol.trim().toUpperCase(), ...(exchange.trim() ? { exchange: exchange.trim().toUpperCase() } : {}), confirmed: true, mode }
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
      <p>Enter a Yahoo Finance symbol. We'll detect its exchange automatically. Examples: <a href="https://finance.yahoo.com/quote/SPYI.DE" target="_blank" rel="noopener noreferrer">SPYI.DE</a>, <a href="https://finance.yahoo.com/quote/EUNL.DE" target="_blank" rel="noopener noreferrer">EUNL.DE</a>, <a href="https://finance.yahoo.com/quote/IWDA.AS" target="_blank" rel="noopener noreferrer">IWDA.AS</a>.</p>
      <div className="listing-fields">
        <div><label htmlFor="provider-symbol">Provider symbol</label><input id="provider-symbol" value={symbol}
          onChange={(event) => { setSymbol(event.target.value); setConfirmed(false); }} required maxLength={64} disabled={busy} autoComplete="off"
          aria-invalid={Boolean(symbolError)} aria-describedby={symbolError ? 'provider-symbol-error' : undefined} />
          {symbolError && <p id="provider-symbol-error" className="muted">{symbolError}</p>}</div>
        <div><label htmlFor="exchange-mic">Exchange MIC</label><input id="exchange-mic" value={exchange}
          onChange={(event) => { setExchange(event.target.value.trim()); setConfirmed(false); }} minLength={4} maxLength={4} pattern="[A-Za-z0-9]{4}" disabled={busy} autoComplete="off"
          placeholder="Auto-detect" aria-invalid={Boolean(exchangeError)}
          aria-describedby={['exchange-mic-hint', exchangeError && 'exchange-mic-error'].filter(Boolean).join(' ')} />
          <p id="exchange-mic-hint" className="muted">Optional advanced override. Leave blank to detect the exchange from Yahoo.</p>
          {exchangeError && <p id="exchange-mic-error" className="muted">{exchangeError}</p>}</div>
      </div>
    </>}
    <p><strong>Range:</strong> {estimate.range}.</p>
    <p><strong>Provider requests:</strong> Yahoo Finance is experimental and unmetered here; exact underlying HTTP calls are not estimated.</p>
    <details className="disclosure"><summary>Request details</summary>
      <div className="disclosure-content">
        <p>{manual ? 'Manual selection uses your exact symbol without Yahoo Search. When Exchange MIC is blank, we detect the exchange from that symbol\'s Yahoo metadata.' : detail.listing
          ? 'The saved listing is reused without a Yahoo Search lookup.'
          : 'We select a Yahoo Finance listing from Yahoo Search and cache its symbol and exchange MIC for future requests.'}</p>
        {fixed && <p>The listing cannot change once quotes are saved.</p>}
        <p>Each explicit request refreshes the full Yahoo Finance price history from inception to the latest available trading day, replacing the previous saved snapshot rather than retaining older data vintages. Saved prices use provider split-adjusted OHLC; Adj Close is kept separately, and distributions are not reinvested in analytics.</p>
      </div>
    </details>
    <label className="checkbox-label"><input type="checkbox" checked={confirmed} disabled={busy}
      onChange={(event) => setConfirmed(event.target.checked)} required />
      Allow this request to fetch Yahoo Finance data.</label>
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
  const { detail, loading, error, pollError, postError, submitting, quotes, quotesLoading, quotesError, quotesStale,
    snapshotRevision, refreshSnapshot, acquire, retry, retryQuotes } = useInstrument(isin);
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

  const { catalogue, summary, listing, listing_resolution, acquisition, profile } = detail;
  const pending = pendingJob(acquisition);
  const hasData = summary.quote_count > 0 || quotes.length > 0;
  const priceCurrency = detail.price_currency_override || listingPriceCurrency(quotes);
  const blocked = pending || submitting || !detail.provider_configured;

  async function confirm(body) {
    if (await acquire(body)) setReviewMode(null);
  }

  return <article aria-label="Instrument detail">
    <section className="instrument-heading">
      <p className="eyebrow">{isin}</p>
      <h2 className="instrument-name" ref={heading} tabIndex={-1}>{displayText(catalogue.name) || isin}</h2>
      <p className="muted">{[displayText(catalogue.ticker), displayText(catalogue.asset_class) || 'ETF', `Fund currency: ${catalogue.currency || 'unknown'}`].filter(Boolean).join(' · ')}
        {listing && <> · {listing.symbol} / {listing.exchange}</>}</p>
      <dl className="facts">
        <div><dt>Asset class</dt><dd>{displayText(profile?.asset_class) || 'Not available'}</dd></div>
        <div><dt>Index</dt><dd>{displayText(profile?.index) || 'Not available'}</dd></div>
      </dl>
      <p><a href={`#/goal/${encodeURIComponent(isin)}`}>Plan a goal with this ETF</a></p>
    </section>
    <ReturnStatistics isin={isin} generation={detail.generation} refreshKey={JSON.stringify([acquisition?.id, acquisition?.status, detail.price_currency_override, snapshotRevision])}
      onRefreshSnapshot={refreshSnapshot} refreshingSnapshot={loading || submitting} />
    {!quotes.length && priceCurrency && <p className="muted">Price currency: {priceCurrency}</p>}
    {quotesLoading && <p role="status">Loading all quote pages…{quotes.length ? ' Showing previously loaded quotes meanwhile.' : ''}</p>}
    {quotesError && <div><p role="alert">Could not load the complete chart: {quotesError} Previously loaded quotes are retained.</p>
      <button className="secondary" onClick={quotesStale ? refreshSnapshot : retryQuotes} disabled={loading || submitting}>
        {quotesStale ? 'Refresh latest snapshot' : 'Retry chart'}</button></div>}
    {quotes.length > 0 ? <CandlestickChart quotes={quotes} priceCurrencyOverride={detail.price_currency_override} priceBasis={detail.price_basis} /> : !quotesLoading && <p className="empty-state">No saved quotes to chart yet. {pending ? 'Acquisition is in progress.' : 'Choose Request Data to begin.'}</p>}
    <section aria-label="Market data acquisition">
      <h2>Market data</h2>
      {!detail.provider_configured && <p className="muted">Market data provider is not configured.</p>}
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
      <details className="disclosure manual-splits"><summary>Splits</summary><p>Yahoo Finance prices are already provider split-adjusted. Manual split corrections are not available for this price basis.</p></details>
    </section>
    <details className="disclosure instrument-metadata"><summary>Data details</summary>
      <div className="disclosure-content">
        {listing && listing_resolution?.source === 'yahoo_search' && <p>Auto-selected listing: {displayText(listing_resolution.name) || listing.symbol}.</p>}
        <dl className="facts">
          <div><dt>Profile collected (UTC)</dt><dd>{profile?.collected_at || 'Not collected'}</dd></div>
          <div><dt>Quotes collected (UTC)</dt><dd>{quotes.length ? quotes.reduce((latest, quote) => quote.collected_at > latest ? quote.collected_at : latest, '') || 'Unknown' : 'Not loaded'}</dd></div>
        </dl>
        <p className="muted">Only the latest saved Yahoo Finance snapshot is available. Collection time records this download, not when a quote was first known. Historical as_of cutoffs bound quote dates, not knowledge time; later corrections and split adjustments can remain in earlier quotes. Yahoo charts use provider split-adjusted OHLC, with Adj Close kept separately; distributions are not reinvested in analytics.</p>
        <Metadata title="Catalogue metadata" label="Catalogue" value={catalogue} />
        <Metadata title="Tickerinfo profile and collection metadata" label="Provider profile" value={profile} />
        <Metadata title="Listing, acquisition and coverage metadata" label="Acquisition & coverage" value={{ instrument_id: detail.instrument_id, provider: detail.provider, price_basis: detail.price_basis, listing, listing_resolution, price_currency_override: detail.price_currency_override, acquisition, summary, provider_configured: detail.provider_configured, generation: detail.generation }} />
      </div>
    </details>
  </article>;
}
