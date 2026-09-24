import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import GoalPlanner, { monthlyAnniversary, normalizeEuros, SimulationResult, validSimulationRecord } from './GoalPlanner.jsx';
import { formatEuros } from './goalProjection.js';
import { percent } from './ReturnStatistics.jsx';

const PAGE_SIZE = 20;
const id = (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]+$/.test(value);
const text = (value) => typeof value === 'string' && value.trim().length > 0;
const money = (value) => typeof value === 'string' && /^\d+\.\d{2}$/.test(value);
const validCustomer = (value) => id(value?.id) && text(value.name);
const validGoal = (value, customerId) => validCustomer(value) && value.customer_id === customerId && money(value.goal_eur)
  && typeof value.goal_date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value.goal_date);
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
    retry: () => setAttempt((value) => value + 1) };
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
function CreateForm({ request, customerId, onCreated }) {
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
  return <form className="goal-form planner-create" aria-label={`New ${kind}`} onSubmit={create}>
    <h3>New {kind}</h3>
    <fieldset disabled={busy}>
      <div className="goal-fields">
        <div><label htmlFor={`${kind}-name`}>{customerId ? 'Goal name' : 'Customer name'}</label>
          <input id={`${kind}-name`} value={name} maxLength={200} required onChange={(event) => setName(event.target.value)} autoComplete="off" /></div>
        {customerId && <>
          <div><label htmlFor="new-goal-amount">Target (EUR)</label><input id="new-goal-amount" inputMode="decimal" value={amount} required maxLength={100} onChange={(event) => setAmount(event.target.value)} /></div>
          <div><label htmlFor="new-goal-date">Goal date (UTC)</label><input id="new-goal-date" type="date" required value={date}
            min={monthlyAnniversary(new Date(), 1)} max={monthlyAnniversary(new Date(), 600)} onChange={(event) => setDate(event.target.value)} /></div>
        </>}
      </div>
      <button type="submit">{busy ? 'Creating…' : `Create ${kind}`}</button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
  </form>;
}
function Customers({ request, isin }) {
  const [revision, setRevision] = useState(0);
  return <>
    <div className="page-heading"><h2 className="instrument-name">Customers</h2><p className="muted">Choose a customer, create a goal and save the simulations you want to keep.</p></div>
    {isin && <p className="planner-prefill">Planning with {isin}. Choose a customer and goal first; this instrument will be preselected for the new simulation.</p>}
    <PagedList key={revision} request={request} path="/customers" label="Customers" validate={validCustomer} empty="No customers yet. Create your first customer below.">
      {(items) => <ul className="planner-cards">{items.map((customer) => <li key={customer.id}>
        <a href={`${customerRoute(customer.id)}${prefill(isin)}`}>{customer.name}<span>View goals →</span></a>
      </li>)}</ul>}
    </PagedList>
    <CreateForm request={request} onCreated={() => setRevision((value) => value + 1)} />
  </>;
}
function CustomerGoals({ request, customer, isin }) {
  const [revision, setRevision] = useState(0);
  return <>
    <h2 className="instrument-name">{customer.name}</h2>
    <p className="muted">Each goal has a fixed target and date. Explore different ways to fund it with separate simulations.</p>
    <PagedList key={revision} request={request} path={`${customerPath(customer.id)}/goals`} label="Goals" validate={(item) => validGoal(item, customer.id)} empty="No goals yet. Create a goal below.">
      {(items) => <ul className="planner-cards">{items.map((goal) => <li key={goal.id}>
        <a href={`${goalRoute(customer.id, goal.id)}${prefill(isin)}`}>{goal.name}<span>{formatEuros(goal.goal_eur)} · {goal.goal_date}</span></a>
      </li>)}</ul>}
    </PagedList>
    <CreateForm request={request} customerId={customer.id} onCreated={() => setRevision((value) => value + 1)} />
  </>;
}
function Time({ value }) {
  return <time dateTime={value}>{new Date(value).toLocaleString('en-GB', { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short' })} UTC</time>;
}
function Parameters({ strategy }) {
  return Object.entries(strategy.parameters).map(([key, value]) => <span key={key} className="simulation-parameter">{key.replaceAll('_', ' ')}: {String(value)}</span>);
}
function SimulationHistory({ request, goal, isin }) {
  const route = goalRoute(goal.customer_id, goal.id);
  const validSummary = (item) => id(item?.id) && text(item.strategy_name) && money(item.monthly_contribution_eur)
    && Number.isFinite(Date.parse(item.saved_at)) && Number.isFinite(Date.parse(item.calculated_at))
    && money(item.inputs?.starting_capital_eur) && typeof item.inputs?.reliability === 'number'
    && typeof item.inputs?.strategy?.parameters === 'object' && item.inputs.strategy.parameters !== null
    && typeof item.success_probability === 'number' && item.success_probability >= 0 && item.success_probability <= 1;
  return <>
    <div className="section-heading page-heading"><div><h2 className="instrument-name">{goal.name}</h2>
      <p className="muted">{formatEuros(goal.goal_eur)} by {goal.goal_date}</p></div>
      <a className="button-link" href={`${route}/new${prefill(isin)}`}>New simulation</a></div>
    <PagedList request={request} path={`${goalPath(goal.customer_id, goal.id)}/simulations`} label="Saved simulations" validate={validSummary}
      empty="No saved simulations yet. Calculate a simulation, then choose Save simulation to keep it.">
      {(items) => <div className="planner-table-scroll" tabIndex={0} role="region" aria-label="Simulation comparison">
        <table className="planner-table"><caption>Saved simulations for {goal.name}</caption>
          <thead><tr>{['Saved', 'Strategy / parameters', 'Starting capital', 'Reliability target', 'Monthly contribution', 'Estimated success'].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
          <tbody>{items.map((run) => <tr key={run.id}>
            <th scope="row"><a href={simulationRoute(goal.customer_id, goal.id, run.id)}><Time value={run.saved_at} /></a></th>
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
          : <SimulationHistory request={request} goal={goal} isin={isin} />}
    </>}
  </LoadState>;
}
function CustomerPage({ request, customerId, ...props }) {
  const resource = useRead(request, customerPath(customerId));
  const valid = validCustomer(resource.data) && resource.data.id === customerId;
  return <LoadState resource={resource} valid={valid}>
    {valid && (props.goalId ? <>
      <nav className="planner-breadcrumbs" aria-label="Customer navigation"><a href={`${customerRoute(customerId)}${prefill(props.isin)}`}>← {resource.data.name} · Goals</a></nav>
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
