# Cryostat Temperature Monitoring & Alerting — Implementation Handoff

**Audience:** implementing undergrad
**Owner / PI:** Ben
**Status:** design agreed; per-fridge log samples and a few decisions (see §11) still pending from Ben.

This document is the spec. Read §2 and §3 before writing any code — the safety-critical design choices there are deliberate and must not be "simplified away." If something here seems like unnecessary complexity, ask before removing it; most of it exists because the dangerous failure mode for a cryostat monitor is *silence*, not a high reading.

---

## 1. Goal

Five cryostats (a mix of dilution refrigerators and ADRs), each with its own custom temperature-logging software writing to disk on a Windows host, mostly ~4 stage sensors each (e.g. 50 K, 4 K, still, MXC for the dil fridges; ADRs differ) plus possibly gas-handling-system pressures and other channels.

Build a system that:
1. Ships each fridge's logged readings to a central database every ~30 s.
2. Acts as a **watchdog**: alerts to Slack when a parameter goes out of range **or when a fridge stops reporting**.
3. Tolerates network/host outages without losing data.
4. Lets people query status and mute alerts conversationally via the existing OpenClaw agent — **without** putting that agent in the safety-critical alert path.

Central server is **`labmanager`** (Ubuntu 24.04), which already runs OpenClaw wired to Slack. The Synology NAS is demoted to backup target.

Data volume is tiny (≈5 fridges × ≤8 channels every 30 s ≈ a few rows/sec, ~10⁸ rows/year). Do **not** use InfluxDB or any time-series DB; plain PostgreSQL is the right tool.

---

## 2. Architecture

```
[fridge host ×5, Windows]
   custom logger writes disk log
        │
   host daemon (Python, NSSM service)
     tail log → parse → local SQLite spool → POST batch /30s
        │  (LAN or tailnet; bearer token per host)
        ▼
[labmanager, Ubuntu 24.04]
   ingest service (FastAPI + uvicorn, systemd)
        ├─► PostgreSQL  ── readings, last_seen, maintenance, alert_state
        │
   watchdog (plain Python, systemd)              ◄── reads PostgreSQL
        ├─ staleness check (per fridge)           AUTHORITATIVE ALERT PATH
        ├─ threshold checks (per channel)         deterministic, no LLM
        ├─ maintenance mute (duration-capped)
        ├─► Slack  (dedicated incoming webhook, separate from OpenClaw)
        └─► healthchecks.io heartbeat  ── catches labmanager/watchdog death
   Grafana (optional) ── dashboards + redundant "No Data" alerts
   OpenClaw (already running) ── NOT in alert path:
        • answers "what's MXC on fridge 3?" via read-only DB role
        • posts daily "all 5 alive + base temps" summary
        • sets time-boxed maintenance mutes via the ingest /maintenance endpoint
```

### 2.1 Why the watchdog is separate from OpenClaw (do not merge these)

OpenClaw is a non-deterministic LLM agent. It is fine for conversation and triage. It must **not** be the thing that decides whether to alert, because:

- Non-determinism: the same data can yield different judgments run-to-run.
- The watchdog's #1 job is detecting *missing data*. An LLM agent depends on the gateway process, the model API, skills not throwing, and cron firing — every one of those is a new way to go silent exactly when you need an alert.
- It's an attack surface: OpenClaw also reads free-form Slack text, which is a prompt-injection path. Keep it away from safety logic.

**Rules:**
- The alert Slack webhook is its **own** Slack app/incoming-webhook, with its own token, independent of OpenClaw's bot. Alerts must fire even if OpenClaw is completely dead.
- OpenClaw gets a **read-only** Postgres role for everything except maintenance mutes, which it sets through the constrained `/maintenance` endpoint (which caps duration). It cannot disable the watchdog.

---

## 3. Non-negotiable design principles

