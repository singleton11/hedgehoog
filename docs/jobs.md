# Jobs dashboard

The authenticated **Jobs** workspace section (`#/jobs`) monitors background
market-data downloads, ETF catalogue imports, and automatic refresh sweeps.
It is read-only: viewing or refreshing this page never starts a download,
retries a job, or calls a market-data provider.

## Status and totals

The summary shows total jobs, queued jobs, in-progress jobs (running plus
retrying), finished jobs (succeeded plus failed), successful jobs, and failures.
Counts cover all recorded jobs and are not restricted by the list filters.
A successful refresh sweep means that dispatch finished successfully, not that
all child downloads have completed; those downloads have their own job rows.

The list supports status and job-type filters, newest-first pagination, job IDs,
instrument links, queued/start/finish timestamps, and safe error summaries.
The page checks for updates three seconds after the previous request finishes,
so slow requests do not overlap. Leaving the page aborts outstanding requests
and stops polling. Failed updates retain the last successful page with a stale
data warning and retry automatically; a manual refresh is also available.

## History and limitations

Historical market-data downloads are read from existing durable acquisition
records. Catalogue imports and refresh sweeps are tracked from deployment of
this feature onward; old worker executions cannot be reconstructed from the
broker. Counts describe retained records, not an independent lifetime counter.

Jobs are shared operational data for authenticated workspace users. No broker
credentials, task arguments, raw provider responses, or tracebacks are exposed.
Beat-scheduled sweeps first appear when a worker begins executing them; startup
producers record queued jobs before publishing. Acquisition retries retain the
existing acquisition state and may still appear queued or running rather than
retrying. Tracking writes are best effort during database outages. Counts and
pages are separate database reads, not a transactional snapshot.

A queued/running state is not a worker-health check. A worker that is killed
can leave a stale running record until redelivery updates it. This dashboard
does not cancel jobs or automatically declare long-running work failed.

Rebuild/restart the API, worker, and scheduler to enable tracking for all task
execution paths (`make services-up` rebuilds the Docker services). Existing
raw quotes, calculation reports, and acquisition publication rules are unchanged.
