import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatisticsSummary, historicalReturnTitle, historicalVolatilityTitle } from './ReturnStatistics.jsx';

describe('split-adjusted statistics', () => {
  it('labels derived prices without presenting them as raw quotes or total returns', () => {
    const statistics = { price_basis: 'split_adjusted_close', quote_count: 100, price_count: 100,
      return_count: 99, daily_mean_log_return: 0, daily_volatility: 0, annualized_mean_log_return: 0,
      annualized_volatility: 0, warnings: [], historical_year_return: 0.1,
      historical_year_start_date: '2025-09-16', historical_year_end_date: '2026-09-16',
      historical_year_price_basis: 'split_adjusted_close', historical_year_volatility: 0,
      historical_year_volatility_start_date: '2025-09-16', historical_year_volatility_end_date: '2026-09-16',
      historical_year_volatility_price_basis: 'split_adjusted_close', historical_year_volatility_return_count: 250 };
    render(<StatisticsSummary statistics={statistics} variant="instrument" />);
    expect(screen.getByText('Split-adjusted close')).toBeInTheDocument();
    expect(screen.getByText('+10%')).toHaveAttribute('title',
      '2025-09-16 – 2026-09-16. Split-adjusted closes; cash distributions are not reinvested.');
    expect(historicalReturnTitle(statistics)).not.toContain('Unadjusted');
    expect(historicalVolatilityTitle(statistics)).toBe(
      '2025-09-16 – 2026-09-16. 250 usable daily log returns. Split-adjusted closes; cash distributions are not reinvested. Sample standard deviation × √252; missing-weekday intervals are excluded.');
    expect(historicalVolatilityTitle(statistics)).not.toContain('Unadjusted');
  });
});
