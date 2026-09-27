from datetime import date
from decimal import Decimal

import pytest

from goal_forecast import forecast_goal_date, calendar_month_difference
from goal_simulation import GoalInputs


def forecast(capital='0', monthly='100', goal_date=date(2027, 1, 15), mean=0):
    body = GoalInputs(goal_eur='1200', starting_capital_eur=capital,
                      goal_date=goal_date, reliability=0.9)
    return forecast_goal_date({'daily_mean_log_return': mean, 'daily_log_return_variance': 0},
                              body, date(2026, 1, 15), Decimal(monthly), paths=100, seed=42)


@pytest.mark.parametrize('capital,monthly,expected', [
    ('0', '100', '2027-01-15'), ('600', '100', '2026-07-15'),
    ('0', '50', '2028-01-15'), ('1200', '0', '2026-01-15'),
])
def test_fixed_payment_forecasts(capital, monthly, expected):
    result = forecast(capital, monthly)
    assert result['forecast_goal_date'] == expected
    assert result['forecast_status'] == 'reached'


def test_unreachable_and_numerical_failure_are_distinct():
    assert forecast(monthly='0')['forecast_status'] == 'not_reached'
    assert forecast(mean=1000)['forecast_status'] == 'unavailable'
    assert forecast(monthly='0')['forecast_goal_date'] is None
    assert forecast(monthly='0')['forecast_search_end_date'] == '2076-01-15'


def test_fractional_deadline_does_not_add_a_payment():
    assert forecast(goal_date=date(2026, 12, 20))['forecast_goal_date'] == '2027-01-15'


def test_search_checks_terminal_probability_not_ever_hit(monkeypatch):
    import numpy as np
    import goal_forecast

    def factors(*args):
        # Different paths hit the goal in the first two months, but neither
        # checkpoint meets 90%. Only the third checkpoint qualifies.
        yield date(2026, 2, 15), True, np.array([2.0, 0.5])
        yield date(2026, 3, 15), True, np.array([0.25, 4.0])
        yield date(2026, 4, 15), True, np.array([4.0, 1.0])

    monkeypatch.setattr(goal_forecast, 'period_factors', factors)
    body = GoalInputs(goal_eur='1200', starting_capital_eur='1000',
                      goal_date=date(2027, 1, 15), reliability=0.9)
    result = forecast_goal_date({'daily_mean_log_return': 0, 'daily_log_return_variance': 0},
                                body, date(2026, 1, 15), Decimal('0'), paths=2, seed=42)
    assert result['forecast_goal_date'] == '2026-04-15'


def test_calendar_difference_sign_and_month_end():
    assert calendar_month_difference(date(2027, 1, 15), date(2026, 7, 15)) == -6
    assert calendar_month_difference(date(2027, 1, 15), date(2028, 1, 15)) == 12
    assert calendar_month_difference(date(2026, 1, 31), date(2026, 2, 28)) == 1
    assert calendar_month_difference(date(2026, 1, 15), date(2026, 1, 15)) == 0
