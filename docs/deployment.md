# Deployment & reachability — closing Phase 0 on labmanager

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

Generate strong tokens — one **per fridge host** plus one **maintenance** token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per token
```

Create `/etc/cryostat-monitor/ingest.env` (mode 600, owned by the `cryo` user)
from `server/.env.example`:

```ini
CRYO_DB_DSN=postgresql:///cryo?host=/var/run/postgresql
CRYO_TOKENS={"<bluefors_1-token>":"bluefors_1"}
CRYO_MAINTENANCE_TOKENS=["<maintenance-token>"]
CRYO_MAX_MAINTENANCE_MINUTES=720
```

```bash
sudo install -d -m 750 /etc/cryostat-monitor
sudo useradd --system --no-create-home cryo 2>/dev/null || true
sudo chown root:cryo /etc/cryostat-monitor/ingest.env
sudo chmod 640 /etc/cryostat-monitor/ingest.env
```

Add each fridge as you onboard it by extending the `CRYO_TOKENS` JSON object.
Keep these tokens out of git (the repo's `.gitignore` already excludes `*.env`).

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
local port — no nginx, no manual certs:

```bash
sudo tailscale serve --bg --https 443 http://127.0.0.1:8000
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
  -d '{"fridge":"bluefors_1","readings":[{"ts":"2026-06-29T19:00:00Z","channel":"MXC","value":0.0102,"unit":"K"}]}'
```

Then confirm the row landed, back on labmanager:

```bash
sudo -u postgres psql -d cryo -c \
  "SELECT * FROM readings WHERE fridge='bluefors_1';"
sudo -u postgres psql -d cryo -c "SELECT * FROM last_seen;"
```

**Phase 0 is complete when that POST — issued from an actual fridge host over
the tailnet/LAN — lands a row.** Clean up the fake row afterward
(`DELETE FROM readings WHERE fridge='bluefors_1';` as the postgres superuser).

---

## Out of scope here (later phases)
- **UPS + healthchecks.io dead-man's switch** and the watchdog service — Phase 2
  (depends on Q2). `monitor the monitor` is essential but not part of closing
  Phase 0.
- **Grafana** panel + `openclaw_ro` role — Phase 1 (one panel) / Phase 4.
- **`pg_dump` → NAS via Restic**, retention policy — Phase 4 (Q6).
