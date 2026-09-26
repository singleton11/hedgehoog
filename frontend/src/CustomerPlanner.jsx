import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import CustomerTabs from './CustomerTabs.jsx';
import GoalPlanner, { monthlyAnniversary, normalizeEuros, SimulationResult, validSimulationRecord } from './GoalPlanner.jsx';
import { formatEuros } from './goalProjection.js';
import { percent } from './ReturnStatistics.jsx';

const PAGE_SIZE = 20;
const id = (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]+$/.test(value);
const text = (value) => typeof value === 'string' && value.trim().length > 0;
const money = (value) => typeof value === 'string' && /^\d+\.\d{2}$/.test(value);
const validCustomer = (value) => id(value?.id) && text(value.name);
const pnlMoney = (value) => typeof value === 'string' && /^-?\d+\.\d{2}$/.test(value);
const pnlClass = (value) => !pnlMoney(value) || !/[1-9]/.test(value) ? '' : value.startsWith('-') ? 'return-negative' : 'return-positive';
const pnlColumns = [['Realized P&L (incl. fees, EUR)', 'realized_pnl_eur'], ['Unrealized P&L (EUR)', 'unrealized_pnl_eur'],
  ['Today P&L (EUR)', 'today_pnl_eur'], ['Total P&L (EUR)', 'total_pnl_eur']];
const validDate = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
const validCustomerSummary = (value) => validCustomer(value) && (value.net_worth_eur === null || money(value.net_worth_eur))
  && Number.isSafeInteger(value.goal_count) && value.goal_count >= 0 && pnlMoney(value.realized_pnl_eur)
  && ['unrealized_pnl_eur', 'today_pnl_eur', 'total_pnl_eur'].every((key) => value[key] === null || pnlMoney(value[key]))
  && ((value.today_from_date === null && value.today_to_date === null && [null, '0.00'].includes(value.today_pnl_eur))
    || (value.today_pnl_eur !== null && validDate(value.today_from_date) && validDate(value.today_to_date) && value.today_from_date < value.today_to_date));
const validGoal = (value, customerId) => validCustomer(value) && value.customer_id === customerId && money(value.goal_eur)
  && validDate(value.goal_date);
const customerPath = (customerId) => `/customers/${encodeURIComponent(customerId)}`;
const goalPath = (customerId, goalId) => `${customerPath(customerId)}/goals/${encodeURIComponent(goalId)}`;
export const customerRoute = (customerId) => `#/goal/customers/${encodeURIComponent(customerId)}`;
export const goalRoute = (customerId, goalId) => `${customerRoute(customerId)}/goals/${encodeURIComponent(goalId)}`;
export const simulationRoute = (customerId, goalId, simulationId) => `${goalRoute(customerId, goalId)}/simulations/${encodeURIComponent(simulationId)}`;
const prefill = (isin) => isin ? `?instrument=${encodeURIComponent(isin)}` : '';

