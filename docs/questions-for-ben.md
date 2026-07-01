# Open questions for Ben

Status: the pipeline is **built and verified end-to-end against a live Postgres** —
ingest service, both BlueFors parsers (blackfridge + whitefridge), the host daemon
with crash/outage backfill, the deterministic watchdog (staleness + threshold
alerts, maintenance mutes, Slack, healthchecks heartbeat), and a Grafana
dashboard. What's left is **deploying on labmanager** and **confirming the channel
wiring** — both need input from Ben. Items are ordered by what unblocks the most.

---

## 🔴 Unblocks deployment (need these to go live)

### Q3 — Dedicated Slack webhook
A Slack incoming webhook for alerts that is **its own app/webhook, separate from
OpenClaw's bot**, so alerts fire even if OpenClaw is down (§2.1). Send the webhook
URL; it's kept out of git (loaded from `CRYO_ALERT_SLACK_WEBHOOK`).

### healthchecks.io check (dead-man's switch)
Create a check (free tier is fine), set it to alert if pings stop for **>2 min**,
and send the ping URL. This is what catches labmanager or the watchdog itself
dying (§5/§8).

### Q1 — Topology
Are the fridge hosts on the **same LAN/tailnet** as labmanager, or across the
**public internet**? *(Assumed LAN/tailnet, no public exposure — drives the
reachability/TLS setup in `docs/deployment.md`.)*

### Q2 — labmanager stability + deploy access
Is labmanager on a **UPS**, and can we get access (or an admin's help) to install
the systemd services?

> With just the above, **silence / "fridge stopped reporting" monitoring can go
> live immediately** — the most important alarm, and it needs nothing below.

---

## 🟠 Unblocks threshold alerting (the channel wiring)

### Q5a — Channel → stage mapping (blackfridge & whitefridge)
Please confirm the BlueFors channel convention. We've assumed **CH1 = 50 K,
CH2 = 4 K, CH5 = still, CH6 = MXC**. One cold blackfridge day (26-06-20) is fully
consistent with it (MXC ~9 mK, still ~0.85 K, 4 K ~4.1 K, 50 K stage 20–60 K), but
we shouldn't trust safety thresholds on an inference.

### Q5b — Timezone of each fridge host
Assumed `America/Los_Angeles`. Confirm per host (matters for correct timestamps
and staleness math, §3.6).

### Q5c — Pressures & resistances
The maxigauge logs six gauges, stored as **P1–P6 by position**. Which gauge is
which line (still line? OVC?), and do you want threshold alerts on any of them?
Also: store the resistance (`CH* R`) channels too, or temperatures only?

---

## 🟢 Later / lower priority

### Q5d — Remaining fridges (adr_2, bluefors_3, adr_4, fridge_5)
A handful of representative raw log lines + the filename/rotation pattern for each,
so we can write its parser. `adr_2`'s parser is a stub and it is **commented out of
`fridges.yaml`** until samples land (otherwise the watchdog would SILENT-alarm on a
fridge that never reports). Template per fridge:

| Field | Answer |
|---|---|
| Fridge name (e.g. `adr_2`) | |
| Logger software / model | |
| Example log file path + filename glob | |
| Rotation: new file at midnight? append-forever? size-based? | |
| Timestamp format (paste an example) + timezone | |
| Units per column (K? mK? mbar? Pa?) | |
| Which column → which stage (50K / 4K / still / MXC / GGG / FAA / pressures…) | |
| 5–10 representative raw log lines (verbatim, incl. any header) | |

### Q4 — ADR handling
Fold magnet-state / regen awareness into the watchdog, or is **muting during
regen** sufficient?

### Q6 — Retention
Keep raw 30 s data **indefinitely** (volume is trivial, ~10⁸ rows/yr) or downsample
after some window? (Nightly `pg_dump` backups are already scripted — see
`docs/deployment.md`; this question is only about downsampling the live table.)

### Q7 — On-call / ack
Who receives alerts, and do we need **acknowledgement + escalation**, or is posting
to a channel enough for now?
