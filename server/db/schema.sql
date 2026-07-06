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
--   last_ts     = max DATA timestamp (host clock, converted to UTC) — shown to humans.
--   received_at = server clock when NEW data last arrived (all-duplicate replays
--                 don't count) — the staleness basis, so a skewed fridge-host
--                 clock cannot mask real silence (§3.1, §12).
CREATE TABLE IF NOT EXISTS last_seen (
    fridge      text PRIMARY KEY,
    last_ts     timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
-- Migration for databases created before received_at existed. Add it NULLABLE
-- first and backfill from last_ts — NOT now() — so a fridge that is CURRENTLY
-- silent when the column is added does not get a fresh received_at that would
-- make the watchdog treat it as just-seen and suppress the SILENT alarm for a
-- full staleness window (§3.1). last_ts is the best available proxy for when we
-- last heard from it, and it errs toward firing rather than masking silence.
-- Then adopt the default + NOT NULL to match the fresh-CREATE definition above.
-- All steps are idempotent: on a DB that already has the column they are no-ops.
ALTER TABLE last_seen ADD COLUMN IF NOT EXISTS received_at timestamptz;
UPDATE last_seen SET received_at = last_ts WHERE received_at IS NULL;
ALTER TABLE last_seen ALTER COLUMN received_at SET DEFAULT now();
ALTER TABLE last_seen ALTER COLUMN received_at SET NOT NULL;

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
