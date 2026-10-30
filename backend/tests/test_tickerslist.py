"""Yahoo search equivalents of the retired tickerslist matrices; synthetic HTTP only."""
from copy import deepcopy
import json
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from market_provider import MarketDataError
from yahoo_finance import YahooFinanceClient
from test_marketstack import assert_safe, client_for, prohibit_network, wire_safe
from test_yahoo_finance import listing, no_live_http, transport


WEBN_ROWS = [
    listing("WEBNF", "PNK", longname="Unrelated Bank", quoteType="EQUITY"),
    listing("WEBNF", "IEX", longname="Unrelated Bank", quoteType="EQUITY"),
    listing("WEBN.F", "FRA", longname=""),
    listing("WEBND.XD", "DXE", longname=""),
    listing("WEBN.DE", "GER", longname="Example All Country World Fund"),
    listing("WEBN.MU", "MUN", longname=""),
]
SAMPLE = {"quotes": WEBN_ROWS, "pagination": {"limit": 1000, "offset": 0, "count": 6, "total": 6}}


def test_search_path_params_and_exact_webn_match_not_first_named_venue(transport):
    state, calls = transport
    state["search"] = deepcopy(SAMPLE)
    callback = Mock()
    with YahooFinanceClient(before_request=callback) as client:
        page = client.tickerslist_page("WEBN")
        assert page["data"] == WEBN_ROWS and page["raw"] == SAMPLE
        # Multiple exact roots are ambiguous, even if only one venue has a name.
        with pytest.raises(MarketDataError) as caught:
            client.resolve_listing("WEBN", "")
        assert_safe(caught.value, "listing_not_found")
        selected, evidence = client.resolve_listing("WEBN.DE", "IE0003XJA0J9")
    assert selected == {"symbol": "WEBN.DE", "exchange": "XETR"}
    assert evidence["data"] == WEBN_ROWS[4] and evidence["raw"] == SAMPLE
    searches = [call for call in calls if urlsplit(call[1]).path.endswith("/search")]
    assert len(searches) == 3 and len(calls) == callback.call_count == 5
    for call, query in zip(searches, ("WEBN", "WEBN", "WEBN.DE")):
        assert call[0] == "GET" and urlsplit(call[1]).path == "/v1/finance/search"
        assert {key: value for key, value in call[2]["params"].items() if key != "crumb"} == {
            "q": query, "quotesCount": 50, "newsCount": 0, "listsCount": 0, "enableFuzzyQuery": False}
        assert call[2]["allow_redirects"] is False


@pytest.mark.parametrize("offset", [0, 6])
def test_complete_search_per_call_and_nonzero_offset_is_offline(transport, offset):
    client, calls, callback = client_for(transport, SAMPLE, phase="search")
    with client:
        if offset:
            with pytest.raises(MarketDataError) as caught:
                client.tickerslist_page("WEBN", offset=offset)
            assert_safe(caught.value, "invalid_request")
            assert calls == []
            callback.assert_not_called()
        else:
            page = client.tickerslist_page("WEBN", offset=offset)
            assert page["data"] == WEBN_ROWS and page["raw"] == SAMPLE
            assert page["pagination"] == {"offset": 0, "count": 6, "total": 6, "limit": 6}
            assert len(calls) == callback.call_count == 3


@pytest.mark.parametrize("key,value", [
    ("limit", 100), ("limit", True), ("offset", 1), ("offset", None),
    ("count", 5), ("count", "6"), ("count", 6.0), ("total", 5), ("total", -1),
])
def test_malformed_pagination_values_are_preserved_raw_not_used_as_cursor(transport, key, value):
    payload = deepcopy(SAMPLE)
    payload["pagination"][key] = value
    client, _, _ = client_for(transport, payload, phase="search")
    with client:
        page = client.tickerslist_page("WEBN")
    assert page["raw"] == wire_safe(payload)
    assert page["data"] == WEBN_ROWS and page["pagination"]["total"] == 6


@pytest.mark.parametrize("key", ["limit", "offset", "count", "total"])
def test_missing_raw_pagination_fields_are_not_invented(transport, key):
    payload = deepcopy(SAMPLE)
    del payload["pagination"][key]
    client, _, _ = client_for(transport, payload, phase="search")
    with client:
        page = client.tickerslist_page("WEBN")
    assert page["raw"] == payload and key not in page["raw"]["pagination"]
    assert page["pagination"] == {"limit": 6, "offset": 0, "count": 6, "total": 6}


