"""Experimental, single-attempt yfinance 1.7.0 acquisition.

Private yfinance APIs are deliberately pinned: a local YfData bypasses its global
singleton, persistent cookies and response cache. PriceHistory consumes the same
responses we capture, but its DataFrame is NOT evidence (it fills null volumes,
rounds through floats, and can drop malformed rows). Normalization uses raw JSON.
Yahoo OHLC/Close already reflect splits; Adj Close additionally reflects dividends.
No adjustment or repair is performed here. Consumers must honor price_basis.

Each history range is one complete page. Nonzero offsets are rejected, not
refetched. Instances are synchronous, not intended for concurrent use. Profile
acquisition uses validated chart metadata only; optional quote modules are not
queried and profile completeness is not claimed.
"""

from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException
import json
import logging
import math
import re
from typing import Any, Self
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from curl_cffi.requests import Session
from yfinance.data import YfData
from yfinance.exceptions import YFRateLimitError
from yfinance.scrapers.history import PriceHistory

from market_provider import MarketDataError


_MESSAGES = {
    "invalid_request": "The Yahoo Finance request parameters are invalid.",
    "invalid_symbol": "Yahoo Finance did not recognize the requested symbol.",
    "identity_mismatch": "Yahoo Finance returned a different instrument.",
    "malformed_response": "Yahoo Finance returned an unreadable or unsupported response.",
    "authentication_error": "Yahoo Finance denied access. No automatic retry was made.",
    "plan_restricted": "Yahoo Finance denied access. No automatic retry was made.",
    "quota_exceeded": "Yahoo Finance rate limited the session. No automatic retry was made.",
    "http_error": "Yahoo Finance returned an unsuccessful response. No automatic retry was made.",
    "transport_error": "Yahoo Finance could not be reached. No automatic retry was made.",
    "listing_not_found": "No unambiguous Yahoo listing was found. Set the symbol and MIC manually.",
    "unknown_exchange": "The Yahoo exchange is not mapped. Set the symbol and MIC manually.",
    "client_closed": "The Yahoo Finance client is closed.",
}


def _error(code: str) -> MarketDataError:
    return MarketDataError(code, _MESSAGES[code])


