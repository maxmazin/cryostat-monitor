-- cryostat-monitor database schema (PostgreSQL on labmanager)
-- Apply with:  psql -d cryo -f schema.sql
--
-- Narrow schema: one row per channel-reading. Absorbs the fact that the five
-- fridges are heterogeneous (ADRs have no still/MXC). A new sensor is just a
-- new `channel` value, never a migration.
--
-- Units convention (canonicalized in the parser): temperatures in kelvin,
-- pressures in mbar. The `unit` column records what was stored for sanity
-- checking; thresholds in config assume these canonical units.
-- UTC everywhere internally (see §3.6 of the spec).

CREATE TABLE IF NOT EXISTS readings (
    ts       timestamptz       NOT NULL,
    fridge   text              NOT NULL,
    channel  text              NOT NULL,
    value    double precision  NOT NULL,
    unit     text              NOT NULL,
    PRIMARY KEY (fridge, channel, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_fridge_ts ON readings (fridge, ts DESC);

-- Fast "latest value per fridge" without scanning history.
CREATE TABLE IF NOT EXISTS last_seen (
    fridge    text PRIMARY KEY,
    last_ts   timestamptz NOT NULL  -- max data timestamp received
);

-- Active maintenance windows; watchdog suppresses alerts while now() < until_ts.
CREATE TABLE IF NOT EXISTS maintenance (
    fridge    text NOT NULL,
    until_ts  timestamptz NOT NULL,
    reason    text,
    set_by    text,
    created   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_maint_fridge ON maintenance (fridge, until_ts);

-- Persisted alert state so a watchdog restart doesn't re-spam or forget.
CREATE TABLE IF NOT EXISTS alert_state (
    fridge        text NOT NULL,
    alert_key     text NOT NULL,   -- 'SILENT' or channel name
    state         text NOT NULL,   -- 'OK' | 'ALERTING'
    since         timestamptz NOT NULL,
    last_notified timestamptz,
    PRIMARY KEY (fridge, alert_key)
);

-- ---------------------------------------------------------------------------
-- Read-only role for OpenClaw. It answers status questions and nothing else;
-- maintenance mutes go through the constrained /maintenance ingest endpoint,
-- never direct writes. Set a real password before running in production.
-- ---------------------------------------------------------------------------
-- CREATE ROLE openclaw_ro LOGIN PASSWORD 'CHANGE_ME';
-- GRANT CONNECT ON DATABASE cryo TO openclaw_ro;
-- GRANT USAGE ON SCHEMA public TO openclaw_ro;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO openclaw_ro;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO openclaw_ro;
