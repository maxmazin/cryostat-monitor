# Deployment & reachability — closing Phase 0 on labmanager

> **Running the server on a Windows 10 host?** Do it under WSL2 (Ubuntu 24.04):
> see [`deployment-wsl.md`](./deployment-wsl.md), which wraps §1–§4 below with the
> WSL-specific setup (systemd, networking into the VM, boot autostart). The steps
> here apply verbatim inside the WSL shell.

Phase 0's acceptance criterion is: *a `curl` POST of one fake reading **from a
fridge host** lands a row in `readings`.* So far that path is proven only on a
dev Mac. This runbook stands the ingest service up on **labmanager** (Ubuntu
24.04) and confirms reachability from one fridge host over the tailnet/LAN.

Prerequisite: §11 **Q1** (topology) answered. This guide assumes the **tailnet**
default; the LAN-only variant is noted where it differs.

---

## 1. PostgreSQL

```bash
sudo apt update && sudo apt install -y postgresql
sudo -u postgres createdb cryo
sudo -u postgres createuser cryo            # app role (login; no password needed for local peer/trust)
sudo -u postgres psql -d cryo -f /opt/cryostat-monitor/server/db/schema.sql
# The app role owns nothing it didn't create, so grant it the data-table writes
# it needs (and only those — no DELETE; retention pruning is a separate admin job):
sudo -u postgres psql -d cryo -c \
  "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO cryo;"
```

Decide how the service authenticates to Postgres: local **peer/trust** (DSN
`postgresql:///cryo?host=/var/run/postgresql`) is simplest on a single box;
otherwise set a password and put it in the DSN. Record the final DSN for step 3.

> The read-only `openclaw_ro` role (commented at the bottom of `schema.sql`) is
> for Grafana/OpenClaw later — not required to close Phase 0.

## 2. Code + Python env

```bash
sudo mkdir -p /opt/cryostat-monitor
sudo chown "$USER" /opt/cryostat-monitor
git clone https://github.com/maxmazin/cryostat-monitor /opt/cryostat-monitor
cd /opt/cryostat-monitor/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

(Paths match `server/systemd/cryo-ingest.service`: `/opt/cryostat-monitor/server`
and its `.venv`.)

## 3. Configuration & tokens

Create the service user first (the systemd unit runs as `cryo`), then generate
strong tokens — one **per fridge host** plus one **maintenance** token:

```bash
sudo useradd --system --no-create-home cryo 2>/dev/null || true
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per token
```

Now create the config directory and the `ingest.env` file. The file holds
secrets, so it is owned `root:cryo`, mode `640` (the `cryo` group reads it, not
world); the directory is `root:cryo`, mode `750` so only `cryo` can traverse it.
systemd reads `EnvironmentFile=` **as root** before dropping to `User=cryo`, so
these permissions keep the secrets private without blocking startup. Run this
block top to bottom (it creates the file — it is not just a snippet to read):

```bash
sudo install -d -m 750 -o root -g cryo /etc/cryostat-monitor
sudo tee /etc/cryostat-monitor/ingest.env >/dev/null <<'EOF'
CRYO_DB_DSN=postgresql:///cryo?host=/var/run/postgresql
CRYO_TOKENS={"<blackfridge-token>":"blackfridge"}
CRYO_MAINTENANCE_TOKENS=["<maintenance-token>"]
CRYO_MAX_MAINTENANCE_MINUTES=720
EOF
sudo chown root:cryo /etc/cryostat-monitor/ingest.env
sudo chmod 640 /etc/cryostat-monitor/ingest.env
```

Then replace the `<...>` placeholders with the tokens you generated. Add each
fridge as you onboard it by extending the `CRYO_TOKENS` JSON object. Keep these
tokens out of git (the repo's `.gitignore` already excludes `*.env`).

## 4. Run the ingest service (systemd)

```bash
sudo cp /opt/cryostat-monitor/server/systemd/cryo-ingest.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cryo-ingest
systemctl status cryo-ingest
curl -fsS http://127.0.0.1:8000/health        # local smoke test -> {"status":"ok"}
```

The unit binds `127.0.0.1:8000` deliberately — it is **not** exposed directly;
TLS/exposure is handled in step 5.

## 5. TLS & exposure

The host daemon posts to `https://labmanager.<tailnet>.ts.net/ingest`, so the
service needs an HTTPS front end. Pick per Q1:

