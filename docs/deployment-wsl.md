# Running the server on Windows 10 via WSL2 (Ubuntu 24.04)

The server host is a Windows 10 machine, but the ingest service, watchdog, and
PostgreSQL run inside **WSL2 running Ubuntu 24.04**. This gives us the real Linux
toolchain the design was written for — peer-auth Postgres, the committed systemd
units, and `scripts/pg_backup.sh` all work unchanged.

**No application code changes are needed.** The server is already portable; only
the deployment wrapper is WSL-specific. This document is that wrapper — install
WSL, then run the standard Ubuntu deployment *inside* it, then handle the three
things WSL changes: **systemd**, **networking into the VM**, and **auto-start at
Windows boot**.

The fridge-host **daemon stays Windows-native** (it tails `C:\BlueFors\logs`); see
its own setup, not this doc.

---

## 1. Install WSL2 + Ubuntu 24.04

In an **elevated PowerShell** on the Windows host:

```powershell
wsl --update                      # get a WSL build new enough for systemd (>= 0.67.6)
wsl --install -d Ubuntu-24.04     # installs the distro; prompts for a UNIX username/password
wsl --version                     # confirm WSL version 2
```

Note the UNIX username you create — the boot task in §5 runs as your Windows
account but the distro is registered under it.

## 2. Enable systemd inside WSL

Open the Ubuntu shell (`wsl -d Ubuntu-24.04`) and set:

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

Then from PowerShell restart the distro and verify systemd is PID 1:

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04 -- systemctl is-system-running   # "running" or "degraded" is fine
```

## 3. Install the server stack (standard Ubuntu steps, run inside WSL)

Everything in **[`deployment.md`](./deployment.md) §1–§4** now applies verbatim —
run it in the Ubuntu shell:

- **§1 PostgreSQL** — `sudo apt install -y postgresql` (Ubuntu 24.04 ships PG 16),
  `createdb cryo`, apply `server/db/schema.sql`, grant the `cryo` app role. Peer
  auth over the unix socket works, so the DSN is
  `postgresql:///cryo?host=/var/run/postgresql` — no password needed.
- **§2 Code + venv** at `/opt/cryostat-monitor` (clone the repo *inside* WSL, not
  under `/mnt/c` — running the venv off the Windows filesystem is slow).
- **§3 Config + tokens** — `/etc/cryostat-monitor/ingest.env`. Also create
  `/etc/cryostat-monitor/watchdog.env` from `server/watchdog.env.example` with the
  `CRYO_ALERT_SLACK_WEBHOOK` and `CRYO_HEALTHCHECKS_URL` values.
- **§4 systemd** — install `cryo-ingest.service`, `cryo-watchdog.service`, and the
  backup timer; `systemctl enable --now` each. `enable` is what makes them start
  when the distro boots (§5).

### One WSL deviation: bind ingest to all interfaces

WSL2's localhost forwarding reaches services on the VM's `eth0`, not its loopback,
so the ingest service must listen on `0.0.0.0` instead of `127.0.0.1`. This is safe:
WSL2 is NAT-isolated from the LAN, so `0.0.0.0` here is reachable **only** from the
Windows host, not the network. Override with a drop-in (leaves the committed unit,
which is correct for a bare-metal box, untouched):

```bash
sudo mkdir -p /etc/systemd/system/cryo-ingest.service.d
sudo tee /etc/systemd/system/cryo-ingest.service.d/wsl-bind.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/opt/cryostat-monitor/server/.venv/bin/uvicorn ingest.app:app --host 0.0.0.0 --port 8000
EOF
sudo systemctl daemon-reload && sudo systemctl restart cryo-ingest
```

Verify from **Windows** (PowerShell) that localhost forwarding works:

```powershell
Invoke-RestMethod http://localhost:8000/health     # -> {"status":"ok"}
```

## 4. Get the fridge host's POSTs into WSL

The path a reading takes:

```
fridge host --(tailnet)--> Windows host --(tailscale serve)--> Windows localhost:8000
            --(WSL2 localhost forwarding)--> uvicorn 0.0.0.0:8000 inside WSL
```

Run **Tailscale on the Windows host** (not inside WSL — the Windows client is far
less finicky), then expose the forwarded port on the tailnet with HTTPS:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status     # https://<host>.<tailnet>.ts.net -> 127.0.0.1:8000
```

The fridge daemon then posts to `https://<host>.<tailnet>.ts.net/ingest`. Tailscale
provisions a valid cert and the tailnet is WireGuard-encrypted end to end.

In the Tailscale admin console, **disable key expiry** for the server node and all
five fridge-host nodes — otherwise the data path silently breaks on a ~180-day
schedule when a node key expires.

