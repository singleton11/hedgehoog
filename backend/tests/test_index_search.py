"""Search is a read-only Yahoo candidate lookup, not index verification."""
from unittest.mock import Mock

import market_data as md
from market_provider import MarketDataError
from test_indices import login
from test_market_data import api, db, config, clock


def provider(monkeypatch, rows=None, error=None):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.tickerslist_page.return_value = {"data": rows or []}
    client.tickerslist_page.side_effect = error
    factory = Mock(return_value=client)
    monkeypatch.setattr(md, "provider_client", factory)
    return factory, client


def test_search_auth_validation_and_no_implicit_network(api, monkeypatch):
    client, _ = api
    factory, _ = provider(monkeypatch)
    assert client.get('/indices/search?q=World').status_code == 401
    login(client)
    for query in ('a', '  ', 'x' * 101):
        assert client.get('/indices/search', params={'q': query}).status_code == 422
    assert client.get('/indices/search?q=World&limit=21').status_code == 422
    factory.assert_not_called()


def test_search_filters_deduplicates_and_leaves_semantics_unknown(api, db, monkeypatch):
    client, _ = api
    rows = [
        {'symbol': 'ETF', 'shortname': 'MSCI World net ETF', 'quoteType': 'ETF'},
        {'symbol': '^WORLD', 'longname': 'MSCI World Net', 'quoteType': 'INDEX', 'currency': 'USD'},
        {'symbol': '^WORLD', 'shortname': 'Duplicate', 'quoteType': 'INDEX'},
        {'symbol': '^OTHER', 'shortname': 'World', 'quoteType': 'INDEX', 'currency': 'bad'},
        {'symbol': '^BAD,ONE', 'shortname': 'Bad', 'quoteType': 'INDEX'},
        {'symbol': '^MISSING', 'shortname': 'No type'},
        {'symbol': '^EMPTY', 'shortname': ' ', 'quoteType': 'INDEX'}, None,
    ]
    factory, source = provider(monkeypatch, rows)
    login(client)
    before = db.indices.count_documents({})
    response = client.get('/indices/search', params={'q': ' MSCI World net '})
    assert response.status_code == 200
    assert response.json()['items'] == [
        {'symbol': '^WORLD', 'name': 'MSCI World Net', 'currency': 'USD', 'return_variant': 'unknown'},
        {'symbol': '^OTHER', 'name': 'World', 'currency': None, 'return_variant': 'unknown'},
    ]
    source.tickerslist_page.assert_called_once_with('MSCI World net')
    assert factory.call_args.args[1] == 'yfinance'
    source.__exit__.assert_called_once()
    assert db.indices.count_documents({}) == before
    assert db.acquisition_jobs.count_documents({}) == 0
    assert len(client.get('/indices/search?q=World&limit=1').json()['items']) == 1


def test_search_empty_and_safe_provider_failure(api, monkeypatch):
    client, _ = api
    login(client)
    _, source = provider(monkeypatch)
    assert client.get('/indices/search?q=World').json() == {'items': []}
    source.tickerslist_page.side_effect = MarketDataError('quota_exceeded', 'private provider details')
    response = client.get('/indices/search?q=World')
    assert response.status_code == 503
    assert 'private' not in response.text
    assert 'manual entry' in response.text
