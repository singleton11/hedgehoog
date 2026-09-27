// Decode text entities without parsing HTML or creating DOM nodes.
export function displayText(value) {
  if (typeof value !== 'string') return value == null ? '' : String(value);
  const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
  return value.replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (match, entity) => {
    if (entity[0] !== '#') return entities[entity.toLowerCase()] ?? match;
    const point = entity[1].toLowerCase() === 'x' ? parseInt(entity.slice(2), 16) : Number(entity.slice(1));
    return point > 0 && point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff)
      ? String.fromCodePoint(point) : match;
  });
}

// Share the displayed elapsed-day age with bulk-update freshness checks.
export function collectionAgeDays(value, now = Date.now()) {
  const timestamp = typeof value === 'string' ? new Date(value).getTime() : NaN;
  return Number.isFinite(timestamp) ? Math.max(0, Math.floor((now - timestamp) / 86400000)) : null;
}

export const pendingJob = (job) => ['queued', 'running'].includes(job?.status);

export function listingPriceCurrency(quotes) {
  const currencies = [...new Set(quotes.map((quote) => quote.price_currency || null))];
  return currencies.length === 1 && currencies[0] ? currencies[0] : null;
}

// A late/stale read must not replace a job acknowledged by POST, or regress
// running/terminal state. A genuinely newer job from another tab is accepted.
export function olderJob(incoming, current) {
  if (!current) return false;
  if (!incoming) return true;
  if (incoming.id !== current.id) {
    return !incoming.requested_at || incoming.requested_at <= current.requested_at;
  }
  const rank = { queued: 0, running: 1, succeeded: 2, failed: 2 };
  return rank[incoming.status] < rank[current.status];
}

export function acquisitionEstimate(detail, mode, manualOverride = false) {
  const summary = detail.summary;
  const inception = summary.requested_from || detail.catalogue?.inception_date;
  const yahoo = detail.provider === 'yfinance';
  const incremental = !yahoo && mode === 'update' && summary.quote_count > 0 && summary.last_date;
  const start = incremental ? summary.last_date : inception;
  const timestamp = typeof start === 'string' ? Date.parse(start) : NaN;
  // Calendar days deliberately overestimate trading days. This is an estimate,
  // not a reservation or a claim that the provider will return older history.
  const olderEnd = !yahoo && mode === 'backfill' && summary.first_date ? summary.first_date : null;
  const end = olderEnd ? Date.parse(olderEnd) : Date.now();
  const days = Number.isFinite(timestamp) && Number.isFinite(end)
    ? Math.max(1, Math.ceil((end - timestamp) / 86400000) + 1) : null;
  const pages = yahoo ? null : days === null ? null : Math.max(1, Math.ceil(days / 1000));
  const from = incremental ? `New quotes after ${start}` : `Full history from ${start || 'catalogue inception'}`;
  // An unresolved automatic listing needs a provider lookup. Cached listings and
  // manual overrides skip lookup. Yahoo Search is unmetered for Hedgehoog.
  const lookupCalls = detail.listing || manualOverride ? 0 : 1;
  if (yahoo) return { pages, lookupCalls, minimumCalls: 0, calls: null,
    range: `${from} ${olderEnd ? `to before ${olderEnd}` : 'to latest available'}` };
  return { pages, lookupCalls, minimumCalls: 3 + lookupCalls, calls: pages === null ? null : lookupCalls + 2 + pages,
    range: `${from} ${olderEnd ? `to before ${olderEnd}` : 'to latest available'}` };
}

// Read every page before using the acquired universe. Never silently truncate it
// or accept a changed/duplicated inventory while pagination is in flight.
export async function loadAcquiredInstruments(request, signal) {
  const items = [];
  const ids = new Set();
  const limit = 100;
  let total = null;
  do {
    if (signal.aborted) return null;
    const page = await request(`/instruments/acquired?limit=${limit}&offset=${items.length}`, { signal });
    if (signal.aborted) return null;
    if (!Array.isArray(page?.items) || !Number.isInteger(page.total) || page.total < 0
        || page.limit !== limit || page.offset !== items.length || (total !== null && total !== page.total)
        || page.items.length > limit || items.length + page.items.length > page.total
        || (!page.items.length && items.length < page.total)) {
      throw new Error('The acquired ETF list changed or was incomplete. Please retry.');
    }
    total = page.total;
    for (const item of page.items) {
      if (typeof item?.instrument_id !== 'string' || !item.instrument_id || ids.has(item.instrument_id)) {
        throw new Error('The acquired ETF list changed or was incomplete. Please retry.');
      }
      ids.add(item.instrument_id);
      items.push(item);
    }
  } while (items.length < total);
  return items;
}

export async function loadAllQuotes(request, isin, signal, snapshot = {}) {
  const items = [];
  let total = null;
  // Use the server's knowledge-time cutoff, not an untrusted browser clock.
  // Reuse it for later pages if another acquisition finishes during loading.
  let asOf = snapshot.asOf || null;
  let generation = snapshot.generation || null;
  do {
    if (signal.aborted) return null;
    const query = new URLSearchParams({ limit: '1000', offset: String(items.length) });
    if (asOf) query.set('as_of', asOf);
    if (generation) query.set('generation', generation);
    const page = await request(`/instruments/${encodeURIComponent(isin)}/quotes?${query}`, { signal });
    if (signal.aborted) return null;
    if (!Array.isArray(page?.items) || !Number.isInteger(page.total) || page.total < 0
        || page.offset !== items.length || (total !== null && total !== page.total)) {
      throw new Error('Quote pagination changed or returned an unexpected response. Please retry the chart.');
    }
    if (typeof page.as_of !== 'string' || !Number.isFinite(Date.parse(page.as_of))
        || (asOf && Date.parse(asOf) !== Date.parse(page.as_of))) {
      throw new Error('Quote pagination returned an invalid observation cutoff. Please retry the chart.');
    }
    if ((generation && page.generation !== generation)
        || (page.generation != null && (typeof page.generation !== 'string'
          || !/^(legacy|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/.test(page.generation)))) {
      throw new Error('Quote pagination changed history generation. Please retry the chart.');
    }
    generation = page.generation || generation;
    asOf = page.as_of;
    total = page.total;
    if ((!page.items.length && items.length < total) || items.length + page.items.length > total) {
      throw new Error('The backend returned an incomplete quote page. Please retry the chart.');
    }
    items.push(...page.items);
  } while (items.length < total);
  return items;
}
