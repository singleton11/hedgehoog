import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { displayText } from './instruments.js';

export const ACQUIRED_PAGE_SIZE = 20;

function CollectionTime({ value }) {
  const collected = new Date(value);
  if (typeof value !== 'string' || !Number.isFinite(collected.getTime())) return 'Unknown';
  const days = Math.max(0, Math.floor((Date.now() - collected.getTime()) / 86400000));
  const relative = `${days} ${days === 1 ? 'day' : 'days'} ago`;
  const exact = `${collected.toLocaleString(undefined, { timeZone: 'UTC' })} UTC`;
  return <time dateTime={value} title={exact} aria-label={`${relative}, ${exact}`}>{relative}</time>;
}

export default function AcquiredEtfs() {
  const { request } = useAuth();
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setPage(null);
    setError(null);
    async function load() {
      try {
        const data = await request(`/instruments/acquired?limit=${ACQUIRED_PAGE_SIZE}&offset=${offset}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!Array.isArray(data?.items) || !Number.isInteger(data.total) || data.total < 0
            || data.offset !== offset || data.limit !== ACQUIRED_PAGE_SIZE) {
          throw new Error('The backend returned an unexpected acquired ETF list. Please retry.');
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

  function refresh() {
    setOffset(0);
    setAttempt((value) => value + 1);
  }

  return <section aria-labelledby="acquired-etfs-heading">
    <div className="section-heading">
      <h2 id="acquired-etfs-heading">Acquired ETFs{page ? ` (${page.total})` : ''}</h2>
      <button className="secondary" onClick={refresh} disabled={loading}>Refresh list</button>
    </div>
    {loading && <p role="status">Loading acquired ETFs…</p>}
    {error && <><p role="alert">{error}</p><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry list</button></>}
    {page && (page.total === 0
      ? <div className="empty-state"><h3>No acquired ETFs yet</h3><p>Search above, then choose Request Data.</p></div>
      : <>
        <div className="acquired-table-scroll" role="region" aria-label="Acquired ETF data" tabIndex={0}>
          <table className="acquired-table">
            <caption className="sr-only">Acquired ETFs and saved quote coverage</caption>
            <thead><tr><th scope="col">ETF</th><th scope="col">Saved listing</th><th scope="col">Saved quotes</th><th scope="col">Profile collected</th></tr></thead>
            <tbody>{page.items.map((item) => <tr key={item.instrument_id}>
              <th scope="row"><a href={`#/instruments/${encodeURIComponent(item.instrument_id)}`}>{displayText(item.name) || item.isin}</a>
                <span className="muted">{[displayText(item.ticker), item.isin].filter(Boolean).join(' · ')}</span></th>
              <td>{item.listing?.symbol || 'Unknown'}<span className="muted">{item.listing?.exchange || 'Unknown MIC'}</span></td>
              <td>{item.quote_count > 0 ? <>{item.quote_count.toLocaleString()} {item.quote_count === 1 ? 'quote' : 'quotes'}<span className="muted">{item.first_date} – {item.last_date}</span></>
                : <span className="badge">Profile only</span>}</td>
              <td><CollectionTime value={item.collected_at} /></td>
            </tr>)}</tbody>
          </table>
        </div>
        {!page.items.length && <p role="status">No ETFs on this page. Refresh to update.</p>}
        <div className="section-heading acquired-pagination">
          <p role="status">{page.items.length ? `${offset + 1}–${offset + page.items.length} of ${page.total} ETFs` : `${page.total} ETFs total`}</p>
          <div className="button-group">
            <button className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - ACQUIRED_PAGE_SIZE))} aria-label="Previous ETFs">Previous</button>
            <button className="secondary" disabled={offset + page.items.length >= page.total || !page.items.length} onClick={() => setOffset(offset + ACQUIRED_PAGE_SIZE)} aria-label="Next ETFs">Next</button>
          </div>
        </div>
      </>)}
  </section>;
}
