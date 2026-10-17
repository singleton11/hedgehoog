"""Daily PnL searches backward without inventing quotes or multi-day returns."""
from copy import deepcopy

import pytest

from test_market_data import ISIN, api, clock, config, db
from test_portfolio_tracker import tracker, cash, trade, record, quote, detail
from test_latest_saved_pnl import table_summaries


@pytest.mark.parametrize('later', [
    [('2026-09-17', None)],
    [('2026-09-17', None), ('2026-09-18', None)],
    [('2026-09-19', '999'), ('2026-09-20', '999')],
    [('2026-09-18', '130')],  # A missing Thursday must not become a two-day return.
])
def test_latest_available_daily_interval(tracker, db, clock, later):
    from datetime import timedelta
    client, cid, metadata, url = tracker
    clock[0] += timedelta(days=4)
    record(client, url, cash())
    record(client, url, trade(quantity='2', price='80'))
    for day, close in later:
        quote(db, day, close)
    original = deepcopy(list(db.quotes.find()))
    result = detail(client, url)
    position = result['positions'][0]
    assert result['daily_unrealized_pnl_eur'] == position['daily_unrealized_pnl_eur'] == '20.00'
    assert (result['daily_from_date'], result['daily_to_date']) == ('2026-09-15', '2026-09-16')
    assert position['daily_price_date'] == '2026-09-16'
    assert position['daily_previous_value_eur'] == result['previous_value_eur'] == '200.00'
    for summary in table_summaries(client, cid, metadata['id']):
        assert summary['today_pnl_eur'] == '20.00'
        assert (summary['today_from_date'], summary['today_to_date']) == ('2026-09-15', '2026-09-16')
    assert list(db.quotes.find()) == original


def test_no_available_interval_stays_unavailable(tracker, db):
    client, cid, metadata, url = tracker
    record(client, url, cash())
    record(client, url, trade())
    db.quotes.delete_many({'instrument_id': ISIN, 'date': '2026-09-15'})
    assert detail(client, url)['daily_unrealized_pnl_eur'] is None
    for summary in table_summaries(client, cid, metadata['id']):
        assert summary['today_pnl_eur'] is None
