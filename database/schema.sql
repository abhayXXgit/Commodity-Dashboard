-- ============================================================================
-- TransformerProcure Intelligence  —  Database Schema (PostgreSQL 14+)
-- Commodity Price Tracker, Forecasting & Transformer BOM Cost Exposure System
-- ============================================================================
-- Design rules enforced here:
--   * Original source price + currency + unit are NEVER overwritten.
--     Normalised INR/MT values live in separate *_inr_mt columns.
--   * Every price row carries: source, source_url, basis, timestamp, data_class.
--   * data_class distinguishes LIVE / VERIFIED_HISTORICAL / SUPPLIER_QUOTE /
--     ESTIMATED / FORECAST / DEMO_DATA. Never blur these.
-- ============================================================================

CREATE TABLE IF NOT EXISTS commodity_master (
    id                  SERIAL PRIMARY KEY,
    code                VARCHAR(32)  NOT NULL UNIQUE,   -- CU, AL, CRGO, STEEL, OIL
    name                VARCHAR(128) NOT NULL,
    category            VARCHAR(64),                    -- BASE_METAL / CORE / STRUCTURAL / FLUID
    base_unit           VARCHAR(16)  NOT NULL DEFAULT 'MT',
    base_currency       VARCHAR(8)   NOT NULL DEFAULT 'INR',
    benchmark_source    VARCHAR(128),                   -- e.g. 'LME Cash'
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS provider_master (
    id                  SERIAL PRIMARY KEY,
    code                VARCHAR(32)  NOT NULL UNIQUE,   -- NALCO, BALCO, HINDALCO, LME, BME
    name                VARCHAR(128) NOT NULL,
    provider_type       VARCHAR(32)  NOT NULL,          -- EXCHANGE / PRODUCER / SUPPLIER / INTERNAL
    country             VARCHAR(64),
    default_currency    VARCHAR(8)   NOT NULL DEFAULT 'INR',
    default_unit        VARCHAR(16)  NOT NULL DEFAULT 'MT',
    default_basis       VARCHAR(64),                    -- EX_PLANT / CIF / FOB / CASH
    source_url          TEXT,
    ingestion_mode      VARCHAR(32)  DEFAULT 'MANUAL',  -- API / CIRCULAR_IMPORT / MANUAL
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS product_master (
    id                  SERIAL PRIMARY KEY,
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id) ON DELETE CASCADE,
    code                VARCHAR(48)  NOT NULL UNIQUE,   -- AL_INGOT, AL_WIREROD, CU_CATHODE
    name                VARCHAR(128) NOT NULL,
    grade               VARCHAR(64),                    -- P1020A, ETP, EC_GRADE, M4
    specification       TEXT,
    default_unit        VARCHAR(16) NOT NULL DEFAULT 'MT',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS ix_product_commodity ON product_master(commodity_id);

-- ---------------------------------------------------------------------------
-- PRICE DATA
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS historical_prices (
    id                  BIGSERIAL PRIMARY KEY,
    price_date          DATE        NOT NULL,
    commodity_id        INTEGER     NOT NULL REFERENCES commodity_master(id),
    provider_id         INTEGER     NOT NULL REFERENCES provider_master(id),
    product_id          INTEGER     REFERENCES product_master(id),
    grade               VARCHAR(64),
    -- as published by the source (never mutated)
    price               NUMERIC(18,4) NOT NULL,
    currency            VARCHAR(8)    NOT NULL,
    unit                VARCHAR(16)   NOT NULL,
    -- OHLC where the source publishes it
    open_price          NUMERIC(18,4),
    high_price          NUMERIC(18,4),
    low_price           NUMERIC(18,4),
    close_price         NUMERIC(18,4),
    -- commercial build-up
    price_basis         VARCHAR(64),
    location            VARCHAR(128),
    premium             NUMERIC(18,4) DEFAULT 0,
    freight             NUMERIC(18,4) DEFAULT 0,
    tax_pct             NUMERIC(9,4)  DEFAULT 0,
    -- normalised analytics value (derived, safe to recompute)
    price_inr_mt        NUMERIC(18,4),
    fx_rate_used        NUMERIC(18,6),
    -- provenance
    effective_date      DATE,
    source              VARCHAR(128) NOT NULL,
    source_url          TEXT,
    data_class          VARCHAR(32)  NOT NULL DEFAULT 'VERIFIED_HISTORICAL',
    validation_status   VARCHAR(32)  NOT NULL DEFAULT 'PENDING', -- PASS/WARN/FAIL/PENDING
    quality_flags       TEXT,
    revision            INTEGER      NOT NULL DEFAULT 1,
    data_timestamp      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hist UNIQUE (price_date, commodity_id, provider_id, product_id, grade, revision),
    CONSTRAINT ck_price_positive CHECK (price > 0)
);
CREATE INDEX IF NOT EXISTS ix_hist_date      ON historical_prices(price_date);
CREATE INDEX IF NOT EXISTS ix_hist_series    ON historical_prices(commodity_id, provider_id, product_id, price_date);
CREATE INDEX IF NOT EXISTS ix_hist_class     ON historical_prices(data_class);

CREATE TABLE IF NOT EXISTS live_prices (
    id                  BIGSERIAL PRIMARY KEY,
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    provider_id         INTEGER NOT NULL REFERENCES provider_master(id),
    product_id          INTEGER REFERENCES product_master(id),
    latest_price        NUMERIC(18,4) NOT NULL,
    previous_price      NUMERIC(18,4),
    currency            VARCHAR(8)  NOT NULL,
    unit                VARCHAR(16) NOT NULL,
    price_inr_mt        NUMERIC(18,4),
    day_change          NUMERIC(18,4),
    day_change_pct      NUMERIC(12,4),
    week_change_pct     NUMERIC(12,4),
    month_change_pct    NUMERIC(12,4),
    ytd_change_pct      NUMERIC(12,4),
    price_basis         VARCHAR(64),
    source              VARCHAR(128) NOT NULL,
    source_url          TEXT,
    data_class          VARCHAR(32) NOT NULL DEFAULT 'LIVE',
    confidence          VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',  -- HIGH/MEDIUM/LOW/STALE/UNAVAILABLE
    is_stale            BOOLEAN NOT NULL DEFAULT FALSE,
    stale_reason        TEXT,
    last_updated        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_live UNIQUE (commodity_id, provider_id, product_id)
);

CREATE TABLE IF NOT EXISTS supplier_quotes (
    id                  BIGSERIAL PRIMARY KEY,
    quote_ref           VARCHAR(64),
    supplier_name       VARCHAR(160) NOT NULL,
    provider_id         INTEGER REFERENCES provider_master(id),
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    product_id          INTEGER REFERENCES product_master(id),
    quote_date          DATE NOT NULL,
    valid_until         DATE,
    price               NUMERIC(18,4) NOT NULL,
    currency            VARCHAR(8) NOT NULL DEFAULT 'INR',
    unit                VARCHAR(16) NOT NULL DEFAULT 'MT',
    freight             NUMERIC(18,4) DEFAULT 0,
    premium             NUMERIC(18,4) DEFAULT 0,
    tax_pct             NUMERIC(9,4)  DEFAULT 18,
    -- derived
    landed_cost_inr_mt  NUMERIC(18,4),
    effective_price_inr_mt NUMERIC(18,4),
    variance_vs_market  NUMERIC(18,4),
    variance_pct        NUMERIC(12,4),
    variance_vs_prev    NUMERIC(18,4),
    status_flag         VARCHAR(32),   -- BEST_PRICE / ABOVE_MARKET / BELOW_MARKET / EXPIRED
    moq_mt              NUMERIC(14,4),
    payment_terms       VARCHAR(160),
    delivery_days       INTEGER,
    price_basis         VARCHAR(64),
    remarks             TEXT,
    data_class          VARCHAR(32) NOT NULL DEFAULT 'SUPPLIER_QUOTE',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_quote_cmdty ON supplier_quotes(commodity_id, quote_date);

CREATE TABLE IF NOT EXISTS exchange_rates (
    id                  BIGSERIAL PRIMARY KEY,
    rate_date           DATE NOT NULL,
    base_currency       VARCHAR(8) NOT NULL,
    quote_currency      VARCHAR(8) NOT NULL,
    rate                NUMERIC(18,6) NOT NULL,
    source              VARCHAR(128) NOT NULL,
    data_class          VARCHAR(32) NOT NULL DEFAULT 'VERIFIED_HISTORICAL',
    data_timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_fx UNIQUE (rate_date, base_currency, quote_currency)
);

CREATE TABLE IF NOT EXISTS freight_premium (
    id                  SERIAL PRIMARY KEY,
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    provider_id         INTEGER REFERENCES provider_master(id),
    location            VARCHAR(128),
    effective_from      DATE NOT NULL,
    effective_to        DATE,
    premium_inr_mt      NUMERIC(18,4) DEFAULT 0,
    freight_inr_mt      NUMERIC(18,4) DEFAULT 0,
    remarks             TEXT
);

CREATE TABLE IF NOT EXISTS forecasts (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              VARCHAR(64) NOT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    provider_id         INTEGER REFERENCES provider_master(id),
    product_id          INTEGER REFERENCES product_master(id),
    horizon_days        INTEGER NOT NULL,
    target_date         DATE NOT NULL,
    forecast_price      NUMERIC(18,4) NOT NULL,
    lower_ci            NUMERIC(18,4),
    upper_ci            NUMERIC(18,4),
    model_used          VARCHAR(64) NOT NULL,
    mae                 NUMERIC(18,4),
    rmse                NUMERIC(18,4),
    mape                NUMERIC(12,4),
    direction           VARCHAR(16),   -- UP / DOWN / FLAT
    confidence          VARCHAR(16),   -- HIGH / MEDIUM / LOW
    data_class          VARCHAR(32) NOT NULL DEFAULT 'FORECAST',
    CONSTRAINT uq_forecast UNIQUE (run_id, commodity_id, provider_id, product_id, horizon_days)
);
CREATE INDEX IF NOT EXISTS ix_forecast_lookup ON forecasts(commodity_id, product_id, horizon_days, generated_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id                  BIGSERIAL PRIMARY KEY,
    alert_code          VARCHAR(64) NOT NULL,
    severity            VARCHAR(16) NOT NULL,   -- INFO / WARNING / CRITICAL
    commodity_id        INTEGER REFERENCES commodity_master(id),
    provider_id         INTEGER REFERENCES provider_master(id),
    product_id          INTEGER REFERENCES product_master(id),
    title               VARCHAR(200) NOT NULL,
    message             TEXT NOT NULL,
    metric_value        NUMERIC(18,4),
    threshold_value     NUMERIC(18,4),
    triggered_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged        BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_alert_time ON alerts(triggered_at DESC);

CREATE TABLE IF NOT EXISTS alert_rules (
    id                  SERIAL PRIMARY KEY,
    alert_code          VARCHAR(64) NOT NULL UNIQUE,
    description         TEXT,
    rule_type           VARCHAR(48) NOT NULL,   -- DAY_MOVE / PERCENTILE / FORECAST_MOVE / STALE / QUOTE_EXPIRY / SUPPLIER_SPREAD
    commodity_code      VARCHAR(32),
    threshold           NUMERIC(18,4) NOT NULL,
    direction           VARCHAR(16) DEFAULT 'BOTH',  -- UP / DOWN / BOTH
    severity            VARCHAR(16) NOT NULL DEFAULT 'WARNING',
    is_enabled          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS data_source_log (
    id                  BIGSERIAL PRIMARY KEY,
    source_name         VARCHAR(128) NOT NULL,
    adapter             VARCHAR(128),
    run_started         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_finished        TIMESTAMPTZ,
    status              VARCHAR(32) NOT NULL,   -- SUCCESS / PARTIAL / FAILED / SKIPPED
    rows_fetched        INTEGER DEFAULT 0,
    rows_inserted       INTEGER DEFAULT 0,
    rows_rejected       INTEGER DEFAULT 0,
    duplicates_found    INTEGER DEFAULT 0,
    http_status         INTEGER,
    error_message       TEXT,
    last_success_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_dsl_time ON data_source_log(run_started DESC);

CREATE TABLE IF NOT EXISTS data_quality_flags (
    id                  BIGSERIAL PRIMARY KEY,
    table_name          VARCHAR(64) NOT NULL,
    record_id           BIGINT,
    commodity_id        INTEGER REFERENCES commodity_master(id),
    flag_type           VARCHAR(48) NOT NULL,  -- MISSING_DATE / DUPLICATE / OUTLIER / JUMP / BAD_UNIT / BAD_CURRENCY / NEGATIVE / STALE
    detail              TEXT,
    severity            VARCHAR(16) NOT NULL DEFAULT 'WARNING',
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved            BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS user_settings (
    id                  SERIAL PRIMARY KEY,
    setting_key         VARCHAR(96) NOT NULL UNIQUE,
    setting_value       TEXT,
    value_type          VARCHAR(16) NOT NULL DEFAULT 'string',
    description         TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- TRANSFORMER / BOM / PROCUREMENT  (Section 25)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transformer_master (
    id                  SERIAL PRIMARY KEY,
    job_no              VARCHAR(48) NOT NULL UNIQUE,
    customer            VARCHAR(160),
    transformer_type    VARCHAR(64),          -- POWER / SOLAR / DISTRIBUTION
    rating_mva          NUMERIC(10,3) NOT NULL,
    voltage_ratio       VARCHAR(48),
    hv_kv               NUMERIC(10,3),
    lv_kv               NUMERIC(10,3),
    frequency_hz        NUMERIC(6,2) DEFAULT 50,
    vector_group        VARCHAR(32),
    quantity            INTEGER NOT NULL DEFAULT 1,
    delivery_date       DATE,
    mfg_status          VARCHAR(48),          -- PLANNED / DESIGN / WIP / TESTING / DISPATCHED
    costing_version     VARCHAR(32),
    bom_version         VARCHAR(32),
    base_cost_inr       NUMERIC(18,2),        -- base cost per transformer (ex-commodity revision)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_trf_delivery ON transformer_master(delivery_date);

CREATE TABLE IF NOT EXISTS transformer_bom (
    id                  BIGSERIAL PRIMARY KEY,
    transformer_id      INTEGER NOT NULL REFERENCES transformer_master(id) ON DELETE CASCADE,
    bom_item            VARCHAR(160) NOT NULL,
    material_category   VARCHAR(48) NOT NULL,   -- COPPER/ALUMINIUM/CRGO/STEEL/OIL/INSULATION/TANK/...
    material            VARCHAR(160),
    grade               VARCHAR(64),
    specification       TEXT,
    quantity            NUMERIC(18,4) NOT NULL,
    unit                VARCHAR(16) NOT NULL DEFAULT 'KG',
    unit_weight_kg      NUMERIC(18,4),
    total_weight_mt     NUMERIC(18,6) NOT NULL,   -- normalised consumption in MT per transformer
    commodity_id        INTEGER REFERENCES commodity_master(id),
    provider_id         INTEGER REFERENCES provider_master(id),
    product_id          INTEGER REFERENCES product_master(id),
    price_basis         VARCHAR(64),
    bom_rate_inr_mt     NUMERIC(18,4),          -- rate frozen in the costing
    premium_inr_mt      NUMERIC(18,4) DEFAULT 0,
    currency            VARCHAR(8) DEFAULT 'INR',
    supplier            VARCHAR(160),
    po_number           VARCHAR(64),
    remarks             TEXT
);
CREATE INDEX IF NOT EXISTS ix_bom_trf ON transformer_bom(transformer_id);
CREATE INDEX IF NOT EXISTS ix_bom_cmdty ON transformer_bom(commodity_id);

CREATE TABLE IF NOT EXISTS procurement_coverage (
    id                  BIGSERIAL PRIMARY KEY,
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    transformer_id      INTEGER REFERENCES transformer_master(id),
    as_of_date          DATE NOT NULL DEFAULT CURRENT_DATE,
    stock_mt            NUMERIC(18,4) DEFAULT 0,
    open_po_mt          NUMERIC(18,4) DEFAULT 0,
    confirmed_mt        NUMERIC(18,4) DEFAULT 0,
    avg_po_rate_inr_mt  NUMERIC(18,4),
    supplier_lead_days  INTEGER,
    remarks             TEXT
);

CREATE TABLE IF NOT EXISTS purchase_history (
    id                  BIGSERIAL PRIMARY KEY,
    po_number           VARCHAR(64),
    po_date             DATE NOT NULL,
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    product_id          INTEGER REFERENCES product_master(id),
    supplier_name       VARCHAR(160),
    quantity_mt         NUMERIC(18,4) NOT NULL,
    rate_inr_mt         NUMERIC(18,4) NOT NULL,
    basis               VARCHAR(64),
    transformer_id      INTEGER REFERENCES transformer_master(id),
    data_class          VARCHAR(32) NOT NULL DEFAULT 'VERIFIED_HISTORICAL'
);

CREATE TABLE IF NOT EXISTS recommendations (
    id                  BIGSERIAL PRIMARY KEY,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    commodity_id        INTEGER NOT NULL REFERENCES commodity_master(id),
    signal              VARCHAR(32) NOT NULL,     -- BUY_NOW / BUY_PARTIAL / LOCK_PRICE / WAIT / MONITOR / NEGOTIATE / PHASED
    confidence          VARCHAR(16),
    score               NUMERIC(10,4),
    suggested_cover_pct NUMERIC(6,2),
    rationale           TEXT NOT NULL,
    inputs_json         TEXT
);
CREATE INDEX IF NOT EXISTS ix_reco_time ON recommendations(generated_at DESC);
