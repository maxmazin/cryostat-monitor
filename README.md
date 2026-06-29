# cryostat-monitor

Temperature monitoring & alerting for the lab's five cryostats (dilution refrigerators and ADRs).

Ships each fridge's logged readings to a central PostgreSQL database every ~30 s, acts as a **watchdog** that alerts Slack when a parameter goes out of range **or when a fridge stops reporting**, tolerates network/host outages without losing data, and exposes conversational status/mute via OpenClaw — without putting the LLM agent in the safety-critical alert path.

> **Silence is the primary alarm.** A crashed host or hung logger produces no high reading, so per-fridge staleness detection — not threshold checking — is the most important feature. Read the full spec before changing the design.

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
        ├─ staleness check (per fridge)  ─► Slack (dedicated webhook)
        ├─ threshold checks (per channel)
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

## Status

Design agreed; implementation not yet started. See §10 of the spec for the phased plan and acceptance criteria, and §11 for open questions pending from Ben.
