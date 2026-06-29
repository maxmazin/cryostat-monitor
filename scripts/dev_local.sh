#!/usr/bin/env bash
# Spin up a throwaway local Postgres + ingest service for development on a Mac
# (Homebrew postgresql@16). NOT for production — labmanager uses a real cluster
# and systemd (see server/systemd/). Data dir and venv live under $WORKDIR and
# can be deleted freely.
#
#   ./scripts/dev_local.sh up      # init cluster, apply schema, start ingest
#   ./scripts/dev_local.sh verify  # run the Phase 0 acceptance check
#   ./scripts/dev_local.sh down     # stop everything
set -euo pipefail

PGBIN="$(brew --prefix)/opt/postgresql@16/bin"
export PATH="$PGBIN:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="${CRYO_DEV_WORKDIR:-/tmp/cryo-dev}"
PGDATA="$WORKDIR/pgdata"
SOCK="/tmp/cryopg"          # short path: PG unix sockets cap at 103 bytes
VENV="$WORKDIR/venv"
PORT="${CRYO_DEV_PGPORT:-54329}"

export CRYO_DB_DSN="postgresql://cryo@127.0.0.1:$PORT/cryo"
export CRYO_TOKENS='{"dev-token-bluefors_1":"bluefors_1"}'
export TOKEN="dev-token-bluefors_1"

up() {
    mkdir -p "$SOCK"
    if [ ! -d "$PGDATA" ]; then
        initdb -D "$PGDATA" -U "$USER" --auth=trust >/dev/null
    fi
    pg_ctl -D "$PGDATA" -o "-p $PORT -k $SOCK -c listen_addresses='127.0.0.1'" \
        -l "$PGDATA/server.log" start
    sleep 2
    createdb -h 127.0.0.1 -p "$PORT" cryo 2>/dev/null || true
    psql -h 127.0.0.1 -p "$PORT" -d cryo -c \
        "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='cryo') THEN CREATE ROLE cryo LOGIN; END IF; END \$\$;"
    psql -h 127.0.0.1 -p "$PORT" -d cryo -f "$REPO/server/db/schema.sql" >/dev/null
    psql -h 127.0.0.1 -p "$PORT" -d cryo -c \
        "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO cryo;" >/dev/null

    if [ ! -d "$VENV" ]; then
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install -q --upgrade pip
        "$VENV/bin/pip" install -q -r "$REPO/server/requirements.txt"
    fi
    (cd "$REPO/server" && "$VENV/bin/uvicorn" ingest.app:app \
        --host 127.0.0.1 --port 8000 > "$WORKDIR/uvicorn.log" 2>&1 &)
    # Poll /health until the service is listening (cold start can take a few s).
    for _ in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
            echo "ingest up on http://127.0.0.1:8000  (db port $PORT)"
            return 0
        fi
        sleep 1
    done
    echo "ingest failed to come up; see $WORKDIR/uvicorn.log" >&2
    tail -20 "$WORKDIR/uvicorn.log" >&2 || true
    return 1
}

down() {
    pkill -f "uvicorn ingest.app:app" 2>/dev/null || true
    pg_ctl -D "$PGDATA" stop -m fast 2>/dev/null || true
    echo "stopped"
}

verify() { "$REPO/scripts/verify_phase0.sh"; }

case "${1:-}" in
    up) up ;;
    down) down ;;
    verify) verify ;;
    *) echo "usage: $0 {up|verify|down}"; exit 1 ;;
esac
