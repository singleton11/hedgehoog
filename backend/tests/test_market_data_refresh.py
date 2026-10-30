from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import mongomock
import pytest
from kombu.exceptions import OperationalError

from celery_app import MARKET_DATA_REFRESH_TASK, create_celery_app, enqueue_market_data_refresh
from config import TaskSettings
from market_data import DataError, ensure_indexes
from market_data_refresh import refresh_stale_market_data
from tasks import refresh_market_data_task

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).refresh_tests
    ensure_indexes(db)
    # mongomock recreates partial indexes without respecting their filter.
    create_index = db.acquisition_jobs.create_index

    def idempotent_index(keys, **kwargs):
        name = kwargs.get("name")
        if name in db.acquisition_jobs.index_information():
            return name
        return create_index(keys, **kwargs)

    monkeypatch.setattr(db.acquisition_jobs, "create_index", idempotent_index)
    return db


def saved_etf(db, identifier="IE0000000001", *, published=True):
    db.etfs.insert_one({"isin": identifier, "ticker": "EXAMPLE", "inception_date": "2020-01-01"})
    db.instruments.insert_one({"instrument_id": identifier, "job_id": identifier,
                               "provider": "yfinance", "published": published})
    db.instrument_state.insert_one({"_id": identifier, "listing": {"symbol": "EXAMPLE.DE", "exchange": "XETR"}})
    return identifier


def index(db, identifier="idx-example", *, removed=False):
    db.indices.insert_one({"instrument_id": identifier, "symbol": "^EXAMPLE",
                           **({"removed_at": NOW} if removed else {})})
    return identifier


def success(db, identifier, finished_at, **extra):
    state = db.instrument_state.find_one({"_id": identifier}) or {}
    db.acquisition_jobs.insert_one({"instrument_id": identifier, "status": "succeeded",
                                   "listing": state.get("listing"),
                                   "provider": "yfinance", "published": True,
                                   "finished_at": finished_at, "date_to": finished_at.date().isoformat(), **extra})


def sweep(db, publish=None):
    return refresh_stale_market_data(db, TaskSettings(_env_file=None), now=NOW,
                                    publish=publish or Mock())


def test_scope_includes_all_saved_etfs_and_unacquired_active_indices(db):
    for n in range(30):
        saved_etf(db, f"IE{n:010d}")
    saved_etf(db, "IE9999999999", published=False)
    db.etfs.insert_one({"isin": "IE8888888888", "ticker": "UNACQUIRED"})
    index(db)
    index(db, "idx-removed", removed=True)
    publish = Mock()
    assert sweep(db, publish) == {"requested": 31, "fresh": 0, "skipped": 0, "errors": 0}
    assert publish.call_count == 31
    jobs = list(db.acquisition_jobs.find({"active": True}))
    assert len(jobs) == 31
    assert all(job["requested_by"] == "system:market-data-refresh" for job in jobs)
    assert all(job["mode"] == "refresh" for job in jobs)


def test_startup_catalogue_sweep_includes_unacquired_failed_and_historical_entries_once(db):
    saved = saved_etf(db)
    failed = saved_etf(db, "IE9999999999", published=False)
    for day in ("2026-10-09", "2026-10-10"):
        db.etfs.insert_one({"isin": "IE8888888888", "ticker": "NEW",
                            "inception_date": "2020-01-01", "import_date": day})
    index(db)
    index(db, "idx-removed", removed=True)
    publish = Mock()
    settings = TaskSettings(_env_file=None)
    for _ in range(2):
        result = refresh_stale_market_data(db, settings, now=NOW, publish=publish,
                                          include_catalogue=True)
        assert result == {"requested": 4, "fresh": 0, "skipped": 0, "errors": 0}
    assert publish.call_count == 4
    assert set(db.acquisition_jobs.distinct("instrument_id")) == {
        saved, failed, "IE8888888888", "idx-example",
    }


