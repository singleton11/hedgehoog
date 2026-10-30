"""Missing latest-history failure/retry paths, using synthetic in-memory data only."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pymongo.errors import DuplicateKeyError

import market_data as md
from test_market_data import seed_yahoo
from test_yfinance_acquisition import ISIN, LISTING, Yahoo, clock, config, db, page, quote, run


def test_provider_factory_preserves_timeout_and_request_accounting(config, monkeypatch):
    factory = Mock()
    monkeypatch.setattr("yahoo_finance.YahooFinanceClient", factory)
    before_request = Mock()
    assert md.provider_client(config, "yfinance", before_request) is factory.return_value
    factory.assert_called_once_with(before_request=before_request, timeout=config.market_data_timeout_seconds)
    with pytest.raises(md.MarketDataError) as error:
        md.provider_client(config, "unsupported-synthetic-provider", before_request)
    assert error.value.code == "unknown_provider"
    before_request.assert_not_called()
    assert factory.call_count == 1


def test_missing_profile_is_conflict_not_empty_acquired_universe(db):
    generation = seed_yahoo(db)
    db.instruments.delete_many({"job_id": generation})
    with pytest.raises(md.DataError, match="profile is unavailable") as error:
        md.acquired_universe(db)
    assert error.value.status == 409
    assert not db.acquisition_jobs.find_one({"_id": generation}).get("reader_leases")


def test_history_bounds_reject_empty_or_non_yahoo_generations_and_ignore_unmapped_rows(db):
    with pytest.raises(ValueError, match="nonempty"):
        md.history_bounds_pipeline([])
    for generation, provider in [(None, "yfinance"), ("legacy", "yfinance"), ("saved", "marketstack")]:
        with pytest.raises(md.DataError) as error:
            md.history_bounds_pipeline([{"generation": generation, "provider": provider}])
        assert error.value.status == 409
    assert md.saved_history_bounds(db, [{"_id": ISIN, "listing": None}]) == {}


@pytest.mark.parametrize("winner", [None, "maintenance", "deleting", "different-listing", "same-listing"])
def test_racing_request_never_dispatches_or_overwrites_winner(db, config, monkeypatch, winner):
    md.ensure_indexes(db)
    insert = db.acquisition_jobs.insert_one
    rival = {"_id": "synthetic-race-winner", "instrument_id": ISIN, "provider": "yfinance",
             "active": True, "status": "queued", "requested_at": md.utcnow().replace(tzinfo=None), "listing": LISTING}
    if winner in ("maintenance", "deleting"):
        rival["status"] = winner
    elif winner == "different-listing":
        rival["listing"] = {"symbol": "OTHER.DE", "exchange": "XETR"}

    def race(job):
        if winner is not None:
            insert(deepcopy(rival))
        raise DuplicateKeyError("synthetic simultaneous acquisition")

    monkeypatch.setattr(db.acquisition_jobs, "insert_one", race)
    publish = Mock()
    if winner == "same-listing":
        result = md.request_acquisition(db, config, ISIN, LISTING, "update", "fixture", publish)
        assert result["id"] == rival["_id"]
    else:
        with pytest.raises(md.DataError) as error:
            md.request_acquisition(db, config, ISIN, LISTING, "update", "fixture", publish)
        assert error.value.status == 409
    publish.assert_not_called()
    assert db.instrument_state.count_documents({}) == 0
    assert list(db.acquisition_jobs.find()) == ([] if winner is None else [rival])


def test_incremental_base_loss_after_queue_insert_fails_without_dispatch(db, config, clock, monkeypatch):
    first, _ = run(db, config)
    clock[0] += timedelta(days=1)
    original = md.published_history

    def lose_base(*args, **kwargs):
        if kwargs.get("generation") == first["id"]:
            raise md.DataError(409, "Synthetic base superseded during setup")
        return original(*args, **kwargs)

    monkeypatch.setattr(md, "published_history", lose_base)
    publish = Mock()
    with pytest.raises(md.DataError, match="superseded") as error:
        md.request_acquisition(db, config, ISIN, LISTING, "update", "fixture", publish, incremental=True)
    assert error.value.status == 409
    publish.assert_not_called()
    queued = db.acquisition_jobs.find_one({"base_generation": first["id"]})
    assert queued["status"] == queued["phase"] == "failed"
    assert queued["active"] is False and queued["finished_at"] == clock[0].replace(tzinfo=None)
    assert "Refresh and retry" in queued["error"]
    assert db.quotes.count_documents({"job_id": first["id"]}) == 1


@pytest.mark.parametrize("missing", ["snapshot", "overlap"])
def test_incremental_missing_base_evidence_requires_full_refresh(db, config, missing):
    first, _ = run(db, config)
    if missing == "snapshot":
        db.split_snapshots.delete_one({"_id": first["id"]})
    else:
        db.quotes.delete_many({"job_id": first["id"]})
    job = {"base_generation": first["id"], "listing": LISTING,
           "date_from": "2026-09-15", "date_to": "2026-09-17"}
    source = Yahoo([page([quote()])])
    source.before_request = Mock()
    probe = source.eod_page(LISTING["symbol"], LISTING["exchange"], job["date_from"], job["date_to"])
    assert md.incremental_requires_full_history(db, job, probe) is True


@pytest.mark.parametrize("field,value", [
    ("price_currency", "USD"), ("price_basis", "unadjusted"),
    ("close", None), ("close", True), ("close", "not-a-number"),
    ("close", "NaN"), ("open", None), ("adj_close", "Infinity"),
])
def test_incompatible_incremental_overlap_is_never_combined(db, config, field, value):
    first, _ = run(db, config)
    changed = {**quote(), field: value}
    source = Yahoo([page([changed])])
    source.before_request = Mock()
    probe = source.eod_page(LISTING["symbol"], LISTING["exchange"], "2026-09-15", "2026-09-17")
    assert md.incremental_requires_full_history(db, {
        "base_generation": first["id"], "listing": LISTING,
        "date_from": "2026-09-15", "date_to": "2026-09-17",
    }, probe) is True
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "100"


def test_incomplete_incremental_probe_is_rejected_before_combining(db):
    with pytest.raises(md.MarketDataError) as error:
        md.incremental_requires_full_history(db, {}, page([quote()], total=2))
    assert error.value.code == "incomplete_history"
    assert db.quotes.count_documents({}) == 0


def test_unindexable_yahoo_rows_remain_raw_with_visible_warning(db, config):
    rows = [None, "synthetic invalid row", {"date": None}, quote()]
    result, _ = run(db, config, rows)
    assert result["status"] == "succeeded"
    assert "3 row(s) without a usable date" in result["warning"]
    saved = db.acquisition_jobs.find_one({"_id": result["id"]})
    assert saved["unindexed_rows"] == 3 and saved["rows"] == 4
    assert db.raw_eod_pages.find_one({"job_id": result["id"]})["data"]["data"] == rows
    history = md.read_quotes(db, ISIN)
    assert history["total"] == 1 and history["items"][0]["close"] == "100"


def test_future_inception_never_creates_or_dispatches_job(db, config):
    db.etfs.update_many({"isin": ISIN}, {"$set": {"inception_date": "2099-01-01"}})
    publish = Mock()
    with pytest.raises(md.DataError, match="future") as error:
        md.request_acquisition(db, config, ISIN, LISTING, "update", "fixture", publish)
    assert error.value.status == 422
    publish.assert_not_called()
    assert db.acquisition_jobs.count_documents({}) == 0


@pytest.mark.parametrize("metadata,expected", [
    ({"total": "2"}, 2), ({"total": "-1"}, None), ({"total": True}, None),
    ({"total": "2.5"}, None), ({"total": "\u0662"}, None),
    ({"total": "9" * 5000}, None), ({}, None),
])
def test_saved_pagination_accepts_only_bounded_nonnegative_integers(metadata, expected):
    assert md.pagination_integer({"pagination": metadata}, "total") == expected


def test_saved_pagination_without_total_uses_page_length_not_endless_retry():
    assert md.has_more_pages({"data": [quote()], "pagination": {"limit": "2"}}, 1) is False
    assert md.has_more_pages({"data": [quote()], "pagination": {"limit": "1"}}, 1) is True
    assert md.has_more_pages({"data": [], "pagination": {"total": "100"}}, 0) is False


@pytest.mark.parametrize("rows,total,expected_code", [
    ([{"symbol": LISTING["symbol"], "date": "2026-09-15", "split_factor": "2"}], "0", "inconsistent_split_history"),
    ([], "1", "incomplete_split_history"),
])
def test_corrupt_saved_split_pagination_cannot_publish(db, config, rows, total, expected_code):
    job = md.request_acquisition(db, config, ISIN, LISTING, "update", "fixture", Mock())
    db.raw_split_pages.insert_one({"job_id": job["id"], "offset": 0, "data": page(rows, total=total)})
    result = md.run_acquisition(db, config, job["id"], Yahoo().factory)
    assert result["status"] == "failed"
    saved = db.acquisition_jobs.find_one({"_id": job["id"]})
    assert saved["error_code"] == expected_code and not saved.get("published")
    assert db.market_data_publications.count_documents({}) == 0
    assert db.instrument_state.find_one({"_id": ISIN}) is None


def test_incremental_resume_missing_action_snapshot_cannot_publish(db, config, clock):
    first, _ = run(db, config)
    clock[0] += timedelta(days=1)
    job = md.request_acquisition(db, config, ISIN, LISTING, "update", "fixture", Mock(), incremental=True)
    db.acquisition_jobs.update_one({"_id": job["id"]}, {"$set": {"incremental_checked": True}})
    db.split_snapshots.delete_one({"_id": first["id"]})
    result = md.run_acquisition(db, config, job["id"], Yahoo([page([quote()])]).factory)
    assert result["status"] == "failed"
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["error_code"] == "missing_action_evidence"
    assert md.published_history(db, ISIN, LISTING)["_id"] == first["id"]
    assert db.market_data_publications.count_documents({"_id": job["id"]}) == 0
    assert db.quotes.count_documents({"job_id": first["id"]}) == 1


@pytest.mark.parametrize("inception", [None, "not-a-date", "2026-09-15"])
def test_coverage_discloses_unknown_inception_and_accepts_complete_history(inception):
    result = md.coverage({"inception_date": inception}, ["2026-09-15"])
    assert result["history_status"] == ("available" if inception == "2026-09-15" else "partial")
    assert bool(result["warning"]) is (inception != "2026-09-15")
    assert result["quote_count"] == 1
