import { useId, useMemo, useState } from 'react';

export function Metadata({ title, value }) {
  return <details className="metadata"><summary>{title}</summary>
    <pre tabIndex={0} aria-label={title}>{JSON.stringify(value, null, 2)}</pre>
  </details>;
}

function price(value) {
  if ((typeof value !== 'string' && typeof value !== 'number') || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function candle(quote) {
  const values = ['open', 'high', 'low', 'close'].map((key) => price(quote[key]));
  if (values.some((value) => value === null) || typeof quote.date !== 'string') return null;
  const [open, high, low, close] = values;
  return { quote, open, high, low, close };
}

const raw = (value) => value == null ? 'Not supplied' : String(value);
function description(quote) {
  return `${quote.date} · Open ${raw(quote.open)} · High ${raw(quote.high)} · Low ${raw(quote.low)} · Close ${raw(quote.close)} · Volume ${raw(quote.volume)} · Price currency: ${quote.price_currency || 'unknown'}`;
}

export default function CandlestickChart({ quotes }) {
  const [windowSize, setWindowSize] = useState('250');
  const [hover, setHover] = useState(null);
  const id = useId();
  const visible = useMemo(() => windowSize === 'all' ? quotes : quotes.slice(-Number(windowSize)), [quotes, windowSize]);
  const candles = useMemo(() => visible.map(candle).filter(Boolean), [visible]);
  const currencies = [...new Set(visible.map((quote) => quote.price_currency || null))];
  const currency = currencies.length === 1 && currencies[0] ? currencies[0] : 'Unknown / not consistently supplied';
  const width = 960, height = 380, left = 86, right = 22, top = 20, bottom = 48;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  // Plot the values as supplied, including inconsistent/zero/negative prices.
  // Include all four fields in the domain so none are clipped or repaired.
  const low = candles.reduce((value, item) => Math.min(value, item.low, item.high, item.open, item.close), Infinity);
  const high = candles.reduce((value, item) => Math.max(value, item.low, item.high, item.open, item.close), -Infinity);
  const padding = candles.length ? Math.max((high - low) * 0.06, Math.abs(high) * 0.005, 0.01) : 1;
  const min = low - padding, max = high + padding;
  const y = (value) => top + (max - value) / (max - min) * plotHeight;
  const step = plotWidth / Math.max(candles.length, 1);
  const x = (index) => left + (index + 0.5) * step;
  const bodyWidth = Math.max(0.4, Math.min(14, step * 0.7));
  const dateTicks = [...new Set([0, Math.floor((candles.length - 1) / 2), candles.length - 1])].filter((index) => index >= 0);
  const number = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 7 });

  return <section aria-label="Price history" className="price-history">
    <div className="section-heading"><h2>Price history</h2>
      <div className="button-group" role="group" aria-label="Chart window">
        {[["60", 'Recent 60'], ["250", 'Recent 250'], ['all', 'All quotes']].map(([value, label]) =>
          <button type="button" key={value} className="secondary" aria-pressed={windowSize === value}
            onClick={() => { setWindowSize(value); setHover(null); }}>{label}</button>)}
      </div>
    </div>
    <p className="muted">Unadjusted OHLC · Listing price currency: {currency}. Fund currency is not a substitute. Dates are trading observations, not evenly spaced calendar days.</p>
    <p>{quotes.length} quotes loaded · {visible.length} in this window · {candles.length} candles plotted</p>
    {visible.length > candles.length && <p className="notice">{visible.length - candles.length} quotes with missing or non-numeric OHLC cannot be drawn. They are saved unchanged; no zero values are substituted.</p>}
    {!candles.length ? <p role="status">No plottable OHLC candles available.</p> : <>
      <div className="chart-scroll">
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${id}-title ${id}-description`} onMouseLeave={() => setHover(null)}>
          <title id={`${id}-title`}>Unadjusted OHLC candlestick chart</title>
          <desc id={`${id}-description`}>{candles.length} candles from {candles[0].quote.date} to {candles.at(-1).quote.date}. Green hollow candles close at or above open; red filled candles close below open. Exact values are available in the expandable quote observations below.</desc>
          {Array.from({ length: 5 }, (_, index) => {
            const value = min + (max - min) * index / 4;
            return <g key={index} className="chart-axis"><line x1={left} x2={width - right} y1={y(value)} y2={y(value)} />
              <text x={left - 10} y={y(value) + 4} textAnchor="end">{number.format(value)}</text></g>;
          })}
          {candles.map((item, index) => <g key={`${item.quote.date}-${index}`} className={item.close >= item.open ? 'candle candle-up' : 'candle candle-down'}
            onMouseEnter={() => setHover(item.quote)}>
            <title>{description(item.quote)}</title>
            <line x1={x(index)} x2={x(index)} y1={y(item.high)} y2={y(item.low)} />
            <rect x={x(index) - bodyWidth / 2} y={y(Math.max(item.open, item.close))} width={bodyWidth}
              height={Math.max(1, Math.abs(y(item.open) - y(item.close)))} />
          </g>)}
          {dateTicks.map((index) => <text key={index} className="chart-date" x={x(index)} y={height - 16}
            textAnchor={index === 0 ? 'start' : index === candles.length - 1 ? 'end' : 'middle'}>{candles[index].quote.date}</text>)}
        </svg>
      </div>
      <p className="chart-readout">{hover && visible.includes(hover) ? description(hover) : 'Hover a candle for exact OHLC. Expand quote observations for keyboard-accessible raw and adjusted values.'}</p>
    </>}
    <Metadata title="Quote observations (current window, all provider fields)" value={visible} />
  </section>;
}
