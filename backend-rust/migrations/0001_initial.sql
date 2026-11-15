-- Hedgehoog's PostgreSQL-native schema. Provider evidence and calculation
-- snapshots remain JSONB; identities, ownership, leases, and ledger writes are
-- relational and constrained.

CREATE TABLE users (
    id text PRIMARY KEY,
    login text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'user')),
    disabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE imports (
    id text PRIMARY KEY,
    import_date date NOT NULL UNIQUE,
    etfs boolean NOT NULL DEFAULT false
);

CREATE TABLE etfs (
    id text PRIMARY KEY,
    isin text NOT NULL,
    import_date date NOT NULL,
    name text,
    ticker text,
    asset_class text,
    currency text,
    inception_date date,
    source jsonb NOT NULL,
    UNIQUE (isin, import_date)
);
CREATE INDEX etfs_latest ON etfs (isin, import_date DESC);

CREATE TABLE indices (
    id text PRIMARY KEY,
    instrument_id text NOT NULL UNIQUE,
    symbol text NOT NULL UNIQUE,
    name text NOT NULL,
    currency text,
    return_variant text NOT NULL,
    verification_status text NOT NULL,
    provider text NOT NULL,
    provenance jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    removed_at timestamptz,
    removed_by text
);
CREATE INDEX indices_name ON indices (name, instrument_id);

INSERT INTO indices
    (id, instrument_id, symbol, name, currency, return_variant,
     verification_status, provider, provenance, created_at)
VALUES
    ('idx-3fdeebb0-421c-5371-bd5f-894769847292', 'idx-3fdeebb0-421c-5371-bd5f-894769847292', '^GSPC', 'S&P 500', NULL, 'unknown', 'unverified', 'yfinance', '{"source":"curated","metadata_verified":false}', now()),
    ('idx-98fcbc51-8eb1-5cf9-bda2-668fb52d37aa', 'idx-98fcbc51-8eb1-5cf9-bda2-668fb52d37aa', '^990100-USD-STRD', 'MSCI WORLD', NULL, 'unknown', 'unverified', 'yfinance', '{"source":"curated","metadata_verified":false}', now()),
    ('idx-165b108b-3076-5a75-b416-a0a6307f9596', 'idx-165b108b-3076-5a75-b416-a0a6307f9596', 'DE000SLA1W63.SG', 'Solactive European Overnight Money Market Index', NULL, 'unknown', 'unverified', 'yfinance', '{"source":"curated","metadata_verified":false}', now()),
    ('idx-cc76af03-683e-5878-a8b2-97229b0aeed7', 'idx-cc76af03-683e-5878-a8b2-97229b0aeed7', 'DE000SL0H423.SG', 'Solactive €STR Daily Total Return Index', NULL, 'unknown', 'unverified', 'yfinance', '{"source":"curated","metadata_verified":false}', now());

CREATE TABLE acquisition_jobs (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'deleting', 'retired', 'maintenance')),
    active boolean NOT NULL DEFAULT true,
    phase text,
    mode text,
    provider text,
    price_basis text,
    instrument_kind text,
    history_scope text,
    requested_by text,
    requested_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    lease_until timestamptz,
    owner text,
    listing jsonb,
    requested_listing jsonb,
    listing_resolution jsonb,
    lookup_ticker text,
    date_from date,
    date_to date,
    splits_date_from date,
    splits_date_to date,
    full_date_from date,
    base_generation text,
    last_page_date date,
    full_refresh_reason text,
    incremental_checked boolean,
    download_complete boolean,
    splits_complete boolean,
    published boolean NOT NULL DEFAULT false,
    published_at timestamptz,
    pages integer NOT NULL DEFAULT 0,
    rows integer NOT NULL DEFAULT 0,
    flagged_rows integer NOT NULL DEFAULT 0,
    unindexed_rows integer NOT NULL DEFAULT 0,
    calls integer NOT NULL DEFAULT 0,
    next_offset integer NOT NULL DEFAULT 0,
    next_split_offset integer NOT NULL DEFAULT 0,
    expected_total integer,
    expected_split_total integer,
    error text,
    error_code text,
    warning text,
    history_summary jsonb,
    retention_state text,
    pruned_at timestamptz
);
CREATE UNIQUE INDEX one_active_acquisition ON acquisition_jobs (instrument_id) WHERE active;
CREATE INDEX acquisition_instrument_order ON acquisition_jobs (instrument_id, requested_at DESC, id DESC);
CREATE INDEX acquisition_status_order ON acquisition_jobs (status, requested_at DESC, id DESC);

CREATE TABLE instruments (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    provider text NOT NULL,
    job_id text NOT NULL UNIQUE REFERENCES acquisition_jobs(id) ON DELETE CASCADE,
    collected_at timestamptz NOT NULL,
    collection_date date NOT NULL,
    data jsonb NOT NULL,
    asset_class text,
    index_name text,
    published boolean NOT NULL DEFAULT false
);
CREATE INDEX instruments_inventory ON instruments (instrument_id, collected_at DESC);

