import { describe, expect, it } from 'vitest';
import { formatMoney } from './money.js';
import { formatEuros } from './goalProjection.js';

describe('display-only money formatting', () => {
  it.each([
    ['0', '0.00'], ['12', '12.00'], ['12.3', '12.30'], ['12.344', '12.34'],
    ['12.345', '12.35'], ['999.995', '1,000.00'], ['-12.345', '-12.35'],
    ['-0.004', '0.00'], ['9007199254740993.015', '9,007,199,254,740,993.02'],
    [12.345, '12.35'], [1e-7, '0.00'],
  ])('formats %s as %s', (value, expected) => {
    expect(formatMoney(value)).toBe(expected);
    expect(formatEuros(value)).toBe(expected.startsWith('-') ? `-€${expected.slice(1)}` : `€${expected}`);
  });
  it.each([null, undefined, '', 'invalid', {}, Infinity, NaN])('keeps invalid %s unavailable', (value) => {
    expect(formatMoney(value)).toBe('Unavailable');
  });
  it('does not modify exact input values', () => {
    const quote = { close: '1234.56789' };
    expect(formatMoney(quote.close)).toBe('1,234.57');
    expect(quote.close).toBe('1234.56789');
  });
});
