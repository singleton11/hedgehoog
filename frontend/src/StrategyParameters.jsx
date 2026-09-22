import { useEffect, useId } from 'react';
import { displayText } from './instruments.js';
import { StatisticsSummary, useReturnStatistics } from './ReturnStatistics.jsx';

function InstrumentParameter({ parameter, value = '', instruments, onChange, onReadiness, onInvalidate, fieldKey }) {
  const id = useId();
  const acquired = instruments.some((item) => item.instrument_id === value);
  const { data: statistics, error, loading, retry } = useReturnStatistics(acquired ? value : null);
  const ready = acquired ? Boolean(statistics?.eligible && !loading && !error) : !value && !parameter.required;
  useEffect(() => { onReadiness(fieldKey, value, ready); }, [fieldKey, value, ready, onReadiness]);

  function refresh() {
    onInvalidate();
    onReadiness(fieldKey, value, false);
    retry();
  }

  return <div className="strategy-parameter">
    <label htmlFor={id}>{parameter.label}</label>
    <p className="parameter-hint" id={`${id}-help`}>Instrument parameter · acquired universe only</p>
    <select id={id} value={acquired ? value : ''} aria-describedby={`${id}-help`} required={parameter.required}
      onChange={(event) => onChange(parameter.key, event.target.value)}>
      <option value="">Choose an acquired instrument</option>
      {instruments.map((item) => <option key={item.instrument_id} value={item.instrument_id}>
        {displayText(item.name) || item.instrument_id} · {item.instrument_id}{item.ticker ? ` · ${item.ticker}` : ''}
      </option>)}
    </select>
    {!instruments.length && <p className="empty-state">No acquired instruments yet. <a href="#/add">Add an ETF</a> and request its data first.</p>}
    {value && !acquired && <p role="alert">This instrument is not in the acquired universe. Choose an acquired instrument or <a href={`#/instruments/${encodeURIComponent(value)}`}>acquire its data first</a>.</p>}
    {acquired && <section aria-label={`Selected ${parameter.label.toLowerCase()}`} className="goal-instrument">
      <div className="section-heading">
        <div>
          <h3>{displayText(statistics?.name) || displayText(instruments.find((item) => item.instrument_id === value)?.name) || 'Selected instrument'}</h3>
          <p className="muted">{value}{statistics?.listing && ` · ${statistics.listing.symbol} / ${statistics.listing.exchange}`}</p>
        </div>
        <a href={`#/instruments/${encodeURIComponent(value)}`}>Instrument details</a>
      </div>
      {loading && <p role="status">Checking saved data…</p>}
      {error && <>
        <p role="alert">Could not load statistics: {error}</p>
        <button type="button" className="secondary" onClick={refresh}>Retry statistics</button>
      </>}
      {statistics && <>
        {ready ? <p role="status" className="muted">{statistics.price_currency} · {statistics.return_count} daily returns</p>
          : <div className="empty-state" role="status">
            <p><strong>Not eligible for goal planning.</strong> {statistics.ineligible_reason || 'Saved data is not eligible for this calculation.'}</p>
            <p>Download history or update the price currency in instrument details.</p>
          </div>}
        <details className="disclosure"><summary>Saved return statistics</summary><div className="disclosure-content">
          <StatisticsSummary statistics={statistics} />
          <button type="button" className="secondary" onClick={refresh}>Refresh statistics</button>
        </div></details>
      </>}
    </section>}
  </div>;
}

// Add parameter-type editors here, independently of individual strategies.
const editors = { instrument: InstrumentParameter };
export const supportsParameter = (parameter) => Object.hasOwn(editors, parameter.type)
  && (parameter.type !== 'instrument' || parameter.universe === 'acquired');

export default function StrategyParameters({ strategy, parameters, ...props }) {
  return <div className="strategy-parameters">
    {strategy.parameters.map((parameter) => {
      const Editor = editors[parameter.type];
      return supportsParameter(parameter)
        ? <Editor key={`${strategy.id}:${parameter.key}`} fieldKey={`${strategy.id}:${parameter.key}`}
          parameter={parameter} value={parameters[parameter.key]} {...props} />
        : <p role="alert" key={parameter.key}>The parameter type “{parameter.type}” is not supported by this planner.</p>;
    })}
  </div>;
}
