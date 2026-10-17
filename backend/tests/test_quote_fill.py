from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest

from quote_fill import fill_rows, fill_adjusted_rows, read_policy
from test_market_data import ISIN, LISTING, NOW, api, clock, config, db
from test_portfolio_tracker import tracker, quote
from test_portfolio_performance import invest, get, points


def rows(values, currency='EUR'):
    day = date(2026, 9, 1)
    result = []
    for value in values:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        result.append({'date': day.isoformat(), 'close': value, 'price_currency': currency})
        day += timedelta(days=1)
    return result


def normal(seed=7):
    return {'version': 1, 'method': 'normal', 'seed': seed}


def test_default_smoothing_multigap_preserves_raw_and_dates():
    source = rows([None, '100', None, None, '160', None])
    original = deepcopy(source)
    result = fill_rows(source, ISIN)
    assert [Decimal(r['close']) if r['close'] is not None else None for r in result] == [None, 100, 120, 140, 160, None]
    assert source == original
    assert [r['date'] for r in result] == [r['date'] for r in source]
    assert all('filled_fields' not in r for r in source)


@pytest.mark.parametrize('bad', ['0', '-1', 'Infinity', 'garbage', True])
def test_invalid_is_not_missing(bad):
    source = rows(['100', bad, '120'])
    assert fill_rows(source, ISIN) == source


def test_currency_continuity_foreign_unknown_and_weekend():
    source = rows(['100', None, '120'])
    source[1]['price_currency'] = 'USD'
    assert fill_rows(source, ISIN) == source
    assert fill_rows(rows(['100', None, '120'], 'USD'), ISIN) == rows(['100', None, '120'], 'USD')
    source[1]['price_currency'] = None
    assert fill_rows(source, ISIN) == source
    assert Decimal(fill_rows(source, ISIN, currency_override='EUR')[1]['close']) == 110
    source = rows(['100', None, '120'])
    source[1]['date'] = '2026-09-05'
    source[2]['date'] = '2026-09-07'
    assert fill_rows(source, ISIN) == source


def test_cutoff_never_uses_later_bound_and_absent_dates_not_inserted():
    source = rows(['100', None, '120'])
    assert fill_rows(source, ISIN, cutoff=source[1]['date'])[-1]['close'] is None
    assert fill_rows([source[0], source[2]], ISIN) == [source[0], source[2]]


def test_normal_reproducible_stable_domains_and_future_independent():
    source = rows(['100', '110', '99', '108', None, None, '120'])
    a = fill_rows(source, ISIN, normal())
    assert a == fill_rows(source, ISIN, normal())
    assert a[4]['close'] != fill_rows(source, ISIN, normal(8))[4]['close']
    assert a[4]['close'] != fill_rows(source, 'another', normal())[4]['close']
    assert a[:6] == fill_rows(source[:6], ISIN, normal())
    changed = deepcopy(source); changed[-1]['close'] = '99999'
    assert a[:6] == fill_rows(changed, ISIN, normal())[:6]
    assert all(Decimal(r['close']) > 0 for r in a)


def test_sampling_fit_uses_real_adjacent_weekdays_only():
    source = rows(['100', '110', None, '130', None])
    assert fill_rows(source, ISIN, normal()) == source  # Only one observed return.
    source = rows(['100', '110', '121', None, None])
    actual = fill_rows(source, ISIN, normal())
    assert float(actual[-1]['close']) == pytest.approx(146.41)
    source[1]['date'] = '2026-08-01'  # Never fit a weekend observation.
    assert fill_rows(source, ISIN, normal())[-1]['close'] is None


def test_extremes_fail_closed_and_split_failures_not_repaired():
    source = rows(['1e-300', '1e300', '1e-300', None])
    for seed in range(15):
        result = fill_rows(source, ISIN, normal(seed))
        value = result[-1]['close']
        assert value is None or (Decimal(value).is_finite() and Decimal(value) > 0)
    source = rows(['100', '0', '120'])
    for row in source:
        row['split_adjusted_close'] = row['close'] if row['close'] != '0' else None
    assert fill_adjusted_rows(source, ISIN, None) == source


