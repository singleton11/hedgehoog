import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import CustomerTabs from './CustomerTabs.jsx';
import { formatEuros as formatExactEuros } from './goalProjection.js';

const PAGE_SIZE = 20;
const CHOICE_PAGE_SIZE = 100;
const identifier = (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]+$/.test(value);
const text = (value) => typeof value === 'string' && value.trim().length > 0;
const decimal = (value) => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value);
const nullableDecimal = (value) => value === null || decimal(value);
const pnlClass = (value) => !decimal(value) || !/[1-9]/.test(value) ? '' : value.startsWith('-') ? 'return-negative' : 'return-positive';
const today = () => new Date().toISOString().slice(0, 10);
const customerPath = (cid) => `/customers/${encodeURIComponent(cid)}`;
const portfoliosPath = (cid) => `${customerPath(cid)}/portfolios`;
const portfolioPath = (cid, pid) => `${portfoliosPath(cid)}/${encodeURIComponent(pid)}`;
const customerRoute = (cid) => `#/goal/customers/${encodeURIComponent(cid)}`;
export const portfoliosRoute = (cid, pid) => `${customerRoute(cid)}/portfolios${pid ? `/${encodeURIComponent(pid)}` : ''}`;
const validGoal = (item) => identifier(item?.id) && text(item.name);
const validInstrument = (item) => typeof item?.instrument_id === 'string' && /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(item.instrument_id) && item.currency === 'EUR';
const validPortfolio = (item, cid) => identifier(item?.id) && item.customer_id === cid && text(item.name)
  && item.currency === 'EUR' && (item.goal_id === null || identifier(item.goal_id));
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

