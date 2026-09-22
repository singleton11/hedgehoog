import InstrumentSearch from './InstrumentSearch.jsx';
import Icon from './Icon.jsx';

export default function AddEtf({ onSelect }) {
  return <section aria-labelledby="add-etf-heading" className="add-etf-page">
    <div className="page-heading"><p className="eyebrow">Discover your next investment</p>
      <h2 id="add-etf-heading" className="instrument-name">Add ETF</h2>
      <p className="muted">Find an ETF by name, ticker or ISIN, then choose Request Data on its details page to add it to your acquired ETFs.</p>
    </div>
    <div className="search-card"><span className="feature-icon"><Icon name="search" /></span>
      <InstrumentSearch onSelect={onSelect} />
    </div>
  </section>;
}
