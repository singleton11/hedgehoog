import { useId, useMemo, useState } from 'react';
import { formatEuros, parseProjection } from './goalProjection.js';

const WIDTH = 960, HEIGHT = 390, LEFT = 90, RIGHT = 24, TOP = 24, BOTTOM = 50;
const PLOT_WIDTH = WIDTH - LEFT - RIGHT, PLOT_HEIGHT = HEIGHT - TOP - BOTTOM;
const axisMoney = new Intl.NumberFormat('en', { style: 'currency', currency: 'EUR', notation: 'compact', maximumSignificantDigits: 3 });

export default function GoalProjectionChart({ trajectory, goal, pathCount }) {
  const id = useId();
  const model = useMemo(() => parseProjection(trajectory, goal), [trajectory, goal]);
  const [selected, setSelected] = useState(null);
  if (!model || !Number.isInteger(pathCount) || pathCount < 1) return <p role="alert">The simulation graph is unavailable because its trajectory data is invalid.</p>;
  const { points, target } = model;
  const first = points[0], last = points.at(-1);
  // Include the mean independently: with a skewed distribution it can exceed p95.
  // Normalize before multiplying, avoiding overflow for large finite values.
  const maximum = points.reduce((max, point) => Math.max(max, point.mean_eur, point.p95_eur), Math.max(target, 1));
  const x = (point) => LEFT + (point.time - first.time) / (last.time - first.time) * PLOT_WIDTH;
  const y = (value) => TOP + (1 - value / maximum * 0.95) * PLOT_HEIGHT;
  const coordinates = (point, field) => `${x(point).toFixed(2)},${y(point[field]).toFixed(2)}`;
  const line = (field) => points.map((point, index) => `${index ? 'L' : 'M'}${coordinates(point, field)}`).join(' ');
  const band = (lower, upper) => `${line(upper)} ${[...points].reverse().map((point) => `L${coordinates(point, lower)}`).join(' ')} Z`;
  const ticks = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
  const activeIndex = Math.min(selected ?? points.length - 1, points.length - 1);
  const active = points[activeIndex];

  function hover(event) {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (!bounds.width) return;
    const chartX = (event.clientX - bounds.left) / bounds.width * WIDTH;
    const time = first.time + (chartX - LEFT) / PLOT_WIDTH * (last.time - first.time);
    let nearest = 0;
    for (let index = 1; index < points.length; index++) {
      if (Math.abs(points[index].time - time) < Math.abs(points[nearest].time - time)) nearest = index;
    }
    setSelected(nearest);
  }

  return <section className="goal-projection" aria-label="Portfolio projection">
    <h3>Portfolio projection</h3>
    <div className="projection-legend" aria-label="Graph legend">
      <span><i className="projection-mean-key" />Average (mean)</span>
      <span><i className="projection-outer-key" />5th–95th percentiles</span>
      <span><i className="projection-inner-key" />25th–75th percentiles</span>
      <span><i className="projection-goal-key" />Goal: {formatEuros(goal)}</span>
    </div>
    <div className="chart-scroll">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`} onPointerMove={hover}>
        <title id={`${id}-title`}>Monte Carlo portfolio projection</title>
        <desc id={`${id}-description`}>Portfolio value in EUR from {first.date} to {last.date}. A solid blue line shows the arithmetic mean of {pathCount.toLocaleString('en')} traces. The light cloud spans the 5th–95th percentiles; the darker cloud spans the 25th–75th percentiles at each date. A dashed line marks the goal. Use the projection date slider for exact values.</desc>
        {Array.from({ length: 5 }, (_, index) => {
          const value = maximum * (index / 4);
          return <g key={index} className="chart-axis"><line x1={LEFT} x2={WIDTH - RIGHT} y1={y(value)} y2={y(value)} />
            <text x={LEFT - 10} y={y(value) + 4} textAnchor="end">{axisMoney.format(value)}</text></g>;
        })}
        <path className="projection-outer" d={band('p05_eur', 'p95_eur')} />
        <path className="projection-inner" d={band('p25_eur', 'p75_eur')} />
        <line className="projection-goal" x1={LEFT} x2={WIDTH - RIGHT} y1={y(target)} y2={y(target)} />
        <path className="projection-mean" d={line('mean_eur')} />
        <line className="projection-cursor" x1={x(active)} x2={x(active)} y1={TOP} y2={HEIGHT - BOTTOM} />
        <circle className="projection-marker" cx={x(active)} cy={y(active.mean_eur)} r="4" />
        {ticks.map((index) => <text key={index} className="chart-date" x={x(points[index])} y={HEIGHT - 16}
          textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}>{points[index].date}</text>)}
      </svg>
    </div>
    <label htmlFor={`${id}-date`}>Projection date</label>
    <input id={`${id}-date`} type="range" min="0" max={points.length - 1} step="1" value={activeIndex}
      aria-valuetext={active.date} aria-describedby={`${id}-readout`} onChange={(event) => setSelected(Number(event.target.value))} />
    <p id={`${id}-readout`} className="projection-readout" aria-live="polite">
      <strong>{active.date}</strong> · Average: {formatEuros(active.raw.mean_eur)} · 5th–95th: {formatEuros(active.raw.p05_eur)} – {formatEuros(active.raw.p95_eur)} · 25th–75th: {formatEuros(active.raw.p25_eur)} – {formatEuros(active.raw.p75_eur)}
    </p>
  </section>;
}