function useRead(request, path) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState(null);
  useEffect(() => {
    if (!path) return;
    const abort = new AbortController();
    request(path, { signal: abort.signal }).then((data) => {
      if (!abort.signal.aborted) setState({ request, path, attempt, data });
    }).catch((error) => {
      if (!abort.signal.aborted) setState({ request, path, attempt, error: error.message || 'Unable to load this page.' });
    });
    return () => abort.abort();
  }, [request, path, attempt]);
  const current = state?.request === request && state.path === path && state.attempt === attempt;
  return { data: current ? state.data : null, error: current ? state.error : null, loading: !current,
    retry: () => setAttempt((value) => value + 1),
    update: (data) => setState({ request, path, attempt, data }) };
}
function LoadState({ resource, valid = true, children }) {
  if (resource.loading) return <p role="status">Loading…</p>;
  if (resource.error || !valid) return <><p role="alert">{resource.error || 'The backend returned an unexpected response.'}</p>
    <button className="secondary" type="button" onClick={resource.retry}>Retry</button></>;
  return children;
}
function validPage(data, offset, validate) {
  return Array.isArray(data?.items) && Number.isInteger(data.total) && data.total >= 0 && data.limit === PAGE_SIZE && data.offset === offset
    && data.items.length <= PAGE_SIZE && data.items.length <= Math.max(0, data.total - offset)
    && (offset >= data.total || data.items.length > 0) && data.items.every(validate)
    && new Set(data.items.map((item) => item.id)).size === data.items.length;
}
function PagedList({ request, path, label, validate, children, empty }) {
  const [offset, setOffset] = useState(0);
  const resource = useRead(request, `${path}?limit=${PAGE_SIZE}&offset=${offset}`);
  // A successful creation changes the component key in the parent, starting at page one.
  return <section aria-label={label} className="planner-list">
    <div className="section-heading"><h3>{label}</h3><button type="button" className="secondary" onClick={resource.retry}>Refresh {label.toLowerCase()}</button></div>
    <LoadState resource={resource} valid={validPage(resource.data, offset, validate)}>
      {resource.data?.total === 0 ? <p className="empty-state">{empty}</p> : <>
        {resource.data && children(resource.data.items)}
        <nav aria-label={`${label} pagination`} className="planner-pagination">
          <button type="button" className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
          <span aria-live="polite">{resource.data?.items.length ? offset + 1 : 0}–{offset + (resource.data?.items.length || 0)} of {resource.data?.total}</span>
          <button type="button" className="secondary" disabled={offset + PAGE_SIZE >= (resource.data?.total || 0)} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
        </nav>
      </>}
    </LoadState>
  </section>;
}
function CreateForm({ request, customerId, onCreated, onCancel }) {
  const [name, setName] = useState('');
  const [amount, setAmount] = useState('100000');
  const [date, setDate] = useState(() => monthlyAnniversary(new Date(), 120));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const pending = useRef(null);
  useEffect(() => () => pending.current?.abort(), []);
  const kind = customerId ? 'goal' : 'customer';
  async function create(event) {
    event.preventDefault();
    if (pending.current) return;
    setError(null);
    if (!name.trim()) { setError(`Enter a ${kind} name.`); return; }
    const goalAmount = normalizeEuros(amount);
    if (customerId && (!goalAmount || goalAmount === '0.00' || !date)) { setError('Enter a positive target amount and a goal date.'); return; }
    const abort = new AbortController();
    pending.current = abort;
    setBusy(true);
    try {
      const data = await request(customerId ? `${customerPath(customerId)}/goals` : '/customers', {
        method: 'POST', signal: abort.signal, body: { name: name.trim(), ...(customerId ? { goal_eur: goalAmount, goal_date: date } : {}) },
      });
      if (abort.signal.aborted) return;
      if (!(customerId ? validGoal(data, customerId) : validCustomer(data))) throw new Error('Unexpected response. Refresh the list before trying again.');
      setName('');
      onCreated(data);
    } catch (error) {
      if (!abort.signal.aborted) setError(error.message || `Could not create ${kind}. Refresh the list before retrying.`);
    } finally {
      if (!abort.signal.aborted) { pending.current = null; setBusy(false); }
    }
  }
  return <form id={`new-${kind}-form`} className="goal-form planner-create" aria-label={`New ${kind}`} onSubmit={create}>
    <h3>New {kind}</h3>
    <fieldset disabled={busy}>
      <div className="goal-fields">
        <div><label htmlFor={`${kind}-name`}>{customerId ? 'Goal name' : 'Customer name'}</label>
          <input id={`${kind}-name`} value={name} maxLength={200} required onChange={(event) => setName(event.target.value)} autoComplete="off" autoFocus={Boolean(onCancel)} /></div>
        {customerId && <>
          <div><label htmlFor="new-goal-amount">Target (EUR)</label><input id="new-goal-amount" inputMode="decimal" value={amount} required maxLength={100} onChange={(event) => setAmount(event.target.value)} /></div>
          <div><label htmlFor="new-goal-date">Goal date (UTC)</label><input id="new-goal-date" type="date" required value={date}
            min={monthlyAnniversary(new Date(), 1)} max={monthlyAnniversary(new Date(), 600)} onChange={(event) => setDate(event.target.value)} /></div>
        </>}
      </div>
      <div className="button-group">
        <button type="submit">{busy ? 'Creating…' : `Create ${kind}`}</button>
        {onCancel && <button type="button" className="secondary" onClick={onCancel}>Cancel</button>}
      </div>
    </fieldset>
    {error && <p role="alert">{error}</p>}
  </form>;
}
function EditGoalForm({ request, goal, onSaved, onCancel }) {
  const [name, setName] = useState(goal.name);
  const [amount, setAmount] = useState(goal.goal_eur);
  const [date, setDate] = useState(goal.goal_date);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const pending = useRef(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function save(event) {
    event.preventDefault();
    if (pending.current) return;
    setError(null);
    if (!name.trim()) { setError('Enter a goal name.'); return; }
    const goalAmount = normalizeEuros(amount);
    if (!goalAmount || goalAmount === '0.00' || !date) { setError('Enter a positive target amount and a goal date.'); return; }
    // Send only changed fields: renaming must not overwrite another edit or
    // force an unchanged, older goal date into today's simulation horizon.
    const body = {};
    if (name.trim() !== goal.name) body.name = name.trim();
    if (goalAmount !== goal.goal_eur) body.goal_eur = goalAmount;
    if (date !== goal.goal_date) body.goal_date = date;
    if (!Object.keys(body).length) { onCancel(); return; }
    const abort = new AbortController();
    pending.current = abort;
    setBusy(true);
    try {
      const data = await request(goalPath(goal.customer_id, goal.id), { method: 'PATCH', signal: abort.signal, body });
      if (abort.signal.aborted) return;
      if (!validGoal(data, goal.customer_id) || data.id !== goal.id
        || Object.entries(body).some(([key, value]) => data[key] !== value)) {
        throw new Error('Unexpected update response. Reload the goal to check whether your changes were saved.');
      }
      onSaved(data);
    } catch (error) {
      if (!abort.signal.aborted) setError(error.message || 'Could not update goal. Please retry.');
    } finally {
      if (!abort.signal.aborted) { pending.current = null; setBusy(false); }
    }
  }
  return <form className="goal-form planner-create" aria-label="Edit goal" onSubmit={save}>
    <h3>Edit goal</h3>
    <p className="muted">Changes apply to new simulations. Saved simulations keep their original target and date.</p>
    <fieldset disabled={busy}>
      <div className="goal-fields">
        <div><label htmlFor="edit-goal-name">Goal name</label><input id="edit-goal-name" value={name} maxLength={200} required
          onChange={(event) => setName(event.target.value)} autoComplete="off" autoFocus /></div>
        <div><label htmlFor="edit-goal-amount">Target (EUR)</label><input id="edit-goal-amount" inputMode="decimal" value={amount} required maxLength={100}
          onChange={(event) => setAmount(event.target.value)} /></div>
        <div><label htmlFor="edit-goal-date">Goal date (UTC)</label><input id="edit-goal-date" type="date" required value={date}
          min={date === goal.goal_date ? undefined : monthlyAnniversary(new Date(), 1)} max={monthlyAnniversary(new Date(), 600)}
          onChange={(event) => setDate(event.target.value)} /></div>
      </div>
      <div className="button-group">
        <button type="submit">{busy ? 'Saving changes…' : 'Save changes'}</button>
        <button type="button" className="secondary" onClick={onCancel}>Cancel</button>
      </div>
    </fieldset>
    {error && <p role="alert">{error}</p>}
  </form>;
}
function Customers({ request, isin }) {
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  const addButton = useRef(null);
  const wasCreating = useRef(false);
  useEffect(() => {
    if (wasCreating.current && !creating) addButton.current?.focus();
    wasCreating.current = creating;
  }, [creating]);
  return <>
    <div className="section-heading page-heading"><div><h2 className="instrument-name">Customers</h2><p className="muted">Choose a customer to manage portfolios, goals and saved simulations.</p></div>
      <button ref={addButton} type="button" disabled={creating} aria-expanded={creating} aria-controls={creating ? 'new-customer-form' : undefined} onClick={() => setCreating(true)}>Add customer</button>
    </div>
    {isin && <p className="planner-prefill">Planning with {isin}. Choose a customer and goal first; this instrument will be preselected for the new simulation.</p>}
    {creating && <CreateForm request={request} onCancel={() => setCreating(false)}
      onCreated={() => { setRevision((value) => value + 1); setCreating(false); }} />}
    <PagedList key={revision} request={request} path="/customers" label="Customers" validate={validCustomerSummary} empty="No customers yet. Select Add customer to create your first customer.">
      {(items) => <>
        <p className="muted" id="customer-net-worth-note">Net worth is cash plus holdings across all tracked EUR portfolios, using saved prices—not live prices. External assets and debts are not included. Total P&L is realized plus unrealized P&L.</p>
        <p className="muted" id="customer-pnl-note">Today P&L uses the latest saved closes, not necessarily today’s prices. It includes all buys, sales and fees after the prior close, even trades newer than the latest price; deposits and withdrawals are excluded. This is a saved-price estimate, not live P&L. Missing or mismatched prices are unavailable.</p>
        <div className="planner-table-scroll" tabIndex={0} role="region" aria-label="Customers table">
          <table className="planner-table planner-summary-table customers-table" aria-describedby="customer-net-worth-note customer-pnl-note">
            <caption>Customer portfolio summary</caption>
            <thead><tr><th scope="col">Customer</th><th scope="col">Goals</th><th scope="col">Net worth (EUR)</th>
              {pnlColumns.map(([label, key]) => <th key={key} scope="col">{label}</th>)}
            </tr></thead>
            <tbody>{items.map((customer) => <tr key={customer.id}>
              <th scope="row"><a href={`${customerRoute(customer.id)}${prefill(isin)}`}>{customer.name}</a></th>
              <td>{customer.goal_count}</td>
              <td>{formatEuros(customer.net_worth_eur)}{customer.net_worth_eur === null && <span className="simulation-parameter">Some holdings cannot be valued.</span>}</td>
              {pnlColumns.map(([, key]) => <td key={key} title={key === 'today_pnl_eur' && customer.today_to_date ? `Price date: ${customer.today_to_date}` : undefined}>
                <span className={pnlClass(customer[key])}>{formatEuros(customer[key])}</span>
              </td>)}
            </tr>)}</tbody>
          </table>
        </div>
      </>}
    </PagedList>
  </>;
}
function timeRemaining(date) {
  // Compare calendar days in UTC, independent of local timezone and time of day.
  const today = new Date().toISOString().slice(0, 10);
  const days = Math.round((Date.parse(`${date}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400000);
  if (days === 0) return 'Today';
  const duration = `${Math.abs(days).toLocaleString('en-GB')} ${Math.abs(days) === 1 ? 'day' : 'days'}`;
  return days > 0 ? `${duration} left` : `${duration} past target`;
}
function CreatedDate({ value }) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) return 'Unavailable';
  return <time dateTime={value}>{new Date(value).toISOString().slice(0, 10)}</time>;
}
function CustomerGoals({ request, customer, isin }) {
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  const addButton = useRef(null);
  const wasCreating = useRef(false);
  useEffect(() => {
    if (wasCreating.current && !creating) addButton.current?.focus();
    wasCreating.current = creating;
  }, [creating]);
  return <>
    <CustomerTabs customer={customer} active="goals" isin={isin} />
    <div className="section-heading goals-heading">
      <p className="muted">Choose a goal to review saved simulations or explore ways to fund it.</p>
      <button ref={addButton} type="button" disabled={creating} aria-expanded={creating} aria-controls={creating ? 'new-goal-form' : undefined} onClick={() => setCreating(true)}>Add goal</button>
    </div>
    {creating && <CreateForm request={request} customerId={customer.id} onCancel={() => setCreating(false)}
      onCreated={() => { setRevision((value) => value + 1); setCreating(false); }} />}
    <PagedList key={revision} request={request} path={`${customerPath(customer.id)}/goals`} label="Goals" validate={(item) => validGoal(item, customer.id)} empty="No goals yet. Select Add goal to create your first goal.">
      {(items) => <>
        <p className="goals-scroll-hint muted" id="goals-scroll-note">Scroll horizontally to see all goal details.</p>
        <div className="planner-table-scroll" tabIndex={0} role="region" aria-label="Goals table" aria-describedby="goals-scroll-note">
          <table className="planner-table goals-table">
            <caption>Goal summary for {customer.name}</caption>
            <thead><tr>{['Goal', 'Target (EUR)', 'Goal date (UTC)', 'Time remaining', 'Created (UTC)'].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
            <tbody>{items.map((goal) => <tr key={goal.id}>
              <th scope="row"><a href={`${goalRoute(customer.id, goal.id)}${prefill(isin)}`}>{goal.name}</a></th>
              <td>{formatEuros(goal.goal_eur)}</td>
              <td><time dateTime={goal.goal_date}>{goal.goal_date}</time></td>
              <td>{timeRemaining(goal.goal_date)}</td>
              <td><CreatedDate value={goal.created_at} /></td>
            </tr>)}</tbody>
          </table>
        </div>
      </>}
    </PagedList>
  </>;
}
function Time({ value }) {
  return <time dateTime={value}>{new Date(value).toLocaleString('en-GB', { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short' })} UTC</time>;
}
function Parameters({ strategy }) {
  return Object.entries(strategy.parameters).map(([key, value]) => <span key={key} className="simulation-parameter">{key.replaceAll('_', ' ')}: {String(value)}</span>);
}
function SimulationHistory({ request, goal, isin, onUpdated }) {
  const [editing, setEditing] = useState(false);
  const [updated, setUpdated] = useState(false);
  const route = goalRoute(goal.customer_id, goal.id);
  const validSummary = (item) => id(item?.id) && text(item.strategy_name) && money(item.monthly_contribution_eur)
    && Number.isFinite(Date.parse(item.saved_at)) && Number.isFinite(Date.parse(item.calculated_at))
    && money(item.inputs?.goal_eur) && /^\d{4}-\d{2}-\d{2}$/.test(item.inputs?.goal_date)
    && money(item.inputs?.starting_capital_eur) && typeof item.inputs?.reliability === 'number'
    && typeof item.inputs?.strategy?.parameters === 'object' && item.inputs.strategy.parameters !== null
    && typeof item.success_probability === 'number' && item.success_probability >= 0 && item.success_probability <= 1;
  return <>
    <div className="section-heading page-heading"><div><h2 className="instrument-name">{goal.name}</h2>
      <p className="muted">{formatEuros(goal.goal_eur)} by {goal.goal_date}</p></div>
      <div className="button-group">
        <button type="button" className="secondary" disabled={editing} onClick={() => { setEditing(true); setUpdated(false); }}>Edit goal</button>
        <a className="button-link" href={`${route}/new${prefill(isin)}`}>New simulation</a>
      </div></div>
    {editing && <EditGoalForm request={request} goal={goal} onCancel={() => setEditing(false)}
      onSaved={(data) => { onUpdated(data); setEditing(false); setUpdated(true); }} />}
    {updated && <p role="status">Goal updated.</p>}
    <PagedList request={request} path={`${goalPath(goal.customer_id, goal.id)}/simulations`} label="Saved simulations" validate={validSummary}
      empty="No saved simulations yet. Calculate a simulation, then choose Save simulation to keep it.">
      {(items) => <div className="planner-table-scroll" tabIndex={0} role="region" aria-label="Simulation comparison">
        <table className="planner-table"><caption>Saved simulations for {goal.name}</caption>
          <thead><tr>{['Saved', 'Goal target / date', 'Strategy / parameters', 'Starting capital', 'Reliability target', 'Monthly contribution', 'Estimated success'].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
          <tbody>{items.map((run) => <tr key={run.id}>
            <th scope="row"><a href={simulationRoute(goal.customer_id, goal.id, run.id)}><Time value={run.saved_at} /></a></th>
            <td>{formatEuros(run.inputs.goal_eur)}<span className="simulation-parameter">{run.inputs.goal_date}</span></td>
            <td>{run.strategy_name}<Parameters strategy={run.inputs.strategy} /></td>
            <td>{formatEuros(run.inputs.starting_capital_eur)}</td><td>{percent(run.inputs.reliability)}</td>
            <td>{formatEuros(run.monthly_contribution_eur)}</td><td>{percent(run.success_probability)}</td>
          </tr>)}</tbody>
        </table>
      </div>}
    </PagedList>
  </>;
}
function SavedSimulation({ request, goal, simulationId }) {
  const resource = useRead(request, `${goalPath(goal.customer_id, goal.id)}/simulations/${encodeURIComponent(simulationId)}`);
  const run = resource.data;
  const valid = validSimulationRecord(run, goal, { saved: true, id: simulationId });
  return <><h2 className="instrument-name">Saved simulation</h2>
    <LoadState resource={resource} valid={valid}>
      {valid && <>
        <p className="muted">Read-only result for {goal.name}. Calculated <Time value={run.calculated_at} /> · Saved <Time value={run.saved_at} /></p>
        <dl className="facts">
          <div><dt>Target</dt><dd>{formatEuros(run.inputs.goal_eur)}</dd></div>
          <div><dt>Goal date</dt><dd>{run.inputs.goal_date}</dd></div>
          <div><dt>Starting capital</dt><dd>{formatEuros(run.inputs.starting_capital_eur)}</dd></div>
          <div><dt>Reliability target</dt><dd>{percent(run.inputs.reliability)}</dd></div>
          <div><dt>Strategy</dt><dd>{run.strategy_definition.name}<Parameters strategy={run.inputs.strategy} /></dd></div>
        </dl>
        <a className="button-link" href={`${goalRoute(goal.customer_id, goal.id)}/new`}>New simulation</a>
        <SimulationResult result={run.result} strategy={run.strategy_definition} />
      </>}
    </LoadState>
  </>;
}
function GoalPage({ request, customerId, goalId, newSimulation, simulationId, isin }) {
  const resource = useRead(request, goalPath(customerId, goalId));
  const goal = resource.data;
  const valid = validGoal(goal, customerId) && goal.id === goalId;
  return <LoadState resource={resource} valid={valid}>
    {valid && <>
      {(newSimulation || simulationId) && <nav className="planner-breadcrumbs" aria-label="Goal navigation"><a href={`${goalRoute(customerId, goalId)}${prefill(isin)}`}>← {goal.name} · Saved simulations</a></nav>}
      {newSimulation ? <GoalPlanner isin={isin} goal={goal} apiBase={goalPath(customerId, goalId)} reportHref={(runId) => simulationRoute(customerId, goalId, runId)} />
        : simulationId ? <SavedSimulation request={request} goal={goal} simulationId={simulationId} />
          : <SimulationHistory request={request} goal={goal} isin={isin} onUpdated={resource.update} />}
    </>}
  </LoadState>;
}
function CustomerPage({ request, customerId, ...props }) {
  const resource = useRead(request, customerPath(customerId));
  const valid = validCustomer(resource.data) && resource.data.id === customerId;
  return <LoadState resource={resource} valid={valid}>
    {valid && (props.goalId ? <>
      <nav className="planner-breadcrumbs" aria-label="Customer navigation"><a href={`${customerRoute(customerId)}/goals${prefill(props.isin)}`}>← {resource.data.name} · Goals</a></nav>
      <GoalPage request={request} customerId={customerId} {...props} />
    </> : <CustomerGoals request={request} customer={resource.data} isin={props.isin} />)}
  </LoadState>;
}
export default function CustomerPlanner(props) {
  const { request, user, status } = useAuth();
  const [identity, setIdentity] = useState({ request, version: 0 });
  if (identity.request !== request) setIdentity({ request, version: identity.version + 1 });
  // Reset forms, loaded data and in-flight operations on every route/session change.
  const key = JSON.stringify([props, user?.id, status, identity.version]);
  return <article key={key} className="customer-planner" aria-label="Customers">
    {props.customerId && <nav className="planner-breadcrumbs" aria-label="Planner navigation"><a href={`#/goal${prefill(props.isin)}`}>← Customers</a></nav>}
    {props.customerId ? <CustomerPage request={request} {...props} /> : <Customers request={request} isin={props.isin} />}
  </article>;
}
