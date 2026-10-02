import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PortfolioPlanTracking from './PortfolioPlanTracking.jsx';

const result = {
  plan_id: 'plan1', on_track: false, success_probability: 0.7, original_probability: 0.9,
  target_probability: 0.9, probability_change_pp: -20, target_gap_pp: -20,
  equity_eur: '12000.00', monthly_contribution_eur: '100.00', required_monthly_contribution_eur: '150.00',
  monthly_contribution_change_eur: '50.00', goal_eur: '100000.00', goal_date: '2036-01-01',
  strategy_name: 'Buy and hold', warnings: ['Model uncertainty'],
  forecast_status: 'reached', forecast_goal_date: '2037-07-01', forecast_search_end_date: '2076-01-01',
  schedule_difference_months: 18, schedule_difference_years: 1.5,
};
const props = { portfolioPath: '/customers/c/portfolios/p', planId: 'plan1', revision: 0 };
describe('Portfolio plan tracking', () => {
  it('shows metrics without differences, explanatory text or warnings', async () => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue(result)} />);
    expect(await screen.findByText('Below target probability')).toBeInTheDocument();
    expect(screen.getByText('70.0%')).toBeInTheDocument();
    expect(screen.queryByText(/pp|Gap to target|Change from saved plan|Monthly change from plan/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Model and valuation warnings|Model uncertainty|From today's portfolio|Buy and hold/)).not.toBeInTheDocument();
    const section = screen.getByRole('region', { name: 'Plan progress' });
    expect(section.querySelectorAll('dl.facts')).toHaveLength(4);
    expect(section.querySelectorAll('dl.facts > div')).toHaveLength(11);
    expect(screen.getByText('Goal achieved').nextElementSibling).toHaveTextContent('12.0%');
    expect(screen.getByRole('progressbar', { name: 'Portfolio goal progress' })).toHaveAttribute('value', '12');
    expect(screen.getByText('Amount remaining').nextElementSibling).toHaveTextContent('€88,000.00');
    expect(screen.getByText('Below target probability')).toHaveClass('return-negative');
    expect(screen.getByText('Planned monthly investment')).toBeInTheDocument();
    expect(screen.getByText('Monthly investment needed to reach target')).toBeInTheDocument();
  });
  it.each([
    ['99.99', true, 'Ahead of plan'],
    ['100.00', true, 'On track'],
    ['0.00', true, 'Ahead of plan'],
    ['99.99', false, 'Below target probability'],
  ])('classifies a required payment of %s with on_track=%s as %s', async (required, onTrack, status) => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue({
      ...result, on_track: onTrack, success_probability: onTrack ? 0.95 : 0.7,
      required_monthly_contribution_eur: required,
    })} />);
    expect(await screen.findByText(status)).toHaveClass(onTrack ? 'return-positive' : 'return-negative');
  });
  it.each([[-18, '18.0 months (1.5 years) ahead'], [18, '18.0 months (1.5 years) late'], [0, 'On schedule']])('shows signed timing %s', async (months, label) => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue({
      ...result, schedule_difference_months: months, schedule_difference_years: months / 12,
    })} />);
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByText('2037-07-01')).toBeInTheDocument();
  });
  it.each([['not_reached', 'Target probability not reached within 50 years'], ['unavailable', 'Forecast unavailable']])('shows %s without inventing timing', async (status, label) => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue({
      ...result, forecast_status: status, forecast_goal_date: null,
      schedule_difference_months: null, schedule_difference_years: null,
    })} />);
    expect(await screen.findByText(label)).toBeInTheDocument();
  });
  it('shows uncapped achieved percentage with a visually full bar above the target', async () => {
    render(<PortfolioPlanTracking {...props} equity="150000.00" target="100000.00" request={vi.fn().mockRejectedValue(new Error('Deadline reached'))} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Deadline reached');
    expect(screen.getByText('Goal achieved').nextElementSibling).toHaveTextContent('150.0%');
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '100');
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '150.0% of goal achieved');
  });
  it('shows arithmetic progress even when forecasting fails', async () => {
    render(<PortfolioPlanTracking {...props} equity="12000.00" target="100000.00" request={vi.fn().mockRejectedValue(new Error('Deadline reached'))} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Deadline reached');
    expect(screen.getByText('Goal achieved').nextElementSibling).toHaveTextContent('12.0%');
    expect(screen.getByText('Amount remaining').nextElementSibling).toHaveTextContent('€88,000.00');
  });
  it('rejects malformed timing', async () => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue({ ...result, schedule_difference_months: null })} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unexpected');
  });
  it('does not request calculations without a plan', () => {
    const request = vi.fn();
    render(<PortfolioPlanTracking {...props} planId={null} request={request} />);
    expect(request).not.toHaveBeenCalled();
    expect(screen.getByText(/Assign a saved plan/)).toBeInTheDocument();
  });
  it('hides previous results on refresh and ignores late replies', async () => {
    let resolve;
    const request = vi.fn().mockResolvedValueOnce(result).mockImplementationOnce(() => new Promise((done) => { resolve = done; })).mockResolvedValueOnce({ ...result, plan_id: 'plan2', on_track: true });
    const view = render(<PortfolioPlanTracking {...props} request={request} />);
    await screen.findByText('Below target probability');
    view.rerender(<PortfolioPlanTracking {...props} request={request} revision={1} />);
    expect(screen.queryByText('Below target probability')).not.toBeInTheDocument();
    view.rerender(<PortfolioPlanTracking {...props} request={request} planId="plan2" revision={1} />);
    await screen.findByText('On track');
    await act(async () => resolve(result));
    expect(screen.queryByText('Below target probability')).not.toBeInTheDocument();
  });
  it('rejects mismatched plan results', async () => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue({ ...result, plan_id: 'wrong' })} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unexpected');
  });
});
