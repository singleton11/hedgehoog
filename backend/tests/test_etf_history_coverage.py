import csv
from datetime import date
import json

import pytest

import etf_history_coverage as coverage
from market_provider import MarketDataError


START = date(1970, 1, 1)
END = date(2026, 9, 1)
RECORD = {"isin": "IE00TEST00001", "ticker": "TEST"}


class FakeClient:
    failure = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def resolve_listing(self, ticker, isin):
        if self.failure:
            raise MarketDataError(self.failure, "Safe failure")
        return {"symbol": "TEST.DE", "exchange": "XETR"}, {}

    def eod_page(self, symbol, exchange, start, end):
        assert (symbol, exchange, start, end) == ("TEST.DE", "XETR", START, END)
        return {"data": [
            {"date": "2026-08-01", "close": "10"},
            {"date": "2026-08-01", "close": "11"},
            {"date": "2026-08-02", "close": None},
            {"date": "2026-08-03", "close": "NaN"},
            {"date": "2026-08-04", "close": "0"},
            {"date": "2026-08-05", "close": "12"},
            {"date": "2026-09-02", "close": "12"},
        ]}


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    monkeypatch.setattr(coverage, "YahooFinanceClient", FakeClient)
    monkeypatch.setattr(FakeClient, "failure", None)


def test_counts_distinct_dates_with_valid_close():
    row = coverage.survey(RECORD, START, END, lambda: None, 20)
    assert row["day_closes"] == 2
    assert row["status"] == "ok"
    assert row["yahoo_symbol"] == "TEST.DE"
    assert row["first_close_date"] == "2026-08-01"
    assert row["last_close_date"] == "2026-08-05"


def test_failures_are_not_zero_history(monkeypatch):
    monkeypatch.setattr(FakeClient, "failure", "listing_not_found")
    row = coverage.survey(RECORD, START, END, lambda: None, 20)
    assert row["day_closes"] == ""
    assert row["error_code"] == "listing_not_found"
    assert row["status"] == "error"


def test_empty_history_is_zero(monkeypatch):
    monkeypatch.setattr(FakeClient, "eod_page", lambda *args: {"data": []})
    row = coverage.survey(RECORD, START, END, lambda: None, 20)
    assert row["day_closes"] == 0
    assert row["status"] == "no_history"


def cli_files(tmp_path):
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps([RECORD, {**RECORD, "isin": "IE00TEST00002"}]))
    output = tmp_path / "coverage.csv"
    args = ["--catalogue", str(catalogue), "--output", str(output), "--end", END.isoformat(), "--delay", "0", "--workers", "1"]
    return output, args


def test_logs_each_instrument(tmp_path, capsys):
    _, args = cli_files(tmp_path)
    assert coverage.main(args) == 0
    log = capsys.readouterr().err
    for isin in ("IE00TEST00001", "IE00TEST00002"):
        assert f"ticker=TEST isin={isin} START requesting Yahoo history" in log
        assert f"ticker=TEST isin={isin} DONE yahoo_symbol=TEST.DE status=ok day_closes=2 error=- elapsed=" in log


def test_limit_resume_and_no_overwrite(tmp_path):
    output, args = cli_files(tmp_path)
    assert coverage.main([*args, "--limit", "1"]) == 0
    assert coverage.main([*args, "--resume"]) == 0
    assert coverage.main([*args, "--resume"]) == 0
    with output.open(newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 2
    assert {row["isin"] for row in rows} == {"IE00TEST00001", "IE00TEST00002"}
    with pytest.raises(SystemExit):
        coverage.main(args)
    assert coverage.main([*args, "--resume", "--end", "2026-08-31"]) == 1


def test_rate_limit_saves_row_and_stops(tmp_path, monkeypatch):
    output, args = cli_files(tmp_path)
    monkeypatch.setattr(FakeClient, "failure", "quota_exceeded")
    assert coverage.main(args) == 2
    with output.open(newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1
    assert rows[0]["error_code"] == "quota_exceeded"
    assert rows[0]["day_closes"] == ""


def test_resume_rejects_truncated_csv(tmp_path):
    output, args = cli_files(tmp_path)
    output.write_text(",".join(coverage.FIELDS) + "\nTEST,2,IE00TEST00001\n")
    assert coverage.main([*args, "--resume"]) == 1


def test_spawn_workers_offline():
    # Missing tickers exercise real spawned processes without network requests.
    records = [{"isin": f"IE00TEST0000{index}"} for index in range(1, 5)]
    results = list(coverage.survey_results(records, START, END, 0, 20, 2))
    assert len(results) == 4
    assert {row["isin"] for _, row, _ in results} == {record["isin"] for record in records}
    assert all(row["error_code"] == "missing_ticker" for _, row, _ in results)


def test_stopped_pacer_never_requests():
    from threading import Event

    stop = Event()
    stop.set()
    with pytest.raises(MarketDataError, match="Survey stopped"):
        coverage.RequestPacer(0, stop=stop)()


def test_request_pacing(monkeypatch):
    ticks = iter([10, 10.25, 11])
    sleeps = []
    monkeypatch.setattr(coverage.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(coverage.time, "sleep", sleeps.append)
    pacer = coverage.RequestPacer(1)
    pacer()
    pacer()
    assert sleeps == [0.75]
