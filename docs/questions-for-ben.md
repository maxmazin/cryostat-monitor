# Open questions for Ben — resolve before/early in Phase 1

These are the §11 questions from the handoff spec, reordered by what actually
blocks progress. Phase 0 (DB + ingest service + tests + CI) is done; Phase 1 is
"one fridge end-to-end" (parser + host daemon + spool + backfill).

---

## 🔴 Blocks Phase 1 right now

### Q5 — Per-fridge log samples (the critical input)

The parsers are the bulk of the work and can't be written against reality
without real samples. **Please pick the *ugliest* log format to do first** (the
spec says start there) and fill in the template below for it. The other four can
follow once the first is working.

For **each** fridge:

| Field | Answer |
|---|---|
| Fridge name (e.g. `blackfridge`) | |
| Logger software / model | |
| Example log file path | |
| Filename pattern (glob) | |
| Rotation: new file at midnight? append-forever? size-based? | |
| Timestamp format (paste an example) | |
| Timezone of the timestamps | |
| Units per column (K? mK? mbar? Pa?) | |
| Which column → which stage (50K / 4K / still / MXC / GGG / FAA / pressures…) | |
| 5–10 representative raw log lines (paste verbatim, incl. a header row if any) | |

> Edge cases worth a sample if they exist: a partially-written final line, a
> file just after midnight rotation, a line with a missing/blank channel.

### Q1 — Topology

Are the 5 fridge hosts on the **same LAN/tailnet** as labmanager, or genuinely
across the **public internet**? *(We've assumed LAN/tailnet, no public
exposure — this drives the TLS/reachability setup in `docs/deployment.md`.)*

---

## 🟠 Needed soon (Phase 1 deploy → Phase 2)

### Q2 — labmanager stability
Is labmanager on a **UPS** and stable, or a box people actively tinker with?
Determines how hard we lean on the external dead-man's switch (Phase 2).

### Q3 — Dedicated Slack webhook
OK to create a **dedicated incoming webhook**, separate from OpenClaw's bot, for
alerts? *(Strongly recommended — alerts must fire even if OpenClaw is dead.)*
Needed for Phase 2, but the Slack app can be created now.

---

## 🟢 Can decide a bit later

### Q4 — ADR handling
Fold magnet-state / regen-cycle awareness into the watchdog, or is **muting
during regen** sufficient?

### Q6 — Retention
Keep raw 30 s data **indefinitely** (volume is trivial, ~10⁸ rows/yr) or
downsample after some window?

### Q7 — On-call / ack
Who receives alerts, and do we need **acknowledgement + escalation**, or is
posting to a channel enough for now?
