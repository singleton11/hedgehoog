"""Provider-neutral adapter matrices, migrated to pinned Yahoo's final HTTP boundary.

Marketstack remains retired. All responses and session secrets here are synthetic.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
import logging
import socket
import traceback
from unittest.mock import Mock
from urllib.parse import urlsplit

from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import (
    ConnectionError, DNSError, HTTPError, ProxyError, SSLError, Timeout,
)
import pytest

from market_provider import MarketDataError
from marketstack import MarketstackClient
from yahoo_finance import YahooFinanceClient
from test_yahoo_finance import (
    END, START, SYMBOL, chart, listing, no_live_http, response, stamp, transport,
)


API_KEY = "test-secret-do-not-log+/=&"
EXCHANGE = "XLON"


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in adapter tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def page():
    return chart()


def wire_safe(payload):
    """Expected lossless, BSON-safe values, independent of adapter internals."""
    raw = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return json.loads(raw, parse_float=lambda value: format(Decimal(value), "f"),
                      parse_int=lambda value: int(value) if -(2**63) <= int(value) < 2**63 else value,
                      parse_constant=str)


def client_for(transport, payload, *, status=200, before_request=None, phase="meta"):
    state, calls = transport
    callback = before_request if before_request is not None else Mock()

    def handler(session, method, url, kwargs):
        target = ((phase == "meta" and "/chart/" in url and "period1" not in kwargs.get("params", {}))
                  or (phase == "history" and "period1" in kwargs.get("params", {}))
                  or (phase == "search" and url.endswith("/search")))
        if target:
            body = payload.encode() if isinstance(payload, str) else payload
            return response(url, body, status)

    state["handler"] = handler
    return YahooFinanceClient(api_key=API_KEY, before_request=callback), calls, callback


def get_page(client, **overrides):
    params = dict(symbol=SYMBOL, exchange=EXCHANGE, date_from=START, date_to=END)
    params.update(overrides)
    return client.eod_page(**params)


def assert_safe(error, code):
    assert isinstance(error, MarketDataError)
    assert error.code == code
    assert str(error) == error.message
    assert error.args == (error.message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    diagnostic = "".join(traceback.format_exception(error))
    for private in (API_KEY, "https://private.invalid", "access_key", "raw-provider-message", "private-cookie", "private-crumb"):
        assert private not in diagnostic


def test_retired_marketstack_constructor_never_reserves_or_requests(monkeypatch):
    callback = Mock()
    final_http = Mock(side_effect=AssertionError("Retired provider attempted HTTP"))
    monkeypatch.setattr(Session, "request", final_http)
    with pytest.raises(MarketDataError) as caught:
        MarketstackClient(API_KEY, callback)
    assert_safe(caught.value, "provider_retired")
    callback.assert_not_called()
    final_http.assert_not_called()


def test_tickerinfo_path_params_callback_and_all_metadata(transport):
    state, calls = transport
    metadata = state["meta"]["chart"]["result"][0]["meta"]
    metadata.update(sector="", industry="", ipo_date=None, executives=[],
                    address={"city": "", "postal_code": ""}, reporting_currency=None)
    events = []

    def handler(session, method, url, kwargs):
        assert events[-1] == "quota"
        events.append("http")
        assert method == "GET" and urlsplit(url).scheme == "https"
        assert urlsplit(url).hostname.endswith(".yahoo.com")
        assert kwargs["timeout"] == 7 and kwargs["allow_redirects"] is False
        assert "access_key" not in kwargs.get("params", {})
        if "/chart/" in url:
            assert urlsplit(url).path == f"/v8/finance/chart/{SYMBOL}"
            assert {key: value for key, value in kwargs["params"].items() if key != "crumb"} == {
                "range": "1d", "interval": "1d"}

    state["handler"] = handler
    with YahooFinanceClient(API_KEY, lambda: events.append("quota"), timeout=7) as client:
        result = client.tickerinfo(SYMBOL)
    assert events == ["quota", "http"] * 3
    assert len(calls) == 3
    for key, value in metadata.items():
        assert result[key] == value
    assert result["exchangeName"] == "LSE" and result["reporting_currency"] is None
    assert result["profile_source"] == "chart_metadata"
    assert "isin" not in result
    assert API_KEY not in json.dumps(calls)


def test_eod_params_and_one_reserved_attempt_per_explicit_page(transport):
    _, calls = transport
    callback = Mock()
    with YahooFinanceClient(before_request=callback) as client:
        first = get_page(client, date_from=date.fromisoformat(START), date_to=date.fromisoformat(END))
        assert first["pagination"] == {"limit": 2, "offset": 0, "count": 2, "total": 2}
        assert callback.call_count == len(calls) == 4
        history = calls[-1][2]["params"]
        assert history["period1"] == stamp(START, hour=0)
        assert history["period2"] == stamp("2024-04-03", hour=0)
        assert history["interval"] == "1d" and history["events"] == "div,splits,capitalGains"
        assert get_page(client) == first
        assert len(calls) == 4  # Cached complete range, not a second provider page.
        with pytest.raises(MarketDataError) as caught:
            get_page(client, offset=2)
        assert_safe(caught.value, "invalid_request")
        assert len(calls) == callback.call_count == 4


def test_closes_owned_client_transport_on_success_and_error(transport, monkeypatch):
    original = Session.close
    closed = []

    def close(session):
        closed.append(session)
        return original(session)

    monkeypatch.setattr(Session, "close", close)
    for status in (200, 503):
        client, _, _ = client_for(transport, chart(), status=status)
        try:
            with client:
                client.tickerinfo(SYMBOL)
        except MarketDataError as error:
            assert status == 503 and error.code == "http_error"
        assert client._session in closed
        assert client._closed and not client._session.captures and not client._session.secrets


def test_default_transport_disables_retries_and_environment(transport, monkeypatch):
    original = Session.__init__
    options = []

    def initialize(session, *args, **kwargs):
        options.append(kwargs.copy())
        return original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "__init__", initialize)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    with YahooFinanceClient() as client:
        client.tickerinfo(SYMBOL)
        assert client._session.trust_env is False
    assert options == [{"impersonate": "chrome", "trust_env": False, "retry": 0}]
    assert all(call[2]["allow_redirects"] is False for call in transport[1])


def test_callback_failure_prevents_attempt_and_propagates_unchanged(transport):
    failure = RuntimeError("Local quota reservation failed")
    callback = Mock(side_effect=failure)
    client, calls, _ = client_for(transport, chart(), before_request=callback)
    with client, pytest.raises(RuntimeError) as caught:
        client.tickerinfo(SYMBOL)
    assert caught.value is failure
    callback.assert_called_once_with()
    assert calls == []


def test_full_envelope_and_nullable_adjusted_fields_are_preserved(transport, page):
    page["provider_extra"] = {"version": 2}
    raw = page["chart"]["result"][0]
    raw["indicators"]["adjclose"][0]["adjclose"][0] = None
    raw["indicators"]["quote"][0]["unknown_adjusted"] = [None, 595.22]
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    row = result["data"][0]
    assert row["open"] == "10.25" and row["volume"] is None
    assert row["adj_close"] is None and row["close"] == "10.5"
    assert not any(key in row for key in ("adj_open", "adj_high", "adj_low", "adj_volume"))
    assert row["price_basis"] == "provider_split_adjusted" and row["date"] == START
    assert result["raw"]["provider_extra"] == {"version": 2}


def test_missing_price_currency_and_adjusted_fields_are_not_invented(transport, page):
    raw = page["chart"]["result"][0]
    del raw["meta"]["currency"]
    del raw["indicators"]["adjclose"]
    raw["indicators"]["quote"][0]["volume"] = [None, None]
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    for row in result["data"]:
        assert row["volume"] is None and row["price_currency"] is None and row["adj_close"] is None
        assert not any(key in row for key in ("adj_open", "adj_high", "adj_low", "adj_volume"))


def test_exact_decimal_tokens_unknown_nested_fields_and_integer_tokens(transport):
    raw = json.dumps(chart()).replace("10.25", "714.9000000000000000001234567890123456789", 1)
    raw = raw.replace('"volume": [null, 0]', '"volume": [9007199254740993, 0]')
    raw = raw.replace('"high": [11.0, 12.0]', '"high": [716, 12.0]')
    raw = raw.replace('"chart":', '"unknown": {"nested": [0.10000000000000000000000000001, 1.2300e-7, 1e3, 4, true, null]}, "chart":', 1)
    client, _, _ = client_for(transport, raw, phase="history")
    with client:
        result = get_page(client)
    row = result["data"][0]
    assert row["open"] == "714.9000000000000000001234567890123456789"
    assert row["high"] == 716 and type(row["high"]) is int
    assert row["volume"] == 9007199254740993 and type(row["volume"]) is int
    assert result["raw"]["unknown"]["nested"] == ["0.10000000000000000000000000001", "0.00000012300", "1000", 4, True, None]
    assert result["raw"] == wire_safe(raw)


def test_exact_decimals_also_apply_to_metadata(transport):
    raw = json.dumps(chart()).replace('"priceHint": 2', '"priceHint": 2, "unknown": [1.2345678901234567890,3], "tiny": 0.00000000000000001')
    client, _, _ = client_for(transport, raw)
    with client:
        info = client.tickerinfo(SYMBOL)
    assert info["unknown"] == ["1.2345678901234567890", 3]
    assert info["tiny"] == "0.00000000000000001" and info["symbol"] == SYMBOL


@pytest.mark.parametrize("data", [None, [], {}, "", "invalid", True, False, 0, 123, [None, {"unknown": "value"}], {"name": "Missing symbol"}, {"symbol": None}, {"symbol": 123}, {"symbol": "FUND"}, {"symbol": "FUND.L "}])
def test_metadata_shapes_without_exact_identity_fail_closed(transport, data):
    payload = chart()
    payload["chart"]["result"][0]["meta"] = data
    client, calls, callback = client_for(transport, payload)
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    code = "identity_mismatch" if isinstance(data, dict) and isinstance(data.get("symbol"), str) else "malformed_response"
    assert_safe(caught.value, code)
    assert len(calls) == callback.call_count == 3


@pytest.mark.parametrize("payload", [{}, {"symbol": "OTHER.L"}, {"unknown": [None, "metadata"], "pagination": None}])
def test_metadata_without_chart_envelope_is_not_accepted(transport, payload):
    client, _, _ = client_for(transport, payload)
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert_safe(caught.value, "malformed_response")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_metadata_tokens_are_preserved_as_strings(transport, constant):
    raw = json.dumps(chart()).replace('"priceHint": 2', '"priceHint": 2, "unknown": [' + constant + ']')
    client, _, _ = client_for(transport, raw)
    with client:
        assert client.tickerinfo(SYMBOL)["unknown"] == [constant]


@pytest.mark.parametrize("endpoint", ["eod", "tickerinfo", "tickerslist"])
def test_huge_integer_tokens_are_preserved_as_strings_beyond_bson_signed64(transport, endpoint):
    values = [-(10**100), -(2**63) - 1, -(2**63), 9007199254740993, 2**63 - 1, 2**63, 10**100]
    expected = [str(-(10**100)), str(-(2**63) - 1), -(2**63), 9007199254740993, 2**63 - 1, str(2**63), str(10**100)]
    payload = chart()
    payload["chart"]["result"][0]["meta"]["unknown"] = {"nested": values}
    payload["provider_extra"] = 10**100
    phase = "history" if endpoint == "eod" else "meta"
    if endpoint == "tickerslist":
        phase = "search"
        payload = {"quotes": [{"unknown": {"nested": values}}], "provider_extra": 10**100}
    client, _, _ = client_for(transport, payload, phase=phase)
    with client:
        if endpoint == "eod":
            result = get_page(client)
            actual = result["raw"]["chart"]["result"][0]["meta"]["unknown"]["nested"]
        elif endpoint == "tickerslist":
            result = client.tickerslist_page(SYMBOL)
            actual = result["data"][0]["unknown"]["nested"]
        else:
            result = client.tickerinfo(SYMBOL)
            actual = result["unknown"]["nested"]
    assert actual == expected
    assert [type(value) for value in actual] == [str, str, int, int, int, str, str]
    if endpoint != "tickerinfo":
        assert result["raw"]["provider_extra"] == str(10**100)


def test_case_normalization_preserves_raw_identity_and_requested_mic(transport, page):
    page["chart"]["result"][0]["meta"]["symbol"] = SYMBOL.lower()
    transport[0]["meta"] = deepcopy(page)
    transport[0]["history"] = page
    with YahooFinanceClient() as client:
        assert client.tickerinfo(SYMBOL)["symbol"] == SYMBOL
        result = get_page(client, exchange="xlon")
    assert result["data"][0]["exchange"] == "xlon"
    assert result["raw"]["chart"]["result"][0]["meta"]["symbol"] == SYMBOL.lower()


@pytest.mark.parametrize("status", [200, 400, 503])
@pytest.mark.parametrize("wrapper", ["chart", "finance", "quoteSummary"])
@pytest.mark.parametrize("provider_type,code", [
    ("Unauthorized", "plan_restricted"), ("Forbidden", "plan_restricted"),
    ("Too Many Requests", "quota_exceeded"), ("Not Found", "invalid_symbol"),
])
def test_provider_errors_on_success_and_non_success_http_are_safe(transport, status, wrapper, provider_type, code):
    payload = {wrapper: {"error": {"code": provider_type, "description": f"raw-provider-message {API_KEY}"}, "result": None}}
    client, calls, callback = client_for(transport, payload, status=status)
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    expected = "http_error" if status != 200 else code
    if status == 200 and provider_type == "Not Found" and wrapper != "chart":
        expected = "malformed_response"
    assert_safe(caught.value, expected)
    assert len(calls) == callback.call_count == 3


@pytest.mark.parametrize("provider_type", [API_KEY, "raw-provider-message", None, 101, True, [], {}, {"secret": API_KEY}])
def test_arbitrary_provider_error_type_is_not_reflected(transport, provider_type):
    client, _, _ = client_for(transport, {"chart": {"error": {"code": provider_type, "description": API_KEY}}})
    with client, pytest.raises(MarketDataError) as caught:
        get_page(client)
    assert_safe(caught.value, "malformed_response")


@pytest.mark.parametrize("status,code", [(401, "authentication_error"), (402, "http_error"), (403, "plan_restricted"), (429, "quota_exceeded"), (404, "http_error"), (500, "http_error"), (503, "http_error")])
@pytest.mark.parametrize("body", ["not JSON raw-provider-message", chart()])
def test_http_status_errors_do_not_depend_on_valid_error_json(transport, status, code, body):
    client, _, _ = client_for(transport, body, status=status)
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert_safe(caught.value, code)


@pytest.mark.parametrize("body", ["", "broken " + API_KEY, b"\xff", "[]", "null", "42", "true", {"error": None}, {"error": []}, {"error": API_KEY}])
def test_malformed_json_and_nonobject_or_error_envelopes_are_safe(transport, body):
    client, _, _ = client_for(transport, body)
    with client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert_safe(caught.value, "malformed_response")


@pytest.mark.parametrize("exception_type", [ConnectionError, DNSError, HTTPError, ProxyError, SSLError, Timeout, TimeoutError, OSError])
def test_transport_failures_are_not_retried_or_chained(transport, exception_type, caplog):
    state, calls = transport

    def handler(session, method, url, kwargs):
        logging.getLogger("curl_cffi.requests").debug("raw-provider-message %s", API_KEY)
        raise exception_type(f"raw-provider-message {url} {API_KEY}")

    state["handler"] = handler
    callback = Mock()
    caplog.set_level(logging.DEBUG)
    with YahooFinanceClient(before_request=callback) as client, pytest.raises(MarketDataError) as caught:
        get_page(client)
    assert_safe(caught.value, "transport_error")
    assert len(calls) == callback.call_count == 1
    assert API_KEY not in caplog.text and "raw-provider-message" not in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_http_request_logs_are_suppressed_but_unrelated_logging_is_not(transport, caplog, level):
    caplog.set_level(level)
    with YahooFinanceClient() as client:
        client.tickerinfo(SYMBOL)
    logging.getLogger("curl_cffi").info("unrelated HTTP client log")
    logging.getLogger("yfinance").debug("unrelated transport log")
    assert "unrelated HTTP client log" in caplog.text
    assert ("unrelated transport log" in caplog.text) == (level == logging.DEBUG)
    assert API_KEY not in caplog.text and "private-crumb" not in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_suppression_covers_transport_but_is_scoped_to_request_context(transport, caplog, level):
    state, _ = transport
    caplog.set_level(level)

    def handler(session, method, url, kwargs):
        for name in ("yfinance", "curl_cffi", "curl_cffi.requests", "urllib3", "urllib3.connectionpool"):
            logging.getLogger(name).log(level, "raw-provider-message %s %s", API_KEY, url)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(logging.getLogger("curl_cffi").log, level, "unrelated thread log").result()

    state["handler"] = handler
    with YahooFinanceClient() as client:
        client.tickerinfo(SYMBOL)
    assert "unrelated thread log" in caplog.text
    assert "raw-provider-message" not in caplog.text and API_KEY not in caplog.text
    assert "finance.yahoo.com" not in caplog.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_external_redirect_is_not_followed(transport, status):
    state, calls = transport
    state["handler"] = lambda session, method, url, kwargs: response(url, API_KEY.encode(), status, {"Location": "https://untrusted.invalid/"})
    callback = Mock()
    with YahooFinanceClient(before_request=callback) as client, pytest.raises(MarketDataError) as caught:
        get_page(client)
    assert_safe(caught.value, "http_error")
    assert len(calls) == callback.call_count == 1


def assert_raw_pagination(transport, page):
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    assert result["pagination"] == {"limit": 2, "offset": 0, "count": 2, "total": 2}


@pytest.mark.parametrize("key", ["limit", "offset", "count", "total"])
@pytest.mark.parametrize("value", [None, True, "2", 2.0, -1])
def test_arbitrary_pagination_field_values_are_preserved_raw_not_trusted(transport, page, key, value):
    page["pagination"] = {"limit": 2, "offset": 0, "count": 2, "total": 2, key: value}
    assert_raw_pagination(transport, page)


@pytest.mark.parametrize("update", [
    {"limit": 0}, {"limit": 1001}, {"limit": 100}, {"offset": 1},
    {"count": 1}, {"count": 3}, {"total": 1}, {"total": 0},
])
def test_inconsistent_pagination_counts_bounds_and_offsets_are_raw_only(transport, page, update):
    page["pagination"] = {"limit": 2, "offset": 0, "count": 2, "total": 2, **update}
    assert_raw_pagination(transport, page)


@pytest.mark.parametrize("key", ["limit", "offset", "count", "total"])
def test_missing_raw_pagination_fields_are_not_invented(transport, page, key):
    page["pagination"] = {"limit": 2, "offset": 0, "count": 2, "total": 2}
    del page["pagination"][key]
    assert_raw_pagination(transport, page)


@pytest.mark.parametrize("value", [None, [], "bad", True, 3])
def test_arbitrary_pagination_shapes_are_preserved_raw(transport, page, value):
    page["pagination"] = value
    assert_raw_pagination(transport, page)


@pytest.mark.parametrize("value", [None, {}, "bad", True, 3, 2.0])
def test_history_result_must_be_an_array(transport, page, value):
    page["chart"]["result"] = value
    client, _, _ = client_for(transport, page, phase="history")
    with client, pytest.raises(MarketDataError) as caught:
        get_page(client)
    assert_safe(caught.value, "malformed_response")


def test_history_result_key_is_required(transport, page):
    del page["chart"]["result"]
    client, _, _ = client_for(transport, page, phase="history")
    with client, pytest.raises(MarketDataError) as caught:
        get_page(client)
    assert_safe(caught.value, "malformed_response")


def test_missing_provider_pagination_is_not_invented_in_raw(transport, page):
    assert "pagination" not in page
    assert_raw_pagination(transport, page)


@pytest.mark.parametrize("offset,total", [(0, 0), (2, 2), (0, 1), (2, 3), (3, 2)])
def test_empty_history_is_preserved_regardless_of_raw_reported_total(transport, page, offset, total):
    page["pagination"] = {"limit": 1000, "offset": offset, "count": 0, "total": total}
    page["chart"]["result"][0]["timestamp"] = []
    client, calls, callback = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["data"] == [] and result["pagination"] == {"limit": 0, "offset": 0, "count": 0, "total": 0}
    assert result["raw"] == wire_safe(page)
    assert len(calls) == callback.call_count == 4


def test_more_rows_than_old_page_limit_are_preserved(transport, page):
    raw = page["chart"]["result"][0]
    raw["timestamp"] *= 501
    for container in (raw["indicators"]["quote"][0], raw["indicators"]["adjclose"][0]):
        for key in container:
            container[key] *= 501
    client, calls, callback = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page) and len(result["data"]) == 1002
    assert result["pagination"]["total"] == 1002
    assert len(calls) == callback.call_count == 4


@pytest.mark.parametrize("timestamp", [stamp("2024-03-27"), stamp("2024-04-03"), "2024-03-28garbage", "2024-03-28Tnot-a-time", "2024-09-31", None, 123, "2024-03-28T99:00:00Z", "20240328", True, {}, [], 10**100])
def test_arbitrary_dates_remain_raw_but_do_not_create_unusable_rows(transport, page, timestamp):
    page["chart"]["result"][0]["timestamp"][0] = timestamp
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    assert [row["date"] for row in result["data"]] == [END]


@pytest.mark.parametrize("row", [None, [], [None, "unknown"], {}, "invalid", 3, 1.25, True, False])
def test_arbitrary_quote_container_shapes_are_preserved_raw(transport, page, row):
    page["chart"]["result"][0]["indicators"]["quote"][0] = row
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page) and len(result["data"]) == 2
    for item in result["data"]:
        assert all(item[key] is None for key in ("open", "high", "low", "close", "volume"))


@pytest.mark.parametrize("timestamp", [stamp(START), str(stamp(START)), f"{stamp(START)}.0"])
def test_valid_numeric_timestamps_are_preserved_verbatim(transport, page, timestamp):
    page["chart"]["result"][0]["timestamp"][0] = timestamp
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        row = get_page(client)["data"][0]
    assert row["date"] == START and row["yahoo_timestamp"] == timestamp


@pytest.mark.parametrize("case", ["duplicate", "same-day-different-time", "descending"])
def test_duplicate_or_unordered_dates_are_not_silently_repaired(transport, page, case):
    timestamps = page["chart"]["result"][0]["timestamp"]
    if case == "descending":
        timestamps.reverse()
    else:
        timestamps[1] = stamp(START, hour=13) if case == "same-day-different-time" else timestamps[0]
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    assert [row["date"] for row in result["data"]] == ([END, START] if case == "descending" else [START, START])
    assert [row["close"] for row in result["data"]] == ["10.5", "11.25"]


@pytest.mark.parametrize("key", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [None, True, False, "714.0", 0, -1, -0.5, [], {}])
def test_arbitrary_raw_ohlc_values_are_preserved(transport, page, key, value):
    page["chart"]["result"][0]["indicators"]["quote"][0][key][0] = value
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    assert result["data"][0][key] == (str(value) if isinstance(value, float) else value)


def test_null_close_does_not_discard_the_row_or_page(transport, page):
    page["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = None
    client, calls, callback = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    assert result["data"][0]["close"] is None and len(result["data"]) == 2
    assert len(calls) == callback.call_count == 4


@pytest.mark.parametrize("key", ["open", "high", "low", "close", "volume", "timestamp", "symbol", "exchangeTimezoneName"])
def test_missing_raw_fields_are_not_invented_and_identity_fails_closed(transport, page, key):
    raw = page["chart"]["result"][0]
    if key in ("symbol", "exchangeTimezoneName"):
        del raw["meta"][key]
    elif key == "timestamp":
        del raw[key]
    else:
        del raw["indicators"]["quote"][0][key]
    client, _, _ = client_for(transport, page, phase="history")
    if key in ("symbol", "exchangeTimezoneName"):
        with client, pytest.raises(MarketDataError) as caught:
            get_page(client)
        assert_safe(caught.value, "malformed_response")
    else:
        with client:
            result = get_page(client)
        assert result["raw"] == wire_safe(page)
        if key == "timestamp":
            assert result["data"] == []
        else:
            assert all(row[key] is None for row in result["data"])


@pytest.mark.parametrize("update", [
    {"open": 8}, {"open": 12}, {"close": 8}, {"close": 12}, {"low": 12}, {"high": 8},
])
def test_inconsistent_ohlc_relationships_are_preserved_not_repaired_or_page_fatal(transport, page, update):
    for key, value in update.items():
        page["chart"]["result"][0]["indicators"]["quote"][0][key][0] = value
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert len(result["data"]) == 2 and result["raw"] == wire_safe(page)
    for key, value in update.items():
        assert result["data"][0][key] == value
    assert result["data"][0]["date"] == START


@pytest.mark.parametrize("volume", [None, 0, 0.0, 1, 0.5, 9007199254740993])
def test_volume_may_be_null_or_finite_nonnegative_number(transport, page, volume):
    page["chart"]["result"][0]["indicators"]["quote"][0]["volume"][0] = volume
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)["data"][0]["volume"]
    assert result == (str(volume) if isinstance(volume, float) else volume)


@pytest.mark.parametrize("volume", [-1, -0.5, True, "0", "NaN", {}, []])
def test_arbitrary_volume_values_are_preserved(transport, page, volume):
    page["chart"]["result"][0]["indicators"]["quote"][0]["volume"][0] = volume
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(page)
    assert result["data"][0]["volume"] == (str(volume) if isinstance(volume, float) else volume)


@pytest.mark.parametrize("key", ["open", "high", "low", "close", "volume", "adjclose", "unknown_adjusted"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_price_and_volume_tokens_are_preserved_as_strings(transport, page, key, constant):
    raw = page["chart"]["result"][0]
    container = raw["indicators"]["adjclose"][0] if key == "adjclose" else raw["indicators"]["quote"][0]
    container.setdefault(key, [None, None])[0] = "REPLACE_NUMBER"
    payload = json.dumps(page).replace('"REPLACE_NUMBER"', constant)
    client, _, _ = client_for(transport, payload, phase="history")
    with client:
        result = get_page(client)
    assert result["raw"] == wire_safe(payload)
    if key != "unknown_adjusted":
        assert result["data"][0]["adj_close" if key == "adjclose" else key] == constant


def test_equal_ohlc_is_valid_and_integer_prices_are_preserved(transport, page):
    for key in ("open", "high", "low", "close"):
        page["chart"]["result"][0]["indicators"]["quote"][0][key][0] = 10
    client, _, _ = client_for(transport, page, phase="history")
    with client:
        row = get_page(client)["data"][0]
    assert all(type(row[key]) is int and row[key] == 10 for key in ("open", "high", "low", "close"))


@pytest.mark.parametrize("overrides", [
    {"offset": -1}, {"offset": True}, {"offset": 0.0}, {"offset": "0"}, {"offset": 2},
    {"date_from": "2024-09-31"}, {"date_from": "2024-10-01"},
    {"date_from": "20240328"}, {"date_to": "2024-04-02garbage"},
    {"date_from": datetime(2024, 3, 28)}, {"date_to": None},
    {"symbol": ""}, {"symbol": None}, {"exchange": ""}, {"exchange": None},
])
def test_invalid_request_does_not_reserve_quota_or_attempt_http(transport, overrides):
    client, calls, callback = client_for(transport, chart())
    with client, pytest.raises(MarketDataError) as caught:
        get_page(client, **overrides)
    assert_safe(caught.value, "invalid_request")
    callback.assert_not_called()
    assert calls == []
