import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import GoalProjectionChart from './GoalProjectionChart.jsx';
import { parseProjection } from './goalProjection.js';

const points = [
  { date: '2026-01-31', mean_eur: '10000.00', p05_eur: '10000.00', p25_eur: '10000.00', p50_eur: '10000.00', p75_eur: '10000.00', p95_eur: '10000.00' },
  { date: '2026-02-28', mean_eur: '15000.00', p05_eur: '9000.00', p25_eur: '11000.00', p50_eur: '14000.00', p75_eur: '18000.00', p95_eur: '24000.00' },
  { date: '2026-03-15', mean_eur: '18000.00', p05_eur: '8000.00', p25_eur: '12000.00', p50_eur: '16000.00', p75_eur: '22000.00', p95_eur: '32000.00' },
];
const draw = (trajectory = points, goal = '20000.00') => render(<GoalProjectionChart trajectory={trajectory} goal={goal} pathCount={10000} />);

function expectFiniteChart(container) {
  for (const element of container.querySelectorAll('svg path, svg line, svg circle')) {
    for (const attribute of element.attributes) {
      expect(attribute.value).not.toMatch(/NaN|Infinity/);
    }
  }
}

describe('portfolio projection graph', () => {
  it('draws a mean line, nested closed percentile clouds and a goal reference', () => {
    const { container } = draw();
    const image = screen.getByRole('img', { name: 'Monte Carlo portfolio projection' });
    expect(image).toHaveAccessibleDescription(expect.stringContaining('arithmetic mean of 10,000 traces'));
    expect(image).toHaveAccessibleDescription(expect.stringContaining('Use the projection date slider for exact values'));
    expect(container.querySelector('.projection-outer')).toHaveAttribute('d', expect.stringMatching(/^M.+ Z$/));
    expect(container.querySelector('.projection-inner')).toHaveAttribute('d', expect.stringMatching(/^M.+ Z$/));
    expect(container.querySelector('.projection-mean').getAttribute('d').match(/L/g)).toHaveLength(2);
    expect(container.querySelector('.projection-goal')).toBeInTheDocument();
    expect(screen.getByText('Average (mean)')).toBeInTheDocument();
    expect(screen.getByText('5th–95th percentiles')).toBeInTheDocument();
    expect(screen.getByText('25th–75th percentiles')).toBeInTheDocument();
    expect(screen.getByText('Goal: €20,000.00')).toBeInTheDocument();
    expect(screen.queryByText(/not a confidence interval for the mean/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Average and outcome cloud from all/)).not.toBeInTheDocument();
    expectFiniteChart(container);
  });

  it('exposes exact date/value readouts with a keyboard-accessible slider', () => {
    draw();
    const slider = screen.getByRole('slider', { name: 'Projection date' });
    expect(slider).toHaveValue('2');
    expect(slider).toHaveAttribute('aria-valuetext', '2026-03-15');
    expect(screen.getByText(/Average: €18,000.00/)).toHaveTextContent('5th–95th: €8,000.00 – €32,000.00');
    fireEvent.change(slider, { target: { value: '0' } });
    expect(slider).toHaveAttribute('aria-valuetext', '2026-01-31');
    expect(screen.getByText(/Average: €10,000.00/)).toHaveTextContent('25th–75th: €10,000.00 – €10,000.00');
  });

  it('places dates proportionally in time, including a partial last month', () => {
    const { container } = draw();
    const coords = container.querySelector('.projection-mean').getAttribute('d').match(/[ML]([\d.]+),/g).map((value) => Number(value.slice(1, -1)));
    expect((coords[1] - coords[0]) / (coords[2] - coords[0])).toBeCloseTo(28 / 43, 4);
  });

  it('supports one-payment zero-variance graphs and huge goals without invalid geometry', () => {
    const flat = [points[0], { ...points[0], date: '2026-02-28' }];
    const { container, rerender } = draw(flat, '10000.00');
    expect(container.querySelector('.projection-mean').getAttribute('d')).toMatch(/^M90\.00,39\.80 L936\.00,39\.80$/);
    expectFiniteChart(container);
    rerender(<GoalProjectionChart trajectory={flat} goal={`1${'0'.repeat(307)}.00`} pathCount={10000} />);
    expectFiniteChart(container);
  });

  it('does not clip a skewed mean above the cloud or a goal above all paths', () => {
    const skewed = [points[0], { ...points[1], mean_eur: '200000.00' }, points[2]];
    const { container } = draw(skewed, '300000.00');
    const ys = container.querySelector('.projection-mean').getAttribute('d').split(' ').map((pair) => Number(pair.split(',')[1]));
    expect(ys.every((value) => value >= 24 && value <= 340)).toBe(true);
    expect(Number(container.querySelector('.projection-goal').getAttribute('y1'))).toBeLessThan(Math.min(...ys));
  });

  it.each([
    null, [], [points[0]],
    [points[1], points[0]], [points[0], points[0]],
    [points[0], { ...points[1], mean_eur: 'Infinity' }],
    [points[0], { ...points[1], mean_eur: '' }],
    [points[0], { ...points[1], p05_eur: '-1' }],
    [points[0], { ...points[1], p95_eur: '1' }],
    [points[0], { ...points[1], date: '2026-02-30' }],
  ])('rejects malformed or unordered data instead of drawing misleading paths', (trajectory) => {
    expect(parseProjection(trajectory, '10000.00')).toBeNull();
    draw(trajectory);
    expect(screen.getByRole('alert')).toHaveTextContent('trajectory data is invalid');
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