function formatEuros(value) {
  if (!decimal(value)) return 'Unavailable';
  const [whole, fraction = ''] = value.split('.');
  // Display only: retain every significant decimal without floating-point rounding.
  return formatExactEuros(`${whole}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`);
}
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
  return { busy, error, message, submit, fail: (error) => { setError(error); setMessage(null); } };
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
function GoalChoice({ goals, value, onChange, prefix }) {
  return <div><label htmlFor={`${prefix}-goal`}>Linked goal (optional)</label>
    <select id={`${prefix}-goal`} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">No goal</option>
      {value && !goals.data?.some((goal) => goal.id === value) && <option value={value}>Current goal ({value})</option>}
      {(goals.data || []).map((goal) => <option key={goal.id} value={goal.id}>{goal.name}</option>)}
    </select>
    <p className="muted">A goal is optional. Attach, change or detach it later without changing transactions.</p>
  </div>;
}
function MetadataForm({ request, customerId, portfolio, goals, onSaved }) {
  const editing = Boolean(portfolio);
  const [name, setName] = useState(portfolio?.name || '');
  const [goalId, setGoalId] = useState(portfolio?.goal_id || '');
  const mutation = useMutation(request);
  const prefix = editing ? 'portfolio-settings' : 'new-portfolio';
  async function save(event) {
    event.preventDefault();
    if (mutation.busy) return;
    if (!name.trim()) { mutation.fail('Enter a portfolio name.'); return; }
    await mutation.submit(editing ? portfolioPath(customerId, portfolio.id) : portfoliosPath(customerId), editing ? 'PATCH' : 'POST',
      { name: name.trim(), goal_id: goalId || null }, (data) => validPortfolio(data, customerId) && (!editing || data.id === portfolio.id),
      (data) => { if (!editing) { setName(''); setGoalId(''); } onSaved(data); }, editing ? 'Portfolio settings saved.' : 'Portfolio created.');
  }
  return <form className="goal-form planner-create" aria-label={editing ? 'Portfolio settings' : 'New portfolio'} onSubmit={save} noValidate>
    <h3>{editing ? 'Portfolio settings' : 'New portfolio'}</h3>
    <fieldset disabled={mutation.busy}>
      <div className="goal-fields"><div><label htmlFor={`${prefix}-name`}>Portfolio name</label>
        <input id={`${prefix}-name`} value={name} required maxLength={200} autoComplete="off" onChange={(event) => setName(event.target.value)} /></div>
        <GoalChoice goals={goals} value={goalId} onChange={setGoalId} prefix={prefix} />
      </div>
      <button type="submit">{mutation.busy ? 'Saving…' : editing ? 'Save portfolio settings' : 'Create portfolio'}</button>
    </fieldset>
    <ReadStatus resource={goals} label="goals" />
    <MutationStatus mutation={mutation} />
  </form>;
}
function PortfolioList({ request, customerId, goals }) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const resource = useRead(request, `${portfoliosPath(customerId)}?limit=${PAGE_SIZE}&offset=${offset}`, { revision });
  const valid = validPage(resource.data, offset, PAGE_SIZE, (item) => validPortfolio(item, customerId));
  return <>
    <p className="muted">Track separate EUR portfolios with manual cash movements and trades. Goals can be linked whenever you are ready.</p>
    <section className="planner-list" aria-label="Portfolios">
      <div className="section-heading"><h3>Portfolios</h3><button className="secondary" type="button" onClick={resource.retry}>Refresh portfolios</button></div>
      <ReadStatus resource={resource} label="portfolios" invalid={!valid} />
      {valid && <>{resource.data.total === 0 ? <p className="empty-state">No portfolios yet. Create your first portfolio below.</p>
        : <ul className="planner-cards">{resource.data.items.map((portfolio) => <li key={portfolio.id}>
          <a href={portfoliosRoute(customerId, portfolio.id)}>{portfolio.name}<span>EUR · {portfolio.goal_id ? goals.data?.find((goal) => goal.id === portfolio.goal_id)?.name || 'Linked goal' : 'No goal'}</span></a>
        </li>)}</ul>}
        {resource.data.total > 0 && <Pagination label="Portfolios" offset={offset} total={resource.data.total} count={resource.data.items.length} setOffset={setOffset} loading={resource.loading} />}
      </>}
    </section>
    <MetadataForm request={request} customerId={customerId} goals={goals} onSaved={() => { setOffset(0); setRevision((value) => value + 1); }} />
  </>;
}
function Summary({ data }) {
  const metrics = [['Cash', 'cash_eur'], ['Positions value', 'positions_value_eur'], ['Equity', 'equity_eur'], ['Realized P&L', 'realized_pnl_eur'], ['Total unrealized P&L', 'unrealized_pnl_eur'], ['Daily unrealized P&L', 'daily_unrealized_pnl_eur']];
  return <section aria-label="Portfolio summary"><h3>Portfolio summary</h3>
    <dl className="facts">{metrics.map(([label, key]) => <div key={key}><dt>{label}</dt><dd className={key.includes('pnl') ? pnlClass(data[key]) : undefined}>{formatEuros(data[key])}</dd></div>)}</dl>
    <p className="muted">Daily price comparison (UTC): {data.daily_from_date && data.daily_to_date ? `${data.daily_from_date} to ${data.daily_to_date}` : 'Unavailable'}.</p>
    <p className="muted">Weighted-average cost includes buy commissions; sell commissions reduce realized P&amp;L. Values are calculated by the server, not this browser.</p>
    {data.warnings.length > 0 && <ul className="portfolio-warnings" aria-label="Valuation warnings">{data.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </section>;
}
function Positions({ positions }) {
  return <section className="planner-list" aria-label="Positions"><h3>Positions</h3>
    {positions.length === 0 ? <p className="empty-state">No open positions.</p> : <>
      <p id="portfolio-positions-scroll" className="portfolio-scroll-hint">Scroll horizontally to view all columns.</p>
      <div className="planner-table-scroll" role="region" tabIndex={0} aria-label="Positions table" aria-describedby="portfolio-positions-scroll">
      <table className="planner-table portfolio-table"><caption>Current holdings · EUR</caption><thead><tr>
        {['Instrument', 'Quantity', 'Average cost', 'Cost basis', 'Market price / date', 'Position value', 'Total unrealized P&L', 'Daily unrealized P&L'].map((label) => <th key={label} scope="col">{label}</th>)}
      </tr></thead><tbody>{positions.map((position) => <tr key={position.instrument.instrument_id}>
        <th scope="row">{instrumentName(position.instrument)}<span className="simulation-parameter">{position.instrument.instrument_id}</span>{position.warning && <span className="portfolio-warning">{position.warning}</span>}</th>
        <td>{position.quantity}</td><td>{formatEuros(position.average_price_eur)}</td><td>{formatEuros(position.cost_basis_eur)}</td>
        <td>{formatEuros(position.market_price_eur)}<span className="simulation-parameter">{position.price_date || 'Date unavailable'}</span></td>
        <td>{formatEuros(position.position_value_eur)}</td><td className={pnlClass(position.unrealized_pnl_eur)}>{formatEuros(position.unrealized_pnl_eur)}</td>
        <td className={pnlClass(position.daily_unrealized_pnl_eur)}>{formatEuros(position.daily_unrealized_pnl_eur)}<span className="simulation-parameter">{position.previous_price_date && position.price_date ? `${position.previous_price_date} to ${position.price_date}` : 'Dates unavailable'}</span></td>
      </tr>)}</tbody></table>
    </div></>}
  </section>;
}
function TransactionForm({ request, path, cash = false, instruments, positions, onSaved, disabled }) {
  const [type, setType] = useState(cash ? 'deposit' : 'buy');
  const [date, setDate] = useState(today);
  const [instrumentId, setInstrumentId] = useState('');
  const [amount, setAmount] = useState('');
  const [quantity, setQuantity] = useState('');
  const [price, setPrice] = useState('');
  const [commission, setCommission] = useState('');
  const mutation = useMutation(request);
  // Retain IDs for failed/uncertain payloads, including a payload edited and then restored.
  const retries = useRef(new Map());
  const prefix = cash ? 'cash' : 'trade';
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
      () => { retries.current.clear(); setAmount(''); setQuantity(''); setPrice(''); setCommission(''); setDate(today()); onSaved(); }, `${transactionName(type)} saved.`);
  }
  return <form className="goal-form planner-create" aria-label={cash ? 'Cash movement' : 'Trade'} aria-describedby={`${prefix}-ledger-notice`} onSubmit={save} noValidate>
    <h3>{cash ? 'Cash movement' : 'Trade'}</h3>
    <fieldset disabled={disabled || mutation.busy}>
      <div className="goal-fields">
        <div><label htmlFor={`${prefix}-type`}>{cash ? 'Cash movement type' : 'Trade type'}</label><select id={`${prefix}-type`} value={type} onChange={(event) => setType(event.target.value)}>
          {(cash ? ['deposit', 'withdrawal'] : ['buy', 'sell']).map((value) => <option key={value} value={value}>{transactionName(value)}</option>)}
        </select></div>
        <div><label htmlFor={`${prefix}-date`}>{cash ? 'Cash date (UTC)' : 'Trade date (UTC)'}</label><input id={`${prefix}-date`} type="date" required max={today()} value={date} onChange={(event) => setDate(event.target.value)} /></div>
        {cash ? <div><label htmlFor="cash-amount">Amount (EUR)</label><input id="cash-amount" inputMode="decimal" required maxLength={100} value={amount} onChange={(event) => setAmount(event.target.value)} /></div> : <>
          <div><label htmlFor="trade-instrument">Trade instrument</label><select id="trade-instrument" value={choices.has(instrumentId) ? instrumentId : ''} onChange={(event) => setInstrumentId(event.target.value)} required>
            <option value="">Choose an instrument</option>{[...choices.values()].map((item) => <option key={item.instrument_id} value={item.instrument_id}>{instrumentName(item)}</option>)}
          </select></div>
          <div><label htmlFor="trade-quantity">Quantity</label><input id="trade-quantity" inputMode="decimal" required maxLength={100} value={quantity} onChange={(event) => setQuantity(event.target.value)} /></div>
          <div><label htmlFor="trade-price">Average execution price (EUR)</label><input id="trade-price" inputMode="decimal" required maxLength={100} value={price} onChange={(event) => setPrice(event.target.value)} /></div>
          <div><label htmlFor="trade-commission">Commission (EUR, optional)</label><input id="trade-commission" inputMode="decimal" maxLength={100} placeholder="0.00" value={commission} onChange={(event) => setCommission(event.target.value)} /></div>
        </>}
      </div>
      <p className="muted portfolio-ledger-notice" id={`${prefix}-ledger-notice`}>Transactions are append-only: saved entries cannot be edited or deleted. Dividends, splits and other corporate actions are not processed.</p>
      <button type="submit">{mutation.busy ? 'Saving…' : cash ? 'Save cash movement' : 'Save trade'}</button>
    </fieldset>
    {!cash && <><ReadStatus resource={instruments} label="instruments" />
      {!instruments.loading && !instruments.error && instruments.data?.length === 0 && <p className="muted">No acquired EUR instruments are available to buy. <a href="#/add">Add an ETF</a> or open an instrument in <a href="#/">your ETFs</a> to set its price currency to EUR. Existing holdings remain available to sell.</p>}
      <p className="muted">Long only and cash-funded. Deposit cash before buying. Historical transactions must keep cash and holdings non-negative throughout the ledger; the server validates this.</p></>}
    <MutationStatus mutation={mutation} />
  </form>;
}
function Transactions({ request, path, revision }) {
  const [offset, setOffset] = useState(0);
  const resource = useRead(request, `${path}?limit=${PAGE_SIZE}&offset=${offset}`, { revision });
  const valid = validPage(resource.data, offset, PAGE_SIZE, validTransaction);
  return <section className="planner-list" aria-label="Transaction history">
    <div className="section-heading"><h3>Transaction history</h3><button type="button" className="secondary" onClick={resource.retry}>Refresh transactions</button></div>
    <ReadStatus resource={resource} label="transactions" invalid={!valid} />
    {valid && <>{resource.data.total === 0 ? <p className="empty-state">No transactions yet. Record a deposit to fund your first purchase.</p> : <>
      <p id="portfolio-transactions-scroll" className="portfolio-scroll-hint">Scroll horizontally to view all columns.</p>
      <div className="planner-table-scroll" role="region" tabIndex={0} aria-label="Transactions table" aria-describedby="portfolio-transactions-scroll"><table className="planner-table portfolio-table">
        <caption>Transactions · newest date and sequence first</caption><thead><tr>{['Date (UTC)', 'Type', 'Instrument', 'Quantity', 'Execution price', 'Commission', 'Cash amount'].map((label) => <th key={label} scope="col">{label}</th>)}</tr></thead>
        <tbody>{resource.data.items.map((event) => <tr key={event.id}><th scope="row"><time dateTime={event.date}>{event.date}</time></th><td>{transactionName(event.type)}</td>
          <td className="portfolio-instrument">{event.instrument ? instrumentName(event.instrument) : '—'}</td><td>{event.quantity ?? '—'}</td><td>{event.price_eur === undefined ? '—' : formatEuros(event.price_eur)}</td>
          <td>{event.commission_eur === undefined ? '—' : formatEuros(event.commission_eur)}</td><td>{event.amount_eur === undefined ? '—' : formatEuros(event.amount_eur)}</td>
        </tr>)}</tbody>
      </table></div>
      <Pagination label="Transactions" offset={offset} total={resource.data.total} count={resource.data.items.length} setOffset={setOffset} loading={resource.loading} />
    </>}</>}
  </section>;
}
function PortfolioDetail({ request, customerId, portfolioId, goals }) {
  const [revision, setRevision] = useState(0);
  const path = portfolioPath(customerId, portfolioId);
  const resource = useRead(request, path, { revision });
  const instruments = useRead(request, '/portfolio-instruments', { all: true, validate: validInstrument, itemKey: 'instrument_id' });
  const valid = validDetail(resource.data, customerId, portfolioId);
  const refresh = () => setRevision((value) => value + 1);
  return <>
    <ReadStatus resource={resource} label="portfolio" invalid={!valid} />
    {valid && <>
      <div className="section-heading page-heading"><div><h2 className="instrument-name">{resource.data.portfolio.name}</h2>
        <p className="muted">EUR · {resource.data.portfolio.goal_id ? goals.data?.find((goal) => goal.id === resource.data.portfolio.goal_id)?.name || 'Linked goal' : 'No linked goal'}</p></div>
        <button className="secondary" type="button" onClick={refresh}>Refresh portfolio</button>
      </div>
      <Summary data={resource.data} />
      <Positions positions={resource.data.positions} />
      <div className="portfolio-forms">
        <TransactionForm request={request} path={`${path}/transactions`} cash onSaved={refresh} disabled={resource.loading || Boolean(resource.error)} />
        <TransactionForm request={request} path={`${path}/transactions`} instruments={instruments} positions={resource.data.positions} onSaved={refresh} disabled={resource.loading || Boolean(resource.error)} />
      </div>
      <Transactions request={request} path={`${path}/transactions`} revision={revision} />
      <MetadataForm request={request} customerId={customerId} portfolio={resource.data.portfolio} goals={goals} onSaved={refresh} />
    </>}
  </>;
}
function PortfolioPage({ request, customerId, portfolioId, isin }) {
  const customer = useRead(request, customerPath(customerId));
  const goals = useRead(request, `${customerPath(customerId)}/goals`, { all: true, validate: validGoal });
  const validCustomer = customer.data?.id === customerId && text(customer.data?.name);
  return <article className="customer-planner portfolio-tracker" aria-label="Portfolio tracker">
    <nav className="planner-breadcrumbs" aria-label="Portfolio navigation"><a href={`#/goal${isin ? `?instrument=${encodeURIComponent(isin)}` : ''}`}>← Customers</a>
      {portfolioId && <><a href={`${customerRoute(customerId)}/goals`}>{validCustomer ? `${customer.data.name} · Goals` : 'Customer goals'}</a>
        <a href={portfoliosRoute(customerId)}>Portfolios</a></>}
    </nav>
    <ReadStatus resource={customer} label="customer" invalid={!validCustomer} />
    {validCustomer && (portfolioId ? <PortfolioDetail request={request} customerId={customerId} portfolioId={portfolioId} goals={goals} /> : <>
      <CustomerTabs customer={customer.data} active="portfolios" isin={isin} />
      <PortfolioList request={request} customerId={customerId} goals={goals} />
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
