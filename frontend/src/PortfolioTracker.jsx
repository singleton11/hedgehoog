import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import CustomerTabs from './CustomerTabs.jsx';
import Icon from './Icon.jsx';
import DegiroImport from './DegiroImport.jsx';
import PortfolioPerformance from './PortfolioPerformance.jsx';
import PortfolioPlanTracking from './PortfolioPlanTracking.jsx';
import { formatEuros as formatExactEuros } from './goalProjection.js';

const PAGE_SIZE = 20;
const CHOICE_PAGE_SIZE = 100;
const identifier = (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]+$/.test(value);
const text = (value) => typeof value === 'string' && value.trim().length > 0;
const decimal = (value) => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value);
const nullableDecimal = (value) => value === null || decimal(value);
const money = (value) => typeof value === 'string' && /^-?\d+\.\d{2}$/.test(value);
const pnlColumns = [['Realized P&L (incl. fees, EUR)', 'realized_pnl_eur'], ['Unrealized P&L (EUR)', 'unrealized_pnl_eur'],
  ['Today P&L (EUR)', 'today_pnl_eur'], ['Total P&L (EUR)', 'total_pnl_eur']];
const pnlClass = (value) => !decimal(value) || !/[1-9]/.test(value) ? '' : value.startsWith('-') ? 'return-negative' : 'return-positive';
const today = () => new Date().toISOString().slice(0, 10);
const customerPath = (cid) => `/customers/${encodeURIComponent(cid)}`;
const portfoliosPath = (cid) => `${customerPath(cid)}/portfolios`;
const portfolioPath = (cid, pid) => `${portfoliosPath(cid)}/${encodeURIComponent(pid)}`;
const customerRoute = (cid) => `#/goal/customers/${encodeURIComponent(cid)}`;
export const portfoliosRoute = (cid, pid) => `${customerRoute(cid)}/portfolios${pid ? `/${encodeURIComponent(pid)}` : ''}`;
const validPlan = (item) => identifier(item?.id) && identifier(item.goal_id) && item.is_plan === true
  && text(item.strategy_name) && text(item.saved_at) && text(item.inputs?.goal_date);
const planName = (plan) => plan ? `${plan.strategy_name} · ${plan.inputs.goal_date} · ${plan.id}` : 'Linked plan';
const validInstrument = (item) => typeof item?.instrument_id === 'string' && /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(item.instrument_id) && item.currency === 'EUR';
const validPortfolio = (item, cid) => identifier(item?.id) && item.customer_id === cid && text(item.name)
  && item.currency === 'EUR' && (item.plan_id === null || identifier(item.plan_id));
const validPortfolioSummary = (item, cid) => validPortfolio(item, cid)
  && (item.net_worth_eur === null || (money(item.net_worth_eur) && !item.net_worth_eur.startsWith('-')))
  && money(item.realized_pnl_eur)
  && ['unrealized_pnl_eur', 'today_pnl_eur', 'total_pnl_eur'].every((key) => item[key] === null || money(item[key]))
  && ((item.today_from_date === null && item.today_to_date === null && [null, '0.00'].includes(item.today_pnl_eur))
    || (item.today_pnl_eur !== null && validDate(item.today_from_date) && validDate(item.today_to_date) && item.today_from_date < item.today_to_date));
const validPosition = (item) => validInstrument(item?.instrument) && ['quantity', 'average_price_eur', 'cost_basis_eur'].every((key) => decimal(item[key]))
  && ['market_price_eur', 'position_value_eur', 'unrealized_pnl_eur', 'daily_unrealized_pnl_eur'].every((key) => nullableDecimal(item[key]));
const validDetail = (data, cid, pid) => validPortfolio(data?.portfolio, cid) && data.portfolio.id === pid
  && ['cash_eur', 'realized_pnl_eur'].every((key) => decimal(data[key]))
  && ['positions_value_eur', 'equity_eur', 'unrealized_pnl_eur', 'daily_unrealized_pnl_eur'].every((key) => nullableDecimal(data[key]))
  && Array.isArray(data.positions) && data.positions.every(validPosition) && Array.isArray(data.warnings) && data.warnings.every((item) => typeof item === 'string');
const validTransaction = (item) => identifier(item?.id) && text(item.client_id) && validDate(item.date)
  && (['deposit', 'withdrawal'].includes(item.type) ? decimal(item.amount_eur)
    : ['buy', 'sell'].includes(item.type) && validInstrument(item.instrument) && ['quantity', 'price_eur', 'commission_eur'].every((key) => decimal(item[key])));
const instrumentName = (item) => [item.name || item.instrument_id, item.symbol, item.exchange].filter(Boolean).join(' · ');
const transactionName = (type) => ({ buy: 'Buy', sell: 'Sell', deposit: 'Deposit', withdrawal: 'Withdrawal' })[type];

