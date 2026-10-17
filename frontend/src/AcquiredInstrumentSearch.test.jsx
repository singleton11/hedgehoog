import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AcquiredInstrumentSearch, { instrumentLabel } from './AcquiredInstrumentSearch.jsx';

const instruments = [
  { instrument_id: 'IE0000000001', name: 'World &amp; Europe ETF', ticker: 'WORLD' },
  { instrument_id: 'IE0000000002', name: 'Emerging Markets ETF', ticker: 'EM' },
];
function Harness({ initial = '', onChange = () => {}, items = instruments }) {
  const [value, setValue] = useState(initial);
  return <><label htmlFor="instrument">Instrument</label><AcquiredInstrumentSearch id="instrument" label="Instrument"
    value={value} instruments={items} onChange={(next) => { setValue(next); onChange(next); }} />
    <button type="button">Outside</button></>;
}
afterEach(cleanup);
const input = () => screen.getByRole('combobox', { name: 'Instrument' });

describe('acquired instrument search', () => {
  it.each(['world', 'WORLD', 'Europe', 'IE0000000001', 'world europe'])('filters acquired instruments by %s', (query) => {
    render(<Harness />);
    fireEvent.change(input(), { target: { value: query } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option')).toHaveTextContent('World & Europe ETF');
  });

  it('requires explicit selection and supports keyboard navigation without submitting', () => {
    const onChange = vi.fn();
    const submit = vi.fn((event) => event.preventDefault());
    render(<form onSubmit={submit}><Harness onChange={onChange} /></form>);
    fireEvent.change(input(), { target: { value: 'ETF' } });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(input(), { key: 'Enter' });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(input(), { key: 'ArrowUp' });
    expect(input()).toHaveAttribute('aria-activedescendant', screen.getAllByRole('option')[1].id);
    fireEvent.keyDown(input(), { key: 'Enter' });
    expect(onChange).toHaveBeenLastCalledWith(instruments[1].instrument_id);
    expect(input()).toHaveValue(instrumentLabel(instruments[1]));
    expect(input()).toHaveAttribute('aria-expanded', 'false');
    expect(submit).not.toHaveBeenCalled();
  });

  it('displays a preselected instrument, clears it when editing, and selects a replacement', () => {
    const onChange = vi.fn();
    render(<Harness initial={instruments[0].instrument_id} onChange={onChange} />);
    expect(input()).toHaveValue(instrumentLabel(instruments[0]));
    fireEvent.change(input(), { target: { value: 'emerging' } });
    expect(onChange).toHaveBeenLastCalledWith('');
    expect(input()).toHaveValue('emerging');
    fireEvent.click(screen.getByRole('option'));
    expect(onChange).toHaveBeenLastCalledWith(instruments[1].instrument_id);
    fireEvent.click(screen.getByRole('button', { name: 'Clear instrument' }));
    expect(input()).toHaveValue('');
    expect(input()).toHaveFocus();
    expect(screen.getAllByRole('option')).toHaveLength(2);
  });

  it('shows no matches and dismisses results on Escape or blur', () => {
    render(<Harness />);
    fireEvent.change(input(), { target: { value: 'unacquired' } });
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('No matching acquired instruments');
    fireEvent.keyDown(input(), { key: 'Escape' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    fireEvent.focus(input());
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    fireEvent.blur(input(), { relatedTarget: screen.getByRole('button', { name: 'Outside' }) });
    expect(input()).toHaveAttribute('aria-expanded', 'false');
  });

  it('keeps instruments beyond the first inventory page searchable', () => {
    const items = Array.from({ length: 125 }, (_, index) => ({ instrument_id: `id-${index}`, name: `Fund ${index}` }));
    render(<Harness items={items} />);
    fireEvent.change(input(), { target: { value: 'Fund 124' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    fireEvent.click(screen.getByRole('option'));
    expect(input()).toHaveValue('Fund 124 · id-124');
  });
});
