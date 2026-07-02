# Runbook — responding to cryostat-monitor alerts

> Draft. Fill in lab-specific contacts and procedures (§4, §10 Phase 4). The
> point of a runbook is that whoever is on-call at 3 a.m. can act without
> paging the PI.

## Alert types

### SILENT state — a fridge stopped reporting

The watchdog has not received data from `<fridge>` for longer than
`staleness_factor × poll_interval`. Slack lifecycle alerts are not sent from
stale data; a crashed host or hung logger produces no reliable lifecycle
transition.

Triage:
1. Is the fridge host powered on and on the network? (ping / tailnet status)
2. Is the host daemon (NSSM service) running? Restart it if not.
3. Is the custom logger still writing its log file? Check the file's mtime.
4. Is `labmanager` / the ingest service up? (`systemctl status cryo-ingest`)
5. If the fridge itself is warming, escalate to <FRIDGE OWNER>.

### Lifecycle Slack alerts

Slack pages only on the four configured lifecycle milestones:

1. Cooling started.
2. Base temperature reached.
3. Warming started.
4. Room temperature reached.

The Slack message includes the fridge, lifecycle channel, value, and data
timestamp.

Triage:
1. Confirm the trend in Grafana.
2. If unexpected, check whether a planned cooldown/warmup/regen is underway.
3. If the transition is unexpected, escalate to <FRIDGE OWNER>.

### Dead-man's switch (healthchecks.io)

If you get a healthchecks.io alert (email + its own Slack), **the watchdog or
labmanager itself is down** — the whole monitor is silent. This is the most
urgent case. Check power/UPS, then `cryo-watchdog` and `cryo-ingest` services.

## Maintenance mutes

Before a planned warmup, sensor swap, or ADR regen, set a time-boxed mute so
lifecycle transitions do not page:

- Via OpenClaw: "mute adr_2 for 6 h, regen cycle".
- Via the API directly:
  ```
  POST /maintenance { "fridge": "adr_2", "minutes": 360, "reason": "regen", "set_by": "<you>" }
  ```
Duration is capped server-side. Mutes expire automatically — you cannot
accidentally silence a fridge forever.

## Contacts

- Fridge owners: <fill in per fridge>
- PI: Ben
- On-call / escalation policy: <fill in — see §11 Q7>
