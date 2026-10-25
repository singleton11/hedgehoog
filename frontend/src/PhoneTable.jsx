import { Children, cloneElement, isValidElement, useId } from 'react';

const elements = (children) => Children.toArray(children).filter(isValidElement);
const headingText = (node) => {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  return isValidElement(node) ? Children.toArray(node.props.children).map(headingText).join('') : '';
};

// Derive phone labels from the same headings as the native table, including spans.
// Cloning elements preserves keys, controls and component state across updates.
export default function PhoneTable({ children, className = '', ...props }) {
  const id = useId();
  const sections = elements(children);
  const columns = [];
  const occupied = [];
  const head = sections.find((section) => section.type === 'thead');
  const labelledHead = head && cloneElement(head, { role: 'rowgroup' }, elements(head.props.children).map((row, rowIndex) => {
    let column = 0;
    return cloneElement(row, { role: 'row' }, elements(row.props.children).map((cell, cellIndex) => {
      while (occupied[rowIndex]?.[column]) column++;
      const header = { id: cell.props.id || `${id}-heading-${rowIndex}-${cellIndex}`, label: headingText(cell).trim() };
      const width = cell.props.colSpan || 1;
      const height = cell.props.rowSpan || 1;
      for (let x = column; x < column + width; x++) {
        (columns[x] ||= []).push(header);
        for (let y = rowIndex; y < rowIndex + height; y++) (occupied[y] ||= [])[x] = true;
      }
      column += width;
      return cloneElement(cell, { id: header.id, role: 'columnheader' });
    }));
  }));

  return <table {...props} role="table" className={`${className} phone-table`}>
    {sections.map((section) => {
      if (section === head) return labelledHead;
      if (section.type !== 'tbody') return section;
      return cloneElement(section, { role: 'rowgroup' }, elements(section.props.children).map((row, rowIndex) => {
        let column = 0;
        const rowCells = elements(row.props.children);
        const rowHeader = rowCells.find((cell) => cell.type === 'th' && cell.props.scope === 'row');
        const rowId = rowHeader && (rowHeader.props.id || `${id}-row-${rowIndex}`);
        return cloneElement(row, { role: 'row' }, rowCells.map((cell) => {
          const headers = columns.slice(column, column + (cell.props.colSpan || 1)).flat();
          column += cell.props.colSpan || 1;
          const unique = [...new Map(headers.map((header) => [header.id, header])).values()];
          const label = unique.map((header) => header.label).join(' · ');
          const wide = cell.type === 'th' || /^(Actions|Market data|Plan|Status|Details|Reason|Parameters|Strategy \/ parameters)$/.test(label);
          return cloneElement(cell, {
            role: cell.type === 'th' ? 'rowheader' : 'cell',
            id: cell === rowHeader ? rowId : cell.props.id,
            headers: [cell.props.headers || unique.map((header) => header.id).join(' '), cell.type === 'td' ? rowId : null].filter(Boolean).join(' '),
            className: `${cell.props.className || ''}${wide ? ' phone-cell-wide' : ''}`,
          }, <span className="phone-cell-label" aria-hidden="true" data-label={label} />, cell.props.children);
        }));
      }));
    })}
  </table>;
}
