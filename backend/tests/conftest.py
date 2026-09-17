from unittest.mock import Mock

import httpx
import pytest

from etfs import ETF_SOURCE_URL


@pytest.fixture
def etf_records():
    return [
        {
            "isin": "IE00B4L5Y983",
            "name": "Example World UCITS ETF",
            "ticker": "EXAMPLE",
            "update_date": "2026-09-12",
            "ter": 0.2,
            "hedged": False,
            "number_of_holdings": None,
            "holdings": [{"name": "Example holding", "percentage": 5.25, "isin": "US0378331005"}],
            "countries": [{"name": "United States", "percentage": 70.5}],
            "sectors": [],
            "future_source_field": {"values": [1, None, "value"]},
        },
        {"isin": "IE00B3XXRP09", "name": "Example S&P 500 UCITS ETF", "ticker": "EXAMPLE"},
    ]


@pytest.fixture
def etf_http_get(monkeypatch, etf_records):
    get = Mock(return_value=httpx.Response(
        200, json=etf_records, request=httpx.Request("GET", ETF_SOURCE_URL),
    ))
    monkeypatch.setattr("etfs.httpx.get", get)
    return get
