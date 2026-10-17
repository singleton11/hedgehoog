# Jobs monitoring

`GET /jobs?status=&job_type=&limit=50&offset=0` requires a valid application
session. All authenticated users can view global operational metadata, not just
their own acquisitions. The endpoint is read-only and performs no provider calls,
lease recovery, retry, cancellation, or history migration.

## Response

```json
{
  "items": [
    {
      "id": "durable-job-or-celery-task-id",
      "job_type": "market_data_acquisition",
      "status": "succeeded",
      "instrument_id": "IE00B4L5Y983",
      "requested_at": "2026-10-17T02:00:00Z",
      "started_at": "2026-10-17T02:00:01Z",
      "finished_at": "2026-10-17T02:01:00Z",
      "error": null
    }
  ],
  "total": 1,
  "counts": {
    "total": 1,
    "queued": 0,
    "running": 0,
    "retrying": 0,
    "succeeded": 1,
    "failed": 0
  },
  "job_types": [
    {"value": "market_data_acquisition", "label": "Market data acquisition"},
    {"value": "etf_import", "label": "ETF catalogue import"},
    {"value": "market_data_refresh", "label": "Market data refresh"}
  ]
}
```

`status` accepts queued, running, retrying, succeeded, or failed. `job_type`
accepts the three values above. Omitted or empty filters mean all. Invalid
filters return 422. `limit` is 1-200 (default 50); `offset` is nonnegative
(default 0). `total` is the filtered count before pagination; `counts` is always
global. `job_types` always contains all supported options.

Items are sorted newest requested time first, then descending ID and type for
stable ties. Dates are UTC ISO timestamps or null when absent in legacy records.
Non-instrument jobs have null `instrument_id`. Failed/retrying jobs expose fixed
safe messages, never persisted exception strings, broker URLs, provider payloads,
or tracebacks. Other statuses have null `error`. Database outages return a safe
503. Unauthenticated requests return 401. Responses have `Cache-Control: no-store`.

## Persistence and limitations

- Acquisitions are read directly from existing `acquisition_jobs`, including
  historical records, without migration. Existing queued/running/succeeded/failed
  states are preserved. The temporary `deleting` removal mutex is excluded.
- Acquisition task execution and retry semantics are unchanged. In particular,
  an acquisition awaiting a Celery persistence/lease retry can still appear
  running or queued; monitoring does not rewrite its authoritative lease state.
- ETF imports and refresh sweeps persist in `background_jobs`, keyed by Celery
  request ID. API startup records queued before publish. A publish failure marks
  that record failed and still raises the original error. Direct producers without
  a database and Beat-scheduled tasks first appear when the worker starts.
- Retries and redeliveries reuse the task ID. `requested_at` is retained;
  `started_at` is the latest attempt start. Retryable exceptions mark retrying
  until the configured attempt budget is exhausted; other errors mark failed.
  A skipped already-current catalogue import is a successful execution.
- Refresh-sweep success means candidate processing completed; it does not mean
  all child acquisitions succeeded. Those acquisitions have their own records.
- Lifecycle writes are best effort on MongoDB errors so monitoring cannot alter
  task results or retry semantics. Database outages, hard worker termination, or
  retry-message publication failures may leave records absent or stale. There is
  no heartbeat reconciliation, broker inspection, or retrospective reconstruction
  of catalogue/sweep executions predating this feature.
- Counts and pages use separate database reads rather than a transactional
  snapshot; concurrent task transitions can briefly make them disagree.
- Existing removal workflows delete acquisition history for a removed instrument;
  the dashboard does not retain a separate audit copy. Background execution
  history has no automatic retention policy.
