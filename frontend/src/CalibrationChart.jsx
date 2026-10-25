import PhoneTable from './PhoneTable.jsx';
import { useId } from 'react';
import { percent } from './ReturnStatistics.jsx';

const WIDTH = 640, HEIGHT = 440, LEFT = 78, RIGHT = 28, TOP = 24, BOTTOM = 76;
const x = (value) => LEFT + value * (WIDTH - LEFT - RIGHT);
const y = (value) => TOP + (1 - value) * (HEIGHT - TOP - BOTTOM);
const range = (bin) => `${percent(bin.lower)}–${percent(bin.upper)}`;

// The caller validates all ten bins before rendering this chart.
export default function CalibrationChart({ bins, countLabel = 'forecasts' }) {
  const id = useId();
  const populated = bins.filter((bin) => bin.count > 0);
  return <section className="calibration-chart" aria-label="Goal success calibration curve">
    <h3>Calibration curve</h3>
    <p className="muted">Dashed line: ideal calibration.</p>
    <div className="chart-scroll" role="region" aria-label="Scrollable calibration chart" tabIndex={0}>
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}>
      <title id={`${id}-title`}>Financial goal success calibration curve</title>
      <desc id={`${id}-description`}>Horizontal axis: mean predicted probability, 0% to 100%. Vertical axis: observed success frequency, 0% to 100%. Only nonempty bins are plotted; labels give evaluated {countLabel} counts. All ten bins, including empty bins, are in the table below.</desc>
      {[0, 0.25, 0.5, 0.75, 1].map((value) => <g className="chart-axis" key={value}>
        <line x1={LEFT} x2={WIDTH - RIGHT} y1={y(value)} y2={y(value)} />
        <line x1={x(value)} x2={x(value)} y1={TOP} y2={HEIGHT - BOTTOM} />
        <text x={LEFT - 10} y={y(value) + 4} textAnchor="end">{percent(value)}</text>
        <text x={x(value)} y={HEIGHT - BOTTOM + 23} textAnchor="middle">{percent(value)}</text>
      </g>)}
      <line className="calibration-ideal" x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} strokeDasharray="7 5" />
      <polyline className="calibration-curve" points={populated.map((bin) => `${x(bin.mean_predicted_probability)},${y(bin.observed_success_rate)}`).join(' ')} />
      {populated.map((bin) => <g key={bin.lower}>
        <circle className="calibration-point" cx={x(bin.mean_predicted_probability)} cy={y(bin.observed_success_rate)} r="5">
          <title>{range(bin)}: {bin.count} {countLabel}; predicted {percent(bin.mean_predicted_probability)}, observed {percent(bin.observed_success_rate)}</title>
        </circle>
        <text className="calibration-count" x={x(bin.mean_predicted_probability)} y={y(bin.observed_success_rate) + (bin.observed_success_rate > 0.9 ? 20 : -12)}
          textAnchor={bin.mean_predicted_probability > 0.9 ? 'end' : bin.mean_predicted_probability < 0.1 ? 'start' : 'middle'}>n={bin.count}</text>
      </g>)}
      <text className="calibration-axis-label" x={(LEFT + WIDTH - RIGHT) / 2} y={HEIGHT - 15} textAnchor="middle">Mean predicted probability</text>
      <text className="calibration-axis-label" transform={`translate(18 ${(TOP + HEIGHT - BOTTOM) / 2}) rotate(-90)`} textAnchor="middle">Observed success frequency</text>
    </svg>
    </div>
    <details className="disclosure"><summary>Calibration bins</summary><div className="disclosure-content">
    <div className="calibration-table-scroll" role="region" aria-label="Calibration bin data" tabIndex={0}>
      <PhoneTable className="calibration-table">
        <caption>Calibration bins</caption>
        <thead><tr><th scope="col">Predicted probability bin</th><th scope="col">{countLabel === 'scenarios' ? 'Scored scenarios' : 'Evaluated forecasts'}</th><th scope="col">Mean predicted probability</th><th scope="col">Observed success frequency</th></tr></thead>
        <tbody>{bins.map((bin) => <tr key={bin.lower}>
          <th scope="row">{range(bin)}</th><td>{bin.count}</td>
          <td>{bin.count ? percent(bin.mean_predicted_probability) : `No ${countLabel}`}</td>
          <td>{bin.count ? percent(bin.observed_success_rate) : `No ${countLabel}`}</td>
        </tr>)}</tbody>
      </PhoneTable>
    </div>
    </div></details>
  </section>;
}
