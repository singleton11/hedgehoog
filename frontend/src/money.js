// Display-only half-up rounding. Never convert exact API decimals to Number.
export function formatMoney(value, { symbol = '', grouping = true, unavailable = 'Unavailable' } = {}) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    // Expand scientific notation at the chart boundary only.
    value = value.toLocaleString('en-US', { useGrouping: false, maximumFractionDigits: 20 });
  }
  if (typeof value !== 'string' || !/^-?\d+(?:\.\d+)?$/.test(value)) return unavailable;
  const [whole, fraction = ''] = value.replace(/^-/, '').split('.');
  const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0').slice(0, 2))
    + (fraction[2] >= '5' ? 1n : 0n);
  const integer = String(cents / 100n);
  return `${value.startsWith('-') && cents > 0n ? '-' : ''}${symbol}${grouping ? integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',') : integer}.${String(cents % 100n).padStart(2, '0')}`;
}
