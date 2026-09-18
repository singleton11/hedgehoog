// Money remains exact text until the chart's plotting boundary.
export function formatEuros(value) {
  if (typeof value !== 'string' || !/^-?\d+(?:\.\d+)?$/.test(value)) return 'Unavailable';
  const [whole, fraction = ''] = value.replace(/^-/, '').split('.');
  return `${value.startsWith('-') ? '-' : ''}€${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${fraction.padEnd(2, '0')}`;
}

const fields = ['mean_eur', 'p05_eur', 'p25_eur', 'p50_eur', 'p75_eur', 'p95_eur'];
function amount(value) {
  if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value)) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function parseProjection(trajectory, goal) {
  const target = amount(goal);
  if (target === null || target <= 0 || !Array.isArray(trajectory) || trajectory.length < 2 || trajectory.length > 602) return null;
  const points = [];
  for (const point of trajectory) {
    if (!point || typeof point.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(point.date)) return null;
    const time = Date.parse(`${point.date}T00:00:00Z`);
    if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== point.date
      || (points.length && time <= points.at(-1).time)) return null;
    const values = Object.fromEntries(fields.map((field) => [field, amount(point[field])]));
    if (Object.values(values).some((value) => value === null)) return null;
    const percentiles = fields.slice(1).map((field) => values[field]);
    if (percentiles.some((value, index) => index > 0 && value < percentiles[index - 1])) return null;
    points.push({ ...values, time, date: point.date, raw: point });
  }
  return { points, target };
}
