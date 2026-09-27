import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PortfolioPlanTracking from './PortfolioPlanTracking.jsx';

const result = {
  plan_id: 'plan1', on_track: false, success_probability: 0.7, original_probability: 0.9,
  target_probability: 0.9, probability_change_pp: -20, target_gap_pp: -20,
  equity_eur: '12000.00', monthly_contribution_eur: '100.00', required_monthly_contribution_eur: '150.00',
  monthly_contribution_change_eur: '50.00', goal_eur: '100000.00', goal_date: '2036-01-01',
  strategy_name: 'Buy and hold', warnings: ['Model uncertainty'],
};
const props = { portfolioPath: '/customers/c/portfolios/p', planId: 'plan1', revision: 0 };
describe('Portfolio plan tracking', () => {
  it('shows probability, change and the revised payment', async () => {
    render(<PortfolioPlanTracking {...props} request={vi.fn().mockResolvedValue(result)} />);
    expect(await screen.findByText('Below target probability')).toBeInTheDocument();
    expect(screen.getByText('70.0%')).toBeInTheDocument();
    expect(screen.getAllByText('-20.0 pp')).toHaveLength(2);
    const section = screen.getByRole('region', { name: 'Plan progress' });
    expect(section.querySelectorAll('dl.facts')).toHaveLength(2);
    expect(section.querySelectorAll('dl.facts > div')).toHaveLength(8);
    expect(screen.getByText('Below target probability')).toHaveClass('return-negative');
    expect(screen.getByText('Planned monthly investment')).toBeInTheDocument();
    expect(screen.getByText('Monthly investment needed to reach target')).toBeInTheDocument();
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
