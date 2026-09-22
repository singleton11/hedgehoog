import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { collectionAgeDays, loadAcquiredInstruments, olderJob, pendingJob } from './instruments.js';
import { POLL_INTERVAL } from './useInstrument.js';

export const updatePending = (update) => Boolean(update?.submitting || update?.checking || pendingJob(update?.job));

function validJob(job, isin) {
  return job?.id && job.instrument_id === isin && ['queued', 'running', 'succeeded', 'failed'].includes(job.status);
}

// Capture the whole inventory before any POST can change its collection-time ordering.
async function acquiredIds(request, signal) {
  const items = await loadAcquiredInstruments(request, signal);
  if (!items) return null;
  const now = Date.now();
  const ids = items.filter((item) => collectionAgeDays(item.collected_at, now) !== 0).map((item) => item.instrument_id);
  return { ids, skipped: items.length - ids.length };
}

export default function useAcquiredUpdates(onComplete) {
  const { request } = useAuth();
  const [updates, setUpdates] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [skippedCount, setSkippedCount] = useState(0);
  const submission = useRef(null);
  const controllers = useRef(new Set());
  const timers = useRef(new Set());

  useEffect(() => () => {
    controllers.current.forEach((controller) => controller.abort());
    timers.current.forEach(clearTimeout);
    controllers.current.clear();
    timers.current.clear();
  }, []);

  function track() {
    const controller = new AbortController();
    controllers.current.add(controller);
    return controller;
  }

  function change(isin, patch) {
    setUpdates((previous) => ({ ...previous, [isin]: { ...previous[isin], ...patch } }));
  }

  function monitor(isin, acceptedJob = null) {
    const controller = track();
    const { signal } = controller;
    let current = acceptedJob;
    function schedule() {
      const timer = setTimeout(() => { timers.current.delete(timer); poll(); }, POLL_INTERVAL);
      timers.current.add(timer);
    }
    async function poll() {
      if (signal.aborted) return;
      try {
        const detail = await request(`/instruments/${encodeURIComponent(isin)}`, { signal });
        if (signal.aborted) return;
        if (detail?.instrument_id !== isin || (detail.acquisition != null && !validJob(detail.acquisition, isin))) {
          throw new Error('Acquisition status returned an unexpected response.');
        }
        if (olderJob(detail.acquisition, current)) {
          schedule();
          return;
        }
        current = detail.acquisition;
        change(isin, { job: current, checking: false, pollError: null });
        if (pendingJob(current)) schedule();
        else {
          controllers.current.delete(controller);
          onComplete();
        }
      } catch (error) {
        if (signal.aborted) return;
        change(isin, { pollError: `${error.message} Status will retry automatically.` });
        schedule();
      }
    }
    schedule();
  }

  async function update(isin = null) {
    if (submission.current) return;
    const controller = track();
    submission.current = controller;
    const { signal } = controller;
    setSubmitting(true);
    setError(null);
    setSkippedCount(0);
    try {
      const batch = isin ? { ids: [isin], skipped: 0 } : await acquiredIds(request, signal);
      if (signal.aborted) return;
      const { ids, skipped } = batch;
      setSkippedCount(skipped);
      for (let index = 0; index < ids.length; index += 1) {
        if (signal.aborted) return;
        const id = ids[index];
        change(id, { submitting: true, checking: false, job: null, error: null, pollError: null });
        try {
          const result = await request(`/instruments/${encodeURIComponent(id)}/acquisitions`, {
            method: 'POST', body: { confirmed: true, mode: 'update' }, signal,
          });
          if (signal.aborted) return;
          if (!validJob(result?.job, id)) throw new Error('Acquisition returned an unexpected response.');
          change(id, { submitting: false, job: result.job });
          if (pendingJob(result.job)) monitor(id, result.job);
          else onComplete();
        } catch (error) {
          if (signal.aborted) return;
          change(id, { submitting: false, checking: true, error: `Request could not be confirmed: ${error.message}` });
          // An uncertain POST may have queued a job. Reconcile by GET, never retry POST automatically.
          monitor(id);
          if (ids.length > 1 && error.status !== 422 && error.status !== 404) {
            throw new Error(`${error.message} ${ids.length - index - 1} ETFs were not submitted. Already queued updates will continue.`);
          }
        }
      }
    } catch (error) {
      if (!signal.aborted) setError(error.message);
    } finally {
      controllers.current.delete(controller);
      if (!signal.aborted) {
        submission.current = null;
        setSubmitting(false);
      }
    }
  }

  return { updates, submitting, error, update, skippedCount, busy: submitting || Object.values(updates).some(updatePending) };
}
