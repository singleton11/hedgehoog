import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import CandlestickChart, { Metadata } from './CandlestickChart.jsx';
import { loadAllQuotes, pendingJob, STALE_GENERATION_MESSAGE } from './instruments.js';

const VARIANTS = { unknown: 'Unknown', price: 'Price', net_total_return: 'Net total return', gross_total_return: 'Gross total return' };

export default function IndexDetail({ instrumentId }) {
  const { request } = useAuth();
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [quotes, setQuotes] = useState([]);
  const [quotesLoading, setQuotesLoading] = useState(false);
  const [quotesError, setQuotesError] = useState(null);
  const [chartAttempt, setChartAttempt] = useState(0);
  const [snapshotRevision, setSnapshotRevision] = useState(0);
  const [quotesStale, setQuotesStale] = useState(false);
  const snapshotRefreshPending = useRef(false);
  const heading = useRef(null);
  const ready = Boolean(detail);
  useEffect(() => { if (ready) heading.current?.focus(); }, [ready]);

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    setLoading(true);
    setError(null);
    async function load() {
      try {
        const data = await request(`/indices/${encodeURIComponent(instrumentId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (data?.instrument_id !== instrumentId || typeof data.name !== 'string'
            || !Number.isInteger(data.summary?.quote_count) || data.summary.quote_count < 0) {
          throw new Error('The backend returned unexpected index details. Please retry.');
        }
        setDetail(data);
        if (snapshotRefreshPending.current) {
          snapshotRefreshPending.current = false;
          setSnapshotRevision((value) => value + 1);
        }
        setError(null);
        if (pendingJob(data.acquisition)) timer = setTimeout(load, 2000);
      } catch (err) {
        if (!controller.signal.aborted) setError(err.message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [request, instrumentId, attempt]);

  const generation = detail?.generation;
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    setQuotes([]);
    setQuotesLoading(true);
    setQuotesError(null);
    setQuotesStale(false);
    loadAllQuotes(request, instrumentId, controller.signal, { generation }, 'indices')
      .then((items) => { if (!controller.signal.aborted && items) setQuotes(items); })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setQuotesStale(err.status === 409);
          setQuotesError(err.status === 409 ? STALE_GENERATION_MESSAGE : err.message);
        }
      })
      .finally(() => { if (!controller.signal.aborted) setQuotesLoading(false); });
    return () => controller.abort();
  }, [request, instrumentId, ready, generation, chartAttempt, snapshotRevision]);

  function refreshSnapshot() {
    snapshotRefreshPending.current = true;
    setAttempt((value) => value + 1);
  }

  return <article aria-label="Index detail">
    <nav aria-label="Index navigation"><a href="#/indices">← Back to indices</a></nav>
    {loading && <p role="status">Loading index details…</p>}
    {error && <div><p role="alert">{error}</p><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry details</button></div>}
    {detail && <>
      <section className="instrument-heading">
        <p className="eyebrow">{detail.symbol} · Yahoo Finance index</p>
        <h2 className="instrument-name" ref={heading} tabIndex={-1}>{detail.name}</h2>
        <dl className="facts">
          <div><dt>Currency</dt><dd>{detail.currency || 'Unknown'}</dd></div>
          <div><dt>Return variant</dt><dd>{VARIANTS[detail.return_variant] || 'Unknown'}</dd></div>
          <div><dt>Identity</dt><dd>{detail.verification_status === 'verified' ? 'Verified index' : 'Unverified'}</dd></div>
          <div><dt>Saved quotes</dt><dd>{detail.summary.quote_count.toLocaleString()}</dd></div>
          <div><dt>History coverage</dt><dd>{detail.summary.first_date && detail.summary.last_date ? `${detail.summary.first_date} – ${detail.summary.last_date}` : 'No saved history'}</dd></div>
        </dl>
        <p className="muted">Index identity checks do not verify currency or dividend-reinvestment semantics. Return variant is catalogue metadata.</p>
        <p className="muted">Only the latest saved Yahoo Finance snapshot is available. Historical as_of cutoffs bound quote dates, not knowledge time; later provider corrections may remain in earlier quotes.</p>
      </section>
      {quotesLoading && <p role="status">Loading all quote pages…</p>}
      {quotesError && <div><p role="alert">Could not load the complete chart: {quotesError}</p>
        <button className="secondary" disabled={loading} onClick={quotesStale ? refreshSnapshot : () => setChartAttempt((value) => value + 1)}>
          {quotesStale ? 'Refresh latest snapshot' : 'Retry chart'}</button></div>}
      {quotes.length > 0 && <CandlestickChart quotes={quotes} instrumentType="index" />}
      {!quotesLoading && !quotesError && !quotes.length && <p className="empty-state">No saved quotes to chart yet. {pendingJob(detail.acquisition) ? 'Acquisition is in progress.' : 'Download market data from the index catalogue to view candlesticks.'}</p>}
      <section aria-label="Index market data">
        <h2>Market data</h2>
        <p role="status">{detail.acquisition?.status || 'Not requested'}{pendingJob(detail.acquisition) && detail.acquisition?.phase ? ` · ${detail.acquisition.phase}` : ''}</p>
        {detail.acquisition?.error && <p role="alert">{detail.acquisition.error}</p>}
        {detail.acquisition?.warning && <p className="muted">{detail.acquisition.warning}</p>}
        {detail.history_request?.warning && <p className="muted">{detail.history_request.warning}</p>}
        <p><a href="#/indices">Download or refresh data in the index catalogue</a>. Viewing this page never starts a download.</p>
      </section>
      <details className="disclosure instrument-metadata"><summary>Data details</summary>
        <div className="disclosure-content">
          <Metadata title="Index catalogue metadata" label="Catalogue" value={{ instrument_id: detail.instrument_id, symbol: detail.symbol, name: detail.name, currency: detail.currency, return_variant: detail.return_variant, provenance: detail.provenance }} />
          <Metadata title="Index provider profile" label="Provider profile" value={detail.profile} />
          <Metadata title="Index acquisition and coverage" label="Acquisition & coverage" value={{ acquisition: detail.acquisition, summary: detail.summary, generation: detail.generation, history_request: detail.history_request }} />
        </div>
      </details>
    </>}
  </article>;
}
