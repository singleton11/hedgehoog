from datetime import timedelta
from unittest.mock import Mock

import market_data as md
from test_indices import IndexProvider, clock, config, db, index, run


def test_index_automatic_diff_retains_history_with_nullable_ohlc(db, config, clock, index):
    first, _ = run(db, config, index)
    assert first["status"] == "succeeded"
    clock[0] += timedelta(days=1)
    job = md.request_acquisition(db, config, index["instrument_id"], None, "update", "system", Mock(), incremental=True)
    source = IndexProvider()
    result = md.run_acquisition(db, config, job["id"], source.factory)
    assert result["status"] == "succeeded" and result["mode"] == "incremental"
    assert [call[3:5] for call in source.calls if call[0] == "eod"] == [("2026-09-15", "2026-09-18")]
    listing = {"symbol": index["symbol"], "exchange": "YAHOO"}
    assert md.quote_dates(db, index["instrument_id"], listing) == ["2026-09-15"]
    assert db.acquisition_jobs.find_one({"_id": job["id"]})["base_generation"] == first["id"]
