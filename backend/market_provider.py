"""Structural acquisition contract shared by market-data providers."""

from datetime import date
from types import TracebackType
from typing import Any, Protocol, Self


class MarketDataError(Exception):
    """An application-safe code and message, never a provider exception."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class MarketDataProvider(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc_value: BaseException | None, traceback: TracebackType | None,
    ) -> None: ...

    def close(self) -> None: ...

    def tickerinfo(self, symbol: str) -> Any: ...

    def tickerslist_page(self, search: str, offset: int = 0) -> dict: ...

    def eod_page(
        self, symbol: str, exchange: str, date_from: str | date,
        date_to: str | date, offset: int = 0,
    ) -> dict: ...

    def splits_page(
        self, symbol: str, date_from: str | date, date_to: str | date,
        offset: int = 0,
    ) -> dict: ...
