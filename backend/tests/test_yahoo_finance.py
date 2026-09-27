"""Offline integration tests: real pinned yfinance, fake final HTTP boundary."""

from copy import deepcopy
from datetime import datetime
import json
import logging
from unittest.mock import Mock
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from curl_cffi.requests import Response, Session
import pytest

from market_provider import MarketDataError
from yahoo_finance import YahooFinanceClient, splits_from_eod_page


SYMBOL = "FUND.L"
START, END = "2024-03-28", "2024-04-02"


def stamp(day, hour=12):
    return int(datetime.fromisoformat(f"{day}T{hour:02}:00:00").replace(tzinfo=ZoneInfo("Europe/London")).timestamp())


def chart():
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": SYMBOL, "currency": "GBP", "exchangeTimezoneName": "Europe/London",
                 "exchangeName": "LSE", "instrumentType": "ETF", "priceHint": 2,
                 "regularMarketTime": stamp(END), "longName": "Example Fund", "validRanges": ["1d", "max"]},
        "timestamp": [stamp(START), stamp(END)],
        "indicators": {"quote": [{"open": [10.25, 11.5], "high": [11.0, 12.0],
                                    "low": [9.0, 10.0], "close": [10.5, 11.25],
                                    "volume": [None, 0]}], "adjclose": [{"adjclose": [9.25, 10.25]}]},
        "events": {"splits": {str(stamp(END)): {"date": stamp(END), "numerator": 4,
                                                  "denominator": 1, "splitRatio": "4:1"}}},
    }]}}


def response(url, body, status=200, headers=None):
    r = Response()
    r.url = url
    r.status_code = status
    r.content = body if isinstance(body, bytes) else json.dumps(body).encode()
    if headers:
        r.headers.update(headers)
    return r


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected live HTTP request")
    monkeypatch.setattr(Session, "request", forbidden)


@pytest.fixture
def transport(monkeypatch):
    calls = []
    state = {"history": chart(), "meta": chart(), "search": {"quotes": []}, "handler": None}

    def send(session, method, url, **kwargs):
        calls.append((method, url, deepcopy(kwargs)))
        if state["handler"]:
            custom = state["handler"](session, method, url, kwargs)
            if custom is not None:
                return custom
        path = urlsplit(url).path
        if urlsplit(url).hostname == "fc.yahoo.com":
            session.cookies.set("A3", "private-cookie", domain=".yahoo.com")
            return response(url, b"cookie bootstrap", 404)
        if path.endswith("/getcrumb"):
            return response(url, b"private-crumb")
        if "/chart/" in path:
            return response(url, state["history"] if "period1" in kwargs.get("params", {}) else state["meta"])
        if path.endswith("/search"):
            return response(url, state["search"])
        pytest.fail("Unexpected endpoint")

    monkeypatch.setattr(Session, "request", send)
    return state, calls


def test_constructor_is_offline_and_client_is_isolated():
    with YahooFinanceClient() as first, YahooFinanceClient(api_key="ignored") as second:
        assert first.name == "yfinance"
        assert first._data is not second._data
        assert first._data._session is first._session
        assert second._data._session is second._session


def test_real_history_exact_json_null_volume_dates_and_split_basis(transport):
    state, calls = transport
    # Parsing the response through floats would erase these trailing digits.
    state["history"] = json.dumps(chart()).replace("10.5", "10.500000000000000019").encode()
    callback = Mock()
    with YahooFinanceClient(before_request=callback, timeout=7) as client:
        page = client.eod_page(SYMBOL, "XLON", START, END)
        splits = client.splits_page(SYMBOL, START, END)
        assert len(calls) == 4  # cookie, crumb, timezone metadata, history
        assert callback.call_count == len(calls)
        assert all(call[2]["timeout"] == 7 and call[2]["allow_redirects"] is False for call in calls)
        history_request = calls[-1][2]["params"]
        assert history_request["period1"] == int(datetime(2024, 3, 28, tzinfo=ZoneInfo("Europe/London")).timestamp())
        assert history_request["period2"] == int(datetime(2024, 4, 3, tzinfo=ZoneInfo("Europe/London")).timestamp())
        assert history_request["events"] == "div,splits,capitalGains"
        assert page["pagination"] == {"offset": 0, "count": 2, "total": 2, "limit": 2}
        row = page["data"][0]
        assert row["date"] == START
        assert row["volume"] is None
        assert row["close"] == "10.500000000000000019"
        assert row["adj_close"] == "9.25"
        assert row["price_currency"] == "GBP"
        assert row["symbol"] == SYMBOL and row["exchange"] == "XLON"
        assert row["price_basis"] == "provider_split_adjusted"
        assert page["data"][1]["volume"] == 0
        assert page["data"][1]["date"] == END
        raw = page["raw"]["chart"]["result"][0]
        assert raw["indicators"]["quote"][0]["volume"] == [None, 0]
        assert raw["indicators"]["quote"][0]["close"][0] == row["close"]
        assert splits["data"][0]["split_factor"] == "4"
        assert splits["data"][0]["yahoo"]["splitRatio"] == "4:1"
        # Mutating a returned page cannot mutate cached evidence.
        page["raw"].clear()
        assert client.eod_page(SYMBOL, "XLON", START, END)["raw"]
        assert len(calls) == 4


