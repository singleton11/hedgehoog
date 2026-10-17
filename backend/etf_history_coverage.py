"""Standalone Yahoo daily-close coverage survey; never writes application data."""

import argparse
import csv
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import multiprocessing
from pathlib import Path
import sys
import time
from threading import Lock

from etfs import download_etfs
from market_provider import MarketDataError
from yahoo_finance import YahooFinanceClient


FIELDS = (
    "ticker", "day_closes", "isin", "yahoo_symbol", "status", "error_code",
    "first_close_date", "last_close_date", "date_from", "date_to", "checked_at",
)


class RequestPacer:
    def __init__(self, delay, lock=None, shared_last=None, stop=None):
        self.delay = delay
        self.last_request = None
        self.lock = lock if lock is not None else Lock()
        self.shared_last = shared_last
        self.stop = stop

    def __call__(self):
        with self.lock:
            if self.stop is not None and self.stop.is_set():
                raise MarketDataError("survey_stopped", "Survey stopped before request")
            previous = self.shared_last.value if self.shared_last is not None else self.last_request
            if previous is not None and previous != 0:
                delay = max(0, self.delay - (time.monotonic() - previous))
                if self.stop is None:
                    time.sleep(delay)
                elif self.stop.wait(delay):
                    raise MarketDataError("survey_stopped", "Survey stopped before request")
            now = time.monotonic()
            if self.shared_last is not None:
                self.shared_last.value = now
            else:
                self.last_request = now


_worker_pacer = None


def init_worker(delay, lock, last_request, stop):
    global _worker_pacer
    _worker_pacer = RequestPacer(delay, lock, last_request, stop)


def logged_survey(index, total, record, start, end, pacer, timeout):
    label = f"[{index}/{total}] ticker={record.get('ticker') or '-'} isin={record['isin']}"
    print(f"{label} START requesting Yahoo history", file=sys.stderr, flush=True)
    started = time.monotonic()
    row = survey(record, start, end, pacer, timeout)
    return label, row, time.monotonic() - started


def worker_survey(index, total, record, start, end, timeout):
    return logged_survey(index, total, record, start, end, _worker_pacer, timeout)


def survey_results(records, start, end, delay, timeout, workers):
    if workers == 1:
        pacer = RequestPacer(delay)
        for index, record in enumerate(records, 1):
            yield logged_survey(index, len(records), record, start, end, pacer, timeout)
        return
    # Spawn isolates yfinance's global state. Keep at most workers tasks in
    # flight; only the parent writes CSV, in completion rather than input order.
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    lock = context.Lock()
    last_request = context.Value("d", 0)
    executor = ProcessPoolExecutor(
        max_workers=workers, mp_context=context, initializer=init_worker,
        initargs=(delay, lock, last_request, stop),
    )
    tasks = iter(enumerate(records, 1))
    futures = set()

    def submit_next():
        item = next(tasks, None)
        if item is not None:
            index, record = item
            futures.add(executor.submit(worker_survey, index, len(records), record, start, end, timeout))

    try:
        for _ in range(min(workers, len(records))):
            submit_next()
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            results = [future.result() for future in completed]
            denied = any(result[1]["error_code"] in ACCESS_ERRORS for result in results)
            if denied:
                stop.set()
            for result in results:
                futures.difference_update(completed)
                if result[1]["error_code"] != "survey_stopped":
                    yield result
            if denied:
                return
            for _ in completed:
                submit_next()
    finally:
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)


ACCESS_ERRORS = {"quota_exceeded", "authentication_error", "plan_restricted"}


