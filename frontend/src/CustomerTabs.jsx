export default function CustomerTabs({ customer, active, isin }) {
  const route = `#/goal/customers/${encodeURIComponent(customer.id)}`;
  const query = isin ? `?instrument=${encodeURIComponent(isin)}` : '';
  // These tabs navigate between routes, so keep native link/keyboard/history behavior.
  return <header className="customer-heading">
    <h2 className="instrument-name">{customer.name}</h2>
    <nav className="customer-tabs" aria-label="Customer sections">
      <a href={`${route}/portfolios${query}`} aria-current={active === 'portfolios' ? 'page' : undefined}>Portfolios</a>
      <a href={`${route}/goals${query}`} aria-current={active === 'goals' ? 'page' : undefined}>Goals</a>
    </nav>
  </header>;
}
