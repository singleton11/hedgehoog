"""Offline tickerslist contract, including the bounded WEBN live sample shape."""
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from marketstack import MarketstackClient, MarketstackError
from test_marketstack import API_KEY, assert_safe, client_for, prohibit_network


WEBN_ROWS = [
    {"name": "Westpac Banking Corp", "ticker": "WEBNF", "has_intraday": False, "has_eod": True, "stock_exchange": {"name": "OTC PINK CURRENT", "acronym": "", "mic": "PINC"}},
    {"name": "Westpac Banking Corp", "ticker": "WEBNF", "has_intraday": True, "has_eod": False, "stock_exchange": {"name": "INVESTORS EXCHANGE", "acronym": "IEX", "mic": "IEXG"}},
    {"name": "", "ticker": "WEBN.DU", "has_intraday": False, "has_eod": True, "stock_exchange": {"name": "BOERSE DUESSELDORF - QUOTRIX MTF", "acronym": "", "mic": "DUSD"}},
    {"name": "", "ticker": "WEBND.XD", "has_intraday": False, "has_eod": True, "stock_exchange": {"name": "CBOE EUROPE - DXE ORDER BOOKS (NL)", "acronym": "CBOE EU DXE", "mic": "CEUX"}},
    {"name": "Amundi Prime All Country World UCITS ETF Acc", "ticker": "WEBN.DE", "has_intraday": False, "has_eod": True, "stock_exchange": {"name": "XETRA", "acronym": "XETRA", "mic": "XETR"}},
    {"name": "", "ticker": "WEBN.MU", "has_intraday": False, "has_eod": True, "stock_exchange": {"name": "BOERSE MUENCHEN", "acronym": "", "mic": "XMUN"}},
]
SAMPLE = {"pagination": {"limit": 1000, "offset": 0, "count": 6, "total": 6}, "data": WEBN_ROWS}


def test_tickerslist_path_params_and_first_named_exact_webn_match():
    from market_data import resolve_listing
    callback = Mock()

    def respond(request):
        callback.assert_called_once_with()
        assert request.url.path == "/v2/tickerslist"
        assert dict(request.url.params) == {"access_key": API_KEY, "search": "WEBN", "limit": "1000", "offset": "0"}
        return httpx.Response(200, json=SAMPLE)

    with MarketstackClient(API_KEY, callback, httpx.MockTransport(respond)) as client:
        listing, evidence = resolve_listing(client, "WEBN", "IE0003XJA0J9")
    assert listing == {"symbol": "WEBN.DE", "exchange": "XETR"}
    assert evidence["data"] == WEBN_ROWS[4]


@pytest.mark.parametrize("offset", [0, 6])
def test_one_page_per_call_preserves_envelope(offset):
    payload = deepcopy(SAMPLE)
    payload["pagination"].update(offset=offset, total=offset + 6)
    client, handler, callback = client_for(payload)
    with client:
        assert client.tickerslist_page("WEBN", offset=offset) == payload
    assert handler.call_count == callback.call_count == 1
    assert handler.call_args.args[0].url.params["offset"] == str(offset)


@pytest.mark.parametrize("key,value", [
    ("limit", 100), ("limit", True), ("offset", 1), ("offset", None),
    ("count", 5), ("count", "6"), ("count", 6.0), ("total", 5), ("total", -1),
])
def test_malformed_pagination_values_are_preserved(key, value):
    payload = deepcopy(SAMPLE)
    payload["pagination"][key] = value
    client, _, _ = client_for(payload)
    expected = deepcopy(payload)
    expected["pagination"][key] = str(value) if isinstance(value, float) else value
    with client:
        assert client.tickerslist_page("WEBN") == expected


@pytest.mark.parametrize("key", ["limit", "offset", "count", "total"])
def test_missing_pagination_fields_are_not_invented(key):
    payload = deepcopy(SAMPLE)
    del payload["pagination"][key]
    client, _, _ = client_for(payload)
    with client:
        assert client.tickerslist_page("WEBN") == payload


