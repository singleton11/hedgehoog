import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PhoneTable from './PhoneTable.jsx';
import CalibrationChart from './CalibrationChart.jsx';

const tableSources = import.meta.glob('./*.jsx', { query: '?raw', import: 'default', eager: true });

it('opts every non-ETF native table definition into phone cards without replacing the ETF table', () => {
  const definitions = Object.entries(tableSources).filter(([path]) => !path.includes('.test.') && !path.endsWith('/PhoneTable.jsx'));
  const nativeTables = definitions.flatMap(([path, source]) => [...source.matchAll(/<table\b/g)].map(() => path));
  expect(nativeTables).toEqual(['./AcquiredEtfs.jsx']);
  expect(definitions.reduce((count, [, source]) => count + [...source.matchAll(/<PhoneTable\b/g)].length, 0)).toBe(12);
});

function Example({ countLabel = 'Scored scenarios', onAction = () => {} }) {
  return <PhoneTable className="calibration-table">
    <caption>Audit</caption>
    <thead><tr><th scope="col" rowSpan={2}>Scenario</th><th scope="colgroup" colSpan={2}>Historical</th><th scope="col" rowSpan={2}>Actions</th></tr>
      <tr><th scope="col">{countLabel}</th><th scope="col">Return</th></tr></thead>
    <tbody><tr><th scope="row">scenario-1</th><td>9007199254740993.01</td><td>1.25000001</td><td><button onClick={onAction}>Remove</button></td></tr></tbody>
  </PhoneTable>;
}

describe('phone table cards', () => {
  it.each(['Details', 'Reason', 'Parameters', 'Strategy / parameters'])('gives long %s fields full-width phone space', (heading) => {
    render(<PhoneTable><thead><tr><th scope="col">Identity</th><th scope="col">{heading}</th></tr></thead>
      <tbody><tr><th scope="row">Item</th><td>Long explanatory text</td></tr></tbody></PhoneTable>);
    expect(screen.getByRole('cell')).toHaveClass('phone-cell-wide');
  });
  it.each(['forecasts', 'scenarios'])('uses the current %s bin count heading and retains all ten empty bins', (countLabel) => {
    const bins = Array.from({ length: 10 }, (_, index) => ({ lower: index / 10, upper: (index + 1) / 10, count: 0 }));
    const { container } = render(<CalibrationChart bins={bins} countLabel={countLabel} />);
    const table = container.querySelector('table');
    expect(table.querySelectorAll('tbody tr')).toHaveLength(10);
    const heading = countLabel === 'scenarios' ? 'Scored scenarios' : 'Evaluated forecasts';
    for (const row of table.querySelectorAll('tbody tr')) {
      expect(row.children[1].querySelector('.phone-cell-label')).toHaveAttribute('data-label', heading);
      expect(row.children[2]).toHaveTextContent(`No ${countLabel}`);
      expect(row.children[3]).toHaveTextContent(`No ${countLabel}`);
    }
  });
  it('labels every field using its current grouped column headings and associates native headers', () => {
    const { container, rerender } = render(<Example />);
    const table = screen.getByRole('table', { name: 'Audit' });
    expect(table).toHaveClass('phone-table');
    const cells = [...table.querySelectorAll('tbody tr > *')];
    expect(cells.map((cell) => cell.querySelector('.phone-cell-label')?.getAttribute('data-label'))).toEqual([
      'Scenario', 'Historical · Scored scenarios', 'Historical · Return', 'Actions',
    ]);
    for (const cell of cells) {
      expect(cell.querySelector('.phone-cell-label')).toHaveAttribute('aria-hidden', 'true');
      for (const id of cell.getAttribute('headers').split(' ')) expect(container.querySelector(`[id="${id}"]`)).not.toBeNull();
    }
    expect(cells[0]).toHaveAttribute('scope', 'row');
    expect(cells[0].id).not.toBe('');
    for (const cell of cells.slice(1)) expect(cell.getAttribute('headers').split(' ')).toContain(cells[0].id);
    expect(cells[3]).toHaveClass('phone-cell-wide');
    rerender(<Example countLabel="Evaluated forecasts" />);
    expect(cells[1].querySelector('.phone-cell-label')).toHaveAttribute('data-label', 'Historical · Evaluated forecasts');
    expect(cells[1]).toHaveTextContent('9007199254740993.01');
    expect(cells[2]).toHaveTextContent('1.25000001');
  });

  it('keeps one data tree, stable controls, DOM order and unique header IDs across tables', () => {
    const onAction = vi.fn();
    const { rerender } = render(<><Example onAction={onAction} /><Example /></>);
    const buttons = screen.getAllByRole('button', { name: 'Remove' });
    expect(buttons).toHaveLength(2);
    buttons[0].focus();
    rerender(<><Example countLabel="Updated" onAction={onAction} /><Example /></>);
    expect(screen.getAllByRole('button')[0]).toBe(buttons[0]);
    expect(buttons[0]).toHaveFocus();
    fireEvent.click(buttons[0]);
    expect(onAction).toHaveBeenCalledTimes(1);
    const ids = [...document.querySelectorAll('thead th')].map((header) => header.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect([...document.querySelector('tbody tr').children].map((cell) => cell.tagName)).toEqual(['TH', 'TD', 'TD', 'TD']);
  });
});
