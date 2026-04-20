-- ============================================================
-- EMTA Transit Reliability Tracker — Medallion Schema
-- Bronze → Silver → Gold
-- ============================================================

-- Bronze: raw API snapshots, one row per vehicle per poll
CREATE TABLE IF NOT EXISTS bronze_vehicle_pings (
    id                BIGSERIAL PRIMARY KEY,
    vehicle_id        INTEGER NOT NULL,
    route_id          INTEGER,
    route_name        TEXT,
    trip_id           INTEGER,
    direction         TEXT,
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    heading           INTEGER,
    speed             DOUBLE PRECISION,
    adherence_minutes INTEGER,           -- Avail "Deviation" field as-is (positive=late, negative=early)
    display_status    TEXT,               -- Avail "DisplayStatus" (human-readable: "On Time", "Late")
    op_status         TEXT,               -- Avail "OpStatus" (machine-readable status code)
    is_on_route       BOOLEAN,            -- Derived: route_id IS NOT NULL AND trip_id IS NOT NULL
    destination       TEXT,
    last_stop         TEXT,
    comm_status       TEXT,
    vehicle_name      TEXT,
    last_updated_api  TIMESTAMPTZ,        -- Parsed from .NET /Date()/ format
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_json          JSONB               -- Full original payload for debugging
);

CREATE INDEX IF NOT EXISTS idx_bronze_observed_at ON bronze_vehicle_pings (observed_at);
CREATE INDEX IF NOT EXISTS idx_bronze_route_id    ON bronze_vehicle_pings (route_id);
CREATE INDEX IF NOT EXISTS idx_bronze_vehicle_id  ON bronze_vehicle_pings (vehicle_id);


-- Silver: cleaned, delay-classified, time-enriched
CREATE TABLE IF NOT EXISTS silver_arrivals (
    id                BIGSERIAL PRIMARY KEY,
    vehicle_id        INTEGER NOT NULL,
    route_id          INTEGER,
    route_name        TEXT,
    trip_id           INTEGER,
    direction         TEXT,
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    speed             DOUBLE PRECISION,
    adherence_minutes INTEGER,
    delay_bucket      TEXT NOT NULL,       -- 'early', 'on_time', 'late', 'very_late'
    display_status    TEXT,
    is_on_route       BOOLEAN,
    observed_at       TIMESTAMPTZ NOT NULL,
    hour_of_day       INTEGER NOT NULL,    -- 0-23, America/New_York
    day_of_week       INTEGER NOT NULL,    -- 0=Monday … 6=Sunday (ISO)
    day_name          TEXT NOT NULL         -- 'Monday', 'Tuesday', etc.
);

CREATE INDEX IF NOT EXISTS idx_silver_observed_at ON silver_arrivals (observed_at);
CREATE INDEX IF NOT EXISTS idx_silver_route_id    ON silver_arrivals (route_id);
CREATE INDEX IF NOT EXISTS idx_silver_bucket      ON silver_arrivals (delay_bucket);


-- Gold: aggregated reliability per route / hour / day-of-week
CREATE TABLE IF NOT EXISTS gold_route_reliability (
    id                    BIGSERIAL PRIMARY KEY,
    route_id              INTEGER NOT NULL,
    route_name            TEXT,
    hour_of_day           INTEGER NOT NULL,
    day_of_week           INTEGER NOT NULL,
    day_name              TEXT NOT NULL,
    total_pings           INTEGER NOT NULL,
    on_time_count         INTEGER NOT NULL,
    early_count           INTEGER NOT NULL,
    late_count            INTEGER NOT NULL,
    very_late_count       INTEGER NOT NULL,
    on_time_pct           NUMERIC(5,2),
    avg_adherence_minutes NUMERIC(10,2),
    reliability_score     NUMERIC(5,2),     -- Weighted composite 0-100
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (route_id, hour_of_day, day_of_week)
);


-- AI-generated weekly insights (one row per week)
CREATE TABLE IF NOT EXISTS ai_weekly_insights (
    id              BIGSERIAL PRIMARY KEY,
    week_start      DATE NOT NULL UNIQUE,   -- Monday of the analysis week
    narrative       TEXT NOT NULL,           -- Multi-paragraph analysis from Claude
    tweet_draft     TEXT,                    -- ≤280 char social post
    headline_text   TEXT,                    -- ≤100 char headline / email subject
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
