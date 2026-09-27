import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { collectionAgeDays, displayText } from './instruments.js';
import { formatReturn, historicalReturnTitle, historicalVolatilityTitle, percent } from './ReturnStatistics.jsx';
import useAcquiredUpdates, { updatePending } from './useAcquiredUpdates.js';
import Icon from './Icon.jsx';

export const ACQUIRED_PAGE_SIZE = 20;

function CollectionTime({ value }) {
  const days = collectionAgeDays(value);
  if (days === null) return 'Unknown';
  const collected = new Date(value);
  const relative = `${days} ${days === 1 ? 'day' : 'days'} ago`;
  const exact = `${collected.toLocaleString(undefined, { timeZone: 'UTC' })} UTC`;
  return <time dateTime={value} title={exact} aria-label={`${relative}, ${exact}`}>{relative}</time>;
}

function ReturnCell({ value, title, headers }) {
  const display = formatReturn(value);
  return <td className={`annual-statistic ${display.className}`} title={title} headers={headers}>
    {display.text}
  </td>;
}

function HistoricalYearReturn({ item }) {
  return <ReturnCell value={item.historical_year_return} title={historicalReturnTitle(item)} headers="acquired-historical acquired-historical-return" />;
}

function UpdateConfirmation({ selection, onConfirm, onCancel }) {
  const [confirmed, setConfirmed] = useState(false);
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <form className="acquisition-form" aria-label="Confirm market data update" onSubmit={(event) => {
    event.preventDefault();
    if (confirmed) onConfirm();
  }}>
    <h3 ref={heading} tabIndex={-1}>Update {selection.isin ? selection.name : `all ${selection.total} acquired ETFs`}?</h3>
    <p>{selection.isin ? 'Refresh the saved Yahoo Finance profile and full price history.' : 'Update acquired ETFs across all pages, skipping those collected 0 days ago (less than 24 hours). Keep this page open until all requests are submitted.'} Saved listings are reused; each submitted refresh fetches full history through the latest available trading day.</p>
    <label className="checkbox-label"><input type="checkbox" required checked={confirmed}
      onChange={(event) => setConfirmed(event.target.checked)} />Allow {selection.isin ? 'this update' : 'these updates'} to fetch Yahoo Finance data.</label>
    <div className="button-group"><button type="submit" disabled={!confirmed}>Confirm {selection.isin ? 'update' : 'update all'}</button>
      <button type="button" className="secondary" onClick={onCancel}>Cancel</button></div>
  </form>;
}

function RemoveConfirmation({ selection, removing, error, onConfirm, onCancel }) {
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <form className="acquisition-form" aria-label="Confirm ETF removal" onSubmit={(event) => {
    event.preventDefault();
    if (!removing) onConfirm();
  }}>
    <h3 ref={heading} tabIndex={-1}>Remove {selection.name}?</h3>
    <p>This permanently deletes saved prices, profiles, splits and market-data settings for all users. Portfolios and saved calculations are kept, but live valuations may be unavailable until data is acquired again. The ETF remains searchable and can be added again.</p>
    {error && <p role="alert">{error}</p>}
    <div className="button-group"><button type="submit" disabled={removing}>{removing ? 'Removing…' : 'Delete saved data'}</button>
      <button type="button" className="secondary" disabled={removing} onClick={onCancel}>Cancel</button></div>
  </form>;
}

function UpdateStatus({ update }) {
  if (!update) return null;
  const status = update.submitting ? 'Submitting…' : update.checking ? 'Checking status…'
    : update.job?.status === 'succeeded' ? (update.error ? null : 'Updated')
    : update.job?.status === 'failed' ? 'Update failed' : update.job?.status === 'running' ? 'Updating…'
    : update.job?.status === 'queued' ? 'Queued' : null;
  return <>
    {status && <span className="muted" role="status">{status}</span>}
    {(update.error || update.job?.error) && <span className="update-error" role="alert">{update.error || update.job.error}</span>}
    {update.pollError && <span className="update-error" role="alert">{update.pollError}</span>}
  </>;
}