def test_history_options_are_explicit(transport, monkeypatch):
    from yahoo_finance import PriceHistory
    original = PriceHistory.history
    seen = []

    def history(self, **kwargs):
        seen.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(PriceHistory, "history", history)
    with YahooFinanceClient() as client:
        client.eod_page(SYMBOL, "XLON", START, END)
    assert seen == [{"start": START, "end": "2024-04-03", "interval": "1d", "auto_adjust": False,
                     "back_adjust": False, "repair": False, "keepna": True, "actions": True,
                     "rounding": False, "timeout": 15}]


def test_split_cache_different_range_fetches_again(transport):
    _, calls = transport
    with YahooFinanceClient() as client:
        client.eod_page(SYMBOL, "XLON", START, END)
        client.splits_page(SYMBOL, "2000-01-01", END)
    assert len(calls) == 5
    assert calls[-1][2]["params"]["period1"] != calls[-2][2]["params"]["period1"]


def test_splits_from_persisted_eod_raw_uses_original_actions_without_http(transport, monkeypatch):
    state, calls = transport
    with YahooFinanceClient() as client:
        eod = client.eod_page(SYMBOL, "XLON", START, END)
    persisted = json.loads(json.dumps(eod))
    state["history"]["chart"]["result"][0]["events"]["splits"][str(stamp(END))]["numerator"] = 7
    call_count = len(calls)

    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected replay HTTP request")

    monkeypatch.setattr(Session, "request", forbidden)
    splits = splits_from_eod_page(persisted, SYMBOL, START, END)

    assert len(calls) == call_count
    assert splits["pagination"] == {"offset": 0, "count": 1, "total": 1, "limit": 1}
    assert splits["provider"] == "yfinance"
    assert splits["price_basis"] == "provider_split_adjusted"
    assert splits["data"][0]["split_factor"] == "4"
    assert splits["data"][0]["yahoo"]["splitRatio"] == "4:1"
    assert splits["raw"] == persisted["raw"]


def test_splits_from_persisted_eod_raw_accepts_mongo_safe_numeric_strings(transport):
    state, _ = transport
    with YahooFinanceClient() as client:
        eod = client.eod_page(SYMBOL, "XLON", START, END)
    event = eod["raw"]["chart"]["result"][0]["events"]["splits"][str(stamp(END))]
    event.update(date=str(event["date"]), numerator="10.0", denominator="2.0")

    splits = splits_from_eod_page(json.loads(json.dumps(eod)), SYMBOL, START, END)

    assert splits["data"][0]["date"] == END
    assert splits["data"][0]["split_factor"] == "5"


@pytest.mark.parametrize("page,symbol,date_from,date_to,code", [
    ({}, SYMBOL, START, END, "malformed_response"),
    ({"raw": {"chart": {}}}, SYMBOL, START, END, "malformed_response"),
    ({"raw": {"chart": {"error": None, "result": [{"meta": {"symbol": "OTHER.L"}}]}}}, SYMBOL, START, END, "identity_mismatch"),
    ({"raw": chart()}, SYMBOL, END, START, "invalid_request"),
])
def test_splits_from_persisted_eod_raw_safe_errors(page, symbol, date_from, date_to, code):
    with pytest.raises(MarketDataError) as caught:
        splits_from_eod_page(page, symbol, date_from, date_to)
    assert caught.value.code == code
    assert caught.value.__context__ is None