def test_startup_catalogue_sweep_reuses_fresh_success_and_isolates_invalid_entries(db):
    identifier = saved_etf(db)
    success(db, identifier, NOW)
    db.etfs.insert_one({"isin": "IE8888888888", "ticker": "INVALID"})
    db.etfs.insert_one({"isin": "IE7777777777", "ticker": "NEW",
                        "inception_date": "2020-01-01"})
    result = refresh_stale_market_data(db, TaskSettings(_env_file=None), now=NOW,
                                      publish=Mock(), include_catalogue=True)
    assert result == {"requested": 1, "fresh": 1, "skipped": 1, "errors": 0}


@pytest.mark.parametrize("age,fresh", [(0, True), (10, True), (11, False), (13, False), (23, False), (24, False)])
def test_successful_refresh_checks_current_scheduled_window(db, age, fresh):
    identifier = saved_etf(db)
    # Deliberately naive: MongoDB normally returns UTC without a tzinfo.
    success(db, identifier, (NOW - timedelta(hours=age)).replace(tzinfo=None))
    result = sweep(db)
    assert result["fresh"] == int(fresh)
    assert result["requested"] == int(not fresh)


@pytest.mark.parametrize("check_hour,run_hour,fresh", [(0, 1, True), (1, 1, True), (1, 2, False), (2, 2, True), (2, 12, True)])
def test_early_restart_does_not_suppress_02_utc_refresh(db, check_hour, run_hour, fresh):
    identifier = saved_etf(db)
    checked = NOW.replace(hour=check_hour)
    success(db, identifier, checked, requested_at=checked)
    result = refresh_stale_market_data(db, TaskSettings(_env_file=None),
                                      now=NOW.replace(hour=run_hour), publish=Mock())
    assert result["fresh"] == int(fresh)
    assert result["requested"] == int(not fresh)


def test_job_started_before_02_but_finished_after_does_not_suppress_refresh(db):
    identifier = saved_etf(db)
    success(db, identifier, NOW.replace(hour=3), requested_at=NOW.replace(hour=1))
    assert sweep(db)["requested"] == 1


def test_failed_or_unpublished_jobs_do_not_make_data_fresh(db):
    identifier = saved_etf(db)
    success(db, identifier, NOW - timedelta(days=3))
    success(db, identifier, NOW, status="failed")
    success(db, identifier, NOW, published=False)
    assert sweep(db)["requested"] == 1


def test_success_from_another_provider_does_not_make_data_fresh(db):
    identifier = saved_etf(db)
    success(db, identifier, NOW, provider="marketstack")
    assert sweep(db)["requested"] == 1


def test_recent_success_must_have_checked_through_today(db):
    identifier = saved_etf(db)
    success(db, identifier, NOW, date_to="2026-10-09")
    assert sweep(db)["requested"] == 1


@pytest.mark.parametrize("hour,fresh", [(1, False), (2, True)])
def test_published_quote_must_also_belong_to_current_scheduled_window(db, hour, fresh):
    identifier = saved_etf(db)
    listing = db.instrument_state.find_one({"_id": identifier})["listing"]
    success(db, identifier, NOW, _id="published", listing=listing, status="running",
            requested_at=NOW.replace(hour=hour), published_at=NOW)
    db.quotes.insert_one({"instrument_id": identifier, **listing, "provider": "yfinance",
                          "job_id": "published", "date": NOW.date().isoformat()})
    result = sweep(db)
    assert result["fresh"] == int(fresh)
    assert result["requested"] == int(not fresh)


def test_unpublished_quote_for_today_does_not_skip_download(db):
    identifier = saved_etf(db)
    listing = db.instrument_state.find_one({"_id": identifier})["listing"]
    db.quotes.insert_one({"instrument_id": identifier, **listing, "provider": "yfinance",
                          "job_id": "failed", "date": NOW.date().isoformat()})
    assert sweep(db)["requested"] == 1


def test_profile_collection_time_alone_does_not_make_data_fresh(db):
    identifier = saved_etf(db)
    db.instruments.update_one({"instrument_id": identifier}, {"$set": {"collected_at": NOW}})
    assert sweep(db)["requested"] == 1


