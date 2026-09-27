import { describe, expect, it, vi } from 'vitest';
import { loadAllQuotes } from './instruments.js';

const FIRST = '11111111-1111-4111-8111-111111111111';
const SECOND = '22222222-2222-4222-8222-222222222222';
const cutoff = '2026-09-17T12:00:00Z';
const page = (offset, generation = FIRST) => ({
  items: [{ date: `2026-09-${14 + offset}`, close: '100' }],
  total: 2, offset, limit: 1000, as_of: cutoff, generation,
});

describe('immutable quote generations', () => {
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
      .rejects.toThrow('changed history generation');
  });

  it('pins the detail generation on the first chart request', async () => {
    const request = vi.fn().mockResolvedValueOnce(page(0)).mockResolvedValueOnce(page(1));
    await loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: FIRST });
    expect(new URL(request.mock.calls[0][0], 'http://localhost').searchParams.get('generation')).toBe(FIRST);
  });

  it('pins the explicit no-publication state instead of adopting a late publication', async () => {
    const request = vi.fn().mockResolvedValue({ ...page(0, 'legacy'), total: 0, items: [] });
    expect(await loadAllQuotes(request, 'IE00B4L5Y983', new AbortController().signal, { generation: 'legacy' })).toEqual([]);
    expect(new URL(request.mock.calls[0][0], 'http://localhost').searchParams.get('generation')).toBe('legacy');
  });
});