@pytest.mark.parametrize("payload", [
    {"data": []}, {"data": WEBN_ROWS}, {"data": [], "pagination": None},
    {"data": WEBN_ROWS, "pagination": []}, {"data": WEBN_ROWS, "pagination": "invalid"},
    {"data": WEBN_ROWS, "pagination": True}, {"data": WEBN_ROWS, "pagination": -1},
    {**SAMPLE, "data": [None] * 6},
    {"data": [], "pagination": {"limit": 1000, "offset": 0, "count": 0, "total": 1}},
])
def test_missing_malformed_or_inconsistent_pagination_preserves_download(payload):
    client, handler, callback = client_for(payload)
    with client:
        assert client.tickerslist_page("WEBN") == payload
    assert handler.call_count == callback.call_count == 1


@pytest.mark.parametrize("row", [None, [], ["unknown", None], {}, "invalid", 3, True, False, {"ticker": None, "stock_exchange": "invalid"}, {"ticker": "UNRELATED", "has_eod": "unknown"}])
def test_arbitrary_search_rows_are_preserved_without_validation(row):
    payload = deepcopy(SAMPLE)
    payload["data"][0] = row
    client, _, _ = client_for(payload)
    with client:
        assert client.tickerslist_page("WEBN") == payload


def test_more_rows_than_requested_limit_are_preserved():
    payload = {"data": WEBN_ROWS * 167, "pagination": {"count": 6, "total": 6}}
    client, handler, callback = client_for(payload)
    with client:
        assert client.tickerslist_page("WEBN") == payload
    assert handler.call_count == callback.call_count == 1


def test_exact_numeric_tokens_and_unknown_envelope_fields_are_preserved():
    raw = '''{"pagination":{"count":1.2300},"data":[0.10000000000000000000000000001,
        {"unknown":[1.2300e-7,1e3,4,true,null,NaN,Infinity,-Infinity]}],
        "provider_extra":{"ratio":0.00000000000000001}}'''
    client, _, _ = client_for(raw)
    with client:
        assert client.tickerslist_page("WEBN") == {
            "pagination": {"count": "1.2300"},
            "data": ["0.10000000000000000000000000001", {
                "unknown": ["0.00000012300", "1000", 4, True, None, "NaN", "Infinity", "-Infinity"],
            }],
            "provider_extra": {"ratio": "0.00000000000000001"},
        }


@pytest.mark.parametrize("payload", [
    {}, {"pagination": SAMPLE["pagination"]}, {**SAMPLE, "data": None},
    {**SAMPLE, "data": {}}, {**SAMPLE, "data": "invalid"},
    {**SAMPLE, "data": True}, {**SAMPLE, "data": 6},
])
def test_search_data_must_be_present_and_an_array(payload):
    client, _, _ = client_for(payload)
    with client, pytest.raises(MarketstackError) as caught:
        client.tickerslist_page("WEBN")
    assert_safe(caught.value, "malformed_response")


@pytest.mark.parametrize("search,offset", [(None, 0), ("", 0), (" ", 0), ("WEBN", -1), ("WEBN", True), ("WEBN", 1.0)])
def test_invalid_search_does_not_spend_quota(search, offset):
    client, handler, callback = client_for(SAMPLE)
    with client, pytest.raises(MarketstackError) as caught:
        client.tickerslist_page(search, offset=offset)
    assert_safe(caught.value, "invalid_request")
    callback.assert_not_called()
    handler.assert_not_called()


def test_search_errors_are_sanitized():
    client, _, _ = client_for({"error": {"type": "usage_limit_reached", "message": API_KEY}}, status=429)
    with client, pytest.raises(MarketstackError) as caught:
        client.tickerslist_page("WEBN")
    assert_safe(caught.value, "quota_exceeded")