def test_settings_defaults_permissions_and_strict_seed(tracker, db):
    client, _, _, _ = tracker
    endpoint = '/settings/market-data'
    assert client.get(endpoint).json() == {'version': 1, 'method': 'smooth', 'seed': 0}
    for seed in [True, -1, 4294967296, 1.5, '42', None]:
        assert client.put(endpoint, json={'method': 'normal', 'seed': seed}).status_code == 422
    assert client.put(endpoint, json={'method': 'bad', 'seed': 0}).status_code == 422
    assert client.put(endpoint, json={'method': 'normal', 'seed': 4294967295}).status_code == 200
    assert read_policy(db)['seed'] == 4294967295
    db.users.update_one({'login': 'admin'}, {'$set': {'role': 'user'}})
    assert client.get(endpoint).status_code == 200
    assert client.put(endpoint, json={'method': 'smooth', 'seed': 0}).status_code == 403
    assert read_policy(db)['seed'] == 4294967295
    client.post('/auth/logout')
    assert client.get(endpoint).status_code == 401
    assert client.put(endpoint, json={'method': 'smooth', 'seed': 0}).status_code == 401


def test_portfolio_twr_repairs_derived_gap_not_saved_evidence(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    db.quotes.update_many({'date': '2026-09-16'}, {'$set': {'data.close': None}})
    quote(db, '2026-09-17', '120')
    original = list(db.quotes.find())
    result = get(client, url)
    assert points(result)['2026-09-16'] == 10
    assert result['summary']['twr_percent'] == '20.00000000'
    assert list(db.quotes.find()) == original
    client.put('/settings/market-data', json={'method': 'normal', 'seed': 7})
    assert get(client, url)['summary']['twr_percent'] is None  # Not enough preceding returns.


def test_statistics_policy_cache_and_bootstrap_consistency(db):
    from test_goal_planner import seed_profile, seed_history, history
    from return_statistics import refresh_statistics, instrument_return_runs
    seed_profile(db)
    source = history()
    source[30]['close'] = source[30]['adj_close'] = None
    seed_history(db, source)
    original = list(db.quotes.find())
    a, _ = refresh_statistics(db, ISIN)
    assert a['filled_quote_count'] == 1
    before = db.instrument_statistics.find_one({'_id': ISIN})['signature']
    db.app_settings.insert_one({'_id': 'market_data', 'method': 'normal', 'seed': 9})
    b, _ = refresh_statistics(db, ISIN)
    assert b['quote_fill_policy']['seed'] == 9
    assert db.instrument_statistics.find_one({'_id': ISIN})['signature'] != before
    runs = instrument_return_runs(db, ISIN, as_of=NOW)
    assert sum(map(len, runs)) == b['return_count']
    assert list(db.quotes.find()) == original


def test_missing_fields_and_extra_settings_are_rejected(tracker):
    client, _, _, _ = tracker
    for body in [{}, {'method': 'smooth'}, {'seed': 0}, {'method': 'normal', 'seed': 1, 'extra': True}]:
        assert client.put('/settings/market-data', json=body).status_code == 422


def test_smoothing_precision_does_not_depend_on_calculation_context():
    from decimal import localcontext
    source = rows(['100', None, None, '101'])
    with localcontext() as context:
        context.prec = 28
        a = fill_rows(source, ISIN)
    with localcontext() as context:
        context.prec = 80
        b = fill_rows(source, ISIN)
    assert a == b


def test_normal_never_anchors_a_weekend_quote():
    source = rows(['100', '110', '105'])
    source.extend([
        {'date': '2026-09-05', 'close': '999', 'price_currency': 'EUR'},
        {'date': '2026-09-07', 'close': None, 'price_currency': 'EUR'},
    ])
    assert fill_rows(source, ISIN, normal())[-1]['close'] is None
