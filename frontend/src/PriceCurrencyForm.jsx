import { useId, useState } from 'react';

const CURRENCIES = ['EUR', 'USD', 'CHF', 'GBP', 'RUB'];

export default function PriceCurrencyForm({ busy, saving, error, onSave }) {
  const id = useId();
  const [selected, setSelected] = useState('');

  function submit(event) {
    event.preventDefault();
    if (busy || !CURRENCIES.includes(selected)) return;
    onSave(selected);
  }

  return <form className="price-currency-form" aria-label="Set price currency" onSubmit={submit}>
    <label htmlFor={id}>Price currency</label>
    <div className="button-group">
      <select id={id} value={selected} disabled={busy} required aria-describedby={`${id}-help`}
        onChange={(event) => setSelected(event.target.value)}>
        <option value="" disabled>Select currency</option>
        {CURRENCIES.map((value) => <option key={value} value={value}>{value}</option>)}
      </select>
      <button type="submit" className="secondary" disabled={busy || !selected}>
        {saving ? 'Saving…' : 'Save currency'}
      </button>
    </div>
    <p id={`${id}-help`} className="muted">Sets the price label only; amounts are not converted.</p>
    {error && <p role="alert">Could not save price currency: {error}</p>}
  </form>;
}