def test_manual_active_job_and_repeated_sweeps_publish_only_once(db):
    saved_etf(db)
    publish = Mock()
    sweep(db, publish)
    sweep(db, publish)
    publish.assert_called_once()
    assert db.acquisition_jobs.count_documents({"active": True}) == 1


def test_one_invalid_catalogue_entry_does_not_block_other_refreshes(db):
    invalid = saved_etf(db)
    db.etfs.update_one({"isin": invalid}, {"$unset": {"inception_date": ""}})
    index(db)
    assert sweep(db) == {"requested": 1, "fresh": 0, "skipped": 1, "errors": 0}


def test_publish_failure_continues_other_instruments_and_can_retry(db):
    saved_etf(db)
    index(db)
    publish = Mock(side_effect=[OperationalError("broker offline"), None])
    with pytest.raises(DataError, match="could not be queued"):
        sweep(db, publish)
    assert publish.call_count == 2
    assert db.acquisition_jobs.count_documents({"status": "failed"}) == 1
    retry_publish = Mock()
    sweep(db, retry_publish)
    retry_publish.assert_called_once()


def test_stale_running_job_is_replaced(db):
    identifier = saved_etf(db)
    db.acquisition_jobs.insert_one({"instrument_id": identifier, "active": True,
                                   "status": "running", "lease_until": NOW - timedelta(days=1000)})
    assert sweep(db)["requested"] == 1
    assert db.acquisition_jobs.count_documents({"status": "failed"}) == 1
    assert db.acquisition_jobs.count_documents({"active": True}) == 1


def test_daily_beat_configuration_and_startup_message(monkeypatch):
    settings = TaskSettings(_env_file=None)
    with create_celery_app(settings) as app:
        entry = app.conf.beat_schedule["daily-market-data-refresh"]
        assert entry["task"] == MARKET_DATA_REFRESH_TASK
        assert entry["options"] == {"queue": "etfs"}
        assert entry["schedule"].hour == {2}
        assert entry["schedule"].minute == {0}
        assert app.conf.timezone == "UTC"
    producer = MagicMock()
    producer.__enter__.return_value = producer
    producer.send_task.return_value.id = "refresh-id"
    factory = Mock(return_value=producer)
    monkeypatch.setattr("celery_app.create_celery_app", factory)
    assert enqueue_market_data_refresh(settings) == "refresh-id"
    factory.assert_called_once_with(settings)
    producer.send_task.assert_called_once_with(MARKET_DATA_REFRESH_TASK, queue="etfs")


def test_catalogue_sweep_message_preserves_scope(monkeypatch):
    settings = TaskSettings(_env_file=None)
    producer = MagicMock()
    producer.__enter__.return_value = producer
    producer.send_task.return_value.id = "catalogue-refresh-id"
    monkeypatch.setattr("celery_app.create_celery_app", Mock(return_value=producer))
    assert enqueue_market_data_refresh(settings, include_catalogue=True) == "catalogue-refresh-id"
    producer.send_task.assert_called_once_with(
        MARKET_DATA_REFRESH_TASK, queue="etfs", kwargs={"include_catalogue": True},
    )


@pytest.mark.parametrize("include_catalogue", [False, True])
def test_worker_owns_client_and_calls_sweep(monkeypatch, include_catalogue):
    settings = TaskSettings(_env_file=None)
    monkeypatch.setattr("tasks.TaskSettings", lambda: settings)
    mongo = MagicMock()
    mongo.__enter__.return_value = mongo
    monkeypatch.setattr("tasks.MongoClient", Mock(return_value=mongo))
    refresh = Mock(return_value={"requested": 2})
    monkeypatch.setattr("tasks.refresh_stale_market_data", refresh)
    assert refresh_market_data_task(include_catalogue=include_catalogue) == {"requested": 2}
    options = {"include_catalogue": True} if include_catalogue else {}
    refresh.assert_called_once_with(mongo[settings.mongodb_database], settings, **options)
    mongo.__exit__.assert_called_once_with(None, None, None)
    assert refresh_market_data_task.name == MARKET_DATA_REFRESH_TASK
    assert refresh_market_data_task.max_retries == 5