# Audited venue identifiers, NOT suffix guesses or Yahoo's human display names.
# NMS/NGM/NCM are Nasdaq tiers; NYQ=NYSE; PCX=NYSE Arca; ASE=NYSE American.
# GER is Xetra, FRA Frankfurt; LSE London; AMS/PAR/BRU/LIS Euronext venues.
# Deliberately omit consolidated/ambiguous codes (e.g. EU-wide trading systems).
YAHOO_EXCHANGE_MICS = {
    "NMS": "XNAS", "NGM": "XNAS", "NCM": "XNAS", "NAS": "XNAS",
    "NYQ": "XNYS", "PCX": "ARCX", "ASE": "XASE", "BTS": "BATS",
    "GER": "XETR", "FRA": "XFRA", "LSE": "XLON", "AMS": "XAMS",
    "PAR": "XPAR", "BRU": "XBRU", "LIS": "XLIS", "MIL": "XMIL",
    "MCE": "XMAD", "EBS": "XSWX", "STO": "XSTO", "CPH": "XCSE",
    "HEL": "XHEL", "OSL": "XOSL", "VIE": "XWBO", "DUB": "XDUB",
}
_LISTING_SUFFIXES = {"DE", "F", "L", "AS", "PA", "BR", "LS", "MI", "MC", "SW", "ST", "CO", "HE", "OL", "VI", "IR"}
_private_request = ContextVar("yahoo_private_request", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_request.get()


# These libraries log URLs, crumb strings and exception messages. Suppress only
# this synchronous context, without mutating global logger levels or config.
_log_filter = _PrivateRequestFilter()
for _logger_name in ("yfinance", "curl_cffi", "curl_cffi.requests", "urllib3", "urllib3.connectionpool"):
    logging.getLogger(_logger_name).addFilter(_log_filter)


class _Abort(BaseException):
    """Cross yfinance's broad Exception handlers without retries or logging."""


_SECRET_KEY = re.compile(r"cookie|crumb|authorization|credential|password|token|secret|sessionid|headers|url", re.I)
_UNSAFE_TEXT = re.compile(r"https?://|(?:crumb|cookie|bearer|authorization|password|token)\s*[:= ]", re.I)


def _safe(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        return {key: _safe(item, secrets) for key, item in value.items()
                if not _SECRET_KEY.search(key) and not _UNSAFE_TEXT.search(key)
                and not any(secret in key for secret in secrets if secret)}
    if isinstance(value, list):
        return [_safe(item, secrets) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if type(value) is int and not -(2**63) <= value < 2**63:
        return str(value)
    if isinstance(value, str):
        if _UNSAFE_TEXT.search(value) or any(secret in value for secret in secrets if secret):
            return "[redacted]"
    return value


def _json(content: bytes) -> dict | None:
    result = None
    try:
        result = json.loads(content, parse_float=Decimal, parse_constant=str)
    except (ValueError, UnicodeError, DecimalException, RecursionError):
        pass
    return result if isinstance(result, dict) else None


class _Session(Session):
    def __init__(self, before_request: Callable[[], None], timeout: float):
        super().__init__(impersonate="chrome", trust_env=False, retry=0)
        self.before_request = before_request
        self.request_timeout = timeout
        self.failure: BaseException | None = None
        self.captures: list[tuple[str, dict, dict | None]] = []
        self.secrets: set[str] = set()

    def abort(self, code: str) -> None:
        if self.failure is None:
            self.failure = _error(code)
        raise _Abort()

    def request(self, method, url, **kwargs):
        if self.failure is not None:
            raise _Abort()
        # No automatic redirects: every actual hop must reserve quota. Yahoo's
        # cookie bootstrap normally returns 404 *with* its cookie, which is not
        # a failed financial-data response.
        kwargs.update(timeout=self.request_timeout, allow_redirects=False)
        for _ in range(6):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or not (
                parsed.hostname == "yahoo.com" or parsed.hostname.endswith(".yahoo.com")
            ) or parsed.username or parsed.password:
                self.abort("http_error")
            try:
                self.before_request()
            except BaseException as exc:
                self.failure = exc
            if self.failure is not None:
                raise _Abort()
            response = None
            try:
                response = super().request(method, url, **kwargs)
            except Exception:
                pass
            if response is None:
                self.abort("transport_error")
            self.secrets.update(cookie.value for cookie in self.cookies.jar if cookie.value)
            status = response.status_code
            if status in (401, 403, 429, 451):
                self.abort({401: "authentication_error", 403: "plan_restricted", 429: "quota_exceeded", 451: "plan_restricted"}[status])
            if status >= 400 and not (parsed.hostname == "fc.yahoo.com" and status == 404):
                self.abort("http_error")
            if 300 <= status < 400:
                location = response.headers.get("Location")
                if not location:
                    self.abort("http_error")
                url = urljoin(url, location)
                # Query parameters belong to the original URL, never a redirect.
                kwargs.pop("params", None)
                if status == 303 or (status in (301, 302) and method.upper() == "POST"):
                    method = "GET"
                    for key in ("data", "json", "headers"):
                        kwargs.pop(key, None)
                continue
            if parsed.path.endswith("/getcrumb"):
                text = response.text
                if "too many requests" in text.lower():
                    self.abort("quota_exceeded")
                if any(marker in text.lower() for marker in ("access denied", "forbidden", "unauthorized")):
                    self.abort("plan_restricted")
                if not text.strip() or "<" in text:
                    self.abort("malformed_response")
                self.secrets.add(text)
            if "/finance/" in parsed.path and not parsed.path.endswith("/getcrumb"):
                payload = _json(response.content)
                self.captures.append((parsed.path, dict(kwargs.get("params") or {}), payload))
                # A JSON denial is as final as an HTTP denial, even with HTTP 200.
                for envelope in ("chart", "finance", "quoteSummary"):
                    wrapper = payload.get(envelope) if payload else None
                    error = wrapper.get("error") if isinstance(wrapper, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                    if code in ("Unauthorized", "Forbidden", "Too Many Requests"):
                        self.abort("quota_exceeded" if code == "Too Many Requests" else "plan_restricted")
            return response
        self.abort("http_error")


class _LocalData(YfData):
    # Instantiated with type.__call__ below to bypass SingletonMeta. Avoid disk
    # credentials and the base class's process-global lru_cache entirely.
    def _load_cookie_curlCffi(self):
        # The upstream basic-cookie path relies on loading its just-saved disk
        # cookie to avoid bootstrapping twice. Use only our in-memory jar.
        if any(cookie.domain.endswith("yahoo.com") for cookie in self._session.cookies.jar):
            self._cookie = True
            return True
        return False

    def _save_cookie_curlCffi(self):
        return self._load_cookie_curlCffi()

    def cache_get(self, url, params=None, timeout=30):
        return self.get(url, params=params, timeout=timeout)


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100 or any(char.isspace() for char in value):
        raise _error("invalid_request")
    return value.upper()


def _dates(start: str | date, end: str | date, offset: int) -> tuple[date, date]:
    result = None
    try:
        if isinstance(start, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            start = date.fromisoformat(start)
        if isinstance(end, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            end = date.fromisoformat(end)
        if type(start) is date and type(end) is date and start <= end and end < date.max and type(offset) is int and offset == 0:
            result = start, end
    except ValueError:
        pass
    if result is None:
        raise _error("invalid_request")
    return result


def _chart(payload: Any, symbol: str) -> dict:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise _error("malformed_response")
    if chart.get("error"):
        error = chart["error"]
        raise _error("invalid_symbol" if isinstance(error, dict) and error.get("code") == "Not Found" else "malformed_response")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise _error("malformed_response")
    result = results[0]
    meta = result.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("symbol"), str):
        raise _error("malformed_response")
    if meta["symbol"].upper() != symbol:
        raise _error("identity_mismatch")
    return result


def validate_index_identity(meta: Any, symbol: str) -> None:
    """No ETF/equity proxies and no fuzzy or root-symbol identity matches."""
    if (not isinstance(meta, dict) or meta.get("symbol") != symbol
            or meta.get("instrumentType") != "INDEX"):
        raise MarketDataError("index_identity_mismatch", "Yahoo must identify the exact requested symbol as an INDEX. Previous saved history remains active.")


def validate_index_page(page: dict, symbol: str) -> None:
    result = _chart(page.get("raw"), symbol)
    validate_index_identity(result["meta"], symbol)


def _tz(meta: dict) -> ZoneInfo:
    zone = None
    try:
        if isinstance(meta.get("exchangeTimezoneName"), str):
            zone = ZoneInfo(meta["exchangeTimezoneName"])
    except (ValueError, ZoneInfoNotFoundError):
        pass
    if zone is None:
        raise _error("malformed_response")
    return zone


def _day(timestamp: Any, zone: ZoneInfo) -> str | None:
    if isinstance(timestamp, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", timestamp):
        try:
            timestamp = Decimal(timestamp)
        except DecimalException:
            return None
    if type(timestamp) not in (int, Decimal):
        return None
    if isinstance(timestamp, Decimal) and not timestamp.is_finite():
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).astimezone(zone).date().isoformat()
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _array_item(container: Any, key: str, index: int) -> Any:
    values = container.get(key) if isinstance(container, dict) else None
    return values[index] if isinstance(values, list) and index < len(values) else None


def _first(value: Any) -> dict:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}


def _page(rows: list, payload: dict, secrets: set[str] | None = None) -> dict:
    return _safe({"pagination": {"limit": len(rows), "offset": 0, "count": len(rows), "total": len(rows)},
                  "data": rows, "raw": deepcopy(payload), "provider": "yfinance",
                  "price_basis": "provider_split_adjusted"}, secrets or set())


def _split_factor(event: dict) -> Decimal | None:
    try:
        numerator = Decimal(str(event.get("numerator")))
        denominator = Decimal(str(event.get("denominator")))
        factor = numerator / denominator
    except (DecimalException, ValueError, TypeError):
        return None
    return factor if factor.is_finite() and factor > 0 else None


def _splits_from_payload(payload: Any, symbol: str, start: date, end: date, secrets: set[str] | None = None) -> dict:
    result = _chart(payload, symbol)
    zone = _tz(result["meta"])
    events = result.get("events")
    splits = events.get("splits") if isinstance(events, dict) else None
    rows = []
    for event in splits.values() if isinstance(splits, dict) else []:
        if not isinstance(event, dict):
            continue
        day = _day(event.get("date"), zone)
        if day is None or not start.isoformat() <= day <= end.isoformat():
            continue
        rows.append({"date": day, "symbol": symbol, "split_factor": _split_factor(event), "yahoo": deepcopy(event)})
    return _page(rows, payload, secrets)


def splits_from_eod_page(page: dict, symbol: str, date_from: str | date, date_to: str | date) -> dict:
    """Project a Yahoo split page from a persisted EOD page's raw chart payload."""
    symbol = _symbol(symbol)
    start, end = _dates(date_from, date_to, 0)
    if not isinstance(page, dict) or "raw" not in page:
        raise _error("malformed_response")
    return _splits_from_payload(page["raw"], symbol, start, end)


class YahooFinanceClient:
    name = "yfinance"

    def __init__(self, api_key: str = "", before_request: Callable[[], None] | None = None, timeout: float = 15):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise _error("invalid_request")
        self._session = _Session(before_request if before_request is not None else lambda: None, timeout)
        self._data = type.__call__(_LocalData, session=self._session)
        self._timeout = timeout
        self._closed = False
        self._metadata: dict[str, dict] = {}
        self._charts: dict[tuple[str, date, date], dict] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()
        self._closed = True
        self._charts.clear()
        self._metadata.clear()
        self._session.captures.clear()
        self._session.secrets.clear()

    def _ready(self) -> None:
        if self._closed:
            raise _error("client_closed")
        if self._session.failure is not None:
            raise self._session.failure

    def _run(self, operation: Callable[[], Any], *, tolerate_normalization: bool = False) -> Any:
        self._ready()
        token = _private_request.set(True)
        failed = False
        result = None
        try:
            try:
                result = operation()
            except _Abort:
                failed = True
            except YFRateLimitError:
                self._session.failure = _error("quota_exceeded")
            except Exception:
                # Do not retain provider exceptions, even through __context__.
                failed = True
        finally:
            _private_request.reset(token)
        if self._session.failure is not None:
            raise self._session.failure
        if failed and not tolerate_normalization:
            raise _error("malformed_response")
        return result

    def _get_chart_meta(self, symbol: str) -> dict:
        self._ready()
        if symbol not in self._metadata:
            self._session.captures.clear()
            self._run(lambda: self._data.get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
                params={"range": "1d", "interval": "1d"}, timeout=self._timeout,
            ))
            captures = self._session.captures
            if not captures:
                raise _error("malformed_response")
            payload = captures[-1][2]
            result = _chart(payload, symbol)
            self._metadata[symbol] = deepcopy(result["meta"])
        return self._metadata[symbol]

    def tickerinfo(self, symbol: str) -> dict:
        symbol = _symbol(symbol)
        meta = self._get_chart_meta(symbol)
        return _safe({**deepcopy(meta), "symbol": symbol, "currency": meta.get("currency"),
                      "name": meta.get("longName") or meta.get("shortName"),
                      "profile_source": "chart_metadata", "price_basis": "provider_split_adjusted"}, self._session.secrets)

    def _history(self, symbol: str, start: date, end: date) -> dict:
        self._ready()
        key = symbol, start, end
        if key in self._charts:
            return self._charts[key]
        meta = self._get_chart_meta(symbol)
        zone = _tz(meta)
        self._session.captures.clear()
        history = PriceHistory(self._data, symbol, zone.key, session=self._session)
        # A malformed quote may break yfinance's DataFrame assembly, but is still
        # evidence. Only normalization failures can be tolerated; transport,
        # callback, identity and provider errors are never suppressed.
        self._run(lambda: history.history(
            start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
            interval="1d", auto_adjust=False, back_adjust=False, repair=False,
            keepna=True, actions=True, rounding=False, timeout=self._timeout,
        ), tolerate_normalization=True)
        captures = [payload for path, params, payload in self._session.captures
                    if "/chart/" in path and "period1" in params and "period2" in params]
        if len(captures) != 1:
            raise _error("malformed_response")
        payload = captures[0]
        result = _chart(payload, symbol)
        _tz(result["meta"])
        self._charts[key] = payload
        return payload

    def _page(self, rows: list, payload: dict) -> dict:
        return _page(rows, payload, self._session.secrets)

    def eod_page(self, symbol: str, exchange: str, date_from: str | date, date_to: str | date, offset: int = 0) -> dict:
        symbol = _symbol(symbol)
        if not isinstance(exchange, str) or not exchange.strip():
            raise _error("invalid_request")
        start, end = _dates(date_from, date_to, offset)
        payload = self._history(symbol, start, end)
        result = _chart(payload, symbol)
        meta = result["meta"]
        zone = _tz(meta)
        timestamps = result.get("timestamp")
        indicators = result.get("indicators")
        indicators = indicators if isinstance(indicators, dict) else {}
        quotes = _first(indicators.get("quote"))
        adj = _first(indicators.get("adjclose"))
        rows = []
        for index, timestamp in enumerate(timestamps if isinstance(timestamps, list) else []):
            day = _day(timestamp, zone)
            if day is None or not start.isoformat() <= day <= end.isoformat():
                continue
            rows.append({**{key: _array_item(quotes, key, index) for key in ("open", "high", "low", "close", "volume")},
                         "adj_close": _array_item(adj, "adjclose", index), "symbol": symbol,
                         "exchange": exchange, "date": day, "price_currency": meta.get("currency"),
                         "price_basis": "provider_split_adjusted", "yahoo_timestamp": timestamp})
        return self._page(rows, payload)

    def splits_page(self, symbol: str, date_from: str | date, date_to: str | date, offset: int = 0) -> dict:
        symbol = _symbol(symbol)
        start, end = _dates(date_from, date_to, offset)
        payload = self._history(symbol, start, end)
        return _splits_from_payload(payload, symbol, start, end, self._session.secrets)

    def tickerslist_page(self, search: str, offset: int = 0) -> dict:
        if not isinstance(search, str) or not search.strip() or type(offset) is not int or offset != 0:
            raise _error("invalid_request")
        self._session.captures.clear()
        self._run(lambda: self._data.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": search, "quotesCount": 50, "newsCount": 0, "listsCount": 0,
                    "enableFuzzyQuery": False}, timeout=self._timeout,
        ))
        captures = self._session.captures
        payload = captures[-1][2] if captures else None
        if not isinstance(payload, dict) or not isinstance(payload.get("quotes"), list):
            raise _error("malformed_response")
        return self._page(deepcopy(payload["quotes"]), payload)

    def resolve_symbol(self, symbol: str) -> tuple[dict, dict]:
        symbol = _symbol(symbol)
        meta = self._get_chart_meta(symbol)
        exchange_name = meta.get("exchangeName")
        if not isinstance(exchange_name, str):
            raise _error("unknown_exchange")
        mic = YAHOO_EXCHANGE_MICS.get(exchange_name)
        if mic is None:
            raise _error("unknown_exchange")
        data = _safe(deepcopy(meta), self._session.secrets)
        return {"symbol": symbol, "exchange": mic}, {
            "source": "yahoo_chart_metadata", "query": symbol,
            "name": data.get("longName") or data.get("shortName"),
            "exchangeName": exchange_name, "data": data,
        }

    def resolve_listing(self, ticker: str, isin: str) -> tuple[dict, dict]:
        ticker = _symbol(ticker)
        if not isinstance(isin, str):
            raise _error("invalid_request")
        isin = isin.upper()
        queries = [ticker]
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            queries.append(isin)
        for query in queries:
            page = self.tickerslist_page(query)
            quotes = [row for row in page["data"] if isinstance(row, dict) and isinstance(row.get("symbol"), str)
                      and row["symbol"].strip() and row.get("quoteType") in ("ETF", "EQUITY", "INDEX", "MUTUALFUND")
                      and ("isin" not in row or not isin or row["isin"] == isin)]
            exact = [row for row in quotes if row["symbol"].upper() == ticker]
            roots = [row for row in quotes if _ticker_root(row["symbol"].upper()) == _ticker_root(ticker)]
            candidates = exact or roots
            if not candidates and query != ticker:
                # Yahoo's explicit ISIN search may return a differently named
                # listing. Accept only a unique result, never substring matches.
                candidates = [row for row in quotes if isinstance(row.get("isin", query), str)
                              and row.get("isin", query).upper() == query]
            if any(not isinstance(row.get("exchange"), str) for row in candidates):
                raise _error("unknown_exchange")
            unique = {(row["symbol"].upper(), row["exchange"]): row for row in candidates}
            if len(unique) > 1:
                raise _error("listing_not_found")
            if len(unique) == 1:
                selected = next(iter(unique.values()))
                mic = YAHOO_EXCHANGE_MICS.get(selected.get("exchange"))
                if mic is None:
                    raise _error("unknown_exchange")
                listing = {"symbol": selected["symbol"].upper(), "exchange": mic}
                return listing, {"source": "yahoo_search", "query": query,
                                 "name": selected.get("longname") or selected.get("shortname"),
                                 "data": deepcopy(selected), "raw": page["raw"]}
        raise _error("listing_not_found")


def _ticker_root(symbol: str) -> str:
    root, separator, suffix = symbol.rpartition(".")
    return root if separator and suffix in _LISTING_SUFFIXES else symbol
