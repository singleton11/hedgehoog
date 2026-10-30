# Acquired ETF pagination validation

Base: `3905e93dd1b26a98f54937de102064a921cf0fc6` (PR54).
Local validation only; no AWS changes, production requests, new infrastructure,
PR publication, merge or deployment.

## Scope

Unfiltered reads now select the latest Yahoo generation per ETF, validate the
attributable profile, and count/order/page inside MongoDB before opening payload
reader leases. Only the page is hydrated and calculated. Ordering remains profile
collection time descending, then instrument ID ascending. Latest-generation
history summaries still reach the statistics cache validator. Missing profiles,
including off-page profiles, remain conflicts rather than silently disappearing.

The universe reads compact generation/profile metadata and batched projected
catalogue labels, without payload leases or quote reads. A final batched
publication-identity/retirement check detects publication/removal during capture.
Page payloads retain the existing reader acquisition and completion checks.

History/negative-return filters deliberately retain the existing before-pagination
selection, totals, cache validation and lease behavior. This patch does not solve
their inventory-wide work or concurrent cold-cache duplicate recalculations.

## Regression and correctness evidence

- Before implementation, the new unit regressions failed: a three-row page took
  20/501 reader leases for inventories of 20/501; universe took 501 leases and
  individual profile/catalogue reads. They pass after the change.
- A real-Mongo baseline rerun executes the original `market_data.py` from the base
  SHA in memory, without changing the checkout. With 40 ETFs and limit 1 it fails
  the new bound: **86 update commands**, versus **8** with this patch.
- New tests cover tied ordering, offsets, empty pages, exact totals, missing
  off-page profiles, publication before page lease acquisition, publication during
  page hydration, lease cleanup, and universe publication during label capture.
- Focused acquisition/filter/retention/safety suite: **204 passed**, two dependency
  deprecation warnings, 21.55 seconds.
- Canonical `bash scripts/run_coverage.sh backend . <report-dir>`: **2116 passed,
  74 opt-in skipped, 1431 dependency warnings**, exit 0, 509.08 seconds. Combined
  branch coverage is **95.52%**; `market_data.py` is **94.78%**. No authenticated
  target-branch coverage-baseline comparison is claimed.
- Canonical frontend coverage: **28 files / 968 tests passed**, exit 0. Statements
  96.67%, branches 93.90%, functions 97.80%, lines 98.39%. `npm run build` passes.
  `npm ci` reports seven existing dependency vulnerabilities; no dependency or
  lockfile changes are included.

The two existing interaction assertions updated here distinguish actual batched
hydration from mongomock's internal `$lookup` implementation, and accept absent
lease metadata when a metadata-only universe correctly never acquired a lease.
The 409 and zero-active-lease assertions remain enforced.

## Disposable real MongoDB

MongoDB **7.0.45**, Docker image `mongo:7`, bound only to local loopback port
28037, with UUID-named databases dropped in fixture finalizers. No production
configuration or service was used. Run from `backend`:

```sh
TEST_MONGODB_URI=mongodb://127.0.0.1:28037 uv run pytest \
  tests/test_acquired_pagination_mongodb.py \
  tests/test_acquired_returns_mongodb.py \
  tests/test_retention_mongo_integration.py -q -s
```

These five tests pass, with real statistics calculations and Mongo command
monitoring. The scale fixture has **30,000 distinct ETFs, 41,628 jobs, 2 KiB of
provider payload per profile, and 366 daily quotes for each of the 40 ETFs used
by nonempty pages** (14,640 quotes total). It is not a 45-million-quote benchmark.
The 40-ETF control has identical per-page client work.

Representative final-source execution (seconds, not an SLA):

| Inventory | Page size / offset | Seconds | Finds | Updates |
| --- | --- | --- | --- | --- |
| 40 | 1 / 0 | 0.2152 | 24 | 8 |
| 30,000 | 1 / 0 | 2.2898 | 24 | 8 |
| 40 | 20 / 20 | 0.5199 | 366 | 160 |
| 30,000 | 20 / 20 | 2.6039 | 366 | 160 |
| 30,000 | 20 / 30,000 | 2.2006 | 0 | 0 |

Universe: 30,000 rows in **3.7757 seconds**, two aggregate commands, two getMores,
600 bounded catalogue finds, **zero writes and zero quote commands**.
Actual metadata-pipeline EXPLAIN shows `job_id_1` for the profile lookup,
30,000 keys/documents examined, **zero lookup collection scans**. Selection and
count still require inventory-proportional server work; this is not constant-time
pagination and not proof of latency on the IO-constrained production EC2 host.

The existing real return-filter test also passes: 60 candidate ETFs / 366 daily
quotes, total 30 nonnegative members; combined five-year filter yields 10 members.
Cold runs calculate 60/20 histories respectively; warm runs calculate zero.
Existing real retention tests pass, including retirement while a reader is active.

## Known existing opt-in failures

Explicitly enabling the additional old acquired-history/universe integration
modules produces **7 passed, 2 failed** across all acquired/retention Mongo tests.
Both failures reproduce using the base SHA's original `market_data.py`:

- `test_acquired_history_integration.py` legacy=True requests legacy history bounds
  that the latest-Yahoo-only contract now explicitly rejects with 409.
- `test_acquired_universe_integration.py` supplies a wrapper without
  `acquisition_jobs` and fixtures with no attributed publication jobs; both base
  and patched code fail with the same missing-attribute error.

These stale opt-in tests are neither silently counted as passes nor rewritten as
part of the pagination fix. The supported Yahoo endpoint-probe cases in the old
history module pass against real Mongo, using the existing quote index.

## Remaining risks

Filtered requests still hold inventory-wide leases and can recalculate all
candidate histories on a cold cache. Concurrent cold requests can duplicate that
work. Unfiltered metadata lookup/count remains O(inventory), and the server-side
simple lookup fetches each chosen profile before projecting compact output.
Universe intentionally returns the complete inventory and uses bounded batches,
so its response/memory/catalogue work remains inventory-proportional. Captured
metadata is not a transactional cross-request/cross-page snapshot. Existing
payload lease expiry and generation races still return 409 for retry. Production
latency, cache/disk pressure and repository GitHub checks remain unverified here.
