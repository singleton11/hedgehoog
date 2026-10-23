import { useEffect, useRef, useState } from 'react';
import AddEtf from './AddEtf.jsx';
import Indices from './Indices.jsx';
import IndexDetail from './IndexDetail.jsx';
import InstrumentDetail from './InstrumentDetail.jsx';
import AcquiredEtfs from './AcquiredEtfs.jsx';
import CustomerPlanner from './CustomerPlanner.jsx';
import PortfolioTracker from './PortfolioTracker.jsx';
import StrategyCalibration from './StrategyCalibration.jsx';
import Icon from './Icon.jsx';
import PerformanceSettings from './PerformanceSettings.jsx';
import Jobs from './Jobs.jsx';

function readRoute() {
  const hash = window.location.hash;
  if (hash === '#/jobs') return { jobs: true };
  if (hash === '#/settings') return { settings: true };
  if (!hash || hash === '#' || hash === '#/') return { isin: null };
  // Opening a customer defaults to Portfolios; Goals has an explicit /goals route.
  const portfolios = hash.match(/^#\/goal\/customers\/([^/?#]+)(?:\/portfolios(?:\/([^/?#]+))?)?(?:\?(.*))?$/);
  if (portfolios) {
    try {
      const [customerId, portfolioId] = portfolios.slice(1, 3).map((value) => value ? decodeURIComponent(value) : null);
      if (![customerId, portfolioId].every((value) => value === null || /^[a-zA-Z0-9_-]+$/.test(value))) return { invalid: true };
      const instrument = new URLSearchParams(portfolios[3]).get('instrument');
      if (instrument && !/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(instrument)) return { invalid: true };
      return { goal: true, portfolios: true, customerId, portfolioId, isin: instrument };
    } catch { return { invalid: true }; }
  }
  const planner = hash.match(/^#\/goal(?:\/customers\/([^/?#]+)(?:\/goals(?:\/([^/?#]+)(?:\/(new|simulations\/([^/?#]+)))?)?)?)?(?:\?(.*))?$/);
  if (planner) {
    try {
      const ids = [planner[1], planner[2], planner[4]].map((value) => value ? decodeURIComponent(value) : null);
      if (ids.some((value) => value && !/^[a-zA-Z0-9_-]+$/.test(value))) return { invalid: true };
      const instrument = new URLSearchParams(planner[5]).get('instrument');
      if (instrument && !/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(instrument)) return { invalid: true };
      return { goal: true, customerId: ids[0], goalId: ids[1], simulationId: ids[2], newSimulation: planner[3] === 'new', isin: instrument };
    } catch { return { invalid: true }; }
  }
  if (hash === '#/calibration' || hash === '#/strategies') return { calibration: true, isin: null };
  const newRun = hash.match(/^#\/calibration\/new(?:\?(.*))?$/);
  if (newRun) return { calibration: true, newRun: true, sourceId: new URLSearchParams(newRun[1]).get('from'), isin: null };
  const runMatch = hash.match(/^#\/calibration\/([^/?#]+)$/);
  if (runMatch) {
    try { return { calibration: true, runId: decodeURIComponent(runMatch[1]), isin: null }; }
    catch { return { invalid: true }; }
  }
  if (hash === '#/indices') return { indices: true, isin: null };
  const indexMatch = hash.match(/^#\/indices\/([^/?#]+)$/);
  if (indexMatch) {
    try {
      const indexId = decodeURIComponent(indexMatch[1]);
      return /^idx-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(indexId)
        ? { indices: true, indexId } : { indices: true, invalid: true };
    } catch { return { indices: true, invalid: true }; }
  }
  if (hash === '#/add') return { add: true, isin: null };
  const strategyMatch = hash.match(/^#\/strategies\/([^/?#]+)$/);
  if (strategyMatch) {
    try { return { calibration: true, newRun: true, strategyId: decodeURIComponent(strategyMatch[1]), isin: null }; }
    catch { return { invalid: true }; }
  }
  const match = hash.match(/^#\/(instruments|goal)\/([^/?#]+)$/);
  if (!match) return { invalid: true };
  try {
    const isin = decodeURIComponent(match[2]);
    return /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(isin) ? { isin, goal: match[1] === 'goal' } : { invalid: true };
  } catch {
    return { invalid: true };
  }
}

function WorkspaceNavigation({ route }) {
  const [expanded, setExpanded] = useState(false);
  const toggle = useRef(null);
  const links = [
    { href: '#/', label: 'ETFs', icon: 'grid', active: !route.jobs && !route.settings && !route.indices && !route.goal && !route.calibration && !route.invalid },
    { href: '#/indices', label: 'Indices', icon: 'chart', active: route.indices },
    { href: '#/goal', label: 'Customers', icon: 'users', active: route.goal },
    { href: '#/calibration', label: 'Calibration', icon: 'chart', active: route.calibration },
    { href: '#/jobs', label: 'Jobs', icon: 'layers', active: route.jobs },
    { href: '#/settings', label: 'Settings', icon: 'layers', active: route.settings },
  ];

  useEffect(() => {
    const close = () => setExpanded(false);
    window.addEventListener('hashchange', close);
    return () => window.removeEventListener('hashchange', close);
  }, []);

  return <nav aria-label="Workspace navigation" className="workspace-navigation" onKeyDown={(event) => {
    if (event.key === 'Escape' && expanded) {
      setExpanded(false);
      toggle.current?.focus();
    }
  }}>
    <div className="mobile-navigation-heading">
      <span>{links.find((link) => link.active)?.label || 'Workspace'}</span>
      <button ref={toggle} className="secondary" aria-expanded={expanded} aria-controls="workspace-navigation-links"
        onClick={() => setExpanded((value) => !value)}>{expanded ? 'Close menu' : 'Menu'}</button>
    </div>
    <div id="workspace-navigation-links" className={`workspace-navigation-links${expanded ? ' is-expanded' : ''}`}>
      <p className="nav-label">Workspace</p>
      {links.map((link) => <a key={link.href} href={link.href} aria-current={link.active ? 'page' : undefined} onClick={() => {
        if (expanded) {
          setExpanded(false);
          document.getElementById('workspace-content')?.focus();
        }
      }}><Icon name={link.icon} />{link.label}</a>)}
    </div>
    <div className="sidebar-note"><span className="sidebar-note-icon"><Icon name="chart" /></span>
      <strong>A little perspective.<br />A longer view.</strong>
      <p>Your data. Your next move.</p>
    </div>
    <p className="sidebar-footer">HEDGEHOOG <span> / </span> INVEST WITH INSIGHT</p>
  </nav>;
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
    <WorkspaceNavigation route={route} />
    <div id="workspace-content" className="workspace-content" tabIndex={-1}>
    {route.jobs ? <Jobs /> : route.settings ? <PerformanceSettings /> : route.indices ? route.invalid ? <p role="alert">Index not found. <a href="#/indices">Back to indices</a>.</p>
      : route.indexId ? <IndexDetail key={route.indexId} instrumentId={route.indexId} /> : <Indices /> : route.portfolios ? <PortfolioTracker customerId={route.customerId} portfolioId={route.portfolioId} isin={route.isin} /> : route.calibration ? <StrategyCalibration newRun={route.newRun} runId={route.runId} sourceId={route.sourceId} strategyId={route.strategyId} /> : route.goal ? <CustomerPlanner customerId={route.customerId} goalId={route.goalId} simulationId={route.simulationId} newSimulation={route.newSimulation} isin={route.isin} /> : <>
      {(route.add || route.isin || route.invalid) && <nav aria-label="Instrument navigation"><a href="#/">← ETFs</a></nav>}
      {route.invalid ? <p role="alert">ETF not found. <a href="#/add">Search for an ETF</a>.</p>
        : route.add ? <AddEtf onSelect={select} />
          : route.isin ? <InstrumentDetail key={route.isin} isin={route.isin} />
            : <AcquiredEtfs />}
    </>}
    </div>
  </div>;
}
