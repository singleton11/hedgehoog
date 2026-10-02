// Calculate from exact cent strings; neither large balances nor close targets
// should be rounded through IEEE-754 before displaying progress.
const cents = (value) => {
  if (typeof value !== 'string' || !/^-?\d+\.\d{2}$/.test(value)) return null;
  return BigInt(value.replace('.', ''));
};
const euros = (value) => `${value < 0n ? '-' : ''}${(value < 0n ? -value : value) / 100n}.${String((value < 0n ? -value : value) % 100n).padStart(2, '0')}`;

export function goalProgress(equity, target) {
  const amount = cents(equity), goal = cents(target);
  if (amount === null || goal === null || goal <= 0n) return null;
  const remaining = goal - amount;
  // One decimal percent, rounded half up, retaining values above 100%.
  const magnitude = ((amount < 0n ? -amount : amount) * 1000n + goal / 2n) / goal;
  return { achieved: `${amount < 0n ? '-' : ''}${magnitude / 10n}.${magnitude % 10n}%`, remaining: euros(remaining) };
}