const formatEuros = formatExactEuros;
const signedEuros = (value) => decimal(value) && !value.startsWith('-') && /[1-9]/.test(value) ? `+${formatEuros(value)}` : formatEuros(value);
const percentOf = (value, basis) => {
  const amount = Number(value), denominator = Number(basis);
  if (!Number.isFinite(amount) || !Number.isFinite(denominator) || denominator <= 0) return null;
  const percent = amount / denominator * 100;
  return `${percent > 0 ? '+' : ''}${percent.toFixed(2)}%`;
};
const pnlDisplay = (value, basis) => {
  const percent = decimal(value) && basis !== null ? percentOf(value, basis) : null;
  return `${signedEuros(value)}${percent === null ? '' : ` (${percent})`}`;
};
function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}
// Validate and normalize text without converting monetary values to floating point.
function inputDecimal(value, places, allowZero = false, fixed = false) {
  const raw = value.trim();
  if (!new RegExp(`^\\d+(?:\\.\\d{1,${places}})?$`).test(raw) || raw.length > 100 || (!allowZero && !/[1-9]/.test(raw))) return null;
  const [whole, fraction = ''] = raw.split('.');
  return `${whole.replace(/^0+(?=\d)/, '')}${fixed ? `.${fraction.padEnd(places, '0')}` : fraction ? `.${fraction}` : ''}`;
}
function validPage(data, offset, limit, validate, key = 'id') {
  return Array.isArray(data?.items) && Number.isInteger(data.total) && data.total >= 0 && data.offset === offset && data.limit === limit
    && data.items.length <= limit && data.items.length <= Math.max(0, data.total - offset)
    && (offset >= data.total || data.items.length > 0) && data.items.every(validate)
    && new Set(data.items.map((item) => item[key])).size === data.items.length;
}