def test_permissive_odd_prices_and_unusable_dates_remain_raw(transport):
    state, _ = transport
    raw = state["history"]["chart"]["result"][0]
    raw["timestamp"] += [None, "bad", 10**100, stamp("2024-04-03")]
    raw["indicators"]["quote"][0].update(close=[-3, {"odd": "value"}], high=[1], open=["NaN", None])
    raw["indicators"].pop("adjclose")
    with YahooFinanceClient() as client:
        page = client.eod_page(SYMBOL, "XLON", START, END)
    assert len(page["data"]) == 2
    assert page["data"][0]["close"] == -3
    assert page["data"][0]["open"] == "NaN"
    assert page["data"][1]["close"] == {"odd": "value"}
    assert page["data"][1]["high"] is None
    assert all(row["adj_close"] is None for row in page["data"])
    assert page["raw"]["chart"]["result"][0]["timestamp"][2:5] == [None, "bad", str(10**100)]


@pytest.mark.parametrize("payload", [b"bad json", {}, {"chart": {}}, {"chart": {"result": []}}, {"chart": {"result": [None]}}])
def test_malformed_history_fails_without_claiming_empty_success(transport, payload):
    state, _ = transport
    state["history"] = payload
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.eod_page(SYMBOL, "XLON", START, END)
    assert caught.value.code == "malformed_response"
    assert caught.value.__context__ is None


@pytest.mark.parametrize("phase", ["meta", "history"])
def test_identity_mismatch_rejected(transport, phase):
    state, _ = transport
    state[phase]["chart"]["result"][0]["meta"]["symbol"] = "LOOKALIKE.L"
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.eod_page(SYMBOL, "XLON", START, END)
    assert caught.value.code == "identity_mismatch"


def test_no_chart_capture_is_not_a_success(transport, monkeypatch):
    from yahoo_finance import PriceHistory
    monkeypatch.setattr(PriceHistory, "history", lambda *args, **kwargs: None)
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.eod_page(SYMBOL, "XLON", START, END)
    assert caught.value.code == "malformed_response"


@pytest.mark.parametrize("status,code", [(429, "quota_exceeded"), (401, "authentication_error"), (403, "plan_restricted"), (500, "http_error")])
@pytest.mark.parametrize("phase", ["cookie", "crumb", "chart", "history", "search"])
def test_http_errors_stop_session_no_retry_in_every_phase(transport, phase, status, code):
    state, calls = transport

    def handler(session, method, url, kwargs):
        target = ((phase == "cookie" and "fc.yahoo.com" in url)
                  or (phase == "crumb" and "getcrumb" in url)
                  or (phase == "chart" and "/chart/" in url)
                  or (phase == "history" and "period1" in kwargs.get("params", {}))
                  or (phase == "search" and "/search" in url))
        if target:
            return response(url, b"private-crumb https://bad.example/?token=secret", status)

    state["handler"] = handler
    with YahooFinanceClient() as client:
        with pytest.raises(MarketDataError) as caught:
            if phase == "search":
                client.resolve_listing("FUND", "")
            else:
                client.eod_page(SYMBOL, "XLON", START, END)
        count = len(calls)
        assert caught.value.code == code
        assert caught.value.__context__ is None
        for operation in (lambda: client.tickerinfo(SYMBOL), lambda: client.resolve_listing("FUND", ""),
                          lambda: client.splits_page(SYMBOL, START, END)):
            with pytest.raises(MarketDataError) as repeated:
                operation()
            assert repeated.value is caught.value
        assert len(calls) == count


@pytest.mark.parametrize("failure_at", [1, 2, 3, 4])
def test_callback_original_exception_is_not_swallowed_or_retried(transport, failure_at):
    _, calls = transport
    original = RuntimeError("budget exhausted")
    attempted = 0

    def reserve():
        nonlocal attempted
        attempted += 1
        if attempted == failure_at:
            raise original

    with YahooFinanceClient(before_request=reserve) as client, pytest.raises(RuntimeError) as caught:
        client.eod_page(SYMBOL, "XLON", START, END)
    assert caught.value is original
    assert len(calls) == failure_at - 1
    assert attempted == failure_at


def test_transport_exception_has_no_private_context_and_no_retry(transport, caplog):
    state, calls = transport

    def fail(*args):
        logging.getLogger("yfinance").error("crumb=private-crumb URL=https://secret.example")
        raise OSError("https://secret.example?crumb=private-crumb")

    state["handler"] = fail
    caplog.set_level(logging.DEBUG)
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert caught.value.code == "transport_error"
    assert caught.value.__context__ is None
    assert "secret.example" not in caplog.text
    assert "private-crumb" not in caplog.text
    assert len(calls) == 1
    logging.getLogger("yfinance").warning("outside acquisition")
    assert "outside acquisition" in caplog.text


