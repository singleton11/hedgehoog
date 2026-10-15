"""Queue stale saved ETFs and active indices without calling a provider."""

import logging
from datetime import timedelta

from celery_app import MARKET_DATA_REFRESH_HOUR, enqueue_acquisition
from market_data import DataError, aware, published_history, request_acquisition, utcnow

logger = logging.getLogger(__name__)


def refresh_stale_market_data(db, settings, *, now=None, publish=enqueue_acquisition):
    now = aware(now or utcnow())
    today = now.date().isoformat()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scheduled = midnight.replace(hour=MARKET_DATA_REFRESH_HOUR)
    fresh_since = scheduled if now >= scheduled else midnight
    # Match the saved ETF inventory, not the entire reference catalogue. Failed
    # unpublished Yahoo profiles must not silently become automatic acquisitions.
    identifiers = set(db.instruments.distinct("instrument_id", {
        "instrument_id": {"$not": {"$regex": "^idx-"}},
        "$or": [{"provider": {"$ne": "yfinance"}}, {"published": True}],
    }))
    identifiers.update(db.indices.distinct("instrument_id", {
        "removed_at": {"$exists": False},
    }))
    counts = {"requested": 0, "fresh": 0, "skipped": 0, "errors": 0}
    publish_failed = False
    for instrument_id in sorted(identifiers):
        name = "yfinance" if instrument_id.startswith("idx-") else settings.market_data_provider
        query = {"instrument_id": instrument_id, "status": "succeeded", "provider": name}
        if name == "yfinance":
            query["published"] = True
        latest = db.acquisition_jobs.find_one(query, sort=[("finished_at", -1)])
        finished = latest.get("finished_at") if latest else None
        # Restarts reuse successful checks, but a pre-02:00 check must not
        # suppress the nightly run. Use request time so jobs crossing 02:00
        # cannot claim to have fetched the later publication window.
        target = today if name == "yfinance" else (now.date() - timedelta(days=1)).isoformat()
        checked = latest.get("requested_at", finished) if latest else None
        if (finished is not None and checked is not None
                and fresh_since <= aware(checked) <= now
                and latest.get("date_to", "") >= target):
            counts["fresh"] += 1
            continue
        state = db.instrument_state.find_one({"_id": instrument_id}) or {}
        if name == "yfinance" and state.get("listing"):
            from market_data import observation_filter
            publication = published_history(db, instrument_id, state["listing"])
            if (publication and publication.get("requested_at") is not None
                    and fresh_since <= aware(publication["requested_at"]) <= now
                    and db.quotes.find_one({**observation_filter(db, instrument_id, state["listing"]), "date": today})):
                counts["fresh"] += 1
                continue
        try:
            # The existing unique active-job gate handles concurrent startup,
            # scheduled and manual requests, including stalled-job recovery.
            request_acquisition(db, settings, instrument_id, None, "update",
                                "system:market-data-refresh", publish, incremental=True)
            counts["requested"] += 1
        except DataError as exc:
            # Bad metadata, removal races and exhausted legacy budgets must not
            # prevent other instruments from refreshing. Broker failures retry
            # the sweep after all candidates have had a chance to be queued.
            counts["errors" if exc.status == 503 else "skipped"] += 1
            publish_failed |= exc.status == 503
            logger.warning("Automatic refresh skipped %s: %s", instrument_id, exc)
    logger.info("Automatic market-data refresh: %s", counts)
    if publish_failed:
        raise DataError(503, "One or more automatic refresh jobs could not be queued")
    return counts
