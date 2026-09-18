import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import CandlestickChart from './CandlestickChart.jsx';

const quote = (date) => ({ date, open: '10', high: '12', low: '9', close: '11', volume: '100', price_currency: 'EUR' });
const observations = () => JSON.parse(screen.getByLabelText('Quote observations (current window, all provider fields)').textContent);
const select = (range) => fireEvent.click(screen.getByRole('button', { name: range, exact: true }));

afterEach(cleanup);

describe('price-history calendar ranges', () => {
  const dates = ['2019-09-16', '2019-09-17', '2023-09-16', '2023-09-17', '2024-03-16', '2024-03-17', '2024-08-16', '2024-08-17', '2024-09-17'];

  it('offers 1m, 6m, 1y, 5y and all with 1y selected by default', () => {
    render(<CandlestickChart quotes={dates.map(quote)} />);
    const buttons = within(screen.getByRole('group', { name: 'Chart window' })).getAllByRole('button');
    expect(buttons.map((button) => button.textContent)).toEqual(['1m', '6m', '1y', '5y', 'all']);
    expect(screen.getByRole('button', { name: '1y' })).toHaveAttribute('aria-pressed', 'true');
    expect(observations().map((item) => item.date)).toEqual(dates.slice(3));
  });

  it.each([['1m', 7], ['6m', 5], ['1y', 3], ['5y', 1], ['all', 0]])('filters %s by calendar date, including the boundary', (range, start) => {
    const { container } = render(<CandlestickChart quotes={dates.map(quote)} />);
    select(range);
    expect(observations().map((item) => item.date)).toEqual(dates.slice(start));
    expect(container.querySelectorAll('.candle')).toHaveLength(dates.length - start);
    expect(screen.getByText(`9 quotes loaded · ${dates.length - start} in this window · ${dates.length - start} candles plotted`)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: range, exact: true })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getAllByRole('button').filter((button) => button.getAttribute('aria-pressed') === 'true')).toHaveLength(1);
  });

  it.each([
    ['1m', '2024-03-31', '2024-02-28', '2024-02-29'],
    ['1m', '2023-03-31', '2023-02-27', '2023-02-28'],
    ['6m', '2024-08-31', '2024-02-28', '2024-02-29'],
    ['1y', '2024-02-29', '2023-02-27', '2023-02-28'],
    ['5y', '2024-02-29', '2019-02-27', '2019-02-28'],
    ['1m', '2024-01-15', '2023-12-14', '2023-12-15'],
  ])('clamps %s ending %s to the target calendar month', (range, latest, excluded, boundary) => {
    render(<CandlestickChart quotes={[excluded, boundary, latest].map(quote)} />);
    select(range);
    expect(observations().map((item) => item.date)).toEqual([boundary, latest]);
  });

  it('reanchors the selected range when quotes refresh and clears the hover on range changes', () => {
    const { container, rerender } = render(<CandlestickChart quotes={['2024-08-17', '2024-09-17'].map(quote)} />);
    fireEvent.mouseEnter(container.querySelector('.candle'));
    expect(container.querySelector('.chart-readout')).toHaveTextContent('2024-08-17');
    select('1m');
    expect(container.querySelector('.chart-readout')).toHaveTextContent('Hover a candle');
    rerender(<CandlestickChart quotes={['2024-08-17', '2024-09-17', '2024-10-17'].map(quote)} />);
    expect(observations().map((item) => item.date)).toEqual(['2024-09-17', '2024-10-17']);
    expect(screen.getByRole('button', { name: '1m' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('handles empty and shorter-than-requested histories', () => {
    const { rerender } = render(<CandlestickChart quotes={[]} />);
    for (const range of ['1m', '6m', '1y', '5y', 'all']) {
      select(range);
      expect(observations()).toEqual([]);
      expect(screen.getByRole('status')).toHaveTextContent('No plottable OHLC candles available.');
    }
    rerender(<CandlestickChart quotes={[quote('2024-09-17')]} />);
    select('5y');
    expect(observations()).toEqual([quote('2024-09-17')]);
  });
});
