"""Catalogue removal preserves identity/history without reseeding removed rows."""
from uuid import uuid4

import pytest

import indices
from test_indices import api, clock, db, config, index, login, run


def test_remove_requires_auth_and_valid_identity(api, index):
    client, _ = api
    base = '/indices/' + index['instrument_id']
    assert client.delete(base).status_code == 401
    login(client)
    assert client.delete('/indices/not-an-index').status_code == 422
    assert client.delete('/indices/idx-' + str(uuid4())).status_code == 404


def test_remove_seed_is_durable_idempotent_and_restorable(api, db, index, monkeypatch):
    client, _ = api
    login(client)
    identifier = index['instrument_id']
    base = '/indices/' + identifier
    monkeypatch.setattr('index_routes.enqueue_acquisition', lambda *args: pytest.fail('Unexpected acquisition'))
    for _ in range(2):
        assert client.delete(base).json() == {'instrument_id': identifier, 'removed': True}
    indices.ensure_catalogue(db)
    result = client.get('/indices').json()
    assert result['total'] == len(indices.SEEDS) - 1
    assert identifier not in [item['instrument_id'] for item in result['items']]
    assert client.get(base).status_code == 404
    assert client.post(base + '/acquisitions', json={'confirmed': True}).status_code == 404
    restored = client.post('/indices', json={'symbol': index['symbol'], 'name': 'Do not overwrite'})
    assert restored.status_code == 201
    assert restored.json()['instrument_id'] == identifier
    assert restored.json()['name'] == index['name']
    assert client.get('/indices').json()['total'] == len(indices.SEEDS)


def test_remove_manual_preserves_all_other_collections(db, config, index):
    run(db, config, index)
    manual = indices.add_index(db, {'symbol': '^CUSTOM', 'name': 'Custom', 'currency': None,
                                  'return_variant': 'unknown'}, 'user')
    before = {name: list(db[name].find()) for name in db.list_collection_names() if name != 'indices'}
    indices.remove_index(db, manual['instrument_id'], 'user')
    indices.remove_index(db, index['instrument_id'], 'user')
    for name, records in before.items():
        assert list(db[name].find()) == records
    restored = indices.add_index(db, {'symbol': index['symbol'], 'name': 'Ignored'}, 'user')
    assert restored['summary']['quote_count'] > 0
    assert restored['verification_status'] == 'verified'
    assert indices.list_indices(db)['total'] == len(indices.SEEDS)
