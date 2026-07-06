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
2. Is the host daemon (NSSM service) running? Restart it if not — see
   [`deployment-daemon.md`](./deployment-daemon.md) §6 for the healthy-daemon
   checks (`daemon starting:` line, no `POST failed` warnings).
3. Is the custom logger still writing its log file? Check the file's mtime.
4. Is `labmanager` / the ingest service up? (`systemctl status cryo-ingest`)
   If `systemctl` is unreachable, the WSL VM itself is down — see "WSL recovery"
   below.
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
If you can't even reach `systemctl`, the WSL VM is down — see "WSL recovery."

## WSL recovery

The whole server stack (Postgres + ingest + watchdog) lives inside a **WSL2 VM**
on the Windows host ([`deployment-wsl.md`](./deployment-wsl.md)). If `systemctl`
is unreachable, the VM isn't running. From the Windows host:

```powershell
schtasks /run /tn cryostat-wsl-boot     # the boot task; pins the VM up headless
# or, interactively:
wsl -d Ubuntu-24.04                     # boots the distro; systemd starts the services
```

- A mid-day `wsl --shutdown` (or Windows update reboot) tears the **entire stack**
  down; either command above brings it back, and systemd restarts everything
  enabled. Spooled fridge data backfills automatically.
- If the VM is up only while your terminal is open, the boot task is broken —
  a Windows password change silently kills it (result `0x8007052E`); see
  [`scripts/windows/README.md`](../scripts/windows/README.md).
- ⚠️ **Never run `wsl --unregister`** as a troubleshooting step: it destroys the
  entire stack — database *and* any backups stored inside the VM. Backups belong
  on `/mnt/c` (deployment-wsl.md §6) precisely so they survive this.

## Secrets recovery

All tokens live **only** in `/etc/cryostat-monitor/*.env` inside the VM: the five
per-host bearer tokens + maintenance token (`ingest.env`), the Slack webhook and
healthchecks URL (`watchdog.env`), and the backup-check URL (`backup.env`). Keep
a current copy of these files in the **lab password manager** — losing the VM
otherwise forces re-issuing five host tokens and touching every fridge host.

## Monthly drill

Once a month (and after every Windows feature update), prove the alert path
end-to-end — an alerting system that has never fired is an alerting system you
can't trust:

1. **healthchecks.io test notification:** in the healthchecks dashboard, use the
   check's *Send test notification* — confirm it lands in email/Slack.
2. **One synthetic alert** (deterministic — trips the real dead-man's switch):
   ```bash
   sudo systemctl stop cryo-watchdog
   # wait ~3 min (> the check's period + grace) for the healthchecks alert
   sudo systemctl start cryo-watchdog
   ```
   Confirm the "down" alert arrives **and** the check recovers to green after
   restart. Don't skip the restart.
3. **Slack delivery:** the alerts from 1–2 appeared in the alert channel, not
   just email.
4. **Boot task:** `schtasks /query /tn cryostat-wsl-boot /v` shows Last Run
   Result `0x0` — re-verify with a reboot test after Windows feature updates or
   password changes.

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