@pytest.mark.parametrize("payload", [
    {"quotes": []}, {"quotes": WEBN_ROWS}, {"quotes": [], "pagination": None},
    {"quotes": WEBN_ROWS, "pagination": []}, {"quotes": WEBN_ROWS, "pagination": "invalid"},
    {"quotes": WEBN_ROWS, "pagination": True}, {"quotes": WEBN_ROWS, "pagination": -1},
    {**SAMPLE, "quotes": [None] * 6},
    {"quotes": [], "pagination": {"limit": 1000, "offset": 0, "count": 0, "total": 1}},
])
def test_missing_malformed_or_inconsistent_pagination_preserves_download(transport, payload):
    client, calls, callback = client_for(transport, payload, phase="search")
    with client:
        page = client.tickerslist_page("WEBN")
    assert page["raw"] == payload and page["data"] == payload["quotes"]
    assert page["pagination"]["total"] == len(payload["quotes"])
    assert len(calls) == callback.call_count == 3


@pytest.mark.parametrize("row", [None, [], ["unknown", None], {}, "invalid", 3, True, False, {"symbol": None, "exchange": "invalid"}, {"symbol": "UNRELATED", "quoteType": "unknown"}])
def test_arbitrary_search_rows_are_preserved_but_not_resolvable(transport, row):
    payload = {"quotes": [row]}
    client, _, _ = client_for(transport, payload, phase="search")
    with client:
        page = client.tickerslist_page("WEBN")
        assert page["data"] == [row] and page["raw"] == payload
        with pytest.raises(MarketDataError) as caught:
            client.resolve_listing("WEBN", "")
    assert_safe(caught.value, "listing_not_found")


def test_more_rows_than_requested_limit_are_preserved(transport):
    payload = {"quotes": WEBN_ROWS * 167, "pagination": {"count": 6, "total": 6}}
    client, calls, callback = client_for(transport, payload, phase="search")
    with client:
        page = client.tickerslist_page("WEBN")
    assert page["raw"] == payload and page["data"] == payload["quotes"]
    assert page["pagination"]["total"] == 1002
    assert len(calls) == callback.call_count == 3


def test_exact_numeric_tokens_and_unknown_envelope_fields_are_preserved(transport):
    raw = '''{"pagination":{"count":1.2300},"quotes":[0.10000000000000000000000000001,
        {"unknown":[1.2300e-7,1e3,4,true,null,NaN,Infinity,-Infinity]}],
        "provider_extra":{"ratio":0.00000000000000001}}'''
    client, _, _ = client_for(transport, raw, phase="search")
    with client:
        page = client.tickerslist_page("WEBN")
    assert page["raw"] == {
        "pagination": {"count": "1.2300"},
        "quotes": ["0.10000000000000000000000000001", {
            "unknown": ["0.00000012300", "1000", 4, True, None, "NaN", "Infinity", "-Infinity"],
        }],
        "provider_extra": {"ratio": "0.00000000000000001"},
    }
    assert page["data"] == page["raw"]["quotes"]
    assert page["pagination"] == {"offset": 0, "count": 2, "total": 2, "limit": 2}


@pytest.mark.parametrize("payload", [
    {}, {"pagination": SAMPLE["pagination"]}, {**SAMPLE, "quotes": None},
    {**SAMPLE, "quotes": {}}, {**SAMPLE, "quotes": "invalid"},
    {**SAMPLE, "quotes": True}, {**SAMPLE, "quotes": 6},
])
def test_search_quotes_must_be_present_and_an_array(transport, payload):
    client, _, _ = client_for(transport, payload, phase="search")
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerslist_page("WEBN")
    assert_safe(caught.value, "malformed_response")


@pytest.mark.parametrize("search,offset", [(None, 0), ("", 0), (" ", 0), ("WEBN", -1), ("WEBN", True), ("WEBN", 1.0), ("WEBN", 6)])
def test_invalid_search_does_not_spend_quota(transport, search, offset):
    client, calls, callback = client_for(transport, SAMPLE, phase="search")
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerslist_page(search, offset=offset)
    assert_safe(caught.value, "invalid_request")
    callback.assert_not_called()
    assert calls == []


def test_search_errors_are_sanitized(transport):
    client, _, _ = client_for(transport, {"finance": {"error": {"code": "Too Many Requests", "description": "raw-provider-message"}}}, status=429, phase="search")
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerslist_page("WEBN")
    assert_safe(caught.value, "quota_exceeded")
