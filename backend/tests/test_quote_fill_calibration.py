from copy import deepcopy
from datetime import date

import pytest

import goal_calibration as gc
import random_calibration as rc
from test_goal_calibration import rows_between, inputs, body
from test_goal_planner import login, seed_profile, seed_history
from test_market_data import ISIN, LISTING, NOW, api, clock, config, db
from test_customer_planner import planner, calculate, save, ttl_clock


@pytest.mark.parametrize('method', ['smooth', 'normal'])
@pytest.mark.parametrize('random_mode', [False, True])
def test_training_fill_has_no_future_outcome_leakage(monkeypatch, method, random_mode):
    rows = rows_between()
    origin = '2023-02-01'
    # An internal training hole is repairable; the origin itself has no right
    # bound in training and must not be smoothed from a held-out quote.
    for row in rows:
        if row['date'] in ('2023-01-10', origin):
            row['close'] = row['adj_close'] = None
    changed = deepcopy(rows)
    for row in changed:
        if row['date'] > origin:
            row['close'] = row['adj_close'] = 9999
    captured = []
    module = rc if random_mode else gc
    real = module.calculate_statistics
    def capture(training):
        captured.append(deepcopy(training))
        return real(training)
    monkeypatch.setattr(module, 'calculate_statistics', capture)
    policy = {'version': 1, 'method': method, 'seed': 11}
    settings = (gc.RandomCalibrationInputs(mode='random', scenario_count=10, lookback_months=6,
                min_horizon_months=1, max_horizon_months=1, start_date=date(2023, 2, 1), end_date=date(2023, 2, 1))
                if random_mode else inputs(start_date=date(2023, 2, 1), end_date=date(2023, 2, 1)))
    evaluate = rc.evaluate_random_history if random_mode else gc.evaluate_history
    for source in (rows, changed):
        try:
            evaluate(source, settings, seed=42, paths=20, quote_fill_policy=policy, instrument_id=ISIN)
        except gc.md.DataError as error:
            assert method == 'smooth' and 'No evaluable' in str(error)
    assert captured[0] == captured[1]
    assert captured[0][-1]['date'] == origin
    assert next(r for r in captured[0] if r['date'] == '2023-01-10')['close'] is not None
    assert (captured[0][-1]['close'] is None) == (method == 'smooth')


def test_saved_simulation_freezes_policy_and_existing_reports(planner, db):
    client, _, _, url = planner
    first = save(client, url, calculate(client, url))
    assert first['result']['quote_fill_policy'] == {'version': 1, 'method': 'smooth', 'seed': 0}
    client.put('/settings/market-data', json={'method': 'normal', 'seed': 123})
    second = calculate(client, url)
    assert second['result']['quote_fill_policy'] == {'version': 1, 'method': 'normal', 'seed': 123}
    assert client.get(url + f"/simulations/{first['id']}").json() == first


def test_saved_calibration_freezes_policy(api, db):
    client, _ = api
    seed_profile(db); seed_history(db, rows_between()); login(client)
    response = client.post('/goals/calibrate', json=body(scenario_count=10))
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['quote_fill_policy']['method'] == 'smooth'
    client.put('/settings/market-data', json={'method': 'normal', 'seed': 123})
    assert client.get('/goals/calibrations/' + first['id']).json()['result'] == first


@pytest.mark.parametrize('random_mode', [False, True])
def test_outcome_draws_anchor_to_training_origin_without_fitting_it(monkeypatch, random_mode):
    import math
    from quote_fill import fill_adjusted_rows

    rows = rows_between()
    origin = '2023-02-01'
    start = '2022-08-01'
    # Earlier history has a different return distribution than the lookback.
    for i, row in enumerate(rows):
        if row['date'] < start:
            row['close'] = row['adj_close'] = 100 * math.exp(0.2 * math.sin(i))
        if row['date'] in (origin, '2023-02-02', '2023-02-03', '2023-02-07'):
            row['close'] = row['adj_close'] = None
    original = deepcopy(rows)
    policy = {'version': 1, 'method': 'normal', 'seed': 11}
    training = fill_adjusted_rows([r for r in rows if start <= r['date'] <= origin], ISIN, policy)
    unpinned = fill_adjusted_rows(rows, ISIN, policy, cutoff='2023-03-01')
    by_date = {r['date']: r for r in unpinned}
    frozen = training[-1]
    assert frozen['close'] != by_date[origin]['close']

    module = rc if random_mode else gc
    replay = module.realized_value
    captured = []
    def inspect(outcome, *args):
        captured.append(deepcopy(outcome))
        return replay(outcome, *args)
    monkeypatch.setattr(module, 'realized_value', inspect)
    settings = (gc.RandomCalibrationInputs(mode='random', scenario_count=10, lookback_months=6,
                min_horizon_months=1, max_horizon_months=1, start_date=date(2023, 2, 1), end_date=date(2023, 2, 1))
                if random_mode else inputs(horizon_months=1, start_date=date(2023, 2, 1), end_date=date(2023, 2, 1)))
    evaluate = rc.evaluate_random_history if random_mode else gc.evaluate_history
    evaluate(rows, settings, seed=42, paths=20, quote_fill_policy=policy, instrument_id=ISIN)
    assert captured
    for outcome in captured:
        actual = {r['date']: r for r in outcome}
        for field in ('close', 'adj_close'):
            assert actual[origin][field] == frozen[field]
            # Same observed fit and random draws as the raw full-history path,
            # but every consecutive draw must inherit the frozen origin scale.
            ratio = float(frozen[field]) / float(by_date[origin][field])
            for day in ('2023-02-02', '2023-02-03'):
                assert float(actual[day][field]) == pytest.approx(float(by_date[day][field]) * ratio)
            # A real observation resets the anchor; pinned synthetic origin
            # must not contaminate subsequent fitting or later draws either.
            assert actual['2023-02-07'][field] == by_date['2023-02-07'][field]
    assert rows == original
