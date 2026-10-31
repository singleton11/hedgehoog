"""Actual Mongo commands and races; only a disposable loopback test server."""
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from time import perf_counter
from uuid import uuid4

import pytest
from pymongo import MongoClient, monitoring
from pymongo.collection import Collection

import market_data as md
import return_statistics as rs
from quote_fill import read_policy

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_MONGODB_URI"), reason="Disposable Mongo required")
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


class Commands(monitoring.CommandListener):
    def __init__(self):
        self.calls = []

    def started(self, event):
        self.calls.append((event.command_name, dict(event.command)))

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


@pytest.fixture
def inventory(monkeypatch, request):
    commands = Commands()
    client = MongoClient(os.environ["TEST_MONGODB_URI"], event_listeners=[commands], tz_aware=True)
    db = client[f"batched_leases_{uuid4().hex}"]
    monkeypatch.setattr(md, "utcnow", lambda: NOW)
    try:
        md.ensure_indexes(db)
        policy = read_policy(db)
        jobs, profiles, states, caches = [], [], [], []
        for index in range(getattr(request, "param", 1000)):
            isin, generation = f"IE{index:010d}", f"job-{index}"
            listing = {"symbol": isin, "exchange": "TEST"}
            state = {"_id": isin, "listing": listing}
            coverage = {"first_date": "2000-01-01", "last_date": "2026-10-29", "quote_count": 2}
            jobs.append({"_id": generation, "instrument_id": isin, "listing": listing, "provider": "yfinance",
                         "published": True, "requested_at": NOW, "published_at": NOW,
                         "history_summary": {"version": 1, "instrument_id": isin, "listing": listing,
                                             "generation": generation, "observations": 2, **coverage}})
            profiles.append({"instrument_id": isin, **listing, "provider": "yfinance", "job_id": generation,
                             "collected_at": NOW - timedelta(days=index // 2)})
            states.append(state)
            caches.append({"_id": isin, "signature": rs._statistics_signature(isin, state, generation, 2, None, NOW, policy),
                           "statistics": {"saved_history": coverage, "historical_year_return": .1 if index < 197 else -.1,
                                          "annualized_mean_log_return": .1}})
        db.acquisition_jobs.insert_many(jobs)
        db.instruments.insert_many(profiles)
        db.instrument_state.insert_many(states)
        db.instrument_statistics.insert_many(caches)
        commands.calls.clear()
        yield db, commands
    finally:
        client.drop_database(db.name)
        client.close()


def test_legacy_history_filter_reuses_caller_leases(inventory):
    db, commands = inventory
    quotes = []
    for job in db.acquisition_jobs.find():
        for day in ("2000-01-01", "2026-10-29"):
            quotes.append({"instrument_id": job["instrument_id"], **job["listing"], "job_id": job["_id"],
                           "provider": "yfinance", "date": day})
    db.quotes.insert_many(quotes)
    db.acquisition_jobs.update_many({}, {"$unset": {"history_summary": ""}})
    commands.calls.clear()
    result = md.acquired_instruments(db, offset=1000, min_history_years=15)
    assert result["total"] == 1000 and result["items"] == []
    counts = Counter(name for name, _ in commands.calls)
    print({"legacy_bounds_commands": dict(counts)})
    assert counts["update"] == 40
    assert counts["find"] <= 21
    assert counts["aggregate"] == 61


@pytest.mark.parametrize("inventory", [200, 1000], indirect=True)
def test_warm_exact_filter_has_bounded_real_commands(inventory):
    db, commands = inventory
    start = perf_counter()
    result = md.acquired_instruments(db, limit=20, offset=20, min_history_years=15, hide_negative_returns=True)
    assert result["total"] == 197
    assert [row["instrument_id"] for row in result["items"]] == [f"IE{index:010d}" for index in range(20, 40)]
    counts = Counter(name for name, _ in commands.calls)
    count = db.acquisition_jobs.count_documents({})
    batches = (count + 49) // 50
    print({"inventory": count, "matched": 197, "seconds": round(perf_counter() - start, 4), "commands": dict(counts)})
    assert counts["update"] == 2 * batches
    assert counts["find"] <= 4 * batches + 5
    assert counts["aggregate"] == 2 * batches + 1
    assert not any(command.get(name) == "quotes" for name, command in commands.calls)
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


def observations(db):
    return [{"_id": job["instrument_id"], "job_id": job["_id"], "listing": job["listing"], "provider": "yfinance"}
            for job in db.acquisition_jobs.find().sort("instrument_id", 1)]


@pytest.mark.parametrize("inventory", [51], indirect=True)
@pytest.mark.parametrize("change", ["publish", "listing", "retiring", "pruned", "missing_token", "expiry", "stored_expiry", "delete"])
def test_completion_rejects_changed_identity_or_lease(inventory, monkeypatch, change):
    from market_data_retention import reader_many
    db, _ = inventory
    selected = observations(db)
    with pytest.raises(md.DataError) as error:
        with reader_many(db, selected):
            job = db.acquisition_jobs.find_one({"_id": "job-0"})
            if change == "publish":
                job.pop("reader_leases")
                db.acquisition_jobs.insert_one({**job, "_id": "new", "requested_at": NOW + timedelta(seconds=1)})
            elif change == "listing":
                db.acquisition_jobs.update_one({"_id": "job-0"}, {"$set": {"listing.symbol": "OTHER"}})
            elif change in ("retiring", "pruned"):
                db.acquisition_jobs.update_one({"_id": "job-0"}, {"$set": {"retention_state": change}})
            elif change == "missing_token":
                db.acquisition_jobs.update_one({"_id": "job-0"}, {"$unset": {"reader_leases": ""}})
            elif change == "stored_expiry":
                token = next(iter(job["reader_leases"]))
                db.acquisition_jobs.update_one({"_id": "job-0"}, {"$set": {f"reader_leases.{token}": NOW}})
            elif change == "expiry":
                monkeypatch.setattr(md, "utcnow", lambda: NOW + timedelta(seconds=300))
            else:
                db.acquisition_jobs.delete_one({"_id": "job-0"})
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("inventory", [51], indirect=True)
@pytest.mark.parametrize("phase", ["before_acquire", "after_acquire", "during_hydrate"])
def test_filtered_publication_race_rejected_before_payload_or_at_completion(inventory, monkeypatch, phase):
    db, _ = inventory
    original_update = Collection.update_many
    original_statistics = rs.acquired_statistics
    published = False

    def publish():
        nonlocal published
        if not published:
            published = True
            job = db.acquisition_jobs.find_one({"_id": "job-0"})
            job.pop("reader_leases", None)
            db.acquisition_jobs.insert_one({**job, "_id": "new", "requested_at": NOW + timedelta(seconds=1)})

    def acquire(collection, query, update, *args, **kwargs):
        if isinstance(update, list) and phase == "before_acquire":
            publish()
        result = original_update(collection, query, update, *args, **kwargs)
        if isinstance(update, list) and phase == "after_acquire":
            publish()
        return result

    def statistics(*args, **kwargs):
        if phase != "during_hydrate":
            pytest.fail("Superseded capture reached payload statistics")
        publish()
        return original_statistics(*args, **kwargs)

    monkeypatch.setattr(Collection, "update_many", acquire)
    monkeypatch.setattr(rs, "acquired_statistics", statistics)
    with pytest.raises(md.DataError) as error:
        md.acquired_instruments(db, min_history_years=15, hide_negative_returns=True)
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("inventory", [53], indirect=True)
@pytest.mark.parametrize("failure", ["partial", "write_exception", "payload_exception", "retiring_during_capture"])
def test_partial_acquisition_and_exceptions_release_every_attempted_chunk(inventory, monkeypatch, failure):
    from market_data_retention import reader_many
    db, _ = inventory
    selected = observations(db)
    original = Collection.update_many
    calls = 0

    def acquire(collection, query, update, *args, **kwargs):
        nonlocal calls
        if isinstance(update, list):
            calls += 1
            if calls == 2 and failure == "partial":
                db.acquisition_jobs.update_one({"_id": "job-50"}, {"$set": {"retention_state": "retiring"}})
        result = original(collection, query, update, *args, **kwargs)
        if isinstance(update, list) and calls == 2 and failure == "retiring_during_capture":
            db.acquisition_jobs.update_one({"_id": "job-0"}, {"$set": {"retention_state": "retiring"}})
        if isinstance(update, list) and calls == 2 and failure == "write_exception":
            raise RuntimeError("ambiguous write")
        return result

    monkeypatch.setattr(Collection, "update_many", acquire)
    with pytest.raises((md.DataError, RuntimeError)):
        with reader_many(db, selected):
            assert failure == "payload_exception"
            raise RuntimeError("payload failed")
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("inventory", [51], indirect=True)
def test_own_token_cleanup_preserves_live_foreign_readers_and_prunes_expired(inventory):
    from market_data_retention import reader_many
    db, _ = inventory
    selected = observations(db)
    live = NOW + timedelta(seconds=120)
    db.acquisition_jobs.update_many({}, {"$set": {"reader_leases": {"foreign": live, "crashed": NOW}}})
    with reader_many(db, selected):
        with reader_many(db, selected):
            for job in db.acquisition_jobs.find():
                assert len(job["reader_leases"]) == 3
                assert job["reader_leases"]["foreign"] == live
                assert "crashed" not in job["reader_leases"]
        assert all(len(job["reader_leases"]) == 2 for job in db.acquisition_jobs.find())
    assert all(job["reader_leases"] == {"foreign": live} for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("inventory", [51], indirect=True)
@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), -.01])
def test_exact_filter_keeps_missing_boolean_nonfinite_and_negative_hidden(inventory, bad):
    db, _ = inventory
    db.instrument_statistics.update_many({}, {"$set": {"statistics.historical_year_return": bad}})
    assert md.acquired_instruments(db, hide_negative_returns=True)["total"] == 0


@pytest.mark.parametrize("inventory", [51], indirect=True)
def test_filtered_missing_offpage_profile_is_conflict(inventory):
    db, _ = inventory
    db.instruments.delete_one({"job_id": "job-50"})
    with pytest.raises(md.DataError) as error:
        md.acquired_instruments(db, limit=1, hide_negative_returns=True)
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("inventory", [1], indirect=True)
def test_real_retention_waits_for_batch_reader_then_can_prune(inventory):
    from types import SimpleNamespace
    from market_data_retention import reader_many, retain_instrument
    db, _ = inventory
    selected = observations(db)
    settings = SimpleNamespace(market_data_retention_batch_size=10)
    with pytest.raises(md.DataError):
        with reader_many(db, selected):
            job = db.acquisition_jobs.find_one({"_id": "job-0"})
            job.pop("reader_leases")
            db.acquisition_jobs.insert_one({**job, "_id": "new", "requested_at": NOW + timedelta(seconds=1)})
            result = retain_instrument(db, settings, selected[0]["_id"])
            assert result["skipped"] == "reader leases still active"
            assert db.instruments.count_documents({"job_id": "job-0"}) == 1
    retain_instrument(db, settings, selected[0]["_id"])
    assert db.instruments.count_documents({"job_id": "job-0"}) == 0
    assert db.acquisition_jobs.find_one({"_id": "job-0"})["retention_state"] == "pruned"


@pytest.mark.parametrize("inventory", [1], indirect=True)
@pytest.mark.parametrize("ordering", ["published_at", "_id"])
def test_latest_ties_use_existing_publication_ordering(inventory, ordering):
    from market_data_retention import reader_many
    db, _ = inventory
    selected = observations(db)
    old = db.acquisition_jobs.find_one({"_id": "job-0"})
    new = {**old, "_id": "zzz"}
    if ordering == "published_at":
        new.update(_id="aaa", published_at=NOW + timedelta(seconds=1))
    db.acquisition_jobs.insert_one(new)
    with pytest.raises(md.DataError):
        with reader_many(db, selected):
            pytest.fail("A superseded generation reached payloads")
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())


