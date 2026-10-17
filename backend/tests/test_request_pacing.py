"""Offline pacing contracts; provider traffic is never generated."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import mongomock
import pytest
from pydantic import ValidationError
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from config import TaskSettings
import request_pacing as pacing


@pytest.fixture
def clock(monkeypatch):
    now = [datetime(2026, 10, 17, tzinfo=timezone.utc)]
    monkeypatch.setattr(pacing, "utcnow", lambda: now[0])
    monkeypatch.setattr(pacing.time, "sleep", lambda delay: now.__setitem__(0, now[0] + timedelta(seconds=delay)))
    return now


def test_independent_callers_share_spacing_and_idle_time_does_not_accumulate_slots(clock):
    db = mongomock.MongoClient().pacing_test
    heartbeat = Mock()
    starts = []
    for _ in range(16):
        pacing.wait_for_yahoo_request(db, 0.05, heartbeat)
        starts.append(clock[0])
    assert [(b - a).total_seconds() for a, b in zip(starts, starts[1:])] == [0.05] * 15
    assert db.provider_request_pacing.count_documents({}) == 1
    assert heartbeat.call_count >= 16
    clock[0] += timedelta(seconds=60)
    pacing.wait_for_yahoo_request(db, 0.05, heartbeat)
    idle_start = clock[0]
    pacing.wait_for_yahoo_request(db, 0.05, heartbeat)
    assert (clock[0] - idle_start).total_seconds() == 0.05


def test_submillisecond_grants_round_deadline_up(clock):
    db = mongomock.MongoClient().pacing_test
    clock[0] += timedelta(microseconds=999)
    first = clock[0]
    pacing.wait_for_yahoo_request(db, 0.05, Mock())
    pacing.wait_for_yahoo_request(db, 0.05, Mock())
    assert (clock[0] - first).total_seconds() >= 0.05
    assert db.provider_request_pacing.find_one()["next_request_at"].microsecond % 1000 == 0


def test_initialization_race_uses_existing_gate(clock, monkeypatch):
    db = mongomock.MongoClient().pacing_test
    pacing.wait_for_yahoo_request(db, 0.05, Mock())
    monkeypatch.setattr(db.provider_request_pacing, "update_one", Mock(side_effect=DuplicateKeyError("race")))
    initial = clock[0]
    pacing.wait_for_yahoo_request(db, 0.05, Mock())
    assert (clock[0] - initial).total_seconds() == 0.05


def test_long_wait_renews_lease_and_stale_owner_aborts(clock):
    db = mongomock.MongoClient().pacing_test
    pacing.wait_for_yahoo_request(db, 60, Mock())
    heartbeat = Mock(side_effect=[None, RuntimeError("lease lost")])
    initial = clock[0]
    with pytest.raises(RuntimeError, match="lease lost"):
        pacing.wait_for_yahoo_request(db, 60, heartbeat)
    assert (clock[0] - initial).total_seconds() == 1


def test_database_failure_fails_closed():
    db = Mock()
    db.provider_request_pacing.find_one_and_update.side_effect = ConnectionFailure("offline")
    with pytest.raises(ConnectionFailure):
        pacing.wait_for_yahoo_request(db, 0.05, Mock())


def test_zero_disables_pacing_without_database_access():
    db = Mock()
    pacing.wait_for_yahoo_request(db, 0, Mock())
    assert not db.mock_calls


@pytest.mark.parametrize("value", [-1, 61, float("nan"), float("inf")])
def test_invalid_intervals_rejected(value):
    with pytest.raises(ValidationError):
        TaskSettings(_env_file=None, yahoo_request_interval_seconds=value)


def test_default_matches_successful_survey():
    assert TaskSettings(_env_file=None).yahoo_request_interval_seconds == 0.05
