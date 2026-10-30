# Batched leases for exact acquired ETF filters

Filtered lists still capture every latest Yahoo publication, lease all selected
payload generations, hydrate matching profiles, apply exact history and return
predicates, then count and paginate in collected-at descending / ISIN ascending
order. The unfiltered server-side page path and single-instrument reader remain
unchanged. Cache signatures and cold calculation paths are not bypassed.

`market_data_retention.reader_many` uses a request-owned UUID and the existing
300-second lifetime. Commands are bounded by 50 identities and the existing
512-KiB BSON budget (including conservative overhead). Each acquisition matches
explicit job/instrument/listing identities, published Yahoo ownership and a
nonretiring state; exact matched counts are required. Atomic pipeline updates
remove expired tokens while preserving live foreign readers. Narrow projected
latest-publication aggregates validate after admission and at completion, using
requested_at / published_at / job ID descending ordering. Missing, superseded,
retiring, pruned, unleased or expired generations return 409.

Attempted chunks are registered before writes, so partial or ambiguous acquisition
writes participate in cleanup. Every attempted chunk is released even if another
release fails; only this request's token is unset. A database outage during release
can still leave tokens until their normal expiry. History endpoint fallback reuses
the enclosing list leases; standalone endpoint consumers obtain a batch lease.

## Executed RED/GREEN evidence

Disposable MongoDB 7.0.45, loopback only, 1 CPU, 768 MiB memory, 256-MiB WiredTiger
cache. These are synthetic metadata/warm-cache fixtures, not production timings.
Actual client commands are measured with a PyMongo CommandListener, not
mongomock's internal lookup calls.

- Original warm 1,000-candidate combined 15-year/nonnegative-return request:
  197 exact matches, 20 requested items, 3.1002 seconds, 3,082 find commands,
  2,000 updates, one aggregate, one getMore. The new command-bound test failed
  because 2,000 updates exceeded the 40-command bound.
- Batched warm request: 197 exact matches, same requested page, about 0.4 seconds,
  82 finds, 40 updates, 41 aggregates, one getMore, no quote commands.
- Small 200-candidate control: 197 exact matches, 18 finds, eight updates, nine
  aggregates, one getMore. Command growth follows 50-candidate chunks.
- Legacy endpoint fallback initially failed its new command-bound test with
  4,020 finds and 2,040 updates (after outer batching). Reusing caller leases
  preserves exact 1,000-match/out-of-range-page results with 20 finds,
  40 updates, 61 aggregates and one getMore.

New real-Mongo tests cover publication before/after acquisition and during
hydration; retirement during capture; actual pruning blocked by admitted leases;
missing tokens, job deletion, listing changes, stored and request expiry; partial
and ambiguous writes, payload exceptions and nested/foreign readers; expired
crash-token cleanup; missing off-page profiles; invalid return values; publication
ties; BSON/identity bounds; and filter presets, ordering, exact totals and offsets.
Existing acquisition/retention/filter suites cover cold cache refresh, cache
invalidation, calendar/leap-day history and latest-only attribution.

## Verification

- Canonical `bash scripts/run_coverage.sh backend`: 2,116 passed, 109 skipped,
  1,431 dependency deprecation warnings in 505.20 seconds; 95.51% combined
  coverage (96.77% statements, 91.73% branches). Reports are under
  `coverage/backend/`. Service-dependent opt-in tests are skipped in this run
  and exercised separately where described below.
- Focused offline acquisition/filter/retention suites: 226 passed (two existing
  dependency deprecation warnings).
- Real Mongo suites (new reader tests plus pagination, cold/warm return filtering,
  universe and retention integration): 41 passed in 162.86 seconds. This includes
  30,000-instrument unfiltered pagination and its generation-profile join EXPLAIN:
  `job_id_1` used, zero profile collection scans. Synthetic cold calculations
  still read 60/20 candidate histories, while corresponding warm calls read zero.
- Current published-Yahoo endpoint lookup and missing-carrier integration cases:
  passed separately. The pre-existing legacy case is documented below.

## Known baseline limitation

The opt-in legacy=True case in
`test_acquired_history_integration.py::test_correlated_endpoints_use_existing_indexes_in_both_directions`
already fails on unchanged origin/main (4f741bd): it requests a `legacy` generation
that `history_bounds_pipeline` deliberately rejects with 409. This failure was
reproduced by executing the original module from git, not inferred or skipped as
a new regression. The current published-Yahoo case and other real-Mongo suites
are verified separately. This change does not repair that stale legacy fixture.

## Scope and remaining risk

There is no production access, deployment, frontend change, index change, cache
policy change or publication in this worktree. No claim is made about production
HTTP latency or instant cold-history calculations. Exact filters still perform
inventory-wide work and hold leases for all captured candidates; command batching
reduces network amplification, not that server work or the five-minute deadline.
Retention's independent unconditional statistics eviction remains outside scope.
