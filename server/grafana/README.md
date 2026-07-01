# Grafana — cryostat dashboards

A **redundant** visualization and "No Data" layer on top of the same PostgreSQL
the ingest service writes. Grafana is **not** the alert path: the watchdog is the
authoritative, deterministic alerting mechanism (§2, §8). Grafana connects with a
**read-only** role and never writes.

## Contents

```
grafana/
  provisioning/
    datasources/cryo-postgres.yaml   # the read-only Postgres datasource (uid: cryo_pg)
    dashboards/cryo.yaml             # dashboard provider -> points at the JSON below
  dashboards/
    cryostat-overview.json           # fridge selector + temps + pressures + data-freshness
```

The dashboard has a `fridge` template variable and three panels:
1. **Stage temperatures** — the `K` channels for the selected fridge (log Y, so the
   mK mixing chamber and the ~50 K stage are both legible).
2. **Gauge pressures** — the `mbar` channels (raw positions `P1..P6` until Ben maps
   the still line, §11 Q5; log Y).
3. **Data freshness (all fridges)** — seconds since data last *arrived*
   (`last_seen.received_at`), red past 240 s. This mirrors the watchdog's SILENT
   alarm as a redundant visual check — the watchdog remains authoritative.

## One-time setup on labmanager

1. **Read-only DB role** (mirrors the `openclaw_ro` pattern in `db/schema.sql`):
   ```sql
   CREATE ROLE grafana_ro LOGIN PASSWORD 'CHANGE_ME';
   GRANT CONNECT ON DATABASE cryo TO grafana_ro;
   GRANT USAGE ON SCHEMA public TO grafana_ro;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
   ```
   Ensure `pg_hba.conf` allows `grafana_ro` to connect over `127.0.0.1` (md5/scram);
   the datasource uses a TCP loopback connection, not the peer-auth socket.

2. **Give Grafana the password** (kept out of git). The datasource reads
   `$CRYO_GRAFANA_DB_PASSWORD` from Grafana's environment — e.g. in
   `/etc/default/grafana-server` (Debian/Ubuntu package) or a systemd override:
   ```
   CRYO_GRAFANA_DB_PASSWORD=the-grafana_ro-password
   ```

3. **Install the provisioning files:**
   ```bash
   sudo install -m600 provisioning/datasources/cryo-postgres.yaml /etc/grafana/provisioning/datasources/
   sudo install -m644 provisioning/dashboards/cryo.yaml          /etc/grafana/provisioning/dashboards/
   sudo install -Dm644 dashboards/cryostat-overview.json \
        /var/lib/grafana/dashboards/cryostats/cryostat-overview.json   # matches cryo.yaml's `path`
   sudo systemctl restart grafana-server
   ```

The dashboard appears under the **Cryostats** folder. Panels are UI-editable, but
this JSON is the source of truth — re-export and commit changes here.

## Note on units and timezone

Values are canonical (K, mbar) and timestamps are UTC (§3.6); the dashboard's
`timezone: browser` renders them in the viewer's local time.
