"""Permissive, single-attempt Marketstack acquisition; pagination belongs to the service.

Tickerinfo returns the provider's data object; ticker search and EOD return envelopes.
JSON fractional numbers become exact decimal strings (including in unknown fields);
integer tokens stay integers when BSON-representable. Non-finite tokens become strings.
No provider text, request URL, or credentials are included in public errors.
"""

from collections.abc import Callable
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import logging
import re
from types import TracebackType
from typing import Any, Self

import httpx

from market_provider import MarketDataError


_MESSAGES = {
    "authentication_error": "Marketstack authentication failed.",
    "quota_exceeded": "Marketstack request quota is exhausted.",
    "plan_restricted": "The Marketstack plan does not allow this request.",
    "invalid_symbol": "Marketstack did not recognize the requested symbol.",
    "http_error": "Marketstack returned an unsuccessful response.",
    "transport_error": "Marketstack could not be reached. No automatic retry was made.",
    "malformed_response": "Marketstack returned an unreadable response or an unsupported response envelope.",
    "invalid_request": "The Marketstack request parameters are invalid.",
}
_PROVIDER_ERRORS = {
    "invalid_access_key": "authentication_error",
    "missing_access_key": "authentication_error",
    "inactive_user": "authentication_error",
    "usage_limit_reached": "quota_exceeded",
    "rate_limit_reached": "quota_exceeded",
    "function_access_restricted": "plan_restricted",
    "https_access_restricted": "plan_restricted",
    "invalid_symbol": "invalid_symbol",
    "invalid_symbols": "invalid_symbol",
    "invalid_ticker": "invalid_symbol",
    "symbol_not_found": "invalid_symbol",
    "ticker_not_found": "invalid_symbol",
}


# Compatibility for legacy fixtures/callers; application code uses the neutral error.
MarketstackError = MarketDataError


def _error(code: str) -> MarketstackError:
    return MarketstackError(code, _MESSAGES[code])


# httpx's INFO request log includes access_key in the URL; httpcore DEBUG logs
# can include arbitrary response headers and transport exception text. Suppress
# these records only inside this client's synchronous request, not other threads
# or unrelated HTTP clients. Logger levels/global logging remain unchanged.
_private_request = ContextVar("marketstack_private_request", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_request.get()


_http_log_filter = _PrivateRequestFilter()
for _logger_name in (
    "httpx", "httpcore", "httpcore.connection", "httpcore.connection_pool",
    "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks",
):
    logging.getLogger(_logger_name).addFilter(_http_log_filter)


def _exact_values(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return format(value, "f")
    if type(value) is int and not -(2**63) <= value < 2**63:
        return str(value)
    if isinstance(value, dict):
        return {key: _exact_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_exact_values(item) for item in value]
    return value


def _calendar_date(value: Any, *, timestamp: bool = False) -> date | None:
    if type(value) is date:
        return value if not timestamp else None
    if not isinstance(value, str):
        return None
    result = None
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            result = date.fromisoformat(value)
        elif timestamp and re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T", value):
            result = datetime.fromisoformat(value).date()
    except ValueError:
        pass
    return result


def _http_error(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status in (402, 403):
        return "plan_restricted"
    if status == 429:
        return "quota_exceeded"
    return "http_error"


def _page_rows(payload: dict, offset: int) -> list:
    # The only required page shape is an array. Pagination metadata and row
    # contents are provider evidence, not reasons to discard the download.
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise _error("malformed_response")
    return rows


class MarketstackClient:
    """Use as a context manager; before_request reserves quota for each attempt.

    Callback failures propagate unchanged and prevent the HTTP attempt. Neither
    failed requests nor pages are retried or followed automatically.
    The caller owns pagination progress and its request safety cap.
    """

    def __init__(
        self,
        api_key: str,
        before_request: Callable[[], None],
        transport: httpx.BaseTransport | None = None,
    ):
        self._api_key = api_key
        self._before_request = before_request
        self._client = httpx.Client(
            base_url="https://api.marketstack.com",
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            transport=transport if transport is not None else httpx.HTTPTransport(
                retries=0, trust_env=False,
            ),
        )

    def __enter__(self) -> Self:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        request = self._client.build_request(
            "GET", path, params={"access_key": self._api_key, **params},
        )
        response = None
        token = _private_request.set(True)
        try:
            self._before_request()
            try:
                response = self._client.send(request)
            except (httpx.HTTPError, OSError):
                # Raise below, outside the except block: even __context__ must
                # not retain a credential-bearing transport exception.
                pass
        finally:
            _private_request.reset(token)
        if response is None:
            raise _error("transport_error")

        payload = None
        try:
            payload = json.loads(
                response.content, parse_float=Decimal, parse_constant=str,
            )
        except (ValueError, UnicodeError, InvalidOperation, RecursionError):
            pass
        successful = 200 <= response.status_code < 300
        if not isinstance(payload, dict):
            raise _error("malformed_response" if successful else _http_error(response.status_code))
        if "error" in payload:
            provider_error = payload["error"]
            if not isinstance(provider_error, dict):
                raise _error("malformed_response" if successful else _http_error(response.status_code))
            provider_type = provider_error.get("type")
            # Whitelist lookup only: never reflect arbitrary provider types,
            # messages, contexts, URLs or numeric codes into public errors.
            code = _PROVIDER_ERRORS.get(provider_type) if isinstance(provider_type, str) else None
            raise _error(code or _http_error(response.status_code))
        if not successful:
            raise _error(_http_error(response.status_code))
        return payload

    def tickerinfo(self, symbol: str) -> Any:
        if not isinstance(symbol, str) or not symbol.strip():
            raise _error("invalid_request")
        payload = self._get("/v2/tickerinfo", {"ticker": symbol})
        return _exact_values(payload.get("data", payload))

    def tickerslist_page(self, search: str, offset: int = 0) -> dict:
        if (
            not isinstance(search, str) or not search.strip()
            or type(offset) is not int or offset < 0
        ):
            raise _error("invalid_request")
        payload = self._get("/v2/tickerslist", {"search": search, "limit": 1000, "offset": offset})
        _page_rows(payload, offset)
        return _exact_values(payload)

    def splits_page(self, symbol: str, date_from: str | date, date_to: str | date, offset: int = 0) -> dict:
        start, end = _calendar_date(date_from), _calendar_date(date_to)
        if (
            not isinstance(symbol, str) or not symbol.strip()
            or start is None or end is None or start > end
            or type(offset) is not int or offset < 0
        ):
            raise _error("invalid_request")
        # Unlike EOD, splits are scoped by the full provider symbol, not MIC.
        payload = self._get("/v2/splits", {
            "symbols": symbol, "date_from": start.isoformat(), "date_to": end.isoformat(),
            "limit": 1000, "sort": "ASC", "offset": offset,
        })
        _page_rows(payload, offset)
        return _exact_values(payload)

    def eod_page(
        self,
        symbol: str,
        exchange: str,
        date_from: str | date,
        date_to: str | date,
        offset: int = 0,
    ) -> dict:
        start, end = _calendar_date(date_from), _calendar_date(date_to)
        if (
            not isinstance(symbol, str) or not symbol.strip()
            or not isinstance(exchange, str) or not exchange.strip()
            or start is None or end is None or start > end
            or type(offset) is not int or offset < 0
        ):
            raise _error("invalid_request")
        payload = self._get("/v2/eod", {
            "symbols": symbol,
            "exchange": exchange,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "limit": 1000,
            "sort": "ASC",
            "offset": offset,
        })
        _page_rows(payload, offset)
        return _exact_values(payload)
