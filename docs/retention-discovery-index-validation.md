# Raw-page retention discovery indexes

## Scope

Adds the non-unique `(provider, instrument_id, job_id)` index, using MongoDB's
natural name `provider_1_instrument_id_1_job_id_1`, to `raw_eod_pages` and
`raw_split_pages` in `market_data.ensure_indexes`. The provider prefix supports
cleanup's global legacy `distinct(instrument_id)` and the remaining keys support
per-instrument legacy discovery and fenced job purges. Existing unique
`(job_id, offset)` indexes remain unchanged. No quote index is added.

`cleanup()` now ensures indexes before its global discovery loop, rather than
relying on the later `retain_instrument()` call. Disabled retention still returns
before any index setup. Ownership predicates, attribution, gates, reader leases,
batch sizes and deletion rules are unchanged.

## Local MongoDB regression

`backend/tests/test_retention_discovery_mongodb.py` uses only the explicit
`TEST_MONGODB_URI`, random disposable databases and synthetic fixtures. Each raw
collection starts with the old unique job/offset index and 8,000 unrelated Yahoo
pages containing 4 KiB payloads. MongoDB 7.0.45 runs on a loopback-only Docker port
with a 512 MiB memory limit, one CPU and a 0.25 GiB WiredTiger cache.

RED: before the production change, both parameterized tests failed on the actual
winning-plan COLLSCAN assertion after calling `ensure_indexes()`. The old query
examined all 8,000 documents. A second RED test proved that global discovery ran
before index installation on a fresh database.

GREEN: unhinted executionStats EXPLAIN now uses indexed access, examining zero
documents for absent Marketstack matches on both an existing Yahoo instrument
and a nonexistent instrument. Tests also exercise provider-wide distinct,
matching legacy discovery, bounded ID selection, job/provider-null purge
identities, fenced delete commands and terminal empty matches. Actual cleanup
removes the attributable orphan, preserves all 8,000 Yahoo pages and leaves the
unattributed provider-less row intact. Repeated index setup is idempotent.

Focused command (from `backend/`):

```sh
TEST_MONGODB_URI=mongodb://127.0.0.1:27039 uv run --locked pytest \
  tests/test_retention_discovery_mongodb.py \
  tests/test_retention_mongo_integration.py \
  tests/test_market_data_retention.py \
  tests/test_latest_history_retention_guards.py \
  tests/test_retention_tasks.py -q
```

Result: **31 passed in 26.01 seconds**.

Canonical coverage command (from the checkout root, integration opt-in unset):

```sh
env -u TEST_MONGODB_URI bash scripts/run_coverage.sh backend . coverage/backend-canonical
```

Result: **2,116 passed, 78 skipped, 1,431 dependency warnings in 606.20 seconds**,
exit 0; combined statement/branch coverage **95.53%**. The four new real-Mongo
cases are among the skipped opt-in cases here and pass in the focused command
above. Reports are generated under `coverage/backend-canonical/` (not committed).

An additional full opt-in run with `TEST_MONGODB_URI` enabled completed with
**2,179 passed, 1 skipped, 1 failed and 13 setup errors in 954.28 seconds**, exit 1
(95.54% coverage). The failure is the existing legacy=True history-bounds case
that requests a now-rejected non-Yahoo generation. All 13 setup errors share
older fixtures selecting `market_data_provider="marketstack"`, rejected by the
current Yahoo-only settings. The legacy failure and a representative shared
fixture setup error were independently reproduced with `market_data.py` loaded
from unchanged base commit `4f741bd`; neither test nor provider contract was
weakened. The new four-case real-Mongo regression also passes in this full run.
The initial foreground coverage attempt hit the tool's 420-second timeout;
subsequent background runs completed with the results recorded here.

## Deferred cache invalidation

`retain_instrument()` still unconditionally deletes `instrument_statistics` after
a successful maintenance pass, even when nothing affecting the selected Yahoo
generation changed. This can turn a warm statistics path into a cold calculation.
The statistics signature already includes generation, observation count, split
evidence, listing, currency override, fill policy and cutoff. A separate change
should test preserving valid generation-bound caches across no-op and
old-generation pruning passes while still invalidating legacy/stale caches and
protecting publication/maintenance races. This patch deliberately leaves that
behavior unchanged, including the existing stale-cache deletion regression.

## Limits

These are local correctness/query-plan checks, not production latency evidence.
The synthetic payload sizes do not reproduce production I/O volume. Index builds
on an existing collection still require a bounded operational rollout. Other
provider-only discovery collections and filtered endpoint costs are not claimed
fixed. No production, deployment, publication or merge actions were performed.