1. **Silence is the primary alarm.** A crashed host or hung logger produces *no* high reading, so a threshold-only watchdog stays quiet while the fridge warms. Per-fridge staleness detection is the most important feature, not an afterthought.
2. **Writes are idempotent.** After an outage, hosts re-send buffered data. The DB primary key `(fridge, channel, ts)` plus `ON CONFLICT DO NOTHING` makes re-sends harmless. Never rely on the host to "know" what was already accepted.
3. **Thresholds and intervals live in config, not code**, so they can be tuned without redeploying.
4. **Maintenance muting exists from day one.** Fridges are warmed deliberately (maintenance, sensor swaps; ADR regen ramps the magnet and warms stages by design). Without a per-fridge mute, every planned cycle pages the channel and people learn to ignore alerts.
5. **Monitor the monitor.** If `labmanager` or the watchdog dies, the whole thing goes silent. An external dead-man's switch (healthchecks.io) and a UPS on `labmanager` are part of the system, not optional polish.
6. **UTC everywhere internally.** Parse each fridge's local timestamp, convert to UTC on ingest. Display in local time only in Grafana/Slack.

---

## 4. Repository layout

```
cryostat-monitor/
  host-daemon/                 # deployed to each Windows fridge host
    daemon.py
    spool.py                   # local SQLite buffer
    parsers/
      base.py                  # Parser interface (§6.1)
      bluefors_1.py            # one module per fridge, bespoke
      adr_2.py
      ...
    config.example.toml
    requirements.txt
  server/                      # deployed to labmanager
    ingest/app.py              # FastAPI
    watchdog/watchdog.py
    db/schema.sql
    config/fridges.yaml        # thresholds, intervals, channel maps
    systemd/
      cryo-ingest.service
      cryo-watchdog.service
    requirements.txt
  docs/
    runbook.md                 # how to respond to each alert type (write this)
  README.md
```

---

## 5. Database (PostgreSQL on labmanager)

Narrow schema — one row per channel-reading. This absorbs the fact that the five fridges are heterogeneous (ADRs have no still/MXC); a new sensor is just a new `channel` value, never a migration.

```sql
CREATE TABLE readings (
    ts       timestamptz       NOT NULL,
    fridge   text              NOT NULL,
    channel  text              NOT NULL,
    value    double precision  NOT NULL,
    unit     text              NOT NULL,
    PRIMARY KEY (fridge, channel, ts)
);
CREATE INDEX idx_readings_fridge_ts ON readings (fridge, ts DESC);

-- fast "latest value per fridge" without scanning history
CREATE TABLE last_seen (
    fridge    text PRIMARY KEY,
    last_ts   timestamptz NOT NULL  -- max data timestamp received
);

-- active maintenance windows; watchdog suppresses alerts while now() < until_ts
CREATE TABLE maintenance (
    fridge    text NOT NULL,
    until_ts  timestamptz NOT NULL,
    reason    text,
    set_by    text,
    created   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_maint_fridge ON maintenance (fridge, until_ts);

-- persisted alert state so a watchdog restart doesn't re-spam or forget
CREATE TABLE alert_state (
    fridge        text NOT NULL,
    alert_key     text NOT NULL,   -- 'SILENT' or channel name
    state         text NOT NULL,   -- 'OK' | 'ALERTING'
    since         timestamptz NOT NULL,
    last_notified timestamptz,
    PRIMARY KEY (fridge, alert_key)
);
```

Units convention (canonicalize in the parser): **temperatures in kelvin, pressures in mbar.** The `unit` column records what was stored for sanity-checking; thresholds in config assume these canonical units.

Create a read-only role for OpenClaw:
```sql
CREATE ROLE openclaw_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE cryo TO openclaw_ro;
GRANT USAGE ON SCHEMA public TO openclaw_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO openclaw_ro;
```

---

## 6. Host daemon (Windows)

One instance per fridge. Python 3.11+. Run as a service via **NSSM** wrapping `python daemon.py --config config.toml`; NSSM gives auto-restart (so the daemon also has a watchdog).

Per-host `config.toml`:
```toml
fridge        = "bluefors_1"
parser        = "bluefors_1"
log_glob      = "C:/BlueFors/logs/*/CH*.log"   # confirm per fridge
poll_interval = 30
server_url    = "https://labmanager.<tailnet>.ts.net/ingest"
token         = "<per-host bearer token>"
timezone      = "America/Los_Angeles"          # confirm per fridge
```

### 6.1 Parser interface

Each fridge gets its own module implementing this. Parsers are the bulk of the work because every logger's format/rotation differs.

```python
# parsers/base.py
from dataclasses import dataclass
from datetime import datetime

@dataclass
class Reading:
    ts: datetime      # tz-aware, converted to UTC by the daemon
    channel: str      # canonical name, e.g. "MXC", "4K", "still", "P_still"
    value: float
    unit: str         # "K" or "mbar"

class Parser:
    def parse_new(self, raw_lines: list[str]) -> list[Reading]:
        """Convert newly-read log lines into Readings.
        MUST skip/log malformed lines, never raise on bad input."""
        raise NotImplementedError
```

