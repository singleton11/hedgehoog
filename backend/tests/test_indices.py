"""Offline catalogue/API and shared-worker contracts for real index identities."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pymongo.errors import ConnectionFailure

import indices
import market_data as md
from test_market_data import api, clock, config, db, page, Provider, ISIN, collect
from test_yahoo_finance import transport, START, END
from yahoo_finance import YahooFinanceClient, validate_index_page


@pytest.fixture
def index(db):
    indices.ensure_catalogue(db)
    return db.indices.find_one({"symbol": "^GSPC"})


class IndexProvider(Provider):
    def __init__(self, symbol="^GSPC", kind="INDEX", history_symbol=None, history_kind=None, currency=None, close="100", empty=False):
        meta = {"symbol": symbol, "instrumentType": kind, "exchangeTimezoneName": "UTC", "currency": currency}
        history_meta = {**meta, "symbol": history_symbol or symbol, "instrumentType": history_kind or kind}
        rows = [] if empty else [{"date": "2026-09-15", "close": close, "open": None, "high": None,
                                 "low": None, "volume": None, "symbol": symbol, "price_currency": currency}]
        evidence = {**page(rows), "raw": {"chart": {"result": [{"meta": history_meta, "events": {}}]}}}
        super().__init__([evidence], profile=meta)

    def factory(self, key, before_request):
        self.before_request = before_request
        return self


def request(db, config, index, publish=None):
    return md.request_acquisition(db, config, index["instrument_id"], None, "update", "user", publish or Mock())


def run(db, config, index, source=None):
    job = request(db, config, index)
    source = source or IndexProvider()
    return md.run_acquisition(db, config, job["id"], source.factory), source


def login(client):
    assert client.post("/auth/login", json={"login": "admin", "password": "admin"}).status_code == 200


def test_fresh_catalogue_matches_active_installation_indices(db):
    catalogue = indices.list_indices(db)
    assert {row["symbol"]: row["name"] for row in catalogue["items"]} == {
        "^GSPC": "S&P 500",
        "^990100-USD-STRD": "MSCI WORLD",
        "DE000SLA1W63.SG": "Solactive European Overnight Money Market Index",
        "DE000SL0H423.SG": "Solactive \u20acSTR Daily Total Return Index",
    }
    assert db.acquisition_jobs.count_documents({}) == 0
    assert db.quotes.count_documents({}) == 0
    assert db.instruments.count_documents({}) == 0


def test_new_seed_preserves_existing_manual_record_and_removal(db):
    identifier = "idx-" + str(uuid4())
    record = {
        "_id": identifier, "instrument_id": identifier, "symbol": "DE000SL0H423.SG",
        "name": "User label", "currency": "EUR", "return_variant": "net_total_return",
        "provenance": {"source": "manual", "added_by": "user"},
    }
    db.indices.insert_one(record)
    indices.ensure_catalogue(db)
    assert db.indices.find_one({"instrument_id": identifier}) == record
    indices.remove_index(db, identifier, "user")
    removed = db.indices.find_one({"instrument_id": identifier})
    indices.ensure_catalogue(db)
    assert db.indices.find_one({"instrument_id": identifier}) == removed
    assert indices.list_indices(db)["total"] == 3


def test_seed_is_idempotent_separate_and_stable(db):
    first = indices.list_indices(db)
    second = indices.list_indices(db)
    assert first == second and first["total"] == len(indices.SEEDS)
    for row in first["items"]:
        UUID(row["instrument_id"].removeprefix("idx-"))
        assert row["currency"] is None and row["return_variant"] == "unknown"
        assert row["verification_status"] == "unverified" and row["acquisition"] is None
        assert row["summary"] == {"quote_count": 0, "first_date": None, "last_date": None}
        assert "isin" not in row and "inception_date" not in row
    assert db.etfs.count_documents({}) == 2 + 1
    assert md.search_catalogue(db, "NASDAQ", 10) == []
    assert indices.list_indices(db, 2, 2)["items"] == first["items"][2:4]


def test_all_routes_require_auth(api, index):
    client, _ = api
    base = "/indices/" + index["instrument_id"]
    for url in ("/indices", base, base + "/quotes"):
        assert client.get(url).status_code == 401
    assert client.post("/indices", json={"symbol": "^NEW", "name": "New"}).status_code == 401
    assert client.post(base + "/acquisitions", json={"confirmed": True}).status_code == 401


def test_reads_and_add_never_call_provider_or_queue(api, db, monkeypatch):
    client, _ = api
    network = Mock(side_effect=AssertionError("Unexpected provider call"))
    publish = Mock(side_effect=AssertionError("Unexpected queue publication"))
    monkeypatch.setattr(md, "provider_client", network)
    monkeypatch.setattr("index_routes.enqueue_acquisition", publish)
    login(client)
    record = client.post("/indices", json={"symbol": " ^manual ", "name": " Manual index ", "currency": " eur ", "return_variant": "net_total_return"})
    assert record.status_code == 201
    row = record.json()
    assert (row["symbol"], row["name"], row["currency"], row["return_variant"]) == ("^MANUAL", "Manual index", "EUR", "net_total_return")
    assert row["provenance"]["source"] == "manual" and row["verification_status"] == "unverified"
    assert "added_by" not in row["provenance"]
    duplicate = client.post("/indices", json={"symbol": "^MANUAL", "name": "Do not overwrite", "currency": None})
    assert duplicate.json() == row
    indices.ensure_catalogue(db)
    assert db.indices.count_documents({"symbol": "^MANUAL"}) == 1
    base = "/indices/" + row["instrument_id"]
    assert client.get(base).json() == row
    assert client.get(base + "/quotes").json()["items"] == []
    assert client.get("/indices?limit=2&offset=1").json()["total"] == len(indices.SEEDS) + 1
    assert client.get("/instruments/search?q=MANUAL").json()["items"] == []
    assert client.get("/instruments/acquired").json()["total"] == 0
    assert db.acquisition_jobs.count_documents({}) == 0
    network.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("body", [
    {"symbol": "^NEW", "name": " "}, {"symbol": "AAPL,MSFT", "name": "Index"},
    {"symbol": "^NEW", "name": "Index", "currency": "EURO"},
    {"symbol": "^NEW", "name": "Index", "return_variant": "total_return"},
    {"symbol": "^NEW", "name": "Index", "isin": ISIN},
])
def test_invalid_add_does_not_write(api, db, body):
    client, _ = api
    login(client)
    count = db.indices.count_documents({})
    assert client.post("/indices", json=body).status_code == 422
    assert db.indices.count_documents({}) == count


def test_explicit_confirmation_dedup_and_validation(api, db, index, monkeypatch):
    client, _ = api
    publish = Mock()
    monkeypatch.setattr("index_routes.enqueue_acquisition", publish)
    login(client)
    base = "/indices/" + index["instrument_id"]
    for body in ({}, {"confirmed": False}, {"confirmed": 1}, {"confirmed": "true"}, {"confirmed": True, "symbol": "SPY"}):
        assert client.post(base + "/acquisitions", json=body).status_code == 422
    assert client.post(base + "/acquisitions", json={"confirmed": True}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert db.acquisition_jobs.count_documents({}) == 0
    response = client.post(base + "/acquisitions", json={"confirmed": True})
    assert response.status_code == 202
    job = response.json()["job"]
    assert job["status"] == "queued" and job["instrument_id"] == index["instrument_id"]
    assert client.post(base + "/acquisitions", json={"confirmed": True}).json()["job"]["id"] == job["id"]
    assert client.get(base).json()["acquisition"]["id"] == job["id"]
    publish.assert_called_once()
    for query in ("limit=0", "limit=101", "offset=-1"):
        assert client.get("/indices?" + query).status_code == 422
    assert client.get(base + "/quotes?limit=1001").status_code == 422
    assert client.get(base + "/quotes?as_of=2026-01-01T00:00:00").status_code == 422
    assert client.get("/indices/idx-" + str(uuid4())).status_code == 404
    assert client.post("/indices/idx-" + str(uuid4()) + "/acquisitions", json={"confirmed": True}).status_code == 404
    assert client.get("/indices/" + ISIN).status_code == 422


def test_bulk_acquisition_queues_every_active_index(api, db, index, monkeypatch):
    client, _ = api
    publish = Mock()
    monkeypatch.setattr("index_routes.enqueue_acquisition", publish)
    login(client)
    response = client.post("/indices/acquisitions", json={"confirmed": True})
    assert response.status_code == 202
    jobs = response.json()["jobs"]
    active = list(db.indices.find({"removed_at": {"$exists": False}}))
    assert {job["instrument_id"] for job in jobs} == {row["instrument_id"] for row in active}
    assert publish.call_count == len(active)


def test_worker_uses_yahoo_full_boundary_no_fake_inception_and_keeps_unknowns(db, config, index):
    # Indices explicitly use Yahoo even when legacy ETF acquisitions are configured.
    result, source = run(db, config, index)
    assert result["status"] == "succeeded"
    job = db.acquisition_jobs.find_one({"_id": result["id"]})
    assert job["provider"] == "yfinance" and job["instrument_kind"] == "index"
    assert job["date_from"] == job["splits_date_from"] == indices.HISTORY_START
    assert source.calls == [("tickerinfo", "^GSPC"), ("eod", "^GSPC", "YAHOO", indices.HISTORY_START, "2026-09-17", 0)]
    assert db.marketstack_usage.count_documents({}) == 0
    assert db.raw_eod_pages.count_documents({}) == db.raw_split_pages.count_documents({}) == 1
    detail = indices.index_detail(db, index["instrument_id"])
    assert detail["verification_status"] == "verified"
    assert detail["currency"] is None and detail["return_variant"] == "unknown"
    assert detail["summary"]["quote_count"] == 1
    assert md.read_quotes(db, index["instrument_id"])["items"][0]["open"] is None
    assert md.acquired_instruments(db)["total"] == 0
    assert db.instrument_statistics.count_documents({"_id": index["instrument_id"]}) == 0
    with pytest.raises(md.DataError, match="ETF catalogue"):
        md.require_catalogue(db, index["instrument_id"])
    # Existing ETF acquisition and inventory are still independent.
    collect(db, config)
    assert [row["instrument_id"] for row in md.acquired_instruments(db)["items"]] == [ISIN]


@pytest.mark.parametrize("options", [
    {"kind": "ETF"}, {"kind": "EQUITY"}, {"kind": None}, {"symbol": "^GSPC.X"},
    {"history_kind": "ETF"}, {"history_symbol": "SPY"},
])
def test_exact_index_identity_required_on_profile_and_history(db, config, index, options):
    result, _ = run(db, config, index, IndexProvider(**options))
    assert result["status"] == "failed"
    assert not db.acquisition_jobs.find_one({"_id": result["id"]}).get("published")
    assert indices.index_detail(db, index["instrument_id"])["verification_status"] == "unverified"
    assert md.read_quotes(db, index["instrument_id"])["total"] == 0


def test_failed_refresh_preserves_generation_metadata_and_pagination(db, config, index, clock):
    first, _ = run(db, config, index, IndexProvider(currency="USD"))
    before = indices.index_detail(db, index["instrument_id"])
    clock[0] += timedelta(seconds=2)
    failed, _ = run(db, config, index, IndexProvider(history_kind="ETF", currency="EUR", close="999"))
    assert failed["status"] == "failed"
    after = indices.index_detail(db, index["instrument_id"])
    assert after["generation"] == first["id"] and after["currency"] == "USD"
    assert after["profile"] == before["profile"] and after["summary"] == before["summary"]
    assert after["acquisition"]["id"] == failed["id"]
    old = md.read_quotes(db, index["instrument_id"], generation=first["id"])
    assert old["items"][0]["close"] == "100"
    clock[0] += timedelta(seconds=2)
    second, _ = run(db, config, index, IndexProvider(close="200"))
    assert md.read_quotes(db, index["instrument_id"])["generation"] == second["id"]
    assert md.read_quotes(db, index["instrument_id"], generation=first["id"])["items"][0]["close"] == "100"


def test_empty_history_and_request_limit_do_not_publish(db, config, index):
    result, _ = run(db, config, index, IndexProvider(empty=True))
    assert result["status"] == "failed"
    config.market_data_max_calls_per_job = 1
    result, source = run(db, config, index)
    assert result["status"] == "failed" and len(source.calls) == 1
    assert db.market_data_publications.count_documents({}) == 0


def test_replay_saved_index_evidence_without_provider_calls(db, config, index, clock, monkeypatch):
    original = db.raw_split_pages.find_one
    interrupted = [False]
    def interrupt(*args, **kwargs):
        if not interrupted[0]:
            interrupted[0] = True
            raise ConnectionFailure("restart after chart persisted")
        return original(*args, **kwargs)
    monkeypatch.setattr(db.raw_split_pages, "find_one", interrupt)
    job = request(db, config, index)
    source = IndexProvider()
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    fresh = IndexProvider()
    fresh.tickerinfo = fresh.eod_page = fresh.splits_page = Mock(side_effect=AssertionError("No HTTP on replay"))
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    assert md.run_acquisition(db, config, job["id"], fresh.factory)["status"] == "succeeded"
    fresh.eod_page.assert_not_called()


def test_expired_worker_cannot_publish_index_generation(db, config, index, clock, monkeypatch):
    original = db.market_data_publications.update_one
    replacement = []
    def interrupt(*args, **kwargs):
        if not replacement:
            replacement.append(None)
            clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
            md.expire_stalled_jobs(db, index["instrument_id"])
            replacement[0], _ = run(db, config, index, IndexProvider(close="200"))
        return original(*args, **kwargs)
    monkeypatch.setattr(db.market_data_publications, "update_one", interrupt)
    expired, _ = run(db, config, index)
    assert expired["status"] == "failed" and replacement[0]["status"] == "succeeded"
    quotes = md.read_quotes(db, index["instrument_id"])
    assert quotes["generation"] == replacement[0]["id"] and quotes["items"][0]["close"] == "200"


def test_real_yfinance_normalizes_close_only_index_and_preserves_raw(transport):
    state, calls = transport
    for payload in (state["meta"], state["history"]):
        result = payload["chart"]["result"][0]
        result["meta"].update(symbol="^GSPC", instrumentType="INDEX")
        result["meta"].pop("currency")
        result["indicators"] = {"quote": [{"close": [10.5, 11.25]}]}
        result.pop("events")
    with YahooFinanceClient() as client:
        result = client.eod_page("^GSPC", "YAHOO", START, END)
    validate_index_page(result, "^GSPC")
    assert [row["close"] for row in result["data"]] == ["10.5", "11.25"]
    assert all(row[field] is None for row in result["data"] for field in ("open", "high", "low", "volume", "adj_close", "price_currency"))
    assert set(result["raw"]["chart"]["result"][0]["indicators"]["quote"][0]) == {"close"}
    assert len(calls) == 4


def test_worker_publishes_index_with_preserved_empty_pre_listing_chunk(db, config, index, transport):
    from test_yahoo_finance import stamp, no_data_response
    state, calls = transport
    for payload in (state['meta'], state['history']):
        payload['chart']['result'][0]['meta'].update(
            symbol=index['symbol'], instrumentType='INDEX', firstTradeDate=stamp(START), currency='EUR')
    def handler(session, method, url, kwargs):
        params = kwargs.get('params', {})
        if 'period2' in params and params['period2'] < stamp(START):
            return no_data_response(url, params)
    state['handler'] = handler
    job = request(db, config, index)
    result = md.run_acquisition(db, config, job['id'],
                               lambda key, before_request: YahooFinanceClient(before_request=before_request))
    assert result['status'] == 'succeeded'
    saved = md.read_quotes(db, index['instrument_id'])
    assert saved['generation'] == job['id'] and saved['total'] == 2
    raw = db.raw_eod_pages.find_one({'job_id': job['id']})['data']
    assert 'empty_history' in raw['raw']['history_chunks'][0]['raw']
    validate_index_page(raw, index['symbol'])
    assert len([c for c in calls if 'period1' in c[2].get('params', {})]) == 2


def test_eur_index_cannot_enter_portfolio_selector_or_goal_parameters(api, db, config, index):
    from goal_strategies import BuyAndHoldParameters
    from portfolio_tracker import acquired_instrument
    from pydantic import ValidationError
    client, _ = api
    login(client)
    result, _ = run(db, config, index, IndexProvider(currency="EUR"))
    assert result["status"] == "succeeded"
    assert client.get("/portfolio-instruments").json()["items"] == []
    assert client.get("/instruments/acquired").json()["items"] == []
    with pytest.raises(md.DataError, match="not tradable"):
        acquired_instrument(db, index["instrument_id"], md.utcnow())
    with pytest.raises(ValidationError):
        BuyAndHoldParameters(instrument=index["instrument_id"])


def test_index_quotes_api_exact_contract_and_generation(api, db, config, index):
    client, _ = api
    login(client)
    job, _ = run(db, config, index, IndexProvider(close="123.1234567890123456789"))
    response = client.get("/indices/" + index["instrument_id"] + "/quotes?limit=1")
    assert response.status_code == 200
    result = response.json()
    assert result["generation"] == job["id"] and result["total"] == 1
    assert result["items"][0]["close"] == "123.1234567890123456789"
    assert result["items"][0]["volume"] is None
    assert result["items"][0]["quality_flags"] == []
    assert client.get("/indices/" + index["instrument_id"] + "/quotes?generation=" + str(uuid4())).status_code == 409


def test_concurrent_symbol_add_is_idempotent(db):
    values = {"symbol": "^CUSTOM", "name": "Custom", "currency": None, "return_variant": "unknown"}
    indices.ensure_catalogue(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: indices.add_index(db, values, "user"), range(8)))
    assert len({row["instrument_id"] for row in results}) == 1
    assert db.indices.count_documents({"symbol": "^CUSTOM"}) == 1


def test_broker_failure_leaves_retriable_terminal_job(db, config, index):
    with pytest.raises(md.DataError, match="Could not queue"):
        request(db, config, index, Mock(side_effect=RuntimeError("offline broker")))
    assert db.acquisition_jobs.find_one({"instrument_id": index["instrument_id"]})["active"] is False
    assert request(db, config, index)["status"] == "queued"