### Tailnet (recommended default) — Tailscale Serve

Tailscale provisions a valid HTTPS cert for the MagicDNS name and proxies to the
local port — no nginx, no manual certs. Serve's flag syntax has shifted across
releases, so verify against your installed version (`tailscale version`); on
current releases:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale serve status      # should show https://labmanager.<tailnet>.ts.net -> 127.0.0.1:8000
```

Only devices on your tailnet can reach it. Nothing is published to the public
internet.

### LAN-only variant — reverse proxy

If the hosts share a plain LAN (no tailnet), put **Caddy** (auto-TLS) or nginx
in front of `127.0.0.1:8000` on the lab subnet, and firewall port 443 to the
fridge subnet. Use the LAN hostname/IP in each host's `server_url`.

## 6. Reachability test from a fridge host (the Phase 0 acceptance gate)

On **one fridge host** (PowerShell example; bash is analogous), using that
host's token and the real URL:

```powershell
$URL = "https://labmanager.<tailnet>.ts.net"
curl.exe -fsS "$URL/health"
curl.exe -fsS -X POST "$URL/ingest" `
  -H "Authorization: Bearer <that-host-token>" `
  -H "Content-Type: application/json" `
  -d '{"fridge":"blackfridge","readings":[{"ts":"2026-06-29T19:00:00Z","channel":"MXC","value":0.0102,"unit":"K"}]}'
```

Then confirm the row landed, back on labmanager:

```bash
sudo -u postgres psql -d cryo -c \
  "SELECT * FROM readings WHERE fridge='blackfridge';"
sudo -u postgres psql -d cryo -c "SELECT * FROM last_seen;"
```

**Phase 0 is complete when that POST — issued from an actual fridge host over
the tailnet/LAN — lands a row.** Clean up the fake data afterward as the
postgres superuser — both the reading **and** the `last_seen` row it advanced,
or the Phase 2 watchdog will later treat `blackfridge` as a known fridge that has
gone silent:

```bash
sudo -u postgres psql -d cryo \
  -c "DELETE FROM readings  WHERE fridge='blackfridge';" \
  -c "DELETE FROM last_seen WHERE fridge='blackfridge';"
```

---

## Database backups

Nightly logical backups are scripted in `scripts/pg_backup.sh` (`pg_dump -Fc` to a
local directory, pruning dumps older than `CRYO_BACKUP_KEEP_DAYS`, default 14). Run
it on labmanager via the systemd timer:

```bash
sudo install -m755 scripts/pg_backup.sh /opt/cryostat-monitor/scripts/pg_backup.sh
sudo install -m644 server/systemd/cryo-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cryo-backup.timer
sudo systemctl start cryo-backup.service      # take one now to verify
pg_restore --list /var/backups/cryostat/cryo-*.dump | head   # confirm it's readable
```

Point the site's existing **Restic → NAS** job at `/var/backups/cryostat` for
offsite copies (§10 Phase 4). Restore with `pg_restore -d cryo <dump>`. The raw-data
**retention/downsampling** decision (Q6) is separate and still open — these dumps
protect the data regardless of that choice.

## Out of scope here (later phases)
- **UPS + healthchecks.io dead-man's switch** and the watchdog service — Phase 2
  (depends on Q2). `monitor the monitor` is essential but not part of closing
  Phase 0.
- **Grafana** dashboards — provisioning added in `server/grafana/` (see its
  README to deploy on labmanager). `openclaw_ro` role — Phase 4.
- **Restic → NAS** offsite wiring and the raw-data retention policy — Phase 4 (Q6).