export default function AcquiredEtfs() {
  const { request } = useAuth();
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [selection, setSelection] = useState(null);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState(null);
  const [notice, setNotice] = useState(null);
  const removalRequest = useRef(null);
  const listHeading = useRef(null);
  useEffect(() => () => removalRequest.current?.abort(), [request]);
  const reviewButton = useRef(null);
  const previousSelection = useRef(null);
  useEffect(() => {
    if (previousSelection.current && !selection) reviewButton.current?.focus();
    previousSelection.current = selection;
  }, [selection]);
  const refresh = useCallback(() => setAttempt((value) => value + 1), []);
  const { updates, submitting, error: updateError, update, skippedCount, busy } = useAcquiredUpdates(refresh);
  const pendingCount = Object.values(updates).filter(updatePending).length;
  const completedCount = Object.values(updates).filter((entry) => entry.job?.status === 'succeeded' && !entry.error).length;
  const failedCount = Object.values(updates).filter((entry) => entry.error || entry.job?.status === 'failed').length;

  function review(event, nextSelection) {
    reviewButton.current = event.currentTarget;
    setRemoveError(null);
    setNotice(null);
    setSelection(nextSelection);
  }

  function closeReview() {
    setSelection(null);
  }

  async function removeSelected() {
    if (removalRequest.current) return;
    const controller = new AbortController();
    removalRequest.current = controller;
    setRemoving(true);
    setRemoveError(null);
    try {
      await request(`/instruments/${encodeURIComponent(selection.isin)}`, { method: 'DELETE', signal: controller.signal });
      if (controller.signal.aborted) return;
      setNotice(`${selection.name} removed. Saved market data deleted.`);
      setSelection(null);
      setPage(null);
      listHeading.current?.focus();
      refresh();
    } catch (error) {
      if (!controller.signal.aborted) setRemoveError(error.message);
    } finally {
      if (!controller.signal.aborted) {
        removalRequest.current = null;
        setRemoving(false);
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setPage((previous) => previous?.offset === offset ? previous : null);
    setError(null);
    async function load() {
      try {
        const data = await request(`/instruments/acquired?limit=${ACQUIRED_PAGE_SIZE}&offset=${offset}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!Array.isArray(data?.items) || !Number.isInteger(data.total) || data.total < 0
            || data.offset !== offset || data.limit !== ACQUIRED_PAGE_SIZE) {
          throw new Error('The backend returned an unexpected acquired ETF list. Please retry.');
        }
        if (offset > 0 && offset >= data.total) {
          setOffset(Math.max(0, Math.floor((data.total - 1) / ACQUIRED_PAGE_SIZE) * ACQUIRED_PAGE_SIZE));
          return;
        }
        setPage(data);
      } catch (error) {
        if (!controller.signal.aborted) setError(error.message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    load();
    return () => controller.abort();
  }, [request, offset, attempt]);

  return <section aria-labelledby="acquired-etfs-heading" className="acquired-page">
    <div className="section-heading page-heading">
      <div><p className="eyebrow">Your collection</p>
        <h2 id="acquired-etfs-heading" ref={listHeading} tabIndex={-1}>Acquired ETFs{page ? ` (${page.total})` : ''}</h2>
        <p className="muted">A clearer view of the funds you follow.</p>
      </div>
      <a className="button-link" href="#/add"><Icon name="plus" />Add ETF</a>
    </div>
    {notice && <p role="status">{notice}</p>}
    {selection?.remove && <RemoveConfirmation selection={selection} removing={removing} error={removeError}
      onConfirm={removeSelected} onCancel={closeReview} />}
    {selection && !selection.remove && <UpdateConfirmation key={selection.isin || 'all'} selection={selection} onCancel={closeReview}
      onConfirm={() => { closeReview(); update(selection.isin); }} />}
    {submitting && <p role="status">Submitting market-data updates…</p>}
    {!submitting && pendingCount > 0 && <p role="status">{pendingCount} market-data {pendingCount === 1 ? 'update' : 'updates'} in progress.</p>}
    {!submitting && pendingCount === 0 && completedCount + failedCount > 0 && <p role="status">
      {completedCount} {completedCount === 1 ? 'ETF updated' : 'ETFs updated'}.{failedCount > 0 && ` ${failedCount} ${failedCount === 1 ? 'update needs' : 'updates need'} attention; check the ETF rows or details.`}
    </p>}
    {skippedCount > 0 && <p role="status">Skipped {skippedCount} {skippedCount === 1 ? 'ETF' : 'ETFs'} collected 0 days ago.</p>}
    {updateError && <p role="alert">{updateError}</p>}
    {loading && <p role="status">Loading acquired ETFs…</p>}
    {error && <><p role="alert">{error}</p><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry list</button></>}
    {page && (page.total === 0
      ? <div className="empty-state collection-empty"><span className="empty-icon"><Icon name="layers" /></span><h3>No acquired ETFs yet</h3><p>Choose Add ETF to find a fund and request its data.</p></div>
      : <>
        <div className="collection-table-card">
        <div className="table-heading"><h3>Fund overview</h3><span className="table-hint">Returns &amp; market data</span><span className="table-scroll-hint">Scroll for more →</span></div>
        <div className="acquired-table-scroll" role="region" aria-label="Acquired ETF data" tabIndex={0}>
          <table className="acquired-table">
            <caption className="sr-only">Acquired ETFs, historical annual return and volatility, statistical annual return and volatility, and saved quote coverage</caption>
            <colgroup span={2} /><colgroup span={2} /><colgroup span={2} /><colgroup span={2} /><colgroup span={1} />
            <thead>
              <tr>
                <th scope="col" rowSpan={2}>ETF</th><th scope="col" rowSpan={2}>Saved listing</th>
                <th id="acquired-historical" scope="colgroup" colSpan={2} className="statistic-group">Historical</th>
                <th id="acquired-statistical" scope="colgroup" colSpan={2} className="statistic-group">Statistical</th>
                <th scope="col" rowSpan={2}>Saved quotes</th><th scope="col" rowSpan={2}>Profile collected</th>
                <th scope="col" rowSpan={2} className="market-data-actions"><span>Market data</span>
                  <button className="secondary" disabled={busy || loading || Boolean(selection)}
                    title="Update market data across all pages, skipping ETFs collected 0 days ago"
                    onClick={(event) => review(event, { isin: null, total: page.total })}>Update all</button>
                </th>
              </tr>
              <tr>
                <th id="acquired-historical-return" headers="acquired-historical" scope="col" className="annual-statistic" title="Actual trailing 12-month price change: latest saved price ÷ year-ago price − 1. Not an annualized estimate.">Annual return</th>
                <th id="acquired-historical-volatility" headers="acquired-historical" scope="col" className="annual-statistic" title="Trailing 12 months: sample standard deviation of daily log returns × √252 trading days, ending at the latest saved quote">Annual volatility</th>
                <th id="acquired-statistical-return" headers="acquired-statistical" scope="col" className="annual-statistic" title="Average daily log return × 252 trading days">Annual return</th>
                <th id="acquired-statistical-volatility" headers="acquired-statistical" scope="col" className="annual-statistic" title="All saved history: daily volatility × √252 trading days">Annual volatility</th>
              </tr>
            </thead>
            <tbody>{page.items.map((item) => <tr key={item.instrument_id}>
              <th scope="row"><div className="fund-identity"><span className="fund-avatar" aria-hidden="true">{(displayText(item.ticker) || displayText(item.name) || item.isin).slice(0, 2).toUpperCase()}</span>
                <div><a href={`#/instruments/${encodeURIComponent(item.instrument_id)}`}>{displayText(item.name) || item.isin}</a>
                  <span className="muted">{[displayText(item.ticker), item.isin].filter(Boolean).join(' · ')}</span></div>
              </div></th>
              <td className="saved-listing">{item.listing?.symbol || 'Unknown'}<span className="muted">{item.listing?.exchange || 'Unknown MIC'}</span></td>
              <HistoricalYearReturn item={item} />
              <td className="annual-statistic" headers="acquired-historical acquired-historical-volatility" title={historicalVolatilityTitle(item)}>{percent(item.historical_year_volatility)}</td>
              <ReturnCell value={item.annualized_mean_log_return} headers="acquired-statistical acquired-statistical-return" />
              <td className="annual-statistic" headers="acquired-statistical acquired-statistical-volatility">{percent(item.annualized_volatility)}</td>
              <td>{item.quote_count > 0 ? <>{item.quote_count.toLocaleString()} {item.quote_count === 1 ? 'quote' : 'quotes'}<span className="muted">{item.first_date} – {item.last_date}</span></>
                : <span className="badge">Profile only</span>}</td>
              <td><CollectionTime value={item.collected_at} /></td>
              <td className="market-data-actions">
                <button className="secondary" disabled={submitting || updatePending(updates[item.instrument_id]) || Boolean(selection)}
                  aria-label={`Update market data for ${displayText(item.name) || item.isin}`}
                  onClick={(event) => review(event, { isin: item.instrument_id, name: displayText(item.name) || item.isin })}>Update market data</button>
                <button className="secondary" disabled={busy || loading || Boolean(selection)}
                  aria-label={`Remove ${displayText(item.name) || item.isin}`}
                  onClick={(event) => review(event, { remove: true, isin: item.instrument_id, name: displayText(item.name) || item.isin })}>Remove</button>
                <UpdateStatus update={updates[item.instrument_id]} />
              </td>
            </tr>)}</tbody>
          </table>
        </div>
        {!page.items.length && <p role="status">No ETFs on this page.</p>}
        <div className="section-heading acquired-pagination">
          <p role="status">{page.items.length ? `${offset + 1}–${offset + page.items.length} of ${page.total} ETFs` : `${page.total} ETFs total`}</p>
          <div className="button-group">
            <button className="secondary" disabled={Boolean(selection) || offset === 0} onClick={() => setOffset(Math.max(0, offset - ACQUIRED_PAGE_SIZE))} aria-label="Previous ETFs">Previous</button>
            <button className="secondary" disabled={Boolean(selection) || offset + page.items.length >= page.total || !page.items.length} onClick={() => setOffset(offset + ACQUIRED_PAGE_SIZE)} aria-label="Next ETFs">Next</button>
          </div>
        </div>
        </div>
      </>)}
  </section>;
}
