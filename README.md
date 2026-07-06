# cryostat-monitor

[![CI](https://github.com/maxmazin/cryostat-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/maxmazin/cryostat-monitor/actions/workflows/ci.yml)

Temperature monitoring & alerting for the lab's five cryostats (dilution refrigerators and ADRs).

Ships each fridge's logged readings to a central PostgreSQL database every ~30 s, acts as a **watchdog** that alerts Slack on fridge lifecycle transitions (starts cooling, reaches base temperature, starts warming, reaches room temperature), tolerates network/host outages without losing data, and exposes conversational status/mute via OpenClaw — without putting the LLM agent in the safety-critical alert path.

> **Silence is monitored separately.** A crashed host or hung logger produces no lifecycle transition, so per-fridge staleness detection is still evaluated before lifecycle state. Slack paging is reserved for lifecycle milestones.

## Spec

The authoritative design document is [`cryostat-monitor-handoff.md`](./cryostat-monitor-handoff.md). Read §2 (architecture) and §3 (non-negotiable design principles) before writing any code.

## Architecture (summary)

```
[fridge host ×5, Windows]  custom logger → host daemon (tail/parse → SQLite spool → POST /30s)
        │  (LAN or tailnet; bearer token per host)
        ▼
[labmanager, Ubuntu 24.04]
   ingest service (FastAPI) ──► PostgreSQL (readings, last_seen, maintenance, alert_state)
   watchdog (plain Python)  ◄── PostgreSQL   AUTHORITATIVE ALERT PATH, deterministic, no LLM
        ├─ staleness check (per fridge; suppress stale lifecycle inference)
        ├─ lifecycle transitions ─► Slack (dedicated webhook)
        └─ healthchecks.io heartbeat (dead-man's switch)
   OpenClaw (read-only role + /maintenance) ── NOT in alert path
```

## Planned layout

```
cryostat-monitor/
  host-daemon/      # deployed to each Windows fridge host (daemon, spool, per-fridge parsers)
  server/           # deployed to labmanager (ingest, watchdog, db schema, config, systemd units)
  docs/             # runbook.md and operational docs
  README.md
```

## Running the ingest service (Phase 0)

Phase 0 is complete: the FastAPI ingest service persists readings to PostgreSQL
with idempotent inserts, advances `last_seen`, enforces per-host token auth, and
exposes the capped `/maintenance` endpoint.

### On labmanager (production target)

```bash
# 1. PostgreSQL: create the database and apply the schema
createdb cryo
psql -d cryo -f server/db/schema.sql
# grant the app role write access to the data tables
psql -d cryo -c "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO cryo;"

# 2. Python env
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt

# 3. Configure (copy and edit; .env is gitignored)
cp server/.env.example server/.env     # set CRYO_DB_DSN and CRYO_TOKENS

# 4. Run (systemd unit in server/systemd/cryo-ingest.service)
cd server && set -a && . ./.env && set +a
uvicorn ingest.app:app --host 127.0.0.1 --port 8000
```

Configuration is entirely via environment variables — see `server/.env.example`.

### Local dev (Mac, Homebrew)

`scripts/dev_local.sh` stands up a throwaway Postgres cluster + venv and runs the
service, so you can exercise the full path without touching a real database:

```bash
brew install postgresql@16
./scripts/dev_local.sh up        # init cluster, apply schema, start ingest
./scripts/dev_local.sh verify    # Phase 0 acceptance check (see below)
./scripts/dev_local.sh test      # run the pytest suite against the dev DB
./scripts/dev_local.sh down      # stop everything
```

### Tests

Unit tests (FastAPI `TestClient`, DB layer faked) cover auth, the timezone
contract, non-finite filtering, and the maintenance endpoint. They need no
database:

```bash
cd server
pip install -r requirements.txt -r requirements-dev.txt
pytest                           # unit tests; integration tests auto-skip
```

Integration tests exercise the real idempotent insert and `last_seen` logic
against PostgreSQL; point `CRYO_TEST_DSN` at a schema-applied test database (the
connecting role needs DELETE for row cleanup):

```bash
CRYO_TEST_DSN=postgresql://cryo@127.0.0.1:54329/cryo pytest
```

Host-daemon parser tests (stdlib only, no DB) live in `host-daemon/tests` and
run against the real sample logs in `samples/`:

```bash
cd host-daemon && pytest
```

`./scripts/dev_local.sh test` runs all of this — server (unit + integration)
and host-daemon parsers — against the throwaway dev DB.

### Acceptance check

`scripts/verify_phase0.sh` proves the §10 Phase 0 criterion — a POST of one fake
reading lands a row — and additionally checks idempotency on re-POST:

```
1. health check            -> {"status":"ok"}
2. POST one fake reading    -> {"received":1,"inserted":1}
3. POST it again            -> {"received":1,"inserted":0}   # ON CONFLICT DO NOTHING
4. row present in readings; last_seen advanced
```

## Status

- **Phase 0 — Environment: done.** Schema, roles, FastAPI ingest persisting to
  Postgres, idempotent writes, token auth, capped `/maintenance`. Verified
  end-to-end locally (`verify_phase0.sh`).
- **Phase 1 — One fridge end-to-end: in progress.** BlueFors (blackfridge) parser,
  host daemon (multi-file tailer with byte-offset + file-signature tracking that
  survives midnight rotation and Windows shares where inode is unavailable,
  idempotent SQLite spool, POST/ack/backfill loop) — all built and tested,
  including zero-duplicate backfill after daemon/network outage. Verified
  end-to-end on real sample logs (daemon → ingest → Postgres). Grafana
  dashboard provisioning added (`server/grafana/`). Remaining: confirm the §11
  channel-map/timezone with Ben and deploy the daemon as an NSSM service on the
  host.
- **Phase 2 — Watchdog: built and verified.** Deterministic staleness gating,
  lifecycle transition alerts, persisted alert state, maintenance mutes,
  dedicated Slack webhook, and the healthchecks.io heartbeat. The watchdog path
  is covered end-to-end against a live Postgres (`server/tests/test_watchdog_e2e.py`).
  Slack currently pages only for starts-cooling, reaches-base, starts-warming,
  and reaches-room milestones.
- **Also done:** second BlueFors fridge (whitefridge) onboarded; nightly
  `pg_dump` backups (`scripts/pg_backup.sh` + systemd timer).

What's needed next:
- [`docs/questions-for-ben.md`](./docs/questions-for-ben.md) — open questions,
  prioritized by what unblocks the most (deploy inputs, then the channel wiring).
- [`docs/deployment.md`](./docs/deployment.md) — stand the services up on
  labmanager with TLS and confirm reachability from a fridge host. On a Windows 10
  server host, run the stack under WSL2 (Ubuntu 24.04):
  [`docs/deployment-wsl.md`](./docs/deployment-wsl.md).
- [`docs/deployment-daemon.md`](./docs/deployment-daemon.md) — install the host
  daemon as an NSSM service on each Windows fridge host.

See §10 of the spec for the full phased plan and §11 for open questions pending
from Ben.
