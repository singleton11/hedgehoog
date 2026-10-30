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

export function acquisitionEstimate(detail, _mode, manualOverride = false) {
  const start = detail.summary.requested_from || detail.catalogue?.inception_date;
  return { pages: null, lookupCalls: detail.listing || manualOverride ? 0 : 1, minimumCalls: 0, calls: null,
    range: `Full history from ${start || 'catalogue inception'} to latest available` };
}

export const STALE_GENERATION_MESSAGE = 'The saved Yahoo Finance snapshot has changed. Refresh the latest snapshot to reload details, quotes and statistics. No provider download is started.';

// Capture the lightweight universe in one response: background acquisitions can
// change membership and collection-time ordering between ordinary inventory pages.
export async function loadAcquiredInstruments(request, signal) {
  if (signal.aborted) return null;
  const data = await request('/instruments/acquired/universe', { signal });
  if (signal.aborted) return null;
  if (!Array.isArray(data?.items) || !Number.isInteger(data.total) || data.total !== data.items.length) {
    throw new Error('The acquired ETF list changed or was incomplete. Please retry.');
  }
  const ids = new Set();
  for (const item of data.items) {
    if (typeof item?.instrument_id !== 'string' || !item.instrument_id || ids.has(item.instrument_id)) {
      throw new Error('The acquired ETF list changed or was incomplete. Please retry.');
    }
    ids.add(item.instrument_id);
  }
  return data.items;
}

export async function loadAllQuotes(request, isin, signal, snapshot = {}, resource = 'instruments') {
  const items = [];
  let total = null;
  // as_of bounds quote dates in the latest saved snapshot, not knowledge time.
  // Pin both the server cutoff and generation so pagination never mixes refreshes.
  let asOf = snapshot.asOf || null;
  let generation = snapshot.generation || null;
  // An explicit null detail generation pins absence; undefined allows discovery.
  let pinned = snapshot.generation !== undefined;
  const conflict = () => Object.assign(new Error(STALE_GENERATION_MESSAGE), { status: 409 });
  if (generation === 'legacy') throw conflict();
  do {
    if (signal.aborted) return null;
    const query = new URLSearchParams({ limit: '1000', offset: String(items.length) });
    if (asOf) query.set('as_of', asOf);
    if (generation) query.set('generation', generation);
    const page = await request(`/${resource}/${encodeURIComponent(isin)}/quotes?${query}`, { signal });
    if (signal.aborted) return null;
    if (!Array.isArray(page?.items) || !Number.isInteger(page.total) || page.total < 0
        || page.offset !== items.length || (total !== null && total !== page.total)) {
      throw new Error('Quote pagination changed or returned an unexpected response. Please retry the chart.');
    }
    if (typeof page.as_of !== 'string' || !Number.isFinite(Date.parse(page.as_of))
        || (asOf && Date.parse(asOf) !== Date.parse(page.as_of))) {
      throw new Error('Quote pagination returned an invalid quote-date cutoff. Please retry the chart.');
    }
    if (pinned && (page.generation ?? null) !== generation) throw conflict();
    if (page.generation != null && (typeof page.generation !== 'string'
        || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(page.generation))) {
      throw new Error('Quote pagination returned an invalid history generation. Please refresh the chart.');
    }
    generation = page.generation ?? null;
    pinned = true;
    asOf = page.as_of;
    total = page.total;
    if ((!page.items.length && items.length < total) || items.length + page.items.length > total) {
      throw new Error('The backend returned an incomplete quote page. Please retry the chart.');
    }
    items.push(...page.items);
  } while (items.length < total);
  return items;
}