CREATE TABLE quotes (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    provider text NOT NULL,
    job_id text NOT NULL REFERENCES acquisition_jobs(id) ON DELETE CASCADE,
    source_job_id text,
    collected_at timestamptz NOT NULL,
    collection_date date NOT NULL,
    quote_date date NOT NULL,
    data jsonb NOT NULL,
    quality_flags jsonb,
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    volume numeric,
    adj_close numeric,
    price_currency text,
    UNIQUE (job_id, quote_date)
);
CREATE INDEX quotes_listing_date ON quotes (instrument_id, symbol, exchange, quote_date, collected_at DESC);
CREATE INDEX quotes_job_date ON quotes (job_id, quote_date);

CREATE TABLE raw_eod_pages (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    provider text NOT NULL,
    job_id text NOT NULL REFERENCES acquisition_jobs(id) ON DELETE CASCADE,
    collected_at timestamptz NOT NULL,
    collection_date date NOT NULL,
    page_offset integer NOT NULL,
    data jsonb NOT NULL,
    UNIQUE (job_id, page_offset)
);
CREATE INDEX raw_eod_provider_job ON raw_eod_pages (provider, instrument_id, job_id);

CREATE TABLE raw_split_pages (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    provider text NOT NULL,
    job_id text NOT NULL REFERENCES acquisition_jobs(id) ON DELETE CASCADE,
    collected_at timestamptz NOT NULL,
    collection_date date NOT NULL,
    page_offset integer NOT NULL,
    data jsonb NOT NULL,
    UNIQUE (job_id, page_offset)
);
CREATE INDEX raw_split_provider_job ON raw_split_pages (provider, instrument_id, job_id);

CREATE TABLE split_snapshots (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    job_id text NOT NULL UNIQUE REFERENCES acquisition_jobs(id) ON DELETE CASCADE,
    provider text NOT NULL,
    requested_at timestamptz NOT NULL,
    collected_at timestamptz NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    events jsonb NOT NULL
);
CREATE INDEX split_snapshot_order ON split_snapshots (instrument_id, requested_at DESC, id DESC);

CREATE TABLE market_data_publications (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    provider text NOT NULL,
    price_basis text NOT NULL,
    job_id text NOT NULL REFERENCES acquisition_jobs(id),
    requested_at timestamptz NOT NULL,
    published_at timestamptz NOT NULL
);
CREATE INDEX market_data_publication_order ON market_data_publications (instrument_id, published_at DESC, id DESC);

CREATE TABLE instrument_state (
    instrument_id text PRIMARY KEY,
    listing jsonb,
    listing_resolution jsonb,
    listing_job_id text,
    last_complete_quote_date date,
    price_currency_override text
);

CREATE TABLE instrument_statistics (
    instrument_id text PRIMARY KEY,
    snapshot jsonb NOT NULL
);

CREATE TABLE market_data_reader_leases (
    generation_id text NOT NULL REFERENCES acquisition_jobs(id) ON DELETE CASCADE,
    token text NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (generation_id, token)
);
CREATE INDEX market_data_reader_lease_expiry ON market_data_reader_leases (expires_at);

CREATE TABLE market_data_retention_runs (
    id text PRIMARY KEY,
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    owner text,
    lease_until timestamptz,
    summary jsonb,
    error text
);

CREATE TABLE market_data_retention (
    id text PRIMARY KEY,
    instrument_id text,
    generation_id text,
    provider text,
    pruned_at timestamptz NOT NULL,
    evidence jsonb NOT NULL
);

CREATE TABLE marketstack_usage (
    id text PRIMARY KEY,
    used integer NOT NULL DEFAULT 0 CHECK (used >= 0)
);

CREATE TABLE provider_request_pacing (
    provider text PRIMARY KEY,
    next_request_at timestamptz NOT NULL
);

CREATE TABLE app_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL,
    updated_by text
);

CREATE TABLE manual_splits (
    id text PRIMARY KEY,
    instrument_id text NOT NULL,
    symbol text NOT NULL,
    exchange text NOT NULL,
    split_date date NOT NULL,
    factor numeric NOT NULL CHECK (factor > 0),
    note text NOT NULL DEFAULT '',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL,
    created_by text NOT NULL,
    removed_at timestamptz,
    removed_by text
);
CREATE UNIQUE INDEX manual_splits_listing ON manual_splits (instrument_id, symbol, exchange, split_date) WHERE active;

CREATE TABLE planner_customers (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id),
    name text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (id, user_id)
);
CREATE INDEX planner_customers_owner_order ON planner_customers (user_id, created_at DESC, id DESC);

