# Deploying the host daemon on a Windows fridge host (NSSM service)

Each of the five fridge hosts runs one native-Windows instance of
`host-daemon/daemon.py`, tailing the local logger's files and POSTing to the
server over the tailnet. NSSM wraps it as a Windows service: start at boot,
restart on crash, log-file redirection with rotation. The daemon is designed
for this — it keeps its own state (byte offsets + SQLite spool), so being
killed and restarted at any point loses nothing.

Do these steps **per host**, in order. Track them in the checklist table at the
bottom so configuration drift stays visible.

## 1. Install a real Python

Install Python 3.12+ from [python.org](https://www.python.org/downloads/windows/)
(64-bit, "Install for all users" is fine).

> **Microsoft Store alias trap:** a fresh Windows box has 0-byte
> `python.exe` / `python3.exe` stubs in `%LOCALAPPDATA%\Microsoft\WindowsApps`
> that open the Store instead of running Python — and NSSM pointed at one of
> them silently does nothing. Disable them (Settings → Apps → Advanced app
> settings → **App execution aliases** → turn off both `python` entries) and
> always use the real interpreter's full path. `where.exe python` shows which
> one wins; `python --version` must print a version, not open the Store.

## 2. Code + venv

```powershell
git clone https://github.com/maxmazin/cryostat-monitor C:\cryostat-monitor
cd C:\cryostat-monitor\host-daemon
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

(No git on the host? Copy the `host-daemon\` tree over; record the commit you
copied in the checklist.)

## 3. config.toml

```powershell
copy config.example.toml config.toml
```

Fill in per the comments in `config.example.toml`: `fridge`, `parser`,
`log_globs` (this host's logger paths), `poll_interval`, `server_url`
(`https://<host>.<tailnet>.ts.net/ingest`), and this host's `token` (issued
server-side, `deployment.md` §3). `config.toml` is gitignored — it holds the
token.

> **Windows path gotcha:** in TOML, backslashes in double-quoted strings are
> escape sequences — `"C:\BlueFors\logs"` is invalid (`\B`, `\l`). Use forward
> slashes (`"C:/BlueFors/logs/*/CH* T *.log"`, as the example does — Windows
> accepts them) or single-quoted literal strings (`'C:\BlueFors\logs\...'`).

Leave `backfill = false` unless you really want the logger's multi-year history
shipped (see the example file's comments).

## 4. Dry run first

```powershell
.venv\Scripts\python daemon.py --config config.toml --dry-run
```

This parses the current logs and prints what it *would* post — per-channel
counts, value ranges, latest timestamp — without spooling, committing offsets,
or contacting the server. Sanity-check that the channels, units, and timestamps
look right **before** going live. Zero files matched or zero readings means the
`log_globs` or parser is wrong; fix that now, not after the service is running.

## 5. Install as an NSSM service

Get NSSM (nssm.cc, or `winget install nssm`), then in an **elevated** shell:

```powershell
nssm install cryo-daemon C:\cryostat-monitor\host-daemon\.venv\Scripts\python.exe daemon.py --config config.toml
nssm set cryo-daemon AppDirectory C:\cryostat-monitor\host-daemon

# Log redirection WITH rotation — without it the daemon's stderr grows
# unbounded and fills the disk over months.
nssm set cryo-daemon AppStdout C:\cryostat-monitor\host-daemon\daemon.log
nssm set cryo-daemon AppStderr C:\cryostat-monitor\host-daemon\daemon.log
nssm set cryo-daemon AppRotateFiles 1
nssm set cryo-daemon AppRotateOnline 1
nssm set cryo-daemon AppRotateBytes 10485760          # rotate at 10 MB

# Restart on crash: the daemon is the fridge's only reporter, so a dead daemon
# is a SILENT alert waiting to happen. 5 s throttle avoids a tight crash loop.
nssm set cryo-daemon AppExit Default Restart
nssm set cryo-daemon AppThrottle 5000

nssm start cryo-daemon
```

The service runs as `LocalSystem` by default, which can read `C:\BlueFors\logs`
and write the daemon directory — fine for this. If the logs live on a share,
set a service account that can reach it (`nssm set cryo-daemon ObjectName ...`).

## 6. Verify

1. **Service up:** `nssm status cryo-daemon` → `SERVICE_RUNNING`.
2. **Daemon healthy:** tail `daemon.log` — expect one
   `daemon starting: fridge=... tz=... poll=...` line, then quiet operation.
   Any `POST to ... failed` or `returned HTTP ...` warnings mean the server
   URL, tailnet, or token is wrong (readings spool locally meanwhile — nothing
   is lost, but fix it).
3. **Rows landing, server-side** (inside WSL on the server):
   ```bash
   sudo -u postgres psql -d cryo -c \
     "SELECT max(ts) FROM readings WHERE fridge='<fridge>';"
   ```
   The timestamp should be within one `poll_interval` of now, and keep
   advancing.
4. **Reboot test:** restart the fridge host once; the service must come back
   (`nssm status`) and the gap written while it was down must backfill with no
   duplicates.

Only after all four pass, add/uncomment the fridge in
`server/config/fridges.yaml` — the watchdog SILENT-alarms on any configured
fridge it has never seen.

## Per-host checklist

One row per host; fill in as each is deployed. Names must match the `fridge`
key in that host's `config.toml` *and* the entry in `fridges.yaml`. (Roster per
§11 Q5d; adr_2's parser is still a stub and the last two await log samples.)

| Fridge | Python ver | Store aliases off | Commit deployed | config.toml done | Dry run OK | NSSM installed (rotation + restart) | Reboot test | In fridges.yaml |
|---|---|---|---|---|---|---|---|---|
| blackfridge  | | | | | | | | |
| whitefridge  | | | | | | | | |
| adr_2        | | | | | | | | |
| adr_4        | | | | | | | | |
| fridge_5     | | | | | | | | |
