import { describe, expect, it } from 'vitest';
import { goalProgress } from './goalProgress.js';

describe('goal progress', () => {
  it.each([
    ['12000.00', '100000.00', '12.0%', '88000.00'],
    ['150000.00', '100000.00', '150.0%', '-50000.00'],
    ['0.00', '100000.00', '0.0%', '100000.00'],
    ['999999999999999999.99', '1000000000000000000.00', '100.0%', '0.01'],
  ])('compares %s with %s using exact cents', (equity, target, achieved, remaining) => {
    expect(goalProgress(equity, target)).toEqual({ achieved, remaining });
  });
  it('rejects missing or invalid values', () => {
    expect(goalProgress(null, '100.00')).toBeNull();
    expect(goalProgress('50.00', '0.00')).toBeNull();
  });
});
