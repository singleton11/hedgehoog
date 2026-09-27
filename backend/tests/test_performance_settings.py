from decimal import Decimal, localcontext
import math
import statistics

import pytest

from portfolio_performance import RiskRatios
from test_market_data import ISIN, LISTING, NOW, api, clock, config, db
from test_portfolio_tracker import tracker, quote

SECOND_ISIN = 'idx-00000000-0000-0000-0000-000000000001'
SECOND_LISTING = {'symbol': '^EURTEST', 'exchange': 'YAHOO'}


def second_instrument(db, days=('2026-09-15', '2026-09-16')):
    db.indices.insert_one({'instrument_id': SECOND_ISIN, 'name': 'EUR overnight index',
                           'symbol': SECOND_LISTING['symbol'], 'currency': 'EUR'})
    db.instrument_state.insert_one({'_id': SECOND_ISIN, 'listing': SECOND_LISTING})
    db.instruments.insert_one({'instrument_id': SECOND_ISIN, **SECOND_LISTING,
                               'collected_at': NOW, 'data': {}})
    for day, close in zip(days, ('20', '30')):
        quote(db, day, close, isin=SECOND_ISIN, listing=SECOND_LISTING)
from test_portfolio_performance import get, invest, index_history


def configure(client, identifier=SECOND_ISIN):
    result = client.put('/settings/performance', json={'instrument_id': identifier})
    assert result.status_code == 200, result.text
    return result.json()


def test_settings_auth_and_admin_only(tracker, db):
    client, _, _, _ = tracker
    second_instrument(db)
    saved = configure(client)
    assert saved['proxy']['instrument_id'] == SECOND_ISIN
    assert client.get('/settings/performance').json() == saved
    assert db.app_settings.find_one({'_id': 'portfolio_performance'})['updated_by']
    db.users.update_one({'login': 'admin'}, {'$set': {'role': 'user'}})
    assert client.get('/settings/performance').status_code == 200
    assert client.put('/settings/performance', json={'instrument_id': None}).status_code == 403
    assert client.get('/settings/performance').json() == saved
    client.post('/auth/logout')
    assert client.get('/settings/performance').status_code == 401
    assert client.put('/settings/performance', json={'instrument_id': None}).status_code == 401


def test_setting_requires_acquired_eur_index_and_explicit_clear(tracker, db):
    client, _, _, _ = tracker
    for body in ({}, {'instrument_id': '^IRX'}, {'instrument_id': ISIN}, {'instrument_id': None, 'extra': 1}):
        assert client.put('/settings/performance', json=body).status_code == 422
    assert client.put('/settings/performance', json={'instrument_id': SECOND_ISIN}).status_code == 404
    second_instrument(db)
    configure(client)
    db.quotes.update_many({'instrument_id': SECOND_ISIN}, {'$set': {'data.price_currency': 'USD'}})
    assert client.put('/settings/performance', json={'instrument_id': SECOND_ISIN}).status_code == 422
    assert configure(client, None)['proxy'] is None


def test_proxy_choices_filter_unacquired_non_eur_removed_and_paginate(tracker, db):
    client, _, _, _ = tracker
    endpoint = '/settings/performance/indices'
    assert client.get(endpoint).json()['items'] == []
    second_instrument(db)
    page = client.get(endpoint, params={'limit': 1}).json()
    assert page['total'] == 1 and page['items'][0]['instrument_id'] == SECOND_ISIN
    assert client.get(endpoint, params={'limit': 1, 'offset': 1}).json()['items'] == []
    for currency in ('USD', None):
        db.quotes.update_many({'instrument_id': SECOND_ISIN}, {'$set': {'data.price_currency': currency}})
        assert client.get(endpoint).json()['items'] == []
        assert client.put('/settings/performance', json={'instrument_id': SECOND_ISIN}).status_code == 422
    db.quotes.update_many({'instrument_id': SECOND_ISIN}, {'$set': {'data.price_currency': 'EUR'}})
    db.instruments.delete_many({'instrument_id': SECOND_ISIN})
    assert client.get(endpoint).json()['items'] == []
    assert client.put('/settings/performance', json={'instrument_id': SECOND_ISIN}).status_code == 422
    db.indices.update_one({'instrument_id': SECOND_ISIN}, {'$set': {'removed_at': NOW}})
    assert client.put('/settings/performance', json={'instrument_id': SECOND_ISIN}).status_code == 404
    client.post('/auth/logout')
    assert client.get(endpoint).status_code == 401