### 6.2 Daemon loop (behaviour spec)

Every `poll_interval` seconds:
1. Read **new** bytes from the active log file(s). Track byte offset **and inode/file-id**; if it changes (midnight rotation → new dated file), reset offset and pick up the new file. Handle a partial final line (don't parse half a line).
2. `parser.parse_new(...)` → `Reading`s. Convert local ts → UTC.
3. Append readings to the **local SQLite spool** (`spool.py`), marked un-acked.
4. POST **all un-acked** readings as one batch to the server (§7). On HTTP 2xx, mark them acked. On failure, leave them; they go out next cycle.
5. Periodically prune acked rows older than N days from the spool.

Hard requirements: a malformed line logs a warning and is skipped (no crash); a network outage causes the spool to grow and backfill on recovery; duplicate sends are safe (server dedups).

---

## 7. Ingest service (FastAPI on labmanager)

Single endpoint plus a constrained maintenance endpoint.

**Data contract:**
```http
POST /ingest
Authorization: Bearer <per-host token>
Content-Type: application/json

{
  "fridge": "bluefors_1",
  "readings": [
    {"ts": "2026-06-29T19:00:00Z", "channel": "MXC",   "value": 0.0102, "unit": "K"},
    {"ts": "2026-06-29T19:00:00Z", "channel": "4K",    "value": 3.91,   "unit": "K"},
    {"ts": "2026-06-29T19:00:00Z", "channel": "P_still","value": 0.42,  "unit": "mbar"}
  ]
}
```

Server logic:
1. Resolve token → fridge; reject if `body.fridge` ≠ token's fridge (a host can only write its own data).
2. Bulk insert into `readings` with `ON CONFLICT (fridge, channel, ts) DO NOTHING`.
3. `UPDATE last_seen` with `max(ts)` from the batch.
4. Return 200 with count inserted.

**Maintenance endpoint** (used by OpenClaw and humans):
```http
POST /maintenance
{ "fridge": "adr_2", "minutes": 360, "reason": "regen cycle", "set_by": "ben" }
```
Server **caps `minutes`** at a configured max (e.g. 720) and inserts a `maintenance` row. This is the only write OpenClaw is allowed.

Run behind systemd; bind to the LAN/tailnet interface, not the public internet (§11 Q1).

---

## 8. Watchdog (the safety core, plain Python, systemd)

Deterministic. No LLM. Loop every `CHECK_INTERVAL` (≈15 s):

```
for fridge in config.fridges:
    muted = exists active maintenance row for fridge (now < until_ts)

    # --- staleness (primary) ---
    age = now() - last_seen[fridge]
    if age > fridge.staleness_factor * fridge.poll_interval:
        transition(fridge, "SILENT", bad=True, muted)
        continue                      # if it's silent, skip threshold checks
    else:
        transition(fridge, "SILENT", bad=False, muted)

    # --- thresholds (secondary) ---
    for channel, limits in fridge.channels:
        v = latest_value(fridge, channel)          # most recent reading
        breach = (limits.high is set and v > limits.high) or
                 (limits.low  is set and v < limits.low)
        transition(fridge, channel, bad=breach, muted)

heartbeat()   # ping healthchecks.io EVERY loop, unconditionally
```

`transition(fridge, key, bad, muted)` implements a state machine over `alert_state`:
- OK → bad & not muted: set ALERTING, send Slack "🔴 raised", record `last_notified`.
- ALERTING & still bad: if `now - last_notified > reminder_interval`, send a reminder.
- ALERTING → not bad: set OK, send Slack "🟢 cleared".
- muted: do not send; record state but suppress notifications.

Persisting `alert_state` in the DB is what prevents a watchdog restart from (a) re-spamming an already-known alert or (b) forgetting an active one.

**Slack message** (dedicated webhook) must include: fridge, alert type (SILENT vs THRESHOLD), channel, value, the limit it crossed, and the data timestamp. Make SILENT visually distinct — it's the scary one.

**Dead-man's switch:** the `heartbeat()` call pings a healthchecks.io check URL every loop. Configure healthchecks to alert (email + its own Slack) if pings stop for >2 min. That is what catches `labmanager` or the watchdog itself dying.

---

## 9. OpenClaw integration (conversational layer only)

Out of scope for the core build; wire it up in Phase 4. Give OpenClaw:
- The `openclaw_ro` read-only DB role so it can answer status questions ("MXC on fridge 3?", "plot last 6 h of still pressure").
- Permission to call `/maintenance` for time-boxed mutes ("mute adr_2 for 6 h, regen").
- A daily scheduled task: post "all 5 fridges reporting, base temps: …" to Slack.

It does **not** read `alert_state` to decide alerts and is never on the notification path.

---

## 10. Phased plan & acceptance criteria

Rough effort assumes a competent undergrad new to production services.

**Phase 0 — Environment (≈2–3 days)**
Install PostgreSQL on labmanager; apply `schema.sql`; create roles. Stand up the repo, `fridges.yaml`, FastAPI skeleton returning 200. Confirm tailnet/LAN reachability from one fridge host.
*Done when:* a `curl` POST of one fake reading from a fridge host lands a row in `readings`.

**Phase 1 — One fridge end-to-end (≈1–2 weeks)**
Pick the **ugliest** log format first. Implement its parser, the daemon with SQLite spool and rotation handling, and full ingest. Add one Grafana panel.
*Done when:* live data flows continuously; **and** (a) stopping the daemon for 10 min then restarting backfills the gap with **zero duplicate rows**; (b) blocking the network for 10 min does the same.

**Phase 2 — Watchdog (≈1–2 weeks)**
Staleness + thresholds + maintenance mute + Slack webhook + healthchecks heartbeat + persisted alert state.
*Done when ALL pass:*
- Kill the daemon → a SILENT alert reaches Slack within `staleness_factor × interval + CHECK_INTERVAL`.
- Force an out-of-band value → a THRESHOLD alert fires; restoring it sends a clear.
- Set a maintenance mute → both alert types are suppressed for that fridge until expiry.
- Restart the watchdog mid-alert → it does **not** re-spam, and still clears correctly on recovery.
- Stop the watchdog entirely → healthchecks.io independently alerts.

**Phase 3 — Roll out remaining 4 (≈1–2 weeks)**
A parser per fridge, daemon deployed as an NSSM service per host, config per host. Watch for ADR-specific channels.

**Phase 4 — Harden (≈1 week)**
`pg_dump` to the NAS via the existing Restic backup; decide raw-data retention; alert reminder/escalation tuning; OpenClaw read-only role + `/maintenance` skill + daily summary; write `docs/runbook.md` (what each alert means and who does what).

---

## 11. Open questions for Ben (resolve before/early in Phase 0)

1. **Topology:** are the 5 fridge hosts on the same LAN/tailnet as labmanager, or genuinely across the public internet? *(Default assumed: LAN/tailnet, no public exposure.)*
2. **labmanager suitability:** is it on a UPS and stable, or a box people actively tinker with? Determines how hard we lean on the external dead-man's switch.
3. **Dedicated Slack webhook** separate from OpenClaw's bot — confirm OK. *(Strongly recommended.)*
4. **ADR handling:** fold magnet state / regen-cycle awareness into the watchdog, or is muting during regen sufficient?
5. **Per-fridge log samples** — the critical input. For each fridge provide: a handful of representative log lines, the filename pattern, how files rotate (new file at midnight? append-forever?), which columns map to which stage, the timestamp format, the timezone, and units.
6. **Retention:** keep raw 30 s data indefinitely (volume is trivial) or downsample after some window?
7. **On-call / ack:** who receives alerts, and do we need acknowledgement + escalation, or is posting to a channel enough for now?

---

## 12. Things that will bite you (read before "simplifying")

- Removing silence detection because "thresholds are enough" → the fridge warms unnoticed. This is the whole point.
- Skipping idempotent writes → the first outage backfill creates duplicate rows and breaks averages.
- Letting OpenClaw decide alerts → non-deterministic, injectable safety path.
- Not persisting `alert_state` → every watchdog restart re-pages everyone.
- Forgetting maintenance muting → planned warmups page the channel and alerts get ignored.
- No dead-man's switch / no UPS → labmanager dies and the monitor is silent, which looks identical to "everything fine."
- Mixing timezones → off-by-hours staleness math, especially around DST.
- Crashing the daemon on one malformed log line → you lose a whole fridge over a stray byte.