function useRead(request, path, { revision = 0, all = false, validate, itemKey = 'id' } = {}) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState(null);
  useEffect(() => {
    const abort = new AbortController();
    async function load() {
      try {
        let data;
        if (all) {
          const items = [], seen = new Set();
          let offset = 0, total;
          do {
            const page = await request(`${path}?limit=${CHOICE_PAGE_SIZE}&offset=${offset}`, { signal: abort.signal });
            if (abort.signal.aborted) return;
            if (!validPage(page, offset, CHOICE_PAGE_SIZE, validate, itemKey) || (total !== undefined && page.total !== total)) throw new Error('The available choices changed or could not be loaded completely. Retry to load the full list.');
            total = page.total;
            for (const item of page.items) {
              if (seen.has(item[itemKey])) throw new Error('The available choices changed. Retry to load the full list.');
              seen.add(item[itemKey]); items.push(item);
            }
            offset += page.items.length;
          } while (offset < total);
          data = items;
        } else data = await request(path, { signal: abort.signal });
        if (!abort.signal.aborted) setState({ request, path, revision, attempt, data });
      } catch (error) {
        if (!abort.signal.aborted) setState((previous) => ({ request, path, revision, attempt,
          data: previous?.request === request && previous.path === path ? previous.data : null,
          error: error.message || 'Unable to load this page.' }));
      }
    }
    load();
    return () => abort.abort();
  }, [request, path, revision, attempt, all, validate, itemKey]);
  const same = state?.request === request && state.path === path;
  const current = same && state.revision === revision && state.attempt === attempt;
  return { data: same ? state.data : null, error: current ? state.error : null, loading: !current, retry: () => setAttempt((value) => value + 1) };
}
function ReadStatus({ resource, label, invalid = false }) {
  if (resource.loading) return <p role="status">{resource.data ? 'Refreshing' : 'Loading'} {label}…</p>;
  if (resource.error || invalid) return <div><p role="alert">{resource.error || `The backend returned unexpected ${label}.`}</p>
    <button type="button" className="secondary" onClick={resource.retry}>Retry {label}</button></div>;
  return null;
}
function useMutation(request) {
  const pending = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function submit(path, method, body, validate, onSaved, success) {
    if (pending.current) return;
    const abort = new AbortController();
    pending.current = abort;
    setBusy(true); setError(null); setMessage(null);
    try {
      const data = await request(path, { method, body, signal: abort.signal });
      if (abort.signal.aborted) return;
      if (!validate(data)) throw new Error('Unexpected response. Refresh to check whether the change was saved before retrying.');
      onSaved(data);
      setMessage(success);
    } catch (error) {
      if (!abort.signal.aborted) setError(error.message || 'Unable to save. Please retry.');
    } finally {
      if (!abort.signal.aborted) { pending.current = null; setBusy(false); }
    }
  }
  return { busy, error, message, submit, reset: () => { setError(null); setMessage(null); }, fail: (error) => { setError(error); setMessage(null); } };
}
function MutationStatus({ mutation }) {
  return <>{mutation.error && <p role="alert">{mutation.error}</p>}{mutation.message && <p role="status">{mutation.message}</p>}</>;
}
function Pagination({ label, offset, total, count, setOffset, loading }) {
  return <nav className="planner-pagination" aria-label={`${label} pagination`}>
    <button type="button" className="secondary" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
    <span aria-live="polite">{count ? offset + 1 : 0}–{offset + count} of {total}</span>
    <button type="button" className="secondary" disabled={loading || offset + count >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
  </nav>;
}
function PlanChoice({ plans, value, onChange, prefix, autoFocus = false }) {
  return <div><label htmlFor={`${prefix}-plan`}>Linked plan (optional)</label>
    <select id={`${prefix}-plan`} value={value} autoFocus={autoFocus} onChange={(event) => onChange(event.target.value)}>
      <option value="">No plan</option>
      {value && !plans.data?.some((plan) => plan.id === value) && <option value={value}>Current plan ({value})</option>}
      {(plans.data || []).map((plan) => <option key={plan.id} value={plan.id}>{planName(plan)}</option>)}
    </select>
    <p className="muted">A plan is optional. Mark a saved simulation as a plan first. Attach, change or detach it without changing transactions.</p>
  </div>;
}
function MetadataForm({ request, customerId, plans, onSaved, onCancel }) {
  const [name, setName] = useState('');
  const [planId, setPlanId] = useState('');
  const mutation = useMutation(request);
  const prefix = 'new-portfolio';
  async function save(event) {
    event.preventDefault();
    if (mutation.busy) return;
    if (!name.trim()) { mutation.fail('Enter a portfolio name.'); return; }
    await mutation.submit(portfoliosPath(customerId), 'POST',
      { name: name.trim(), plan_id: planId || null }, (data) => validPortfolio(data, customerId),
      (data) => { setName(''); setPlanId(''); onSaved(data); }, 'Portfolio created.');
  }
  return <form id={`${prefix}-form`} className="goal-form planner-create" aria-label="New portfolio" onSubmit={save} noValidate>
    <h3>New portfolio</h3>
    <fieldset disabled={mutation.busy}>
      <div className="goal-fields"><div><label htmlFor={`${prefix}-name`}>Portfolio name</label>
        <input id={`${prefix}-name`} value={name} required maxLength={200} autoComplete="off" autoFocus={Boolean(onCancel)} onChange={(event) => setName(event.target.value)} /></div>
        <PlanChoice plans={plans} value={planId} onChange={setPlanId} prefix={prefix} />
      </div>
      <div className="button-group">
        <button type="submit">{mutation.busy ? 'Saving…' : 'Create portfolio'}</button>
        {onCancel && <button type="button" className="secondary" onClick={onCancel}>Cancel</button>}
      </div>
    </fieldset>
    <ReadStatus resource={plans} label="plans" />
    <MutationStatus mutation={mutation} />
  </form>;
}
function PortfolioHeader({ request, customerId, portfolio, plans, onSaved, disabled }) {
  const [editor, setEditor] = useState(null);
  const [name, setName] = useState('');
  const [planId, setPlanId] = useState('');
  const renameButton = useRef(null), planButton = useRef(null), previousEditor = useRef(null);
  const mutation = useMutation(request);
  const blocked = disabled || mutation.busy;
  useEffect(() => {
    if (editor) previousEditor.current = editor;
    else if (previousEditor.current && !blocked) {
      (previousEditor.current === 'name' ? renameButton : planButton).current?.focus();
      previousEditor.current = null;
    }
  }, [editor, blocked]);
  function edit(field) {
    setName(portfolio.name); setPlanId(portfolio.plan_id || ''); mutation.reset(); setEditor(field);
  }
  async function save(event, unassign = false) {
    event.preventDefault();
    if (blocked) return;
    const nextName = editor === 'name' ? name.trim() : portfolio.name;
    if (!nextName) { mutation.fail('Enter a portfolio name.'); return; }
    const nextPlan = editor === 'plan' ? (unassign ? null : planId || null) : portfolio.plan_id;
    await mutation.submit(portfolioPath(customerId, portfolio.id), 'PATCH', { name: nextName, plan_id: nextPlan },
      (data) => validPortfolio(data, customerId) && data.id === portfolio.id,
      () => { setEditor(null); onSaved(); }, editor === 'name' ? 'Portfolio name saved.' : nextPlan ? 'Plan assigned.' : 'Plan unassigned.');
  }
  const cancel = () => { mutation.reset(); setEditor(null); };
  const cancelOnEscape = (event) => { if (event.key === 'Escape' && !mutation.busy) { event.preventDefault(); cancel(); } };
  return <header className="portfolio-header">
    <div className="section-heading page-heading">
      <div className="portfolio-identity">
        <div className="portfolio-title-row">
          {editor === 'name' ? <form className="portfolio-name-form" aria-label="Rename portfolio" onSubmit={save} onKeyDown={cancelOnEscape} noValidate>
            <label htmlFor="portfolio-name">Portfolio name</label>
            <fieldset disabled={mutation.busy}><input id="portfolio-name" value={name} required maxLength={200} autoComplete="off" autoFocus onChange={(event) => setName(event.target.value)} />
              <button type="submit" disabled={disabled}>{mutation.busy ? 'Saving…' : 'Save name'}</button>
              <button type="button" className="secondary" onClick={cancel}>Cancel</button>
            </fieldset>
          </form> : <>
            <h2 className="instrument-name">{portfolio.name}</h2>
            <button ref={renameButton} className="secondary portfolio-rename" type="button" aria-label="Edit portfolio name" title="Edit portfolio name" disabled={blocked || Boolean(editor)} onClick={() => edit('name')}><Icon name="edit" /></button>
          </>}
          <button ref={planButton} className="secondary" type="button" disabled={blocked || Boolean(editor)} aria-expanded={editor === 'plan'} aria-controls={editor === 'plan' ? 'portfolio-plan-form' : undefined} onClick={() => edit('plan')}>{portfolio.plan_id ? 'Change plan' : 'Assign plan'}</button>
        </div>
        <p className="muted">EUR · {portfolio.plan_id ? planName(plans.data?.find((plan) => plan.id === portfolio.plan_id)) : 'No linked plan'}</p>
      </div>
    </div>
    {editor === 'plan' && <form id="portfolio-plan-form" className="goal-form planner-create portfolio-plan-form" aria-label="Assign portfolio plan" onSubmit={save} onKeyDown={cancelOnEscape}>
      <h3>{portfolio.plan_id ? 'Change plan' : 'Assign plan'}</h3>
      <fieldset disabled={mutation.busy}>
        <div className="goal-fields"><PlanChoice plans={plans} value={planId} onChange={setPlanId} prefix="portfolio" autoFocus /></div>
        <div className="button-group"><button type="submit" disabled={disabled}>{mutation.busy ? 'Saving…' : 'Save plan'}</button>
          {portfolio.plan_id && <button type="button" className="secondary" disabled={disabled} onClick={(event) => save(event, true)}>Unassign plan</button>}
          <button type="button" className="secondary" onClick={cancel}>Cancel</button></div>
      </fieldset>
      <ReadStatus resource={plans} label="plans" />
    </form>}
    <MutationStatus mutation={mutation} />
  </header>;
}
function PortfolioStatus({ request, customerId, portfolio }) {
  const [status, setStatus] = useState(null);
  const path = portfolioPath(customerId, portfolio.id);
  useEffect(() => {
    if (!portfolio.plan_id) return;
    const abort = new AbortController();
    request(`${path}/plan-tracking`, { signal: abort.signal }).then((data) => {
      if (!abort.signal.aborted) setStatus(data?.plan_id === portfolio.plan_id && typeof data.on_track === 'boolean'
        ? (data.on_track ? 'On track' : 'Off track') : 'Unavailable');
    }).catch(() => { if (!abort.signal.aborted) setStatus('Unavailable'); });
    return () => abort.abort();
  }, [request, path, portfolio.plan_id]);
  return <span role="status">{!portfolio.plan_id ? 'No plan' : status || 'Checking…'}</span>;
}
function PortfolioList({ request, customerId, plans }) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  const addButton = useRef(null);
  const wasCreating = useRef(false);
  useEffect(() => {
    if (wasCreating.current && !creating) addButton.current?.focus();
    wasCreating.current = creating;
  }, [creating]);
  const resource = useRead(request, `${portfoliosPath(customerId)}?limit=${PAGE_SIZE}&offset=${offset}`, { revision });
  const valid = validPage(resource.data, offset, PAGE_SIZE, (item) => validPortfolioSummary(item, customerId));
  return <>
    <div className="section-heading page-heading portfolio-list-actions">
      <button ref={addButton} type="button" disabled={creating} aria-expanded={creating} aria-controls={creating ? 'new-portfolio-form' : undefined} onClick={() => setCreating(true)}>Add portfolio</button>
    </div>
    {creating && <MetadataForm request={request} customerId={customerId} plans={plans} onCancel={() => setCreating(false)}
      onSaved={() => { setOffset(0); setRevision((value) => value + 1); setCreating(false); }} />}
    <section className="planner-list" aria-label="Portfolios">
      <div className="section-heading"><h3>Portfolios</h3></div>
      <ReadStatus resource={resource} label="portfolios" invalid={!valid} />
      {!resource.loading && !resource.error && valid && <>{resource.data.total === 0 ? <p className="empty-state">No portfolios yet. Select Add portfolio to create your first portfolio.</p> : <>
        <p id="portfolio-summary-scroll" className="portfolio-scroll-hint">Scroll horizontally to view all columns.</p>
        <div className="planner-table-scroll" tabIndex={0} role="region" aria-label="Portfolios table" aria-describedby="portfolio-summary-scroll">
          <table className="planner-table planner-summary-table portfolios-table">
            <caption>Portfolio summary</caption>
            <thead><tr><th scope="col">Portfolio</th><th scope="col">Status</th><th scope="col">Net worth (EUR)</th>
              {pnlColumns.map(([label, key]) => <th key={key} scope="col">{label}</th>)}
            </tr></thead>
            <tbody>{resource.data.items.map((portfolio) => <tr key={portfolio.id}>
              <th scope="row"><a href={portfoliosRoute(customerId, portfolio.id)}>{portfolio.name}</a></th>
              <td><PortfolioStatus request={request} customerId={customerId} portfolio={portfolio} /></td>
              <td>{formatEuros(portfolio.net_worth_eur)}{portfolio.net_worth_eur === null && <span className="simulation-parameter">Some holdings cannot be valued.</span>}</td>
              {pnlColumns.map(([, key]) => <td key={key} title={key === 'today_pnl_eur' && portfolio.today_to_date ? `Price date: ${portfolio.today_to_date}` : undefined}>
                <span className={pnlClass(portfolio[key])}>{signedEuros(portfolio[key])}</span>
              </td>)}
            </tr>)}</tbody>
          </table>
        </div>
      </>}
        {resource.data.total > 0 && <Pagination label="Portfolios" offset={offset} total={resource.data.total} count={resource.data.items.length} setOffset={setOffset} loading={resource.loading} />}
      </>}
    </section>
  </>;
}
function Summary({ data, performance }) {
  const totalPnl = data.unrealized_pnl_eur === null ? null : (Number(data.realized_pnl_eur) + Number(data.unrealized_pnl_eur)).toFixed(2);
  const previousPortfolioValue = data.positions_value_eur === null || data.daily_unrealized_pnl_eur === null ? null : (Number(data.positions_value_eur) - Number(data.daily_unrealized_pnl_eur)).toFixed(2);
  const metrics = [['Cash', 'cash_eur'], ['Positions value', 'positions_value_eur'], ['Equity', 'equity_eur'], ['Realized P&L (incl. fees)', 'realized_pnl_eur'], ['Total unrealized P&L', 'unrealized_pnl_eur'], ['Daily unrealized P&L', 'daily_unrealized_pnl_eur'], ['Total P&L', 'total_pnl_eur']];
  const values = { ...data, total_pnl_eur: totalPnl };
  return <section aria-label="Portfolio summary"><h3>Portfolio summary</h3>
    <dl className="facts">{metrics.map(([label, key]) => <div key={key}><dt>{label}</dt><dd className={key.includes('pnl') ? pnlClass(values[key]) : undefined}>{key === 'unrealized_pnl_eur' ? pnlDisplay(values[key], data.positions_value_eur === null ? null : Number(data.positions_value_eur) - Number(values[key])) : key === 'daily_unrealized_pnl_eur' ? pnlDisplay(values[key], previousPortfolioValue) : key.includes('pnl') ? signedEuros(values[key]) : formatEuros(values[key])}</dd></div>)}</dl>
    <dl className="facts">{[['TWR', 'twr_percent'], ['Sharpe', 'sharpe'], ['Sortino', 'sortino']].map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{typeof performance?.[key] === 'string' && Number.isFinite(Number(performance[key])) ? `${Number(performance[key]).toFixed(2)}${key === 'twr_percent' ? '%' : ''}` : 'Unavailable'}</dd></div>)}</dl>
    {data.warnings.length > 0 && <ul className="portfolio-warnings" aria-label="Valuation warnings">{data.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </section>;
}
function Positions({ positions }) {
  return <section className="planner-list" aria-label="Positions"><h3>Positions</h3>
    {positions.length === 0 ? <p className="empty-state">No open positions.</p> : <>
      <p id="portfolio-positions-scroll" className="portfolio-scroll-hint">Scroll horizontally to view all columns.</p>
      <div className="planner-table-scroll" role="region" tabIndex={0} aria-label="Positions table" aria-describedby="portfolio-positions-scroll">
      <table className="planner-table portfolio-table"><caption>Current holdings · EUR</caption><thead><tr>
        {['Instrument', 'Quantity', 'Average cost', 'Cost basis', 'Market price', 'Position value', 'Total unrealized P&L', 'Daily unrealized P&L'].map((label) => <th key={label} scope="col">{label}</th>)}
      </tr></thead><tbody>{positions.map((position) => <tr key={position.instrument.instrument_id}>
        <th scope="row">{instrumentName(position.instrument)}<span className="simulation-parameter">{position.instrument.instrument_id}</span>{position.warning && <span className="portfolio-warning">{position.warning}</span>}</th>
        <td>{position.quantity}</td><td>{formatEuros(position.average_price_eur)}</td><td>{formatEuros(position.cost_basis_eur)}</td>
        <td title={position.price_date ? `Price date: ${position.price_date}` : 'Price date unavailable'}>{formatEuros(position.market_price_eur)}</td>
        <td>{formatEuros(position.position_value_eur)}</td><td className={pnlClass(position.unrealized_pnl_eur)}>{pnlDisplay(position.unrealized_pnl_eur, Number(position.cost_basis_eur))}</td>
        <td className={pnlClass(position.daily_unrealized_pnl_eur)} title={position.price_date ? `Price date: ${position.price_date}` : 'Price date unavailable'}>{pnlDisplay(position.daily_unrealized_pnl_eur, position.position_value_eur === null || position.daily_unrealized_pnl_eur === null ? null : Number(position.position_value_eur) - Number(position.daily_unrealized_pnl_eur))}</td>
      </tr>)}</tbody></table>
    </div></>}
  </section>;
}
function TransactionForm({ request, path, open, onClose, instruments, positions, onSaved, disabled }) {
  const [type, setType] = useState('deposit');
  const cash = type === 'deposit' || type === 'withdrawal';
  const [cashDate, setCashDate] = useState(today), [tradeDate, setTradeDate] = useState(today);
  const date = cash ? cashDate : tradeDate, setDate = cash ? setCashDate : setTradeDate;
  const typeInput = useRef(null);
  useEffect(() => { if (open) typeInput.current?.focus(); }, [open]);
  const [instrumentId, setInstrumentId] = useState('');
  const [amount, setAmount] = useState('');
  const [quantity, setQuantity] = useState('');
  const [price, setPrice] = useState('');
  const [commission, setCommission] = useState('');
  const mutation = useMutation(request);
  // Retain IDs for failed/uncertain payloads, including a payload edited and then restored.
  const retries = useRef(new Map());
  const prefix = 'transaction';
  const choices = new Map((instruments?.data || []).map((item) => [item.instrument_id, item]));
  if (type === 'sell') for (const position of positions) if (!choices.has(position.instrument.instrument_id)) choices.set(position.instrument.instrument_id, position.instrument);
  async function save(event) {
    event.preventDefault();
    if (disabled || mutation.busy) return;
    if (!validDate(date) || date > today()) { mutation.fail('Enter a valid transaction date on or before today (UTC).'); return; }
    let body;
    if (cash) {
      const normalized = inputDecimal(amount, 2, false, true);
      if (!normalized) { mutation.fail('Enter a positive cash amount with at most 2 decimal places.'); return; }
      body = { date, type, amount_eur: normalized, currency: 'EUR' };
    } else {
      if (!choices.has(instrumentId)) { mutation.fail('Choose an available EUR instrument. Held instruments can also be sold.'); return; }
      const qty = inputDecimal(quantity, 8), execution = inputDecimal(price, 8);
      const fee = commission.trim() === '' ? '0.00' : inputDecimal(commission, 2, true, true);
      if (!qty || !execution) { mutation.fail('Enter a positive quantity and execution price with at most 8 decimal places.'); return; }
      if (fee === null) { mutation.fail('Enter a non-negative commission with at most 2 decimal places, or leave it blank.'); return; }
      body = { date, type, instrument_id: instrumentId, quantity: qty, price_eur: execution, commission_eur: fee };
    }
    const signature = JSON.stringify(body);
    try {
      if (!retries.current.has(signature)) retries.current.set(signature, crypto.randomUUID());
    } catch { mutation.fail('Secure transaction IDs are unavailable. Open this app over HTTPS or localhost and retry.'); return; }
    body = { client_id: retries.current.get(signature), ...body };
    await mutation.submit(path, 'POST', body, (data) => validTransaction(data) && data.client_id === body.client_id && data.type === body.type,
      () => {
        retries.current.delete(signature);
        if (cash) setAmount('');
        else { setQuantity(''); setPrice(''); setCommission(''); }
        setDate(today()); onSaved(`${transactionName(type)} saved.`);
      }, null);
  }
  // Hide without unmounting so drafts and uncertain-save retry IDs survive cancellation.
  return <div id="transaction-entry" hidden={!open}>
    <form className="goal-form planner-create portfolio-transaction-form" aria-label="Add transaction" aria-describedby={`${prefix}-ledger-notice`} onSubmit={save}
      onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); if (!mutation.busy) onClose(); } }} noValidate>
    <h3>Add transaction</h3>
    <p className="muted">Choose a transaction type. Cancel keeps your draft while you stay on this portfolio.</p>
    <fieldset disabled={disabled || mutation.busy}>
      <div className="goal-fields">
        <div><label htmlFor={`${prefix}-type`}>Transaction type</label><select ref={typeInput} id={`${prefix}-type`} value={type} onChange={(event) => { setType(event.target.value); mutation.reset(); }}>
          {['deposit', 'withdrawal', 'buy', 'sell'].map((value) => <option key={value} value={value}>{transactionName(value)}</option>)}
        </select></div>
        <div><label htmlFor={`${prefix}-date`}>Transaction date (UTC)</label><input id={`${prefix}-date`} type="date" required max={today()} value={date} onChange={(event) => setDate(event.target.value)} /></div>
        {cash ? <div><label htmlFor="cash-amount">Amount (EUR)</label><input id="cash-amount" inputMode="decimal" required maxLength={100} value={amount} onChange={(event) => setAmount(event.target.value)} /></div> : <>
          <div><label htmlFor="trade-instrument">Trade instrument</label><select id="trade-instrument" value={choices.has(instrumentId) ? instrumentId : ''} onChange={(event) => setInstrumentId(event.target.value)} required>
            <option value="">Choose an instrument</option>{[...choices.values()].map((item) => <option key={item.instrument_id} value={item.instrument_id}>{instrumentName(item)}</option>)}
          </select></div>
          <div><label htmlFor="trade-quantity">Quantity</label><input id="trade-quantity" inputMode="decimal" required maxLength={100} value={quantity} onChange={(event) => setQuantity(event.target.value)} /></div>
          <div><label htmlFor="trade-price">Average execution price (EUR)</label><input id="trade-price" inputMode="decimal" required maxLength={100} value={price} onChange={(event) => setPrice(event.target.value)} /></div>
          <div><label htmlFor="trade-commission">Commission (EUR, optional)</label><input id="trade-commission" inputMode="decimal" maxLength={100} placeholder="0.00" value={commission} onChange={(event) => setCommission(event.target.value)} /></div>
        </>}
      </div>
      <p className="muted portfolio-ledger-notice" id={`${prefix}-ledger-notice`}>Saved entries cannot be edited, but can be removed from transaction history if the remaining cash and holdings stay non-negative. Dividends, splits and other corporate actions are not processed.</p>
    </fieldset>
    <div className="button-group"><button type="submit" disabled={disabled || mutation.busy}>{mutation.busy ? 'Saving…' : 'Save transaction'}</button>
      <button type="button" className="secondary" disabled={mutation.busy} onClick={onClose}>Cancel</button></div>
    {!cash && <><ReadStatus resource={instruments} label="instruments" />
      {!instruments.loading && !instruments.error && instruments.data?.length === 0 && <p className="muted">No acquired EUR instruments are available to buy. <a href="#/add">Add an ETF</a> or open an instrument in <a href="#/">your ETFs</a> to set its price currency to EUR. Existing holdings remain available to sell.</p>}
      <p className="muted">Long only and cash-funded. Deposit cash before buying. Historical transactions must keep cash and holdings non-negative throughout the ledger; the server validates this.</p></>}
    <MutationStatus mutation={mutation} />
    </form>
  </div>;
}
function Transactions({ request, path, revision, onRemoved }) {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(null);
  const confirmationHeading = useRef(null);
  const historyHeading = useRef(null);
  const removeButton = useRef(null);
  const mutation = useMutation(request);
  const resource = useRead(request, `${path}?limit=${PAGE_SIZE}&offset=${offset}`, { revision });
  const valid = validPage(resource.data, offset, PAGE_SIZE, validTransaction);
  const blocked = mutation.busy || resource.loading || Boolean(resource.error) || !valid;
  useEffect(() => { if (selected) confirmationHeading.current?.focus(); }, [selected]);
  useEffect(() => {
    if (valid && !resource.loading && !resource.error && offset > 0 && offset >= resource.data.total) {
      setOffset(Math.max(0, Math.ceil(resource.data.total / PAGE_SIZE) - 1) * PAGE_SIZE);
    }
  }, [valid, resource.loading, resource.error, resource.data?.total, offset]);
  async function remove(event) {
    event.preventDefault();
    if (blocked || !selected) return;
    await mutation.submit(`${path}/${encodeURIComponent(selected.id)}`, 'DELETE', undefined,
      (data) => data?.id === selected.id && data.removed === true,
      () => { setSelected(null); historyHeading.current?.focus(); onRemoved(); }, 'Transaction removed.');
  }
  return <section className="planner-list" aria-label="Transaction history">
    <div className="section-heading"><h3 ref={historyHeading} tabIndex={-1}>Transaction history</h3></div>
    <ReadStatus resource={resource} label="transactions" invalid={!valid} />
    {selected && <form className="goal-form planner-create" aria-label="Remove transaction" onSubmit={remove}>
      <h4 ref={confirmationHeading} tabIndex={-1}>Remove transaction?</h4>
      <p>{transactionName(selected.type)} · {selected.date} · {selected.instrument
        ? `${instrumentName(selected.instrument)} · ${selected.quantity} at ${formatEuros(selected.price_eur)}`
        : formatEuros(selected.amount_eur)}</p>
      <p>This removes the entry from history and recalculates balances, holdings and P&amp;L. It cannot be undone. Removal is blocked if any remaining transaction would leave negative cash or holdings.</p>
      <div className="button-group"><button type="submit" disabled={blocked}>{mutation.busy ? 'Removing…' : 'Confirm removal'}</button>
        <button type="button" className="secondary" disabled={mutation.busy} onClick={() => { setSelected(null); removeButton.current?.focus(); }}>Cancel</button></div>
    </form>}
    <MutationStatus mutation={mutation} />
    {valid && <>{resource.data.total === 0 ? <p className="empty-state">No transactions yet. Record a deposit to fund your first purchase.</p> : <>
      <p id="portfolio-transactions-scroll" className="portfolio-scroll-hint">Scroll horizontally to view all columns.</p>
      <div className="planner-table-scroll" role="region" tabIndex={0} aria-label="Transactions table" aria-describedby="portfolio-transactions-scroll"><table className="planner-table portfolio-table">
        <caption>Transactions · newest date and sequence first</caption><thead><tr>{['Date (UTC)', 'Type', 'Instrument', 'Quantity', 'Execution price', 'Commission', 'Cash amount', 'Actions'].map((label) => <th key={label} scope="col">{label}</th>)}</tr></thead>
        <tbody>{resource.data.items.map((event) => <tr key={event.id}><th scope="row"><time dateTime={event.date}>{event.date}</time></th><td>{transactionName(event.type)}</td>
          <td className="portfolio-instrument">{event.instrument ? instrumentName(event.instrument) : '—'}</td><td>{event.quantity ?? '—'}</td><td>{event.price_eur === undefined ? '—' : formatEuros(event.price_eur)}</td>
          <td>{event.commission_eur === undefined ? '—' : formatEuros(event.commission_eur)}</td><td>{event.amount_eur === undefined ? '—' : formatEuros(event.amount_eur)}</td>
          <td><button type="button" className="secondary" disabled={blocked} aria-label={`Remove ${transactionName(event.type).toLowerCase()} on ${event.date}`} onClick={(click) => { removeButton.current = click.currentTarget; setSelected(event); }}>Remove</button></td>
        </tr>)}</tbody>
      </table></div>
      <Pagination label="Transactions" offset={offset} total={resource.data.total} count={resource.data.items.length} setOffset={setOffset} loading={resource.loading || mutation.busy} />
    </>}</>}
  </section>;
}
function PortfolioDetail({ request, customerId, portfolioId, plans }) {
  const [revision, setRevision] = useState(0);
  const [tab, setTab] = useState('positions');
  const [performance, setPerformance] = useState(null);
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState(null);
  const addButton = useRef(null), wasAdding = useRef(false);
  useEffect(() => {
    if (wasAdding.current && !adding) addButton.current?.focus();
    wasAdding.current = adding;
  }, [adding]);
  function tabKey(event) {
    const tabs = ['positions', 'transactions', 'performance'];
    let next;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') next = tabs[(tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    if (event.key === 'Home') next = tabs[0];
    if (event.key === 'End') next = tabs.at(-1);
    if (next) { event.preventDefault(); setTab(next); document.getElementById(`portfolio-${next}-tab`)?.focus(); }
  }
  const path = portfolioPath(customerId, portfolioId);
  const resource = useRead(request, path, { revision });
  const instruments = useRead(request, '/portfolio-instruments', { all: true, validate: validInstrument, itemKey: 'instrument_id' });
  const valid = validDetail(resource.data, customerId, portfolioId);
  const refresh = () => setRevision((value) => value + 1);
  return <>
    <ReadStatus resource={resource} label="portfolio" invalid={!valid} />
    {valid && <>
      <PortfolioHeader request={request} customerId={customerId} portfolio={resource.data.portfolio} plans={plans} onSaved={refresh} disabled={resource.loading || Boolean(resource.error)} />
      <Summary data={resource.data} performance={performance?.request === request && performance.portfolioPath === path && performance.revision === revision ? performance.summary : null} />
      {!resource.loading && !resource.error && <PortfolioPlanTracking request={request} portfolioPath={path} planId={resource.data.portfolio.plan_id} revision={revision} />}
      <div className="portfolio-section-controls">
        <div className="portfolio-tabs" role="tablist" aria-label="Portfolio sections" onKeyDown={tabKey}>
          {['positions', 'transactions', 'performance'].map((value) => <button key={value} id={`portfolio-${value}-tab`} type="button" role="tab" aria-selected={tab === value} aria-controls={`portfolio-${value}-panel`} tabIndex={tab === value ? 0 : -1} onClick={() => setTab(value)}>{value === 'positions' ? 'Positions' : value === 'transactions' ? 'Transactions' : 'Performance'}</button>)}
        </div>
        <button ref={addButton} type="button" disabled={adding} aria-expanded={adding} aria-controls="transaction-entry" onClick={() => { setAdding(true); setMessage(null); }}>Add transaction</button>
      </div>
      {message && <p role="status">{message}</p>}
      <TransactionForm request={request} path={`${path}/transactions`} open={adding} onClose={() => setAdding(false)} instruments={instruments} positions={resource.data.positions}
        onSaved={(success) => { setMessage(success); setAdding(false); refresh(); }} disabled={resource.loading || Boolean(resource.error)} />
      <div id="portfolio-performance-panel" role="tabpanel" aria-labelledby="portfolio-performance-tab" tabIndex={0} hidden={tab !== 'performance'}>
        <PortfolioPerformance request={request} portfolioPath={path} portfolioId={portfolioId} revision={revision} onSummary={setPerformance} />
      </div>
      <div id="portfolio-positions-panel" role="tabpanel" aria-labelledby="portfolio-positions-tab" tabIndex={0} hidden={tab !== 'positions'}><Positions positions={resource.data.positions} /></div>
      <div id="portfolio-transactions-panel" role="tabpanel" aria-labelledby="portfolio-transactions-tab" tabIndex={0} hidden={tab !== 'transactions'}>
        <DegiroImport request={request} portfolioPath={path} onImported={refresh} disabled={resource.loading || Boolean(resource.error)} />
        <Transactions request={request} path={`${path}/transactions`} revision={revision} onRemoved={refresh} />
      </div>
    </>}
  </>;
}
function PortfolioPage({ request, customerId, portfolioId, isin }) {
  const customer = useRead(request, customerPath(customerId));
  const plans = useRead(request, `${customerPath(customerId)}/plans`, { all: true, validate: validPlan });
  const validCustomer = customer.data?.id === customerId && text(customer.data?.name);
  return <article className="customer-planner portfolio-tracker" aria-label="Portfolio tracker">
    <nav className="planner-breadcrumbs" aria-label="Portfolio navigation"><a href={`#/goal${isin ? `?instrument=${encodeURIComponent(isin)}` : ''}`}>← Customers</a>
      {portfolioId && <><a href={`${customerRoute(customerId)}/goals`}>{validCustomer ? `${customer.data.name} · Goals` : 'Customer goals'}</a>
        <a href={portfoliosRoute(customerId)}>Portfolios</a></>}
    </nav>
    <ReadStatus resource={customer} label="customer" invalid={!validCustomer} />
    {validCustomer && (portfolioId ? <PortfolioDetail request={request} customerId={customerId} portfolioId={portfolioId} plans={plans} /> : <>
      <CustomerTabs customer={customer.data} active="portfolios" isin={isin} />
      <PortfolioList request={request} customerId={customerId} plans={plans} />
    </>)}
  </article>;
}
export default function PortfolioTracker({ customerId, portfolioId, isin }) {
  const { request, user, status } = useAuth();
  const [identity, setIdentity] = useState({ request, version: 0 });
  if (identity.request !== request) setIdentity({ request, version: identity.version + 1 });
  const key = JSON.stringify([customerId, portfolioId, isin, user?.id, status, identity.version]);
  // Remount the entire route on auth/request changes; cleanup aborts reads and writes.
  if (status !== 'authenticated') return <p role="status">Sign in to view portfolios.</p>;
  if (!identifier(customerId) || (portfolioId && !identifier(portfolioId))) return <p role="alert">Portfolio not found.</p>;
  return <PortfolioPage key={key} request={request} customerId={customerId} portfolioId={portfolioId} isin={isin} />;
}
