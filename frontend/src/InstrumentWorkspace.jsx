import { useEffect, useState } from 'react';
import AddEtf from './AddEtf.jsx';
import InstrumentDetail from './InstrumentDetail.jsx';
import AcquiredEtfs from './AcquiredEtfs.jsx';
import GoalPlanner from './GoalPlanner.jsx';
import Icon from './Icon.jsx';

function readRoute() {
  const hash = window.location.hash;
  if (!hash || hash === '#' || hash === '#/') return { isin: null };
  if (hash === '#/goal') return { goal: true, isin: null };
  if (hash === '#/add') return { add: true, isin: null };
  const match = hash.match(/^#\/(instruments|goal)\/([^/?#]+)$/);
  if (!match) return { invalid: true };
  try {
    const isin = decodeURIComponent(match[2]);
    return /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(isin) ? { isin, goal: match[1] === 'goal' } : { invalid: true };
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
    <nav aria-label="Workspace navigation" className="workspace-navigation">
      <p className="nav-label">Workspace</p>
      <a href="#/" aria-current={!route.goal && !route.invalid ? 'page' : undefined}><Icon name="grid" />ETFs</a>
      <a href="#/goal" aria-current={route.goal ? 'page' : undefined}><Icon name="target" />Goal planner</a>
      <div className="sidebar-note"><span className="sidebar-note-icon"><Icon name="chart" /></span>
        <strong>A little perspective.<br />A longer view.</strong>
        <p>Your data. Your next move.</p>
      </div>
      <p className="sidebar-footer">HEDGEHOOG <span> / </span> INVEST WITH INSIGHT</p>
    </nav>
    <div id="workspace-content" className="workspace-content" tabIndex={-1}>
    {route.goal ? <GoalPlanner key={route.isin || 'strategy-planner'} isin={route.isin} /> : <>
      {(route.add || route.isin || route.invalid) && <nav aria-label="Instrument navigation"><a href="#/">← ETFs</a></nav>}
      {route.invalid ? <p role="alert">ETF not found. <a href="#/add">Search for an ETF</a>.</p>
        : route.add ? <AddEtf onSelect={select} />
          : route.isin ? <InstrumentDetail key={route.isin} isin={route.isin} />
            : <AcquiredEtfs />}
    </>}
    </div>
  </div>;
}
