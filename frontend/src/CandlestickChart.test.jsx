import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import CandlestickChart from './CandlestickChart.jsx';

const quote = (date) => ({ date, open: '10', high: '12', low: '9', close: '11', volume: '100', price_currency: 'EUR' });
const observations = () => JSON.parse(screen.getByLabelText('Quote observations (current window, all provider fields)').textContent);
const select = (range) => fireEvent.click(screen.getByRole('button', { name: range, exact: true }));

afterEach(cleanup);

describe('price currency labels', () => {
  it('uses the manual currency in the header, tooltip and hover without modifying raw data or prices', () => {
    const quotes = [{ ...quote('2024-09-17'), price_currency: null }];
    const { container, rerender } = render(<CandlestickChart quotes={quotes} priceCurrencyOverride="CHF" />);
    expect(screen.getByText('Unadjusted OHLC · Listing price currency: CHF (manual)')).toBeInTheDocument();
    const candle = container.querySelector('.candle');
    const shape = candle.querySelector('rect').outerHTML;
    expect(candle.querySelector('title')).toHaveTextContent('Price currency: CHF');
    fireEvent.mouseEnter(candle);
    expect(container.querySelector('.chart-readout')).toHaveTextContent('Price currency: CHF');
    expect(observations()).toEqual(quotes);
    rerender(<CandlestickChart quotes={quotes} priceCurrencyOverride="GBP" />);
    expect(container.querySelector('.chart-readout')).toHaveTextContent('Price currency: GBP');
    expect(container.querySelector('.candle rect').outerHTML).toBe(shape);
    expect(observations()).toEqual(quotes);
  });

  it('keeps provider currency unless an explicit manual override is supplied', () => {
    const quotes = [quote('2024-09-17')];
    const { rerender } = render(<CandlestickChart quotes={quotes} />);
    expect(screen.getByText('Unadjusted OHLC · Listing price currency: EUR')).toBeInTheDocument();
    rerender(<CandlestickChart quotes={quotes} priceCurrencyOverride="USD" />);
    expect(screen.getByText('Unadjusted OHLC · Listing price currency: USD (manual)')).toBeInTheDocument();
    expect(observations()[0].price_currency).toBe('EUR');
  });
});

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
    expect(screen.getByText(`${dates.length - start} candles · ${dates.length - start} of 9 quotes`)).toBeInTheDocument();
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
    expect(container.querySelector('.chart-readout')).toHaveTextContent('Hover for prices');
    rerender(<CandlestickChart quotes={['2024-08-17', '2024-09-17', '2024-10-17'].map(quote)} />);
    expect(observations().map((item) => item.date)).toEqual(['2024-09-17', '2024-10-17']);
    expect(screen.getByRole('button', { name: '1m' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('keeps raw quotes collapsed and available on demand', () => {
    render(<CandlestickChart quotes={[quote('2024-09-17')]} />);
    const raw = screen.getByLabelText('Quote observations (current window, all provider fields)');
    expect(raw).not.toBeVisible();
    fireEvent.click(screen.getByText('Raw quotes'));
    expect(raw).toBeVisible();
    expect(observations()).toEqual([quote('2024-09-17')]);
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