def test_raw_redaction_and_profile_identity(transport, caplog):
    state, calls = transport
    metadata = state["meta"]["chart"]["result"][0]["meta"]
    metadata.update(website="https://example.org/?crumb=private-crumb", note="private-cookie",
                    crumb="private-crumb", authorization="bearer abc", arbitrary="crumb=secret",
                    nested={"headers": {"Cookie": "secret"}, "useful": 3})
    state["history"] = deepcopy(state["meta"])
    caplog.set_level(logging.DEBUG)
    with YahooFinanceClient() as client:
        info = client.tickerinfo(SYMBOL)
        page = client.eod_page(SYMBOL, "XLON", START, END)
    assert info["name"] == "Example Fund" and info["currency"] == "GBP"
    assert info["exchangeName"] == "LSE"
    assert info["profile_source"] == "chart_metadata"
    assert info["website"] == "[redacted]"
    assert info["nested"] == {"useful": 3}
    serialized = json.dumps([info, page])
    for private in ("private-crumb", "private-cookie", "bearer abc", "https://", "crumb=secret"):
        assert private not in serialized
        assert private not in caplog.text
    assert not any("quoteSummary" in call[1] or "/search" in call[1] for call in calls)


def test_json_rate_limit_stops_session(transport):
    state, calls = transport
    state["history"] = {"chart": {"error": {"code": "Too Many Requests", "description": "secret"}, "result": None}}
    with YahooFinanceClient() as client:
        with pytest.raises(MarketDataError) as caught:
            client.eod_page(SYMBOL, "XLON", START, END)
        assert caught.value.code == "quota_exceeded"
        count = len(calls)
        with pytest.raises(MarketDataError):
            client.tickerinfo(SYMBOL)
        assert len(calls) == count


def test_redirects_count_each_http_hop(transport):
    state, calls = transport
    callback = Mock()

    def handler(session, method, url, kwargs):
        if url == "https://fc.yahoo.com":
            return response(url, b"", 302, {"Location": "https://fc.yahoo.com/bootstrap"})

    state["handler"] = handler
    with YahooFinanceClient(before_request=callback) as client:
        client.tickerinfo(SYMBOL)
    assert len(calls) == callback.call_count == 4


def test_redirect_to_non_yahoo_is_not_followed(transport):
    state, calls = transport
    state["handler"] = lambda *args: response(args[2], b"", 302, {"Location": "https://attacker.example/"})
    with YahooFinanceClient() as client, pytest.raises(MarketDataError):
        client.tickerinfo(SYMBOL)
    assert len(calls) == 1


def listing(symbol="FUND.L", exchange="LSE", **extra):
    return {"symbol": symbol, "exchange": exchange, "quoteType": "ETF", "longname": "Example Fund", **extra}


def test_resolution_exact_root_not_substring_and_audited_mic(transport):
    state, calls = transport
    state["search"] = {"quotes": [listing("NOTFUND.L"), listing()]}
    with YahooFinanceClient() as client:
        selected, resolution = client.resolve_listing("FUND", "")
    assert selected == {"symbol": SYMBOL, "exchange": "XLON"}
    assert resolution["source"] == "yahoo_search" and resolution["query"] == "FUND"
    assert resolution["name"] == "Example Fund"
    assert resolution["data"]["symbol"] == SYMBOL
    assert len(resolution["raw"]["quotes"]) == 2
    assert "collected_at" not in resolution
    assert not any("/chart/" in call[1] for call in calls)


@pytest.mark.parametrize("quotes,code", [([listing("NOTFUND.L")], "listing_not_found"),
                                         ([listing(), listing("FUND.DE", "GER")], "listing_not_found"),
                                         ([listing(exchange="UNKNOWN")], "unknown_exchange")])
def test_resolution_fails_closed(transport, quotes, code):
    state, _ = transport
    state["search"] = {"quotes": quotes}
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.resolve_listing("FUND", "")
    assert caught.value.code == code


def test_resolution_exact_symbol_preferred_over_other_venues(transport):
    state, _ = transport
    state["search"] = {"quotes": [listing("FUND.DE", "GER"), listing()]}
    with YahooFinanceClient() as client:
        selected, _ = client.resolve_listing(SYMBOL, "")
    assert selected["exchange"] == "XLON"


