import { afterEach, describe, expect, it, vi } from 'vitest';
import { secureUuid } from './secureUuid.js';

const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
afterEach(() => vi.unstubAllGlobals());

describe('secureUuid', () => {
  it.each([0, 255])('sets UUID v4 version and variant bits from secure bytes filled with %s', (value) => {
    const getRandomValues = vi.fn((bytes) => bytes.fill(value));
    vi.stubGlobal('crypto', { getRandomValues });
    const id = secureUuid();
    expect(id).toBe(value === 0 ? '00000000-0000-4000-8000-000000000000' : 'ffffffff-ffff-4fff-bfff-ffffffffffff');
    expect(id).toMatch(uuidV4);
    expect(getRandomValues).toHaveBeenCalledOnce();
    expect(getRandomValues.mock.calls[0][0]).toBeInstanceOf(Uint8Array);
    expect(getRandomValues.mock.calls[0][0]).toHaveLength(16);
  });

  it('preserves random bytes and generates a fresh ID per call without randomUUID', () => {
    let start = 0;
    const getRandomValues = vi.fn((bytes) => {
      bytes.set(Array.from({ length: 16 }, (_, index) => start + index));
      start += 16;
      return bytes;
    });
    vi.stubGlobal('crypto', { getRandomValues });
    expect(secureUuid()).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f');
    expect(secureUuid()).toBe('10111213-1415-4617-9819-1a1b1c1d1e1f');
    expect(getRandomValues).toHaveBeenCalledTimes(2);
  });

  it.each([undefined, {}, { getRandomValues: () => { throw new Error('unavailable'); } }])('fails closed when secure randomness is unavailable: %s', (crypto) => {
    vi.stubGlobal('crypto', crypto);
    expect(() => secureUuid()).toThrow();
  });
});
