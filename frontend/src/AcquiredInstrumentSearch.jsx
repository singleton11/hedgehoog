import { useEffect, useId, useRef, useState } from 'react';
import { displayText } from './instruments.js';
import Icon from './Icon.jsx';

export function instrumentLabel(item) {
  return [displayText(item.name) || item.instrument_id, item.instrument_id, item.ticker]
    .filter((part, index, parts) => part && parts.indexOf(part) === index).join(' · ');
}

export default function AcquiredInstrumentSearch({ id, label, value, instruments, required, describedBy, onChange }) {
  const listId = useId();
  const input = useRef(null);
  const selected = instruments.find((item) => item.instrument_id === value);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matches = instruments.filter((item) => {
    const text = [displayText(item.name), item.instrument_id, item.isin, item.ticker].filter(Boolean).join(' ').toLocaleLowerCase();
    return terms.every((term) => text.includes(term));
  });

  useEffect(() => {
    if (open && active >= 0) document.getElementById(`${listId}-${active}`)?.scrollIntoView?.({ block: 'nearest' });
  }, [active, open, listId]);

  function choose(item) {
    onChange(item.instrument_id);
    setQuery('');
    setOpen(false);
    setActive(-1);
  }

  function keyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
      setActive(-1);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setOpen(true);
      if (matches.length) setActive((index) => event.key === 'ArrowDown'
        ? (index + 1) % matches.length : (index <= 0 ? matches.length - 1 : index - 1));
    } else if (event.key === 'Enter' && open) {
      event.preventDefault();
      if (active >= 0 && matches[active]) choose(matches[active]);
    }
  }

  return <div className="instrument-search acquired-instrument-search" onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) { setOpen(false); setActive(-1); }
  }}>
    <div className="search-input-wrap">
      <Icon name="search" />
      <input ref={input} id={id} role="combobox" aria-autocomplete="list" aria-expanded={open}
        aria-controls={open ? listId : undefined} aria-describedby={describedBy} required={required}
        aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
        autoComplete="off" placeholder="Search by name, ticker or ISIN"
        value={selected ? instrumentLabel(selected) : query}
        onFocus={() => setOpen(true)} onKeyDown={keyDown}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
          setActive(-1);
          if (value) onChange('');
        }} />
      {(value || query) && <button type="button" className="instrument-search-clear" aria-label={`Clear ${label.toLowerCase()}`}
        onClick={() => { onChange(''); setQuery(''); setActive(-1); setOpen(true); input.current?.focus(); }}>Clear</button>}
    </div>
    {open && <div className="search-results">
      <ul id={listId} role="listbox" aria-label={`Matching ${label.toLowerCase()}s`}>
        {matches.map((item, index) => <li key={item.instrument_id} id={`${listId}-${index}`} role="option"
          aria-selected={item.instrument_id === value} className={active === index ? 'search-option-active' : undefined}
          onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setActive(index)} onClick={() => choose(item)}>
          <strong>{displayText(item.name) || item.instrument_id}</strong>
          <span>{[item.ticker, item.instrument_id].filter(Boolean).join(' · ')}</span>
        </li>)}
      </ul>
      {!matches.length && <p role="status">{instruments.length ? 'No matching acquired instruments. Try a name, ticker or ISIN.' : 'No acquired instruments available.'}</p>}
      {matches.length > 0 && <p className="sr-only" role="status">{matches.length} results. Use arrow keys and Enter to select.</p>}
    </div>}
  </div>;
}