def test_removed_index_and_legacy_etf_setting_make_ratios_unavailable(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    second_instrument(db, days=('2026-09-14', '2026-09-16'))
    configure(client)
    assert get(client, url)['summary']['sharpe'] is not None
    db.indices.update_one({'instrument_id': SECOND_ISIN}, {'$set': {'removed_at': NOW}})
    assert get(client, url)['summary']['sharpe'] is None
    db.app_settings.update_one({'_id': 'portfolio_performance'}, {'$set': {
        'proxy': {'instrument_id': ISIN, 'name': 'Legacy ETF', **LISTING, 'currency': 'EUR'},
    }})
    result = get(client, url)
    assert result['summary']['sharpe'] is None
    assert result['summary']['twr_percent'] == '10.00000000'
    assert configure(client, None)['proxy'] is None


def test_ratios_match_independent_log_excess_oracle_and_zero_deviation():
    factors = [1.02, .98, 1.01, 1.0]
    targets = [1.001, 1.002, 1.001, 1.0]
    excess = [math.log(a) - math.log(b) for a, b in zip(factors, targets)]
    ratios = RiskRatios()
    with localcontext() as context:
        context.prec = 80
        for a, b in zip(factors, targets):
            ratios.add(Decimal(str(a)), Decimal(str(b)))
        result = ratios.values()
    numerator = statistics.mean(excess) * math.sqrt(365)
    assert float(result['sharpe']) == pytest.approx(numerator / statistics.stdev(excess), abs=1e-8)
    assert float(result['sortino']) == pytest.approx(numerator / math.sqrt(sum(min(x, 0) ** 2 for x in excess) / len(excess)), abs=1e-8)
    flat = RiskRatios()
    flat.add(Decimal(1), Decimal(1))
    flat.add_flat_days(1000000)
    assert flat.values() == {'sharpe': None, 'sortino': None}
    ratios.add(None, Decimal(1))
    ratios.add(Decimal(1), Decimal(1))
    assert ratios.values() == {'sharpe': None, 'sortino': None}


def test_batch_flat_days_matches_daily_expansion():
    a, b = RiskRatios(), RiskRatios()
    for ratios in (a, b):
        ratios.add(Decimal('1.03'), Decimal('1.01'))
    a.add_flat_days(100)
    for _ in range(100):
        b.add(Decimal(1), Decimal(1))
    for ratios in (a, b):
        ratios.add(Decimal('.96'), Decimal('1.01'))
    assert a.count == b.count
    assert a.values() == b.values()


def test_api_summary_proxy_gaps_and_benchmark_independence(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    second_instrument(db, days=('2026-09-14', '2026-09-16'))
    configure(client)
    result = get(client, url)
    assert result['summary']['twr_percent'] == '10.00000000'
    assert result['summary']['sharpe'] is not None
    assert result['summary']['sortino'] is not None
    assert result['summary'] == {key: result['points'][-1][key] for key in result['summary']}
    benchmark = index_history(db, [('2026-09-14', '100'), ('2026-09-17', '101')])
    assert get(client, url, benchmark)['summary'] == result['summary']
    db.quotes.delete_many({'instrument_id': SECOND_ISIN, 'date': '2026-09-14'})
    broken = get(client, url)
    assert broken['summary'] == {'twr_percent': '10.00000000', 'sharpe': None, 'sortino': None}
    assert any('matching EUR proxy history' in warning for warning in broken['warnings'])
    quote(db, '2026-09-14', '20', isin=SECOND_ISIN, listing=SECOND_LISTING)
    assert get(client, url)['summary'] == result['summary']
    db.instrument_state.update_one({'_id': SECOND_ISIN}, {'$set': {'listing': LISTING}})
    assert get(client, url)['summary']['sharpe'] is None


def test_sparse_calendar_sampling_matches_daily_oracle_and_extra_benchmark_dates(tracker, db):
    client, _, _, url = tracker
    quote(db, '2026-09-10', '100')
    quote(db, '2026-09-12', '110')
    invest(client, url, day='2026-09-10')
    second_instrument(db, days=('2026-09-09', '2026-09-13'))
    configure(client)
    result = get(client, url)
    # Calendar dates 10..17 inclusive, including carried weekends. Funded
    # days with no saved/event date must contribute real zero observations.
    excess = [0, 0, math.log(1.1), -math.log(1.5), 0, math.log(100 / 110), math.log(1.1), 0]
    assert float(result['summary']['sharpe']) == pytest.approx(statistics.mean(excess) * math.sqrt(365) / statistics.stdev(excess), abs=1e-8)
    benchmark = index_history(db, [('2026-09-09', '100'), ('2026-09-11', '101'), ('2026-09-14', '102')])
    compared = get(client, url, benchmark)
    assert compared['summary'] == result['summary']
    by_date = {point['date']: point for point in compared['points']}
    for point in result['points']:
        assert by_date[point['date']]['sharpe'] == point['sharpe']
        assert by_date[point['date']]['sortino'] == point['sortino']


def test_proxy_history_never_uses_future_observations(tracker, db):
    from datetime import timedelta
    client, _, _, url = tracker
    invest(client, url)
    second_instrument(db, days=('2026-09-14', '2026-09-16'))
    configure(client)
    before = get(client, url)['summary']
    quote(db, '2026-09-16', '999', isin=SECOND_ISIN, listing=SECOND_LISTING, collected_at=NOW + timedelta(days=1))
    assert get(client, url)['summary'] == before
