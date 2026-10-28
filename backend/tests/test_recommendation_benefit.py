"""Edited rebalance groups must improve on cash/contribution purchases alone."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from goal_strategies import STRATEGIES
from test_portfolio_recommendations import saved_plan, get
from test_portfolio_tracker import (
    ISIN, SECOND_ISIN, NOW, api, clock, config, db, tracker, cash, trade,
    record, second_instrument, quote,
)


def test_quantity_edits_cannot_worsen_allocation_and_start_cooldown(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    second_instrument(db)
    extra = ['IE00B3XXRP09', 'IE00BK5BQT80']
    for index, isin in enumerate(extra):
        listing = {'symbol': f'DEMO{index}', 'exchange': 'XETR'}
        db.instrument_state.insert_one({'_id': isin, 'listing': listing})
        db.instruments.insert_one({'instrument_id': isin, **listing, 'collected_at': NOW,
                                   'job_id': 'synthetic-target', 'data': {}})
        quote(db, NOW.date().isoformat(), '30', isin=isin, listing=listing)
    saved_plan(client, db, cid, metadata, initial='1000', monthly='0')
    monkeypatch.setitem(STRATEGIES, 'buy_and_hold_single_instrument', replace(
        STRATEGIES['buy_and_hold_single_instrument'], recommendation_targets=lambda *_: {
            'version': 'four-target-test', 'targets': [{'instrument_id': isin, 'weight': '0.25'}
                for isin in [ISIN, SECOND_ISIN, *extra]],
            'supported_actions': ['deposit', 'sell', 'buy'], 'constraints': {},
        }))
    record(client, url, cash(amount='1000'))
    record(client, url, trade(quantity='4.54545454', price='110'))
    record(client, url, trade(quantity='16.66666666', price='30', instrument_id=SECOND_ISIN))
    snapshot = get(client, url)
    buys = [a for a in snapshot['actions'] if a['type'] == 'buy']
    assert len(buys) == 2
    request = {'snapshot_token': snapshot['snapshot_token'], 'date': snapshot['date'],
               'action_ids': [a['id'] for a in snapshot['actions']],
               'overrides': {a['id']: {'quantity': '1'} for a in buys}}
    before = deepcopy(db.portfolios.find_one({'_id': metadata['id']}))
    preview = client.post(url + '/recommendations/preview', json=request)
    assert preview.status_code == 422, preview.text
    assert 'improve allocation' in preview.json()['detail']
    assert db.portfolios.find_one({'_id': metadata['id']}) == before
    accepted = client.post(url + '/recommendations/preview', json={**request, 'overrides': {}})
    assert accepted.status_code == 200, accepted.text
    invalid_commit = client.post(url + '/recommendations/fulfill', json={
        **request, 'operation_id': str(uuid4()), 'preview_token': accepted.json()['preview_token']})
    assert invalid_commit.status_code == 422, invalid_commit.text
    assert db.portfolios.find_one({'_id': metadata['id']}) == before


@pytest.mark.parametrize("funded", ["1000", "500"])
@pytest.mark.parametrize("cash_fee", ["0", "1"])
@pytest.mark.parametrize("buy_price", ["1", "2"])
def test_reduced_cash_buy_cannot_weaken_cash_first_comparator(tracker, db, funded, cash_fee, buy_price):
    client, cid, metadata, url = tracker
    second_instrument(db)
    quote(db, NOW.date().isoformat(), '1')
    quote(db, NOW.date().isoformat(), '1', isin=SECOND_ISIN,
          listing={'symbol': 'EUNL.F', 'exchange': 'XFRA'})
    saved_plan(client, db, cid, metadata, initial='1000', monthly='0')
    record(client, url, cash(amount=funded))
    record(client, url, trade(quantity='500', price='1', instrument_id=SECOND_ISIN))
    snapshot = get(client, url)
    cash_buy = next(a for a in snapshot['actions'] if a['type'] == 'buy' and not a['rebalance'])
    sale_buy = next(a for a in snapshot['actions'] if a['type'] == 'buy' and a['rebalance'])
    request = {'snapshot_token': snapshot['snapshot_token'], 'date': snapshot['date'],
               'action_ids': [a['id'] for a in snapshot['actions']], 'overrides': {}}
    accepted = client.post(url + '/recommendations/preview', json=request)
    assert accepted.status_code == 200, accepted.text
    before = deepcopy(db.portfolios.find_one({'_id': metadata['id']}))
    reduced = {**request, 'overrides': {
        cash_buy['id']: {'quantity': '1', 'commission_eur': cash_fee, 'price_eur': buy_price},
        sale_buy['id']: {'quantity': '25', 'price_eur': buy_price},
    }}
    preview = client.post(url + '/recommendations/preview', json=reduced)
    assert preview.status_code == 422, preview.text
    assert 'improve allocation' in preview.json()['detail']
    invalid_commit = client.post(url + '/recommendations/fulfill', json={
        **reduced, 'operation_id': str(uuid4()), 'preview_token': accepted.json()['preview_token']})
    assert invalid_commit.status_code == 422, invalid_commit.text
    assert 'improve allocation' in invalid_commit.json()['detail']
    assert db.portfolios.find_one({'_id': metadata['id']}) == before
    assert not before.get('recommendation_last_rebalance')
    # Quantity edits remain allowed when the whole batch beats the independent baseline.
    improved = client.post(url + '/recommendations/preview', json={**request, 'overrides': {
        cash_buy['id']: {'quantity': '450', 'commission_eur': cash_fee},
        sale_buy['id']: {'quantity': '450'},
    }})
    assert improved.status_code == 200, improved.text
    assert Decimal(improved.json()['after']['max_deviation']) < Decimal('0.5')
    if buy_price == '2':
        # Compare at the same edited execution prices, not a cheaper saved close.
        improved = client.post(url + '/recommendations/preview', json={**request, 'overrides': {
            cash_buy['id']: {'quantity': '249', 'commission_eur': cash_fee, 'price_eur': buy_price},
            sale_buy['id']: {'quantity': '25', 'price_eur': buy_price},
        }})
        assert improved.status_code == 200, improved.text
        assert Decimal('0.5') < Decimal(improved.json()['after']['max_deviation']) < Decimal('0.65')


def test_sale_fees_do_not_reduce_cash_only_alternative(tracker, db):
    client, cid, metadata, url = tracker
    second_instrument(db)
    quote(db, NOW.date().isoformat(), '1')
    quote(db, NOW.date().isoformat(), '1', isin=SECOND_ISIN,
          listing={'symbol': 'EUNL.F', 'exchange': 'XFRA'})
    saved_plan(client, db, cid, metadata, initial='1000', monthly='0')
    record(client, url, cash(amount='1000'))
    record(client, url, trade(quantity='500', price='1', instrument_id=SECOND_ISIN))
    snapshot = get(client, url)
    sale, cash_buy, sale_buy = snapshot['actions']
    before = deepcopy(db.portfolios.find_one({'_id': metadata['id']}))
    response = client.post(url + '/recommendations/preview', json={
        'snapshot_token': snapshot['snapshot_token'], 'date': snapshot['date'],
        'action_ids': [a['id'] for a in snapshot['actions']], 'overrides': {
            sale['id']: {'commission_eur': '5'},
            cash_buy['id']: {'quantity': '250'}, sale_buy['id']: {'quantity': '247'},
        }})
    assert response.status_code == 422, response.text
    assert 'improve allocation' in response.json()['detail']
    assert db.portfolios.find_one({'_id': metadata['id']}) == before


def test_sell_only_correction_to_cash_target_still_qualifies(tracker, db, monkeypatch):
    client, cid, metadata, url = tracker
    saved_plan(client, db, cid, metadata, initial='1100', monthly='0')
    monkeypatch.setitem(STRATEGIES, 'buy_and_hold_single_instrument', replace(
        STRATEGIES['buy_and_hold_single_instrument'], recommendation_targets=lambda *_: {
            'version': 'cash-target-test', 'targets': [{'instrument_id': None, 'weight': '1'}],
            'supported_actions': ['deposit', 'sell', 'buy'], 'constraints': {},
        }))
    record(client, url, cash(amount='1100'))
    record(client, url, trade(quantity='10', price='110'))
    snapshot = get(client, url)
    assert [a['type'] for a in snapshot['actions']] == ['sell']
    preview = client.post(url + '/recommendations/preview', json={
        'snapshot_token': snapshot['snapshot_token'], 'date': snapshot['date'],
        'action_ids': [a['id'] for a in snapshot['actions']]})
    assert preview.status_code == 200, preview.text
    assert preview.json()['after']['max_deviation'] == '0'
    assert preview.json()['after']['cash_eur'] == '1100.00'