**LAN-only alternative (no tailnet):** WSL2 on Windows 10 is NAT'd (mirrored
networking is Windows 11 only), so you must forward a Windows port into the VM with
`netsh interface portproxy`. The catch is the WSL IP changes on each boot, so it has
to be re-applied at startup with a script (`wsl hostname -I` → `netsh ... set`). This
is fragile; prefer the tailnet path.

## 5. Auto-start at Windows boot, with no one logged in

A monitoring server must run 24/7 and survive reboots. WSL does **not** start on its
own — it boots only when something invokes it. Create a Task Scheduler task that
boots the distro at startup and keeps the VM alive; systemd then starts the enabled
services.

**Preferred: import the committed task definition** —
[`scripts/windows/cryostat-wsl-boot-task.xml`](../scripts/windows/cryostat-wsl-boot-task.xml)
(import command, verification, and failure modes in
[`scripts/windows/README.md`](../scripts/windows/README.md)). The manual
equivalent, for reference or if you must hand-create it:

Task Scheduler → Create Task (name it `cryostat-wsl-boot`):
- **General:** "Run whether user is logged on or not"; check "Run with highest
  privileges"; set the user to the account that installed Ubuntu (the distro is
  registered under that profile — the `SYSTEM` account can't see it).
- **Triggers:** "At startup."
- **Actions:** Start a program —
  - Program: `C:\Windows\System32\wsl.exe`
  - Arguments: `-d Ubuntu-24.04 -u root -- sleep infinity`

The long-lived `sleep infinity` process pins the WSL2 VM up (it otherwise tears down
when its last process exits), and because systemd is enabled the distro boots it,
which auto-starts `cryo-ingest`, `cryo-watchdog`, and the backup timer.

Tailscale on Windows already runs as a service and reconnects on boot; `tailscale
serve --bg` config persists, so §4 survives reboots too.

**Reboot test:** restart Windows, wait a minute, then from PowerShell run
`Invoke-RestMethod http://localhost:8000/health` and, on the tailnet, hit the
`.ts.net/health` URL from the fridge host. Both should answer with nobody logged in.

**Re-verify the task after any Windows password change or feature update.** A
password change invalidates the task's stored credentials (it silently fails with
result code `0x8007052E` — re-save it); feature updates have been known to drop
scheduled tasks. Check with `schtasks /query /tn cryostat-wsl-boot /v` and a
reboot test (see `scripts/windows/README.md`).

## 6. Backups

`scripts/pg_backup.sh` + the `cryo-backup.timer` run inside WSL exactly as in
`deployment.md` ("Database backups"), with one WSL-specific rule: **put the dumps
on the Windows filesystem, outside the VM.** The whole distro — database included —
lives in one `ext4.vhdx` file, so a `wsl --unregister` (a common WSL troubleshooting
step) or vhdx corruption destroys the database *and* any backups stored inside the
VM in one stroke. Point `CRYO_BACKUP_DIR` at a Windows-mounted path:

```bash
# in /etc/cryostat-monitor/backup.env (loaded by cryo-backup.service)
CRYO_BACKUP_DIR=/mnt/c/cryostat-monitor/backups
```

This also lets a Windows-side offsite job (e.g. Restic → NAS) pick the dumps up
directly. Keeping dumps inside the WSL filesystem is discouraged — only defensible
if something *outside* the VM copies them off nightly, and even then prefer `/mnt/c`.

Create a **second healthchecks.io check** for the backup job and set its URL as
`BACKUP_PING_URL` in the same `/etc/cryostat-monitor/backup.env` — the script pings
it on success (`/fail` on failure), so a silently broken nightly backup pages
instead of being discovered at restore time.

## Gotchas

- **WSL app too old for systemd** — `wsl --update`; needs ≥ 0.67.6. If
  `systemctl` says "System has not been booted with systemd," re-check `/etc/wsl.conf`
  and `wsl --shutdown`.
- **`127.0.0.1` vs `0.0.0.0`** — if `localhost:8000` works *inside* WSL but not from
  Windows, the service is bound to loopback; apply the §3 drop-in.
- **Don't run the app off `/mnt/c`** — cross-filesystem I/O is slow; install under the
  WSL-native filesystem (`/opt`).
- **Windows Fast Startup / auto-updates** — a Windows update reboot restarts the box;
  the §5 task recovers it. Confirm the machine's power settings don't sleep it.
- **Clock** — the watchdog measures staleness against the server (WSL) clock; WSL
  normally syncs time with the Windows host, but if the machine sleeps for long
  periods verify the WSL clock is correct after wake (`date -u`).
