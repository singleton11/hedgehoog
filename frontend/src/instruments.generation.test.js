import { describe, expect, it, vi } from 'vitest';
import { loadAllQuotes } from './instruments.js';

const FIRST = '11111111-1111-4111-8111-111111111111';
const SECOND = '22222222-2222-4222-8222-222222222222';
const cutoff = '2026-09-17T12:00:00Z';
const page = (offset, generation = FIRST) => ({
  items: [{ date: `2026-09-${14 + offset}`, close: '100' }],
  total: 2, offset, limit: 1000, as_of: cutoff, generation,
});

describe('latest-snapshot quote pagination', () => {
  it('pins the first server generation as well as its cutoff on later pages', async () => {
    const request = vi.fn().mockResolvedValueOnce(page(0)).mockResolvedValueOnce(page(1));
    expect(await loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal)).toHaveLength(2);
    const second = new URL(request.mock.calls[1][0], 'http://localhost').searchParams;
    expect(second.get('generation')).toBe(FIRST);
    expect(second.get('as_of')).toBe(cutoff);
  });

  it('rejects a generation change even with the same timestamp, total and dates', async () => {
    const request = vi.fn().mockResolvedValueOnce(page(0)).mockResolvedValueOnce(page(1, SECOND));
    await expect(loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal))
      .rejects.toMatchObject({ status: 409 });
  });

  it('pins the detail generation on the first chart request', async () => {
    const request = vi.fn().mockResolvedValueOnce(page(0)).mockResolvedValueOnce(page(1));
    await loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: FIRST });
    expect(new URL(request.mock.calls[0][0], 'http://localhost').searchParams.get('generation')).toBe(FIRST);
  });

  it('passes an explicit quote-date bound without excluding later-collected saved quotes', async () => {
    const historical = '2026-09-15T00:00:00Z';
    const request = vi.fn().mockResolvedValueOnce({ ...page(0), as_of: historical,
      items: [{ date: '2026-09-14', collected_at: '2026-10-01T00:00:00Z', close: '100' }], total: 1 });
    const result = await loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: FIRST, asOf: historical });
    expect(new URL(request.mock.calls[0][0], 'http://localhost').searchParams.get('as_of')).toBe(historical);
    expect(result).toEqual([{ date: '2026-09-14', collected_at: '2026-10-01T00:00:00Z', close: '100' }]);
  });

  it('propagates stale-generation 409 without adopting the latest snapshot or retrying automatically', async () => {
    const stale = Object.assign(new Error('Refresh instrument details.'), { status: 409 });
    const request = vi.fn().mockResolvedValueOnce(page(0)).mockRejectedValueOnce(stale);
    await expect(loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal)).rejects.toBe(stale);
    expect(request).toHaveBeenCalledTimes(2);
    expect(new URL(request.mock.calls[1][0], 'http://localhost').searchParams.get('generation')).toBe(FIRST);
  });

  it('accepts an empty response only while the pinned no-publication state still holds', async () => {
    const request = vi.fn().mockResolvedValue({ ...page(0, null), total: 0, items: [] });
    expect(await loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: null })).toEqual([]);
    request.mockResolvedValue(page(0));
    await expect(loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: null }))
      .rejects.toMatchObject({ status: 409 });
  });

  it('rejects the retired legacy sentinel instead of making it a readable snapshot', async () => {
    const request = vi.fn();
    await expect(loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: 'legacy' }))
      .rejects.toMatchObject({ status: 409 });
    expect(request).not.toHaveBeenCalled();
  });
});
