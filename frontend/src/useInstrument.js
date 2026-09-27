import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { loadAllQuotes, olderJob, pendingJob } from './instruments.js';

export const POLL_INTERVAL = 2000;

export default function useInstrument(isin) {
  const { request } = useAuth();
  const [detail, setDetail] = useState(null);
  const latest = useRef(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [pollError, setPollError] = useState(null);
  const [postError, setPostError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [savingCurrency, setSavingCurrency] = useState(false);
  const [currencyError, setCurrencyError] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [quotes, setQuotes] = useState([]);
  const [quotesLoading, setQuotesLoading] = useState(false);
  const [quotesError, setQuotesError] = useState(null);
  const [quoteAttempt, setQuoteAttempt] = useState(0);
  const detailController = useRef(null);
  const postController = useRef(null);
  const currencyController = useRef(null);
  const mounted = useRef(false);
  const path = `/instruments/${encodeURIComponent(isin)}`;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; postController.current?.abort(); currencyController.current?.abort(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    detailController.current = controller;
    let timer;
    setError(null);
    setPollError(null);
    setLoading(!latest.current);
    async function load() {
      if (controller.signal.aborted) return;
      let repeat = pendingJob(latest.current?.acquisition);
      try {
        const data = await request(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (data?.instrument_id !== isin || !data.summary || !data.catalogue) {
          throw new Error('Instrument details returned an unexpected response.');
        }
        if (olderJob(data.acquisition, latest.current?.acquisition)) {
          // A lagging read after POST must not re-enable the acquisition button.
          repeat = true;
        } else {
          latest.current = data;
          setDetail(data);
          setPollError(null);
          setError(null);
          repeat = pendingJob(data.acquisition);
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        if (latest.current) setPollError(error.message);
        else setError(error.message);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (repeat) timer = setTimeout(load, POLL_INTERVAL);
        }
      }
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [isin, path, request, refresh]);

  // Replace the chart only after ALL pages succeed. Queued/running progress
  // alone doesn't repeatedly reload quotes, and errors never erase prior data.
  const quoteVersion = detail ? JSON.stringify([
    detail.summary.quote_count, detail.summary.first_date, detail.summary.last_date,
    detail.generation,
    pendingJob(detail.acquisition) ? null : detail.acquisition?.id,
    pendingJob(detail.acquisition) ? null : detail.acquisition?.status,
  ]) : null;
  useEffect(() => {
    if (quoteVersion === null) return;
    const controller = new AbortController();
    setQuotesLoading(true);
    setQuotesError(null);
    const snapshot = { generation: latest.current?.generation, asOf: latest.current?.history_as_of };
    loadAllQuotes(request, isin, controller.signal, snapshot).then((items) => {
      if (!controller.signal.aborted && items !== null) setQuotes(items);
    }).catch((error) => {
      if (!controller.signal.aborted) setQuotesError(error.message);
    }).finally(() => {
      if (!controller.signal.aborted) setQuotesLoading(false);
    });
    return () => controller.abort();
  }, [isin, request, quoteVersion, quoteAttempt]);

  const acquire = useCallback(async (body) => {
    if (postController.current || currencyController.current || pendingJob(latest.current?.acquisition)) return false;
    // Cancel any read that started before this explicit write.
    detailController.current?.abort();
    const controller = new AbortController();
    postController.current = controller;
    setSubmitting(true);
    setPostError(null);
    let accepted = false;
    try {
      const result = await request(`${path}/acquisitions`, { method: 'POST', body, signal: controller.signal });
      if (controller.signal.aborted || !mounted.current) return false;
      if (!result?.job?.id || result.job.instrument_id !== isin) throw new Error('Acquisition returned an unexpected response. Refresh details before retrying.');
      latest.current = { ...latest.current, acquisition: result.job };
      setDetail(latest.current);
      accepted = true;
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) setPostError(error.message);
    } finally {
      if (!controller.signal.aborted && mounted.current) {
        postController.current = null;
        setSubmitting(false);
        // Also reconcile publish failures or uncertain network outcomes by GET;
        // never automatically retry the quota-consuming POST.
        setRefresh((value) => value + 1);
      }
    }
    return accepted;
  }, [isin, path, request]);

  const saveCurrency = useCallback(async (currency) => {
    if (currencyController.current || postController.current) return false;
    // Prevent an in-flight poll from replacing the newly saved setting.
    detailController.current?.abort();
    const controller = new AbortController();
    currencyController.current = controller;
    setSavingCurrency(true);
    setCurrencyError(null);
    let saved = false;
    try {
      const result = await request(`${path}/price-currency`, {
        method: 'PATCH', body: { price_currency: currency }, signal: controller.signal,
      });
      if (controller.signal.aborted || !mounted.current) return false;
      if (result?.price_currency_override !== currency) throw new Error('Price currency returned an unexpected response. Please retry.');
      latest.current = { ...latest.current, price_currency_override: result.price_currency_override };
      setDetail(latest.current);
      saved = true;
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) setCurrencyError(error.message);
    } finally {
      if (!controller.signal.aborted && mounted.current) {
        currencyController.current = null;
        setSavingCurrency(false);
        setRefresh((value) => value + 1);
      }
    }
    return saved;
  }, [path, request]);

  return { detail, loading, error, pollError, postError, submitting, quotes, quotesLoading, quotesError, acquire,
    savingCurrency, currencyError, saveCurrency,
    retry: () => { if (!postController.current && !currencyController.current) setRefresh((value) => value + 1); },
    retryQuotes: () => setQuoteAttempt((value) => value + 1) };
}
