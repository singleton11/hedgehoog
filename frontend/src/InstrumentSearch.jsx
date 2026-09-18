import { useEffect, useId, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { displayText } from './instruments.js';

export const SEARCH_DELAY = 300;

export default function InstrumentSearch({ onSelect }) {
  const { request } = useAuth();
  const id = useId();
  const [query, setQuery] = useState('');
  const [items, setItems] = useState([]);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [attempt, setAttempt] = useState(0);
  const sequence = useRef(0);
  const controller = useRef(null);

  function invalidate() {
    sequence.current += 1;
    controller.current?.abort();
  }

  useEffect(() => {
    const current = ++sequence.current;
    const abort = new AbortController();
    controller.current = abort;
    const text = query.trim();
    if (text.length < 2) return () => abort.abort();
    const timer = setTimeout(async () => {
      if (abort.signal.aborted || current !== sequence.current) return;
      setStatus('loading');
      try {
        const data = await request(`/instruments/search?${new URLSearchParams({ q: text, limit: '15' })}`, { signal: abort.signal });
        if (abort.signal.aborted || current !== sequence.current) return;
        if (!Array.isArray(data?.items)) throw new Error('Search returned an unexpected response.');
        setItems(data.items);
        setStatus('done');
        setActive(-1);
      } catch (error) {
        if (abort.signal.aborted || current !== sequence.current) return;
        setError(error.message);
        setStatus('error');
      }
    }, SEARCH_DELAY);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [query, request, attempt]);

  function change(event) {
    invalidate();
    setQuery(event.target.value);
    setItems([]);
    setActive(-1);
    setError(null);
    setStatus(event.target.value.trim().length >= 2 ? 'waiting' : 'idle');
    setOpen(true);
  }

  function select(item) {
    invalidate();
    setOpen(false);
    setQuery('');
    setItems([]);
    setStatus('idle');
    setActive(-1);
    onSelect(item.instrument_id);
  }

  const expanded = open && query.trim().length >= 2;
  function reopen() {
    setOpen(true);
    if (status === 'idle' && query.trim().length >= 2 && !items.length) {
      setStatus('waiting');
      setAttempt((value) => value + 1);
    }
  }
  function keyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      invalidate();
      setOpen(false);
      setActive(-1);
      setStatus('idle');
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      reopen();
      if (items.length) setActive((index) => event.key === 'ArrowDown'
        ? (index + 1) % items.length : (index < 0 ? items.length - 1 : (index - 1 + items.length) % items.length));
    } else if (event.key === 'Enter' && expanded && active >= 0 && items[active]) {
      event.preventDefault();
      select(items[active]);
    }
  }

  useEffect(() => {
    if (expanded && active >= 0) document.getElementById(`${id}-option-${active}`)?.scrollIntoView?.({ block: 'nearest' });
  }, [active, expanded, id]);

  return (
    <section aria-label="Find an ETF" className="instrument-search" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) { setOpen(false); setActive(-1); }
    }}>
      <label htmlFor={`${id}-input`}>Find an ETF</label>
      <input id={`${id}-input`} role="combobox" aria-autocomplete="list" aria-expanded={expanded}
        aria-controls={`${id}-results`} aria-describedby={`${id}-help`}
        aria-activedescendant={expanded && active >= 0 ? `${id}-option-${active}` : undefined}
        autoComplete="off" maxLength={100} placeholder="Name, ticker or ISIN" value={query}
        onChange={change} onKeyDown={keyDown} onFocus={reopen} />
      <p id={`${id}-help`} className={open && query.trim().length === 1 ? 'muted' : 'sr-only'}>Enter at least 2 characters.</p>
      {expanded && <div className="search-results">
        <ul id={`${id}-results`} role="listbox" aria-label="Matching ETFs" aria-busy={['waiting', 'loading'].includes(status)}>
          {items.map((item, index) => <li role="option" id={`${id}-option-${index}`} key={item.instrument_id}
            aria-selected={active === index} onMouseDown={(event) => event.preventDefault()}
            onMouseEnter={() => setActive(index)} onClick={() => select(item)}>
            <strong>{displayText(item.name) || item.isin}</strong>
            <span>{[item.ticker, item.isin].filter(Boolean).join(' · ')}</span>
          </li>)}
        </ul>
        {['waiting', 'loading'].includes(status) && <p role="status">Searching…</p>}
        {status === 'done' && !items.length && <p role="status">No matching ETFs. Try a name, ticker or ISIN.</p>}
        {status === 'done' && items.length > 0 && <p className="sr-only" role="status">{items.length} results. Use arrow keys and Enter to select.</p>}
        {status === 'error' && <><p role="alert">{error}</p><button className="secondary" onMouseDown={(event) => event.preventDefault()}
          onClick={() => { setError(null); setStatus('waiting'); setAttempt((value) => value + 1); }}>Retry search</button></>}
      </div>}
    </section>
  );
}
