from copy import deepcopy
from decimal import Decimal

import pytest

from test_customer_planner import planner, calculate, save, ttl_clock
from test_market_data import api, clock, config, db
from test_portfolio_tracker import portfolio


def linked(planner):
    client, customer, _, url = planner
    saved = save(client, url, calculate(client, url, starting_capital_eur='0'))
    client.post(f"{url}/simulations/{saved['id']}/plan")
    _, path = portfolio(client, customer['id'])
    client.patch(path, json={'name': 'Tracked', 'plan_id': saved['id']})
    return client, path, saved


def test_same_state_replays_saved_probability_and_payment(planner, db):
    client, path, saved = linked(planner)
    before = deepcopy(db.planner_simulations.find_one({'_id': saved['id']}))
    response = client.get(path + '/plan-tracking')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['success_probability'] == saved['result']['success_probability']
    assert result['required_monthly_contribution_eur'] == saved['result']['monthly_contribution_eur']
    assert result['probability_change_pp'] == 0
    assert result['monthly_contribution_change_eur'] == '0.00'
    assert result['on_track']
    assert db.planner_simulations.find_one({'_id': saved['id']}) == before


def test_refreshes_estimates_and_reuses_seed_and_paths(planner, monkeypatch):
    import goal_strategies as gs
    client, path, saved = linked(planner)
    calls = []
    real = gs.instrument_statistics
    def refreshed(db, instrument):
        calls.append(instrument)
        estimates = real(db, instrument)
        return {**estimates, 'daily_mean_log_return': -0.001, 'daily_log_return_variance': 0.0001}
    monkeypatch.setattr(gs, 'instrument_statistics', refreshed)
    result = client.get(path + '/plan-tracking').json()
    assert calls
    assert result['seed'] == saved['result']['seed']
    assert result['simulation_paths'] == saved['result']['simulation_paths']
    assert result['monthly_contribution_eur'] == saved['result']['monthly_contribution_eur']
    assert result['probability_change_pp'] < 0
    assert not result['on_track']
    assert result['required_contribution_success_probability'] >= result['target_probability']
    assert Decimal(result['monthly_contribution_change_eur']) > 0


def test_current_equity_and_remaining_time_change_projection(planner, db):
    from datetime import date, timedelta
    from plan_tracking import track_plan
    _, _, saved = linked(planner)
    plan = db.planner_simulations.find_one({'_id': saved['id']})
    today = date.fromisoformat(saved['result']['valuation_date'])
    empty = {'equity_eur': '0.00', 'warnings': []}
    funded = {'equity_eur': '50000.00', 'warnings': []}
    baseline = track_plan(db, plan, empty, today)
    richer = track_plan(db, plan, funded, today)
    later = track_plan(db, plan, empty, today + timedelta(days=365 * 5))
    assert richer['success_probability'] >= baseline['success_probability']
    assert Decimal(richer['required_monthly_contribution_eur']) < Decimal(baseline['required_monthly_contribution_eur'])
    assert Decimal(later['required_monthly_contribution_eur']) > Decimal(baseline['required_monthly_contribution_eur'])
    assert later['goal_date'] == saved['inputs']['goal_date']


def test_missing_plan_and_owner_scope(planner, db):
    client, path, saved = linked(planner)
    db.planner_simulations.update_one({'_id': saved['id']}, {'$set': {'user_id': 'foreign'}})
    assert client.get(path + '/plan-tracking').status_code == 404
    client.patch(path, json={'name': 'Unlinked', 'plan_id': None})
    assert client.get(path + '/plan-tracking').status_code == 422
    client.post('/auth/logout')
    assert client.get(path + '/plan-tracking').status_code == 401


def test_missing_valuation_and_expired_deadline(planner, db):
    import market_data as md
    from plan_tracking import track_plan
    from datetime import date
    _, _, saved = linked(planner)
    plan = db.planner_simulations.find_one({'_id': saved['id']})
    with pytest.raises(md.DataError, match='complete EUR'):
        track_plan(db, plan, {'equity_eur': None}, date(2026, 9, 17))
    with pytest.raises(md.DataError, match='deadline'):
        track_plan(db, plan, {'equity_eur': '0.00'}, date(2040, 1, 1))