@pytest.mark.parametrize("inventory", [51], indirect=True)
def test_reader_commands_obey_bson_budget_and_single_identity_limit(inventory, monkeypatch):
    from bson import BSON
    from market_data_retention import reader_many
    db, commands = inventory
    selected = observations(db)
    monkeypatch.setattr(md, "HISTORY_COMMAND_BYTES", 6000)
    commands.calls.clear()
    with reader_many(db, selected):
        pass
    writes = [cmd for name, cmd in commands.calls if name == "update"]
    assert len(writes) > 4  # Byte cap, not just candidate count, splits chunks.
    assert all(len(BSON.encode(cmd)) < 6000 for cmd in writes)
    assert all(len(update["q"].get("$or", update["q"].get("_id", {}).get("$in", []))) <= 50
               for cmd in writes for update in cmd["updates"])
    commands.calls.clear()
    selected[0]["listing"]["symbol"] = "x" * 6000
    with pytest.raises(md.DataError) as error:
        with reader_many(db, selected):
            pytest.fail("Oversized identity reached payloads")
    assert error.value.status == 413
    assert not commands.calls


@pytest.mark.parametrize("inventory", [200], indirect=True)
def test_real_filter_presets_order_totals_and_page_boundaries(inventory):
    db, _ = inventory
    for years in (None, 5, 15, 30):
        for returns in (False, True):
            total = 0 if years == 30 else 197 if returns else 200
            for offset in (0, 20, 197, 200):
                result = md.acquired_instruments(db, offset=offset, min_history_years=years, hide_negative_returns=returns)
                assert result["total"] == total
                assert [row["instrument_id"] for row in result["items"]] == [
                    f"IE{index:010d}" for index in range(offset, min(total, offset + 20))]


@pytest.mark.parametrize("inventory", [1], indirect=True)
@pytest.mark.parametrize("change", ["missing_generation", "provider", "instrument", "unpublished", "missing_listing"])
def test_reader_never_accepts_an_implicit_or_mismatched_identity(inventory, change):
    from market_data_retention import reader_many
    db, _ = inventory
    selected = observations(db)
    if change == "missing_generation":
        selected[0]["job_id"] = None
    elif change == "provider":
        selected[0]["provider"] = "marketstack"
    elif change == "instrument":
        selected[0]["_id"] = "OTHER"
    elif change == "missing_listing":
        selected[0]["listing"] = None
    else:
        db.acquisition_jobs.update_one({}, {"$set": {"published": False}})
    with pytest.raises(md.DataError) as error:
        with reader_many(db, selected):
            pytest.fail("Invalid identity reached payloads")
    assert error.value.status == 409
    assert all(not job.get("reader_leases") for job in db.acquisition_jobs.find())
