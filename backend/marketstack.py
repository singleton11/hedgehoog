"""Retired provider compatibility boundary; never performs provider requests.

The exception alias allows stored-job tooling to decode old failures. Marketstack
payload ownership is inventoried and purged by market_data_retention.
"""
from market_provider import MarketDataError

MarketstackError = MarketDataError


class MarketstackClient:
    def __init__(self, *args, **kwargs):
        raise MarketDataError("provider_retired", "Marketstack is retired. Request Yahoo history instead.")
