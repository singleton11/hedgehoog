ALTER TABLE job_queue DROP CONSTRAINT job_queue_kind_check;
ALTER TABLE job_queue ADD CONSTRAINT job_queue_kind_check
    CHECK (kind IN ('etf_import', 'market_data_refresh', 'market_data_retention',
                   'market_data_acquisition', 'market_data_statistics'));
