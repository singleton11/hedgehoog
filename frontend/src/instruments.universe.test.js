import { describe, expect, it, vi } from 'vitest';
import { loadAcquiredInstruments } from './instruments.js';

const item = (instrument_id) => ({ instrument_id, collected_at: '2026-09-17T12:00:00Z' });

describe('acquired universe capture', () => {
  it('loads more than a page of ETFs in one lightweight response', async () => {
    const items = Array.from({ length: 1062 }, (_, index) => item(`IE${String(index).padStart(10, '0')}`));
    const request = vi.fn().mockResolvedValue({ items, total: items.length });
    const { signal } = new AbortController();
    expect(await loadAcquiredInstruments(request, signal)).toEqual(items);
    expect(request.mock.calls).toEqual([['/instruments/acquired/universe', { signal }]]);
  });

  it('accepts an empty inventory', async () => {
    expect(await loadAcquiredInstruments(vi.fn().mockResolvedValue({ items: [], total: 0 }), new AbortController().signal)).toEqual([]);
  });

  it.each([
    null, {}, { items: null, total: 0 }, { items: [], total: -1 }, { items: [], total: '0' },
    { items: [], total: 1 }, { items: [item('A')], total: 0 },
    { items: [item('A'), item('A')], total: 2 }, { items: [null], total: 1 },
    { items: [item('')], total: 1 }, { items: [item(42)], total: 1 },
  ])('rejects an incomplete or invalid response: %j', async (data) => {
    await expect(loadAcquiredInstruments(vi.fn().mockResolvedValue(data), new AbortController().signal))
      .rejects.toThrow('list changed or was incomplete');
  });

  it('does not request an inventory after cancellation', async () => {
    const abort = new AbortController();
    abort.abort();
    const request = vi.fn();
    expect(await loadAcquiredInstruments(request, abort.signal)).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it('ignores a late response after cancellation', async () => {
    const abort = new AbortController();
    let resolve;
    const request = vi.fn(() => new Promise((done) => { resolve = done; }));
    const pending = loadAcquiredInstruments(request, abort.signal);
    abort.abort();
    resolve(null);
    expect(await pending).toBeNull();
  });
});
