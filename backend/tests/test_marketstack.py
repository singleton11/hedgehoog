"""Offline contracts. Samples are copied from the three authorized live fixtures.

No test reads credentials or needs the original temporary fixture directory.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime
import json
import logging
import socket
import traceback
from unittest.mock import Mock

import httpx
import pytest

from marketstack import MarketstackClient, MarketstackError


API_KEY = "test-secret-do-not-log+/=&"
SYMBOL = "SXR8.DE"
EXCHANGE = "XETR"
START = "2026-09-01"
END = "2026-09-15"

TICKERINFO_JSON = """{
  "data": {
    "name": "iShares Core S&amp;P 500 UCITS ETF USD",
    "ticker": "SXR8.DE", "item_type": "etf", "sector": "", "industry": "",
    "exchange_code": "GER", "full_time_employees": "", "ipo_date": null,
    "date_founded": null, "key_executives": [], "incorporation": "",
    "incorporation_description": "", "start_fiscal": null, "end_fiscal": null,
    "reporting_currency": null,
    "address": {"city": "", "street1": "", "street2": "", "postal_code": "",
      "stateOrCountry": "", "state_or_country_description": ""},
    "post_address": {"city": "", "street1": "", "street2": "", "postal_code": "",
      "stateOrCountry": "", "state_or_country_description": ""},
    "phone": "", "website": "", "previous_names": [], "about": "",
    "mission": null, "vision": null, "stock_exchanges": []
  }
}"""

# The first two rows of eod_recent.json, with pagination adapted to the slice.
EOD_JSON = """{
  "pagination": {"limit": 1000, "offset": 0, "count": 2, "total": 2},
  "data": [
    {
      "open": 714.9, "high": 715.14, "low": 709.1, "close": 711.72,
      "volume": 43917.0, "adj_high": 715.14, "adj_low": 709.1,
      "adj_close": 711.72, "adj_open": 714.9, "adj_volume": null,
      "split_factor": 1.0, "dividend": 0.0,
      "name": "iShares Core S&amp;P 500 UCITS ETF USD", "exchange_code": null,
      "asset_type": null, "price_currency": null, "symbol": "SXR8.DE",
      "exchange": "XETR", "date": "2026-09-01T00:00:00+0000"
    },
    {
      "open": 711.5, "high": 714.32, "low": 708.9, "close": 713.8,
      "volume": 22032.0, "adj_high": 714.32, "adj_low": 708.9,
      "adj_close": 713.8, "adj_open": 711.5, "adj_volume": null,
      "split_factor": 1.0, "dividend": 0.0,
      "name": "iShares Core S&amp;P 500 UCITS ETF USD", "exchange_code": null,
      "asset_type": null, "price_currency": null, "symbol": "SXR8.DE",
      "exchange": "XETR", "date": "2026-09-02T00:00:00+0000"
    }
  ]
}"""

INCEPTION_ROW_JSON = """{
  "open": 596.08, "high": 596.78, "low": 594.8, "close": 595.22,
  "volume": 36635.0, "adj_high": null, "adj_low": null,
  "adj_close": 595.22, "adj_open": null, "adj_volume": null,
  "split_factor": 1.0, "dividend": 0.0,
  "name": "iShares Core S&amp;P 500 UCITS ETF USD", "exchange_code": null,
  "asset_type": null, "price_currency": null, "symbol": "SXR8.DE",
  "exchange": "XETR", "date": "2025-09-17T00:00:00+0000"
}"""


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in Marketstack tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def page():
    return json.loads(EOD_JSON)


def client_for(payload, *, status=200, before_request=None):
    content = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    handler = Mock(return_value=httpx.Response(status, content=content))
    callback = before_request if before_request is not None else Mock()
    client = MarketstackClient(API_KEY, callback, transport=httpx.MockTransport(handler))
    return client, handler, callback


def get_page(client, **overrides):
    params = dict(symbol=SYMBOL, exchange=EXCHANGE, date_from=START, date_to=END)
    params.update(overrides)
    return client.eod_page(**params)


def assert_safe(error, code):
    assert isinstance(error, MarketstackError)
    assert error.code == code
    assert str(error) == error.message
    assert error.args == (error.message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    diagnostic = "".join(traceback.format_exception(error))
    assert API_KEY not in diagnostic
    assert "https://api.marketstack.com" not in diagnostic
    assert "access_key" not in diagnostic
    assert "raw-provider-message" not in diagnostic


def test_tickerinfo_path_params_callback_and_all_metadata():
    events = []

    def handler(request):
        events.append("http")
        assert events == ["quota", "http"]
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.marketstack.com"
        assert request.url.path == "/v2/tickerinfo"
        assert dict(request.url.params) == {"access_key": API_KEY, "ticker": SYMBOL}
        assert request.extensions["timeout"] == {"connect": 5.0, "read": 30.0, "write": 10.0, "pool": 5.0}
        return httpx.Response(200, content=TICKERINFO_JSON)

    with MarketstackClient(API_KEY, lambda: events.append("quota"), httpx.MockTransport(handler)) as client:
        result = client.tickerinfo(SYMBOL)
    assert result == json.loads(TICKERINFO_JSON)["data"]
    assert result["exchange_code"] == "GER"  # Not XETR; not an error.
    assert result["reporting_currency"] is None
    assert "isin" not in result  # ISIN is optional here.


def test_eod_params_and_one_reserved_attempt_per_explicit_page(page):
    events = []

    def handler(request):
        assert events[-1] == "quota"
        events.append("http")
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.marketstack.com"
        assert request.url.path == "/v2/eod"
        expected = {
            "access_key": API_KEY, "symbols": SYMBOL, "exchange": EXCHANGE,
            "date_from": START, "date_to": END, "sort": "ASC", "limit": "1000",
            "offset": "0" if len(events) == 2 else "2",
        }
        assert dict(request.url.params) == expected
        response = deepcopy(page)
        response["pagination"].update(offset=int(expected["offset"]), total=4)
        return httpx.Response(200, json=response)

    with MarketstackClient(API_KEY, lambda: events.append("quota"), httpx.MockTransport(handler)) as client:
        first = get_page(client, date_from=date(2026, 9, 1), date_to=date(2026, 9, 15))
        assert first["pagination"]["total"] == 4
        assert events == ["quota", "http"]  # No automatic pagination.
        second = get_page(client, offset=2)
        assert second["pagination"]["offset"] == 2
    assert events == ["quota", "http", "quota", "http"]


def test_closes_owned_client_transport_on_success_and_error():
    class ClosingTransport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True

    for status in (200, 503):
        transport = ClosingTransport(lambda request: httpx.Response(status, content=TICKERINFO_JSON))
        try:
            with MarketstackClient(API_KEY, Mock(), transport) as client:
                client.tickerinfo(SYMBOL)
        except MarketstackError:
            assert status == 503
        assert transport.closed


def test_default_transport_disables_retries_and_environment(monkeypatch):
    factory = Mock(return_value=httpx.MockTransport(lambda request: httpx.Response(200, content=TICKERINFO_JSON)))
    monkeypatch.setattr(httpx, "HTTPTransport", factory)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    with MarketstackClient(API_KEY, Mock()) as client:
        client.tickerinfo(SYMBOL)
        assert client._client.trust_env is False
        assert client._client.follow_redirects is False
    factory.assert_called_once_with(retries=0, trust_env=False)


def test_callback_failure_prevents_attempt_and_propagates_unchanged():
    failure = RuntimeError("Local quota reservation failed")
    callback = Mock(side_effect=failure)
    client, handler, _ = client_for(TICKERINFO_JSON, before_request=callback)
    with client, pytest.raises(RuntimeError) as caught:
        client.tickerinfo(SYMBOL)
    assert caught.value is failure
    callback.assert_called_once_with()
    handler.assert_not_called()


def test_full_envelope_and_nullable_adjusted_fields_are_preserved():
    payload = '{"pagination":{"limit":1000,"offset":0,"count":1,"total":1},"data":[' + INCEPTION_ROW_JSON + '],"provider_extra":{"version":2}}'
    client, _, _ = client_for(payload)
    with client:
        result = get_page(client, date_from="2010-05-19")
    row = result["data"][0]
    assert set(row) == set(json.loads(INCEPTION_ROW_JSON))
    assert row["open"] == "596.08"
    assert row["volume"] == "36635.0"
    assert row["adj_open"] is None
    assert row["adj_high"] is None
    assert row["adj_low"] is None
    assert row["adj_close"] == "595.22"
    assert row["adj_volume"] is None
    assert row["price_currency"] is None
    assert result["provider_extra"] == {"version": 2}
    # The client does not invent pre-truncation history or declare completeness.
    assert row["date"] == "2025-09-17T00:00:00+0000"


def test_missing_price_currency_and_adjusted_fields_are_not_invented(page):
    for row in page["data"]:
        for key in ("price_currency", "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume"):
            del row[key]
        row["volume"] = None
    client, _, _ = client_for(page)
    with client:
        result = get_page(client)
    for row in result["data"]:
        assert row["volume"] is None
        assert "price_currency" not in row
        assert not any(key.startswith("adj_") for key in row)


def test_exact_decimal_tokens_unknown_nested_fields_and_integer_tokens():
    raw = EOD_JSON.replace('"open": 714.9', '"open": 714.9000000000000000001234567890123456789', 1)
    raw = raw.replace('"volume": 43917.0', '"volume": 9007199254740993', 1)
    raw = raw.replace('"high": 715.14', '"high": 716', 1)
    raw = raw.replace('"asset_type": null', '"asset_type": null, "unknown": {"nested": [0.10000000000000000000000000001, 1.2300e-7, 1e3, 4, true, null]}', 1)
    client, _, _ = client_for(raw)
    with client:
        result = get_page(client)
    row = result["data"][0]
    assert row["open"] == "714.9000000000000000001234567890123456789"
    assert row["high"] == 716 and type(row["high"]) is int
    assert row["volume"] == 9007199254740993 and type(row["volume"]) is int
    assert row["unknown"]["nested"] == ["0.10000000000000000000000000001", "0.00000012300", "1000", 4, True, None]
    assert result["pagination"] == {"limit": 1000, "offset": 0, "count": 2, "total": 2}


def test_exact_decimals_also_apply_to_metadata():
    raw = '{"data":{"ticker":"SXR8.DE","unknown":[1.2345678901234567890,3],"tiny":0.00000000000000001}}'
    client, _, _ = client_for(raw)
    with client:
        assert client.tickerinfo(SYMBOL) == {
            "ticker": SYMBOL, "unknown": ["1.2345678901234567890", 3],
            "tiny": "0.00000000000000001",
        }


@pytest.mark.parametrize("data", [None, [], {}, "", "invalid", True, False, 0, 123, [None, {"unknown": "value"}], {"name": "Missing ticker"}, {"ticker": None}, {"ticker": 123}, {"ticker": "SXR8"}, {"ticker": "SXR8.DE "}])
def test_any_metadata_data_shape_is_preserved_without_identity_validation(data):
    client, handler, callback = client_for({"data": data, "provider_extra": "not part of data"})
    with client:
        result = client.tickerinfo(SYMBOL)
    assert result == data
    assert type(result) is type(data)
    callback.assert_called_once_with()
    assert handler.call_count == 1


@pytest.mark.parametrize("payload", [{}, {"ticker": "OTHER.DE"}, {"unknown": [None, "metadata"], "pagination": None}])
def test_metadata_without_data_key_preserves_whole_payload(payload):
    client, _, _ = client_for(payload)
    with client:
        assert client.tickerinfo(SYMBOL) == payload


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_metadata_tokens_are_preserved_as_strings(constant):
    client, _, _ = client_for('{"data":{"ticker":"SXR8.DE","unknown":[' + constant + ']}}')
    with client:
        assert client.tickerinfo(SYMBOL) == {"ticker": SYMBOL, "unknown": [constant]}


@pytest.mark.parametrize("endpoint", ["eod", "tickerinfo", "tickerslist"])
def test_huge_integer_tokens_are_preserved_as_strings_beyond_bson_signed64(endpoint):
    values = [-(10**100), -(2**63) - 1, -(2**63), 9007199254740993, 2**63 - 1, 2**63, 10**100]
    expected = [str(-(10**100)), str(-(2**63) - 1), -(2**63), 9007199254740993, 2**63 - 1, str(2**63), str(10**100)]
    payload = {"data": [{"unknown": {"nested": values}}], "provider_extra": 10**100}
    client, _, _ = client_for(payload)
    with client:
        if endpoint == "eod":
            result = get_page(client)
        elif endpoint == "tickerslist":
            result = client.tickerslist_page(SYMBOL)
        else:
            result = {"data": client.tickerinfo(SYMBOL)}
    actual = result["data"][0]["unknown"]["nested"]
    assert actual == expected
    assert [type(value) for value in actual] == [str, str, int, int, int, str, str]
    if endpoint != "tickerinfo":
        assert result["provider_extra"] == str(10**100)


def test_case_normalization_is_allowed_without_rewriting_provider_fields(page):
    client, _, _ = client_for({"data": {"ticker": "sxr8.de"}})
    with client:
        assert client.tickerinfo(SYMBOL)["ticker"] == "sxr8.de"
    for row in page["data"]:
        row.update(symbol="sxr8.de", exchange="xetr")
    client, _, _ = client_for(page)
    with client:
        assert get_page(client)["data"][0]["exchange"] == "xetr"


@pytest.mark.parametrize("status", [200, 400, 503])
@pytest.mark.parametrize("provider_type,code", [
    ("invalid_access_key", "authentication_error"),
    ("missing_access_key", "authentication_error"),
    ("inactive_user", "authentication_error"),
    ("usage_limit_reached", "quota_exceeded"),
    ("rate_limit_reached", "quota_exceeded"),
    ("function_access_restricted", "plan_restricted"),
    ("https_access_restricted", "plan_restricted"),
    ("invalid_symbol", "invalid_symbol"),
    ("invalid_symbols", "invalid_symbol"),
])
def test_provider_errors_on_success_and_non_success_http_are_safe(status, provider_type, code):
    client, handler, callback = client_for({
        "error": {"type": provider_type, "message": f"raw-provider-message {API_KEY}",
                  "context": {"url": f"https://api.marketstack.com/v2/eod?access_key={API_KEY}"}},
    }, status=status)
    with client, pytest.raises(MarketstackError) as caught:
        client.tickerinfo(SYMBOL)
    assert_safe(caught.value, code)
    assert handler.call_count == callback.call_count == 1


@pytest.mark.parametrize("provider_type", [API_KEY, "raw-provider-message", None, 101, True, [], {}, {"secret": API_KEY}])
def test_arbitrary_provider_error_type_is_not_reflected(provider_type):
    client, _, _ = client_for({"error": {"type": provider_type, "message": API_KEY}})
    with client, pytest.raises(MarketstackError) as caught:
        get_page(client)
    assert_safe(caught.value, "http_error")


@pytest.mark.parametrize("status,code", [(401, "authentication_error"), (402, "plan_restricted"), (403, "plan_restricted"), (429, "quota_exceeded"), (404, "http_error"), (500, "http_error"), (503, "http_error")])
@pytest.mark.parametrize("body", ["not JSON raw-provider-message", {"data": {"ticker": SYMBOL}}])
def test_http_status_errors_do_not_depend_on_valid_error_json(status, code, body):
    client, _, _ = client_for(body, status=status)
    with client, pytest.raises(MarketstackError) as caught:
        client.tickerinfo(SYMBOL)
    assert_safe(caught.value, code)


@pytest.mark.parametrize("body", ["", "broken " + API_KEY, b"\xff", "[]", "null", "42", "true", {"error": None}, {"error": []}, {"error": API_KEY}])
def test_malformed_json_and_nonobject_or_error_envelopes_are_safe(body):
    client, _, _ = client_for(body)
    with client, pytest.raises(MarketstackError) as caught:
        client.tickerinfo(SYMBOL)
    assert_safe(caught.value, "malformed_response")


@pytest.mark.parametrize("exception_type", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.DecodingError, OSError])
def test_transport_failures_are_not_retried_or_chained(exception_type, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        # Simulate httpcore debug logging an unsafe underlying exception.
        logging.getLogger("httpcore.connection").debug("raw-provider-message %s", API_KEY)
        raise exception_type(f"raw-provider-message {request.url} {API_KEY}")

    callback = Mock()
    caplog.set_level(logging.DEBUG)
    with MarketstackClient(API_KEY, callback, httpx.MockTransport(handler)) as client:
        with pytest.raises(MarketstackError) as caught:
            get_page(client)
    assert_safe(caught.value, "transport_error")
    assert len(calls) == callback.call_count == 1
    assert API_KEY not in caplog.text
    assert "raw-provider-message" not in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_http_request_logs_are_suppressed_but_unrelated_logging_is_not(caplog, level):
    caplog.set_level(level)
    client, _, _ = client_for(TICKERINFO_JSON)
    with client:
        client.tickerinfo(SYMBOL)
    logging.getLogger("httpx").info("unrelated HTTP client log")
    logging.getLogger("httpcore.connection").debug("unrelated transport log")
    assert "unrelated HTTP client log" in caplog.text
    assert ("unrelated transport log" in caplog.text) == (level == logging.DEBUG)
    assert API_KEY not in caplog.text
    assert "access_key" not in caplog.text
    assert "api.marketstack.com" not in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_suppression_covers_httpcore_but_is_scoped_to_request_context(caplog, level):
    caplog.set_level(level)

    def handler(request):
        for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.connection_pool", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
            logging.getLogger(name).log(level, "raw-provider-message %s %s", API_KEY, request.url)
        # A simultaneous unrelated request must not lose its logs.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(logging.getLogger("httpx").log, level, "unrelated thread log").result()
        return httpx.Response(200, content=TICKERINFO_JSON)

    with MarketstackClient(API_KEY, Mock(), httpx.MockTransport(handler)) as client:
        client.tickerinfo(SYMBOL)
    assert "unrelated thread log" in caplog.text
    assert "raw-provider-message" not in caplog.text
    assert API_KEY not in caplog.text
    assert "api.marketstack.com" not in caplog.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed(status):
    handler = Mock(return_value=httpx.Response(status, headers={"location": "https://untrusted.invalid/"}, content=API_KEY))
    callback = Mock()
    with MarketstackClient(API_KEY, callback, httpx.MockTransport(handler)) as client:
        with pytest.raises(MarketstackError) as caught:
            get_page(client)
    assert_safe(caught.value, "http_error")
    assert handler.call_count == callback.call_count == 1


@pytest.mark.parametrize("key", ["limit", "offset", "count", "total"])
@pytest.mark.parametrize("value", [None, True, "2", 2.0, -1])
def test_arbitrary_pagination_field_values_are_preserved(page, key, value):
    page["pagination"][key] = value
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("update", [
    {"limit": 0}, {"limit": 1001}, {"limit": 100}, {"offset": 1},
    {"count": 1}, {"count": 3}, {"total": 1}, {"total": 0},
])
def test_inconsistent_pagination_counts_bounds_and_offsets_are_preserved(page, update):
    page["pagination"].update(update)
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("key", ["limit", "offset", "count", "total"])
def test_missing_pagination_fields_are_not_invented(page, key):
    del page["pagination"][key]
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("value", [None, [], "bad", True, 3])
def test_arbitrary_pagination_shapes_are_preserved(page, value):
    page["pagination"] = value
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("value", [None, {}, "bad", True, 3, 2.0])
def test_eod_data_must_be_an_array(page, value):
    page["data"] = value
    client, _, _ = client_for(page)
    with client, pytest.raises(MarketstackError) as caught:
        get_page(client)
    assert_safe(caught.value, "malformed_response")


def test_eod_data_key_is_required(page):
    del page["data"]
    client, _, _ = client_for(page)
    with client, pytest.raises(MarketstackError) as caught:
        get_page(client)
    assert_safe(caught.value, "malformed_response")


def test_missing_pagination_is_not_invented(page):
    del page["pagination"]
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("offset,total", [(0, 0), (2, 2), (0, 1), (2, 3), (3, 2)])
def test_empty_page_is_preserved_regardless_of_reported_total(offset, total):
    payload = {"pagination": {"limit": 1000, "offset": offset, "count": 0, "total": total}, "data": []}
    client, handler, callback = client_for(payload)
    with client:
        assert get_page(client, offset=offset) == payload
    callback.assert_called_once_with()
    assert handler.call_count == 1


def test_more_rows_than_requested_limit_are_preserved(page):
    page["data"] = page["data"] * 501
    page["pagination"].update(count=1002, total=1002)
    client, handler, callback = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)
    assert handler.call_count == callback.call_count == 1


@pytest.mark.parametrize("key,value", [
    ("symbol", "SXR8"), ("symbol", "OTHER.DE"), ("symbol", None), ("symbol", 123),
    ("exchange", "GER"), ("exchange", "XLON"), ("exchange", None), ("exchange", {}),
    ("date", "2026-08-31T00:00:00+0000"), ("date", "2026-09-16T00:00:00+0000"),
    ("date", "2026-09-01garbage"), ("date", "2026-09-01Tnot-a-time"),
    ("date", "2026-09-31T00:00:00+0000"), ("date", None), ("date", 123),
    ("date", "2026-09-01T99:00:00+0000"), ("date", "20260901"),
])
def test_arbitrary_symbol_mic_and_date_values_are_preserved(page, key, value):
    page["data"][0][key] = value
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("row", [None, [], [None, "unknown"], {}, "invalid", 3, 1.25, True, False])
def test_arbitrary_row_shapes_are_preserved(page, row):
    page["data"][0] = row
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("date_value", ["2026-09-01", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00+00:00"])
def test_valid_iso_dates_are_preserved_verbatim(page, date_value):
    page["data"][0]["date"] = date_value
    client, _, _ = client_for(page)
    with client:
        assert get_page(client)["data"][0]["date"] == date_value


@pytest.mark.parametrize("case", ["duplicate", "same-day-different-time", "descending"])
def test_duplicate_or_unordered_dates_are_not_sorted_or_dropped(page, case):
    if case == "descending":
        page["data"].reverse()
    else:
        page["data"][1]["date"] = "2026-09-01T01:00:00Z" if case == "same-day-different-time" else page["data"][0]["date"]
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("key", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [None, True, False, "714.0", 0, -1, -0.5, [], {}])
def test_arbitrary_raw_ohlc_values_are_preserved(page, key, value):
    page["data"][0][key] = value
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


def test_webn_null_close_does_not_discard_the_row_or_page(page):
    for row in page["data"]:
        row["symbol"] = "WEBN.DE"
    page["data"][0]["close"] = None
    client, handler, callback = client_for(page)
    with client:
        result = get_page(client, symbol="WEBN.DE")
    assert result == json.loads(json.dumps(page), parse_float=str)
    assert result["data"][0]["close"] is None
    assert len(result["data"]) == 2
    assert handler.call_count == callback.call_count == 1


@pytest.mark.parametrize("key", ["open", "high", "low", "close", "volume", "date", "symbol", "exchange"])
def test_missing_raw_fields_are_not_invented(page, key):
    del page["data"][0][key]
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("update", [
    {"open": 708}, {"open": 716}, {"close": 708}, {"close": 716},
    {"low": 716}, {"high": 708},
])
def test_inconsistent_ohlc_relationships_are_preserved_not_repaired_or_page_fatal(page, update):
    page["data"][0].update(update)
    client, _, _ = client_for(page)
    with client:
        result = get_page(client)
    assert len(result["data"]) == 2
    for key, value in update.items():
        assert result["data"][0][key] == value
    assert result["data"][0]["date"] == page["data"][0]["date"]


@pytest.mark.parametrize("volume", [None, 0, 0.0, 1, 0.5, 9007199254740993])
def test_volume_may_be_null_or_finite_nonnegative_number(page, volume):
    page["data"][0]["volume"] = volume
    client, _, _ = client_for(page)
    with client:
        result = get_page(client)["data"][0]["volume"]
    assert result == (str(volume) if isinstance(volume, float) else volume)


@pytest.mark.parametrize("volume", [-1, -0.5, True, "0", "NaN", {}, []])
def test_arbitrary_volume_values_are_preserved(page, volume):
    page["data"][0]["volume"] = volume
    client, _, _ = client_for(page)
    with client:
        assert get_page(client) == json.loads(json.dumps(page), parse_float=str)


@pytest.mark.parametrize("key", ["open", "high", "low", "close", "volume", "adj_open", "adj_volume"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_price_and_volume_tokens_are_preserved_as_strings(key, constant):
    page = json.loads(EOD_JSON)
    page["data"][0][key] = "REPLACE_NUMBER"
    raw = json.dumps(page).replace('"REPLACE_NUMBER"', constant)
    client, _, _ = client_for(raw)
    with client:
        result = get_page(client)
    assert result == json.loads(raw, parse_float=str, parse_constant=str)
    assert result["data"][0][key] == constant


def test_equal_ohlc_is_valid_and_integer_prices_are_preserved(page):
    page["data"][0].update(open=10, high=10, low=10, close=10)
    client, _, _ = client_for(page)
    with client:
        row = get_page(client)["data"][0]
    assert all(type(row[key]) is int and row[key] == 10 for key in ("open", "high", "low", "close"))


@pytest.mark.parametrize("overrides", [
    {"offset": -1}, {"offset": True}, {"offset": 0.0}, {"offset": "0"},
    {"date_from": "2026-09-31"}, {"date_from": "2026-10-01"},
    {"date_from": "20260901"}, {"date_to": "2026-09-15garbage"},
    {"date_from": datetime(2026, 9, 1)}, {"date_to": None},
    {"symbol": ""}, {"symbol": None}, {"exchange": ""}, {"exchange": None},
])
def test_invalid_request_does_not_reserve_quota_or_attempt_http(overrides):
    client, handler, callback = client_for(EOD_JSON)
    with client, pytest.raises(MarketstackError) as caught:
        get_page(client, **overrides)
    assert_safe(caught.value, "invalid_request")
    callback.assert_not_called()
    handler.assert_not_called()
