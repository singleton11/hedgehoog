import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';

const PAGE_SIZE = 25;
const POLL_MS = 3000;
const STATUSES = { queued: 'Queued', running: 'In progress', retrying: 'Retrying', succeeded: 'Succeeded', failed: 'Failed' };
const TYPES = { market_data_acquisition: 'Market-data download', etf_import: 'ETF catalogue import', market_data_refresh: 'Automatic refresh sweep' };

function validatePage(page) {
  const count = (value) => Number.isSafeInteger(value) && value >= 0;
  if (!page || !Array.isArray(page.items) || !count(page.total)
      || !['total', ...Object.keys(STATUSES)].every((key) => count(page.counts?.[key]))
      || page.items.some((job) => !job || typeof job.id !== 'string' || !Object.hasOwn(STATUSES, job.status)
        || !Object.hasOwn(TYPES, job.job_type) || (job.instrument_id != null && typeof job.instrument_id !== 'string')
        || (job.error != null && typeof job.error !== 'string')
        || ['requested_at', 'started_at', 'finished_at'].some((key) => job[key] != null && typeof job[key] !== 'string'))) {
    throw new Error('The backend returned unexpected job data. Please retry.');
  }
  return page;
}

function Timestamp({ value }) {
  if (!value || !Number.isFinite(Date.parse(value))) return <span className="muted">Not yet</span>;
  return <time dateTime={value} title={value}>{new Date(value).toLocaleString()}</time>;
}

function Instrument({ id }) {
  if (!id) return <span className="muted">All instruments</span>;
  const path = /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(id) ? 'instruments'
    : /^idx-[0-9a-f-]+$/.test(id) ? 'indices' : null;
  return path ? <a href={`#/${path}/${encodeURIComponent(id)}`}>{id}</a> : <span>{id}</span>;
}

export default function Jobs() {
  const { request } = useAuth();
  const [status, setStatus] = useState('');
  const [jobType, setJobType] = useState('');
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset),
    ...(status ? { status } : {}), ...(jobType ? { job_type: jobType } : {}) }).toString();
  const page = result?.query === query ? result.page : null;

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    setLoading(true);
    setError(null);
    async function load() {
      try {
        const data = validatePage(await request(`/jobs?${query}`, { signal: controller.signal }));
        if (controller.signal.aborted) return;
        if (offset > 0 && offset >= data.total) {
          setOffset(Math.max(0, Math.ceil(data.total / PAGE_SIZE) - 1) * PAGE_SIZE);
          return;
        }
        setResult({ query, page: data, updatedAt: new Date().toLocaleTimeString() });
        setError(null);
      } catch (err) {
        if (!controller.signal.aborted) setError(err.message);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          // Schedule after completion: slow requests never overlap.
          timer = setTimeout(load, POLL_MS);
        }
      }
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [request, query, attempt, offset]);

  const counts = page?.counts;
  const summary = counts ? [
    ['Total jobs', counts.total], ['Queued', counts.queued],
    ['In progress', counts.running + counts.retrying],
    ['Finished', counts.succeeded + counts.failed], ['Succeeded', counts.succeeded], ['Failed', counts.failed],
  ] : [];

  return <section className="jobs-page" aria-labelledby="jobs-title">
    <div className="page-heading section-heading">
      <div><p className="eyebrow">Background activity</p><h2 id="jobs-title">Jobs</h2>
        <p className="muted">Downloads, catalogue imports and automatic refreshes in one place.</p></div>
      <button className="secondary" disabled={loading} onClick={() => setAttempt((value) => value + 1)}>Refresh jobs</button>
    </div>
    <p className={`jobs-live${error ? ' jobs-live-error' : ''}`} role="status">
      <span aria-hidden="true" />{error ? 'Updates interrupted. Retrying automatically.'
        : page ? `Auto-updates every 3 seconds. Last updated ${result.updatedAt}.` : 'Loading jobs...'}
    </p>
    {error && <p role="alert">{error}{page ? ' Showing the last successful update.' : ''}</p>}
    {!!summary.length && <dl className="jobs-summary" aria-label="All job counts">
      {summary.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value.toLocaleString()}</dd></div>)}
    </dl>}
    <div className="jobs-filters">
      <label htmlFor="jobs-status">Status<select id="jobs-status" value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
        <option value="">All statuses</option>{Object.entries(STATUSES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <label htmlFor="jobs-type">Job type<select id="jobs-type" value={jobType} onChange={(event) => { setJobType(event.target.value); setOffset(0); }}>
        <option value="">All job types</option>{Object.entries(TYPES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <p className="muted">Summary counts cover all jobs, regardless of filters.</p>
    </div>
    <div className="collection-table-card">
      <div className="table-heading"><h3>Job history</h3><span className="table-hint">Newest first</span></div>
      <p className="jobs-scroll-hint">Scroll horizontally to see timestamps and details.</p>
      {!page ? <p className="jobs-empty">{error ? 'Job history is unavailable.' : 'Loading job history...'}</p>
        : !page.items.length ? <p className="jobs-empty">{status || jobType ? 'No jobs match these filters.' : 'No jobs recorded yet.'}</p>
          : <div className="acquired-table-scroll" role="region" aria-label="Job history" tabIndex={0}>
            <table className="acquired-table jobs-table"><thead><tr>
              <th scope="col">Job / type</th><th scope="col">Status</th><th scope="col">Instrument</th>
              <th scope="col">Queued at</th><th scope="col">Started at</th><th scope="col">Finished at</th><th scope="col">Details</th>
            </tr></thead><tbody>{page.items.map((job) => <tr key={`${job.job_type}:${job.id}`}>
              <th scope="row">{TYPES[job.job_type]}<span className="muted jobs-id">{job.id}</span></th>
              <td><span className={`badge job-status job-status-${job.status}`}>{STATUSES[job.status]}</span></td>
              <td><Instrument id={job.instrument_id} /></td><td><Timestamp value={job.requested_at} /></td>
              <td><Timestamp value={job.started_at} /></td><td><Timestamp value={job.finished_at} /></td>
              <td>{job.error || <span className="muted">No errors reported</span>}</td>
            </tr>)}</tbody></table>
          </div>}
      {page && <div className="acquired-pagination section-heading">
        <p>{page.total ? `${offset + 1}-${Math.min(offset + page.items.length, page.total)} of ${page.total.toLocaleString()} jobs` : '0 jobs'}</p>
        <div className="button-group"><button className="secondary" disabled={!offset || loading} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>Previous jobs</button>
          <button className="secondary" disabled={offset + PAGE_SIZE >= page.total || loading} onClick={() => setOffset((value) => value + PAGE_SIZE)}>Next jobs</button></div>
      </div>}
    </div>
    <p className="muted jobs-note">Catalogue import and refresh-sweep history starts when job tracking is enabled. Earlier market-data downloads are included where records still exist. A queued or running status does not guarantee that a worker is currently available.</p>
  </section>;
}