def valid_close(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number > 0


def survey(record, start, end, pacer, timeout):
    row = dict.fromkeys(FIELDS, "")
    row.update(
        ticker=record.get("ticker", ""), isin=record["isin"],
        date_from=start.isoformat(), date_to=end.isoformat(),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    if not isinstance(row["ticker"], str) or not row["ticker"].strip():
        row.update(status="error", error_code="missing_ticker", ticker="")
        return row
    try:
        # A fresh client per ETF bounds memory and prevents failed sessions from
        # poisoning subsequent instruments. The pacer is shared across clients.
        with YahooFinanceClient(before_request=pacer, timeout=timeout) as client:
            listing, _ = client.resolve_listing(row["ticker"], row["isin"])
            row["yahoo_symbol"] = listing["symbol"]
            page = client.eod_page(listing["symbol"], listing["exchange"], start, end)
        days = sorted({
            quote["date"] for quote in page["data"]
            if valid_close(quote.get("close"))
            and start.isoformat() <= quote["date"] <= end.isoformat()
        })
        row.update(day_closes=len(days), status="ok" if days else "no_history")
        if days:
            row.update(first_close_date=days[0], last_close_date=days[-1])
    except MarketDataError as exc:
        row.update(status="error", error_code=exc.code)
    return row


def load_catalogue(path):
    records = json.loads(path.read_text(encoding="utf-8")) if path else download_etfs()
    if not isinstance(records, list) or not records:
        raise ValueError("Catalogue must be a non-empty JSON array of ETF objects")
    seen = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("isin"), str) or not record["isin"].strip():
            raise ValueError("Every catalogue record must have a non-empty ISIN")
        if record["isin"] in seen:
            raise ValueError(f"Duplicate catalogue ISIN: {record['isin']}")
        seen.add(record["isin"])
    return records


def completed_isins(path, start, end):
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("Existing CSV has an incompatible header")
        seen = set()
        for row in reader:
            if (None in row or any(value is None for value in row.values())
                    or not row["isin"] or row["status"] not in {"ok", "no_history", "error"}
                    or row["date_from"] != start.isoformat() or row["date_to"] != end.isoformat()):
                raise ValueError("Existing CSV is incomplete or uses a different date range")
            if row["isin"] in seen:
                raise ValueError("Existing CSV contains duplicate ISINs")
            seen.add(row["isin"])
        return seen


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("Must be finite and non-negative")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, help="Local etfdb JSON; default: download project catalogue")
    parser.add_argument("--output", type=Path, default=Path("etf_history_coverage.csv"))
    parser.add_argument("--resume", action="store_true", help="Append, skipping every previously recorded ISIN, including errors")
    parser.add_argument("--limit", type=positive_int, help="Maximum new ETFs to attempt this run")
    parser.add_argument("--delay", type=nonnegative_float, default=1.0, help="Minimum seconds between actual Yahoo HTTP requests (default: 1)")
    parser.add_argument("--timeout", type=positive_int, default=20, help="Per-request timeout in seconds")
    parser.add_argument("--workers", type=positive_int, default=4, help="Parallel isolated worker processes (default: 4; use 1 for sequential)")
    parser.add_argument("--start", type=date.fromisoformat, default=date(1970, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(timezone.utc).date() - timedelta(days=1), help="Inclusive cutoff; default: yesterday UTC")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--start must not be later than --end")
    if args.output.exists() and not args.resume:
        parser.error("Output already exists; use --resume or a different --output")
    try:
        done = completed_isins(args.output, args.start, args.end) if args.resume else set()
        records = load_catalogue(args.catalogue)
        pending = [record for record in records if record["isin"] not in done]
        if args.limit:
            pending = pending[:args.limit]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        print(f"Catalogue: {len(records)} ETFs; previously recorded: {len(done)}; attempting: {len(pending)}", file=sys.stderr)
        mode = "a" if args.resume else "x"
        with args.output.open(mode, newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=FIELDS)
            if target.tell() == 0:
                writer.writeheader()
                target.flush()
            results = survey_results(pending, args.start, args.end, args.delay, args.timeout, args.workers)
            denied = False
            try:
                for label, row, elapsed in results:
                    writer.writerow(row)
                    target.flush()
                    print(
                        f"{label} DONE yahoo_symbol={row['yahoo_symbol'] or '-'} "
                        f"status={row['status']} day_closes={row['day_closes'] if row['day_closes'] != '' else '-'} "
                        f"error={row['error_code'] or '-'} elapsed={elapsed:.1f}s",
                        file=sys.stderr, flush=True,
                    )
                    if row["error_code"] in ACCESS_ERRORS:
                        print("Yahoo denied/rate-limited access. Stopping; results are saved. Resume later.", file=sys.stderr)
                        denied = True
                        if args.workers == 1:
                            break
            finally:
                results.close()
            if denied:
                return 2
    except KeyboardInterrupt:
        print("Interrupted; completed rows are saved. Use --resume to continue.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
