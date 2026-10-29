"""Automatic Yahoo diffs publish complete latest-only history generations."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pymongo.errors import ConnectionFailure

import market_data as md
from market_data_refresh import refresh_stale_market_data
from test_yfinance_acquisition import ISIN, LISTING, Yahoo, clock, config, db, page, quote, run


def automatic(db, config, rows, splits=None):
    job = md.request_acquisition(db, config, ISIN, None, "update", "system:market-data-refresh", Mock(), incremental=True)
    source = Yahoo([page(rows)], splits=splits)
    return md.run_acquisition(db, config, job["id"], source.factory), source


def test_diff_includes_last_quote_and_materializes_complete_latest_history(db, config, clock):
    first, _ = run(db, config, [quote("2026-09-14", "90"), quote("2026-09-15", "100")])
    cutoff = clock[0]
    clock[0] += timedelta(days=1)
    second, source = automatic(db, config, [quote("2026-09-15", "100"), quote("2026-09-18", "110")])
    assert second["status"] == "succeeded" and second["mode"] == "incremental"
    assert [call[3:5] for call in source.calls if call[0] == "eod"] == [("2026-09-15", "2026-09-18")]
    latest = md.read_quotes(db, ISIN)
    assert latest["generation"] == second["id"]
    assert [row["close"] for row in latest["items"]] == ["90", "100", "110"]
    historical = md.read_quotes(db, ISIN, as_of=cutoff)
    assert historical["generation"] == second["id"]
    assert [row["close"] for row in historical["items"]] == ["90", "100"]
    with pytest.raises(md.DataError) as error:
        md.read_quotes(db, ISIN, as_of=cutoff, generation=first["id"])
    assert error.value.status == 409
    # Old payloads remain until scheduled retention; the latest generation is self-contained.
    assert db.quotes.count_documents({"job_id": first["id"]}) == 2
    assert db.quotes.count_documents({"job_id": second["id"]}) == 3
    copied = db.quotes.find_one({"job_id": second["id"], "date": "2026-09-14"})
    original = db.quotes.find_one({"job_id": first["id"], "date": "2026-09-14"})
    assert copied["source_job_id"] == first["id"]
    assert copied["data"] == original["data"]
    assert copied["collected_at"] == original["collected_at"]
    assert refresh_stale_market_data(db, config, now=clock[0], publish=Mock())["fresh"] == 1

    from market_data_retention import cleanup

    cleanup(db, config, ISIN)
    assert db.quotes.count_documents({"job_id": first["id"]}) == 0
    assert db.acquisition_jobs.find_one({"_id": first["id"]})["retention_state"] == "pruned"
    retained = md.read_quotes(db, ISIN)
    assert retained["generation"] == second["id"]
    assert [row["close"] for row in retained["items"]] == ["90", "100", "110"]


def test_startup_sweep_queues_diff_and_same_day_restarts_skip_it(db, config, clock):
    run(db, config, [quote("2026-09-15")])
    clock[0] += timedelta(days=1)
    publish = Mock()
    assert refresh_stale_market_data(db, config, now=clock[0], publish=publish)["requested"] == 1
    job = db.acquisition_jobs.find_one({"active": True})
    assert job["mode"] == "incremental"
    assert (job["date_from"], job["date_to"]) == ("2026-09-15", "2026-09-18")
    source = Yahoo([page([quote("2026-09-15")])])
    assert md.run_acquisition(db, config, job["_id"], source.factory)["status"] == "succeeded"
    assert refresh_stale_market_data(db, config, now=clock[0], publish=publish)["fresh"] == 1
    publish.assert_called_once()
    clock[0] += timedelta(days=1)
    assert refresh_stale_market_data(db, config, now=clock[0], publish=publish)["requested"] == 1


def test_empty_diff_is_successful_and_prevents_repeated_restart_downloads(db, config, clock):
    run(db, config, [quote("2026-09-15")])
    clock[0] += timedelta(days=2)
    second, _ = automatic(db, config, [])
    assert second["status"] == "succeeded"
    assert md.read_quotes(db, ISIN)["total"] == 1
    publish = Mock()
    for _ in range(3):
        assert refresh_stale_market_data(db, config, now=clock[0], publish=publish)["fresh"] == 1
    publish.assert_not_called()


@pytest.mark.parametrize("changed_overlap,new_split", [(True, False), (False, True), (False, False)])
def test_changed_adjustment_or_missing_overlap_falls_back_to_full_history(db, config, clock, changed_overlap, new_split):
    first, _ = run(db, config, [quote("2026-09-14"), quote("2026-09-15")])
    clock[0] += timedelta(days=1)
    job = md.request_acquisition(db, config, ISIN, None, "update", "system", Mock(), incremental=True)
    rows = ([quote("2026-09-15", "50" if changed_overlap else "100")]
            if changed_overlap or new_split else [quote("2026-09-18")])
    split = {"symbol": LISTING["symbol"], "date": "2026-09-18", "split_factor": "2"}
    source = Yahoo([page(rows), page([quote("2026-09-14", "50"), quote("2026-09-15", "50"), quote("2026-09-18", "55")])],
                   splits=[page([split] if new_split else []), page([split] if new_split else [])])
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded" and result["mode"] == "refresh"
    assert [call[3] for call in source.calls if call[0] == "eod"] == ["2026-09-15", "2010-05-19"]
    assert [row["close"] for row in md.read_quotes(db, ISIN)["items"]] == ["50", "50", "55"]
    with pytest.raises(md.DataError) as error:
        md.read_quotes(db, ISIN, generation=first["id"])
    assert error.value.status == 409
    assert db.quotes.find_one({"job_id": first["id"], "date": "2026-09-14"})["data"]["close"] == "100"


def test_dividend_adjusted_overlap_change_triggers_full_refresh(db, config, clock):
    run(db, config, [quote("2026-09-14", adj="90"), quote("2026-09-15", adj="90")])
    clock[0] += timedelta(days=1)
    job = md.request_acquisition(db, config, ISIN, None, "update", "system", Mock(), incremental=True)
    source = Yahoo([page([quote("2026-09-15", adj="89")]),
                    page([quote("2026-09-14", adj="89"), quote("2026-09-15", adj="89")])],
                   splits=[page([]), page([])])
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded" and result["mode"] == "refresh"
    assert [row["adj_close"] for row in md.read_quotes(db, ISIN)["items"]] == ["89", "89"]


def test_older_corrections_need_manual_refresh_when_overlap_is_unchanged(db, config, clock):
    run(db, config, [quote("2026-09-14", "100"), quote("2026-09-15", "100")])
    clock[0] += timedelta(days=1)
    result, _ = automatic(db, config, [quote("2026-09-15", "100"), quote("2026-09-18", "110")])
    assert result["mode"] == "incremental"
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "100"
    clock[0] += timedelta(seconds=1)
    manual, source = run(db, config, [quote("2026-09-14", "99"), quote("2026-09-15", "100"), quote("2026-09-18", "110")])
    assert manual["mode"] == "refresh"
    assert next(call for call in source.calls if call[0] == "eod")[3] == "2010-05-19"
    assert md.read_quotes(db, ISIN)["items"][0]["close"] == "99"


def test_failed_diff_preserves_history_and_does_not_mark_fresh(db, config, clock):
    first, _ = run(db, config)
    clock[0] += timedelta(days=1)
    job = md.request_acquisition(db, config, ISIN, None, "update", "system", Mock(), incremental=True)
    source = Yahoo([md.MarketDataError("http_error", "offline")])
    assert md.run_acquisition(db, config, job["id"], source.factory)["status"] == "failed"
    assert md.published_history(db, ISIN, LISTING)["_id"] == first["id"]
    assert refresh_stale_market_data(db, config, now=clock[0], publish=Mock())["requested"] == 1


def test_diff_merge_replays_saved_evidence_after_database_failure(db, config, clock, monkeypatch):
    run(db, config, [quote("2026-09-14"), quote("2026-09-15")])
    clock[0] += timedelta(days=1)
    job = md.request_acquisition(db, config, ISIN, None, "update", "system", Mock(), incremental=True)
    original = db.quotes.update_one
    interrupted = []

    def fail_copy(query, update, **kwargs):
        if update.get("$setOnInsert", {}).get("source_job_id") and not interrupted:
            interrupted.append(True)
            raise ConnectionFailure("restart during merge")
        return original(query, update, **kwargs)

    monkeypatch.setattr(db.quotes, "update_one", fail_copy)
    source = Yahoo([page([quote("2026-09-15"), quote("2026-09-18")])])
    with pytest.raises(ConnectionFailure):
        md.run_acquisition(db, config, job["id"], source.factory)
    clock[0] += timedelta(seconds=md.LEASE_SECONDS + 1)
    replay = Yahoo()
    replay.tickerinfo = Mock(side_effect=AssertionError("profile must be replayed"))
    replay.eod_page = Mock(side_effect=AssertionError("chart must be replayed"))
    assert md.run_acquisition(db, config, job["id"], replay.factory)["status"] == "succeeded"
    assert md.read_quotes(db, ISIN)["total"] == 3


def test_incremental_actions_preserve_old_events_without_second_provider_request(db, config, clock):
    split = {"symbol": LISTING["symbol"], "date": "2026-09-14", "split_factor": "2"}
    run(db, config, [quote("2026-09-14"), quote("2026-09-15")], splits=[page([split])])
    clock[0] += timedelta(days=1)
    result, source = automatic(db, config, [quote("2026-09-15"), quote("2026-09-18")])
    assert result["status"] == "succeeded"
    snapshot = db.split_snapshots.find_one({"_id": result["id"]})
    assert snapshot["date_from"] == "2010-05-19"
    assert snapshot["events"] == [{"date": "2026-09-14", "factor": "2"}]
    assert not any(call[0] == "splits" for call in source.calls)
