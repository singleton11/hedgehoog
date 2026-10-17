"""Last-known-close fallback is portfolio-only, explicit and read-only."""
from copy import deepcopy
from decimal import Decimal

import pytest

from portfolio_tracker import carry_trailing_closes, quote_rows
from test_market_data import ISIN, NOW, api, clock, config, db
from test_portfolio_tracker import tracker, quote, detail
from test_portfolio_performance import invest, get, points


def test_trailing_holes_restore_value_and_twr_without_inventing_daily_pnl(tracker, db):
    client, customer_id, metadata, url = tracker
    invest(client, url)
    quote(db, '2026-09-16', None, adj_close='999')
    quote(db, '2026-09-17', 'NaN')
    original = list(db.quotes.find())
    result = detail(client, url)
    position = result['positions'][0]
    assert result['equity_eur'] == result['positions_value_eur'] == '1000.00'
    assert result['unrealized_pnl_eur'] == '0.00'
    assert result['daily_unrealized_pnl_eur'] is None
    assert result['daily_from_date'] is result['daily_to_date'] is None
    assert position['market_price_eur'] == '100'
    assert position['price_date'] == '2026-09-15'
    assert position['latest_quote_date'] == '2026-09-17'
    assert position['price_is_stale'] is True
    assert '2026-09-15' in position['warning'] and '2026-09-17' in position['warning']
    summary = client.get(f'/customers/{customer_id}/portfolios').json()['items'][0]
    assert summary['net_worth_eur'] == '1000.00'
    assert summary['today_pnl_eur'] is None
    perf = get(client, url)
    assert perf['summary'] == {'twr_percent': '0.00000000', 'sharpe': None, 'sortino': None}
    assert points(perf)['2026-09-17'] == 0
    assert any('stale last valid close from 2026-09-15' in warning for warning in perf['warnings'])
    # Statistics/calibration callers retain the unresolved real trailing rows.
    assert quote_rows(db, position['instrument'], NOW)[-1]['close'] == 'NaN'
    assert list(db.quotes.find()) == original
    assert db.portfolios.find_one({'_id': metadata['id']})['revision'] == 2
    # Once a genuine right bound arrives, interpolation and daily PnL resume.
    quote(db, '2026-09-17', '120')
    repaired = detail(client, url)
    assert repaired['equity_eur'] == '1200.00'
    assert repaired['daily_unrealized_pnl_eur'] == '100.00'
    assert repaired['positions'][0]['price_is_stale'] is False
    assert repaired['warnings'] == []


@pytest.mark.parametrize('barrier', ['0', '-1', 'Infinity', 'garbage', True])
def test_trailing_fallback_does_not_skip_invalid_prices(tracker, db, barrier):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, '2026-09-16', barrier)
    quote(db, '2026-09-17', None)
    assert detail(client, url)['equity_eur'] is None
    assert get(client, url)['summary']['twr_percent'] is None


@pytest.mark.parametrize('currency,override,eligible', [
    ('USD', None, False), (None, None, False), (None, 'EUR', True), ('USD', 'EUR', False),
])
def test_trailing_fallback_requires_confirmed_eur(tracker, db, currency, override, eligible):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, '2026-09-16', None, currency=currency)
    if override:
        db.instrument_state.update_one({'_id': ISIN}, {'$set': {'price_currency_override': override}})
    result = detail(client, url)
    assert (result['equity_eur'] is not None) is eligible
    assert (get(client, url)['summary']['twr_percent'] is not None) is eligible


def test_normal_policy_does_not_silently_carry_when_sampling_fails(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    quote(db, '2026-09-16', None)
    assert client.put('/settings/market-data', json={'method': 'normal', 'seed': 7}).status_code == 200
    assert detail(client, url)['equity_eur'] is None
    assert get(client, url)['summary']['twr_percent'] is None


def test_no_future_or_weekend_anchor_or_leading_fill(tracker, db):
    client, _, _, url = tracker
    invest(client, url)
    instrument = detail(client, url)['positions'][0]['instrument']
    for source in [
        [{'date': '2026-09-16', 'close': None, 'price_currency': 'EUR'}],
        [{'date': '2026-09-12', 'close': '100', 'price_currency': 'EUR'},
         {'date': '2026-09-14', 'close': None, 'price_currency': 'EUR'}],
        [{'date': '2026-09-11', 'close': '100', 'price_currency': 'EUR'},
         {'date': '2026-09-12', 'close': None, 'price_currency': 'EUR'}],
    ]:
        before = deepcopy(source)
        assert carry_trailing_closes(db, instrument, source) == before
        assert source == before
    quote(db, '2026-09-16', None)
    quote(db, '2026-09-18', '999')
    assert detail(client, url)['positions'][0]['market_price_eur'] == '100'


def test_stale_returns_do_not_enter_risk_ratios_or_replace_benchmark_prices(tracker, db):
    from test_performance_settings import second_instrument, configure

    client, _, _, url = tracker
    invest(client, url)
    second_instrument(db)
    configure(client)
    quote(db, '2026-09-17', None)
    result = get(client, url)
    assert result['summary']['twr_percent'] == '10.00000000'
    assert result['summary']['sharpe'] is result['summary']['sortino'] is None
    assert result['points'][-1]['sharpe'] is result['points'][-1]['sortino'] is None


def test_stale_twr_preserves_trade_fees_and_external_cash_flows(tracker, db):
    from test_portfolio_tracker import cash, trade, record

    client, _, _, url = tracker
    invest(client, url, quantity='5')
    quote(db, '2026-09-16', None)
    quote(db, '2026-09-17', None)
    record(client, url, cash(amount='100', day='2026-09-16'))
    record(client, url, trade(quantity='1', price='100', commission_eur='6', day='2026-09-16'))
    record(client, url, cash('withdrawal', amount='100', day='2026-09-17'))
    snapshot = detail(client, url)
    assert snapshot['equity_eur'] == '994.00'
    assert snapshot['daily_unrealized_pnl_eur'] is None
    perf = get(client, url)
    assert Decimal(perf['summary']['twr_percent']) == pytest.approx(Decimal('-0.54545455'))
    assert any('stale' in warning for warning in perf['warnings'])