CREATE TABLE planner_goals (
    id text PRIMARY KEY,
    user_id text NOT NULL,
    customer_id text NOT NULL,
    name text NOT NULL,
    goal_eur numeric NOT NULL CHECK (goal_eur > 0),
    goal_date date NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (id, user_id, customer_id),
    FOREIGN KEY (customer_id, user_id) REFERENCES planner_customers(id, user_id)
);
CREATE INDEX planner_goals_owner_customer_order ON planner_goals (user_id, customer_id, created_at DESC, id DESC);

CREATE TABLE planner_simulations (
    id text PRIMARY KEY,
    user_id text NOT NULL,
    customer_id text NOT NULL,
    goal_id text NOT NULL,
    calculated_at timestamptz NOT NULL,
    saved_at timestamptz,
    expires_at timestamptz,
    is_plan boolean NOT NULL DEFAULT false,
    inputs jsonb NOT NULL,
    strategy_definition jsonb NOT NULL,
    result jsonb NOT NULL,
    UNIQUE (id, user_id, customer_id),
    FOREIGN KEY (goal_id, user_id, customer_id) REFERENCES planner_goals(id, user_id, customer_id)
);
CREATE INDEX planner_simulations_owner_goal_order ON planner_simulations (user_id, customer_id, goal_id, calculated_at DESC, id DESC);
CREATE INDEX planner_preview_expiry ON planner_simulations (expires_at) WHERE saved_at IS NULL;

CREATE TABLE portfolios (
    id text PRIMARY KEY,
    user_id text NOT NULL,
    customer_id text NOT NULL,
    name text NOT NULL,
    plan_id text,
    currency text NOT NULL DEFAULT 'EUR' CHECK (currency = 'EUR'),
    created_at timestamptz NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    UNIQUE (id, user_id, customer_id),
    FOREIGN KEY (customer_id, user_id) REFERENCES planner_customers(id, user_id),
    FOREIGN KEY (plan_id, user_id, customer_id) REFERENCES planner_simulations(id, user_id, customer_id)
);
CREATE INDEX portfolios_owner_customer_order ON portfolios (user_id, customer_id, created_at DESC, id DESC);

CREATE TABLE portfolio_transactions (
    id text PRIMARY KEY,
    portfolio_id text NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    client_id text NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    transaction_type text NOT NULL CHECK (transaction_type IN ('deposit', 'withdrawal', 'buy', 'sell')),
    transaction_date date NOT NULL,
    amount_eur numeric,
    quantity numeric,
    price_eur numeric,
    commission_eur numeric,
    instrument_id text,
    instrument_name text,
    symbol text,
    exchange text,
    currency text,
    created_at timestamptz NOT NULL,
    removed_at timestamptz,
    source text,
    source_rows jsonb,
    request jsonb NOT NULL,
    UNIQUE (portfolio_id, client_id),
    UNIQUE (portfolio_id, sequence),
    CHECK (
        (transaction_type IN ('deposit', 'withdrawal') AND amount_eur > 0)
        OR
        (transaction_type IN ('buy', 'sell') AND quantity > 0 AND price_eur > 0 AND commission_eur >= 0)
    )
);
CREATE INDEX portfolio_transactions_active_order ON portfolio_transactions (portfolio_id, transaction_date, sequence) WHERE removed_at IS NULL;

CREATE TABLE portfolio_recommendation_receipts (
    id text PRIMARY KEY,
    portfolio_id text NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    operation_id text NOT NULL,
    fingerprint text NOT NULL,
    revision bigint NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (portfolio_id, operation_id)
);

CREATE TABLE calibration_runs (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id),
    strategy_id text NOT NULL,
    calculated_at timestamptz NOT NULL,
    result jsonb NOT NULL,
    summary jsonb NOT NULL
);
CREATE INDEX calibration_runs_owner_order ON calibration_runs (user_id, calculated_at DESC, id DESC);
CREATE INDEX calibration_runs_owner_strategy_order ON calibration_runs (user_id, strategy_id, calculated_at DESC, id DESC);

-- Durable PostgreSQL queue replacing Celery and RabbitMQ.
CREATE TABLE job_queue (
    id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('etf_import', 'market_data_refresh', 'market_data_retention', 'market_data_acquisition')),
    queue text NOT NULL CHECK (queue IN ('catalogue', 'marketdata')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'retrying', 'succeeded', 'failed')),
    run_at timestamptz NOT NULL DEFAULT now(),
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts >= 1),
    locked_by text,
    locked_at timestamptz,
    lease_until timestamptz,
    last_error text,
    public_error text,
    dedupe_key text UNIQUE
);
CREATE INDEX job_queue_claim ON job_queue (queue, status, run_at, requested_at, id);
CREATE INDEX job_queue_lease ON job_queue (status, lease_until) WHERE status = 'running';
CREATE INDEX job_queue_kind_status_order ON job_queue (kind, status, requested_at DESC, id DESC);