def test_resolution_isin_fallback_explicit_query(transport):
    state, calls = transport
    isin = "IE00B4L5Y983"

    def handler(session, method, url, kwargs):
        if url.endswith("/search"):
            return response(url, {"quotes": [listing("OTHER.DE", "GER")]} if kwargs["params"]["q"] == isin else {"quotes": []})

    state["handler"] = handler
    with YahooFinanceClient() as client:
        selected, resolution = client.resolve_listing("FUND", isin)
    assert selected == {"symbol": "OTHER.DE", "exchange": "XETR"}
    assert resolution["query"] == isin
    assert [call[2]["params"]["q"] for call in calls if call[1].endswith("/search")] == ["FUND", isin]


@pytest.mark.parametrize("args", [(SYMBOL, "XLON", END, START), (SYMBOL, "XLON", "bad", END),
                                   (SYMBOL, "XLON", START, END, 1), ("", "XLON", START, END),
                                   (SYMBOL, "", START, END), (SYMBOL, "XLON", START, "9999-12-31")])
def test_invalid_requests_are_offline(args):
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.eod_page(*args)
    assert caught.value.code == "invalid_request"


def test_missing_volume_and_malformed_splits_preserve_raw(transport):
    state, _ = transport
    raw = state["history"]["chart"]["result"][0]
    del raw["indicators"]["quote"][0]["volume"]
    raw["events"]["splits"] = {
        "bad-factor": {"date": stamp(END), "numerator": "odd", "denominator": 0},
        "bad-date": {"date": None, "numerator": 2, "denominator": 1},
    }
    with YahooFinanceClient() as client:
        page = client.eod_page(SYMBOL, "XLON", START, END)
        splits = client.splits_page(SYMBOL, START, END)
    assert all(row["volume"] is None for row in page["data"])
    assert len(splits["data"]) == 1
    assert splits["data"][0]["split_factor"] is None
    assert splits["raw"]["chart"]["result"][0]["events"]["splits"] == raw["events"]["splits"]


def test_missing_dates_are_raw_only(transport):
    state, _ = transport
    del state["history"]["chart"]["result"][0]["timestamp"]
    with YahooFinanceClient() as client:
        page = client.eod_page(SYMBOL, "XLON", START, END)
    assert page["data"] == []
    assert page["pagination"]["total"] == 0
    assert page["raw"]["chart"]["result"][0]["indicators"]


@pytest.mark.parametrize("text,code", [(b"Too Many Requests", "quota_exceeded"),
                                      (b"<html>Access Denied</html>", "plan_restricted"),
                                      (b"<html>unknown</html>", "malformed_response"),
                                      (b"", "malformed_response")])
def test_crumb_errors_never_trigger_cookie_strategy_retry(transport, text, code):
    state, calls = transport
    state["handler"] = lambda session, method, url, kwargs: response(url, text) if "getcrumb" in url else None
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert caught.value.code == code
    assert len(calls) == 2


def test_global_retry_setting_cannot_retry_transport_failure(transport, monkeypatch):
    from yfinance.config import YfConfig
    state, calls = transport
    monkeypatch.setattr(YfConfig.network, "retries", 3)

    def fail(*args):
        raise TimeoutError("private provider message")

    state["handler"] = fail
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert caught.value.code == "transport_error"
    assert len(calls) == 1


def test_yfinance_rate_limit_exception_is_not_hidden_by_permissive_normalization(transport, monkeypatch):
    from yahoo_finance import PriceHistory
    from yfinance.exceptions import YFRateLimitError

    def fail(*args, **kwargs):
        raise YFRateLimitError()

    monkeypatch.setattr(PriceHistory, "history", fail)
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.eod_page(SYMBOL, "XLON", START, END)
    assert caught.value.code == "quota_exceeded"
    assert caught.value.__context__ is None


def test_resolution_rejects_contradictory_isin(transport):
    state, _ = transport
    state["search"] = {"quotes": [listing(isin="IE00WRONG000")]}
    with YahooFinanceClient() as client, pytest.raises(MarketDataError) as caught:
        client.resolve_listing("FUND", "IE00B4L5Y983")
    assert caught.value.code == "listing_not_found"


def test_closed_client_is_not_reused(transport):
    client = YahooFinanceClient()
    client.close()
    with pytest.raises(MarketDataError) as caught:
        client.tickerinfo(SYMBOL)
    assert caught.value.code == "client_closed"
