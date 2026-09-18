import { useEffect, useState } from 'react';
import InstrumentSearch from './InstrumentSearch.jsx';
import InstrumentDetail from './InstrumentDetail.jsx';
import AcquiredEtfs from './AcquiredEtfs.jsx';

function readRoute() {
  const hash = window.location.hash;
  if (!hash || hash === '#' || hash === '#/') return { isin: null };
  const match = hash.match(/^#\/instruments\/([^/?#]+)$/);
  if (!match) return { invalid: true };
  try {
    const isin = decodeURIComponent(match[1]);
    return /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(isin) ? { isin } : { invalid: true };
  } catch {
    return { invalid: true };
  }
}

export default function InstrumentWorkspace() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const onHashChange = () => setRoute(readRoute());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  function select(isin) {
    window.location.hash = `/instruments/${encodeURIComponent(isin)}`;
    setRoute(readRoute());
  }

  return <div className="instrument-workspace">
    <InstrumentSearch onSelect={select} />
    {(route.isin || route.invalid) && <nav aria-label="Instrument navigation"><a href="#/">← ETFs</a></nav>}
    {route.invalid ? <p role="alert">ETF not found. Try searching above.</p>
      : route.isin ? <InstrumentDetail key={route.isin} isin={route.isin} />
        : <AcquiredEtfs />}
  </div>;
}
