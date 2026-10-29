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

function InventoryCell({ label, children, ...props }) {
  return <td role="cell" {...props}><span className="inventory-cell-label" aria-hidden="true">{label}</span>{children}</td>;
}

function ReturnCell({ value, title, headers, label }) {
  const display = formatReturn(value);
  return <InventoryCell label={label} className={`annual-statistic ${display.className}`} title={title} headers={headers}>
    {display.text}
  </InventoryCell>;
}

function HistoricalYearReturn({ item }) {
  return <ReturnCell label="Historical annual return" value={item.historical_year_return} title={historicalReturnTitle(item)} headers="acquired-historical acquired-historical-return" />;
}

function UpdateConfirmation({ selection, onConfirm, onCancel }) {
  const [confirmed, setConfirmed] = useState(false);
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <form className="acquisition-form" aria-label="Confirm market data update" onSubmit={(event) => {
    event.preventDefault();
    if (confirmed) onConfirm();
  }}>
    <h3 ref={heading} tabIndex={-1}>Update {selection.isin ? selection.name : `all ${selection.total == null ? '' : `${selection.total} `}acquired ETFs`}?</h3>
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
  const [minHistoryYears, setMinHistoryYears] = useState('');
  const [hideNegativeReturns, setHideNegativeReturns] = useState(false);
  const filtersActive = Boolean(minHistoryYears || hideNegativeReturns);
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
    setPage((previous) => previous?.offset === offset && previous.minHistoryYears === minHistoryYears
      && previous.hideNegativeReturns === hideNegativeReturns ? previous : null);
    setError(null);
    async function load() {
      try {
        const historyQuery = minHistoryYears ? `&min_history_years=${minHistoryYears}` : '';
        const returnsQuery = hideNegativeReturns ? '&hide_negative_returns=true' : '';
        const data = await request(`/instruments/acquired?limit=${ACQUIRED_PAGE_SIZE}&offset=${offset}${historyQuery}${returnsQuery}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!Array.isArray(data?.items) || !Number.isInteger(data.total) || data.total < 0
            || data.offset !== offset || data.limit !== ACQUIRED_PAGE_SIZE) {
          throw new Error('The backend returned an unexpected acquired ETF list. Please retry.');
        }
        if (offset > 0 && offset >= data.total) {
          setOffset(Math.max(0, Math.floor((data.total - 1) / ACQUIRED_PAGE_SIZE) * ACQUIRED_PAGE_SIZE));
          return;
        }
        setPage({ ...data, minHistoryYears, hideNegativeReturns });
      } catch (error) {
        if (!controller.signal.aborted) setError(error.message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    load();
    return () => controller.abort();
  }, [request, offset, attempt, minHistoryYears, hideNegativeReturns]);

  return <section aria-labelledby="acquired-etfs-heading" className="acquired-page">
    <div className="section-heading page-heading">
      <div><p className="eyebrow">Your collection</p>
        <h2 id="acquired-etfs-heading" ref={listHeading} tabIndex={-1}>Acquired ETFs{page ? ` (${page.total})` : ''}</h2>
        <p className="muted">A clearer view of the funds you follow.</p>
      </div>
      <a className="button-link" href="#/add"><Icon name="plus" />Add ETF</a>
    </div>
    <div className="section-heading history-filter">
      <div className="button-group">
        <label htmlFor="minimum-history">Saved history
          <select id="minimum-history" value={minHistoryYears} disabled={Boolean(selection)} onChange={(event) => {
            setMinHistoryYears(event.target.value);
            setOffset(0);
          }}>
            <option value="">All</option>
            <option value="5">5y+</option>
            <option value="15">15y+</option>
            <option value="30">30y+</option>
          </select>
        </label>
        <label htmlFor="returns-filter">Returns
          <select id="returns-filter" value={hideNegativeReturns ? 'hide' : ''} aria-describedby="returns-filter-help"
            disabled={Boolean(selection)} onChange={(event) => {
              setHideNegativeReturns(event.target.value === 'hide');
              setOffset(0);
            }}>
            <option value="">All</option>
            <option value="hide">Hide negative/unavailable</option>
          </select>
        </label>
      </div>
      <div>
        <p className="muted">Minimum span between first and last saved quotes.</p>
        <p className="muted" id="returns-filter-help">Shows only ETFs whose historical and statistical annual returns are both available and at least zero.</p>
      </div>
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
      ? <div className="empty-state collection-empty"><span className="empty-icon"><Icon name="layers" /></span><h3>{filtersActive ? 'No ETFs match these filters' : 'No acquired ETFs yet'}</h3><p>{filtersActive ? 'Try relaxing the filters or select All.' : 'Choose Add ETF to find a fund and request its data.'}</p></div>
      : <>
        <div className="collection-table-card">
        <div className="table-heading"><h3>Fund overview</h3>
          <button className="secondary" disabled={busy || loading || Boolean(selection)}
            title="Update all acquired ETFs, including those hidden by filters, skipping ETFs collected 0 days ago"
            onClick={(event) => review(event, { isin: null, total: filtersActive ? null : page.total })}>Update all</button>
        </div>
        <p className="collection-scroll-hint">Scroll horizontally for all columns →</p>
        <div className="acquired-table-scroll" role="region" aria-label="Acquired ETF data" tabIndex={0}>
          <table role="table" className="acquired-table etf-inventory-table">
            <caption className="sr-only">Acquired ETFs, historical annual return and volatility, statistical annual return and volatility, and saved quote coverage</caption>
            <colgroup>
              <col className="inventory-fund" /><col className="inventory-asset" />
              <col className="inventory-index" /><col className="inventory-listing" />
            </colgroup>
            <colgroup span={2} className="inventory-statistics" />
            <colgroup span={2} className="inventory-statistics" />
            <colgroup>
              <col className="inventory-quotes" /><col className="inventory-collected" /><col className="inventory-actions" />
            </colgroup>
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col" rowSpan={2}>ETF</th><th role="columnheader" scope="col" rowSpan={2}>Asset class</th><th role="columnheader" scope="col" rowSpan={2}>Index</th><th role="columnheader" scope="col" rowSpan={2}>Saved listing</th>
                <th role="columnheader" id="acquired-historical" scope="colgroup" colSpan={2} className="statistic-group">Historical</th>
                <th role="columnheader" id="acquired-statistical" scope="colgroup" colSpan={2} className="statistic-group">Statistical</th>
                <th role="columnheader" scope="col" rowSpan={2}>Saved quotes</th><th role="columnheader" scope="col" rowSpan={2}>Profile collected</th>
                <th role="columnheader" scope="col" rowSpan={2} className="market-data-actions"><span>Market data</span></th>
              </tr>
              <tr role="row">
                <th role="columnheader" id="acquired-historical-return" headers="acquired-historical" scope="col" className="annual-statistic" title="Actual trailing 12-month price change: latest saved price ÷ year-ago price − 1. Not an annualized estimate.">Annual return</th>
                <th role="columnheader" id="acquired-historical-volatility" headers="acquired-historical" scope="col" className="annual-statistic" title="Trailing 12 months: sample standard deviation of daily log returns × √252 trading days, ending at the latest saved quote">Annual volatility</th>
                <th role="columnheader" id="acquired-statistical-return" headers="acquired-statistical" scope="col" className="annual-statistic" title="Average daily log return × 252 trading days">Annual return</th>
                <th role="columnheader" id="acquired-statistical-volatility" headers="acquired-statistical" scope="col" className="annual-statistic" title="All saved history: daily volatility × √252 trading days">Annual volatility</th>
              </tr>
            </thead>
            <tbody role="rowgroup">{page.items.map((item) => <tr role="row" key={item.instrument_id}>
              <th role="rowheader" scope="row"><div className="fund-identity"><span className="fund-avatar" aria-hidden="true">{(displayText(item.ticker) || displayText(item.name) || item.isin).slice(0, 2).toUpperCase()}</span>
                <div><a href={`#/instruments/${encodeURIComponent(item.instrument_id)}`}>{displayText(item.name) || item.isin}</a>
                  <span className="muted">{[displayText(item.ticker), item.isin].filter(Boolean).join(' · ')}</span></div>
              </div></th>
              <InventoryCell label="Asset class">{displayText(item.asset_class) || 'Not available'}</InventoryCell>
              <InventoryCell label="Index">{displayText(item.index) || 'Not available'}</InventoryCell>
              <InventoryCell label="Saved listing" className="saved-listing">{item.listing?.symbol || 'Unknown'}<span className="muted">{item.listing?.exchange || 'Unknown MIC'}</span></InventoryCell>
              <HistoricalYearReturn item={item} />
              <InventoryCell label="Historical annual volatility" className="annual-statistic" headers="acquired-historical acquired-historical-volatility" title={historicalVolatilityTitle(item)}>{percent(item.historical_year_volatility)}</InventoryCell>
              <ReturnCell label="Statistical annual return" value={item.annualized_mean_log_return} headers="acquired-statistical acquired-statistical-return" />
              <InventoryCell label="Statistical annual volatility" className="annual-statistic" headers="acquired-statistical acquired-statistical-volatility">{percent(item.annualized_volatility)}</InventoryCell>
              <InventoryCell label="Saved quotes" className="saved-quotes">{item.quote_count > 0 ? <>{item.quote_count.toLocaleString()} {item.quote_count === 1 ? 'quote' : 'quotes'}<span className="muted"><time dateTime={item.first_date}>{item.first_date}</time> – <time dateTime={item.last_date}>{item.last_date}</time></span></>
                : <span className="badge">Profile only</span>}</InventoryCell>
              <InventoryCell label="Profile collected" className="profile-collected"><CollectionTime value={item.collected_at} /></InventoryCell>
              <InventoryCell label="Market data" className="market-data-actions">
                <button className="secondary" disabled={submitting || updatePending(updates[item.instrument_id]) || Boolean(selection)}
                  aria-label={`Update market data for ${displayText(item.name) || item.isin}`}
                  onClick={(event) => review(event, { isin: item.instrument_id, name: displayText(item.name) || item.isin })}>Update market data</button>
                <button className="secondary" disabled={busy || loading || Boolean(selection)}
                  aria-label={`Remove ${displayText(item.name) || item.isin}`}
                  onClick={(event) => review(event, { remove: true, isin: item.instrument_id, name: displayText(item.name) || item.isin })}>Remove</button>
                <UpdateStatus update={updates[item.instrument_id]} />
              </InventoryCell>
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
