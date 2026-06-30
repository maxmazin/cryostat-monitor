#!/usr/bin/env bash
# Spin up a throwaway local Postgres + ingest service for development on a Mac
# (Homebrew postgresql@16). NOT for production — labmanager uses a real cluster
# and systemd (see server/systemd/). Data dir and venv live under $WORKDIR and
# can be deleted freely.
#
#   ./scripts/dev_local.sh up      # init cluster, apply schema, (re)start ingest
#   ./scripts/dev_local.sh verify  # run the Phase 0 acceptance check
#   ./scripts/dev_local.sh test    # run the pytest suite (DB only; no ingest svc)
#   ./scripts/dev_local.sh down    # stop everything
#
# `up` is idempotent: re-running it reuses a running cluster and restarts the
# ingest service cleanly.
set -euo pipefail

PGBIN="$(brew --prefix)/opt/postgresql@16/bin"
export PATH="$PGBIN:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="${CRYO_DEV_WORKDIR:-/tmp/cryo-dev}"
PGDATA="$WORKDIR/pgdata"
SOCK="/tmp/cryopg"          # short path: PG unix sockets cap at 103 bytes
VENV="$WORKDIR/venv"
STAMP="$VENV/.deps-installed"        # marks a successful base-deps install
DEVSTAMP="$VENV/.dev-deps-installed" # marks a successful dev-deps install
PIDFILE="$WORKDIR/uvicorn.pid"
PORT="${CRYO_DEV_PGPORT:-54329}"
INGEST_HOST=127.0.0.1
INGEST_PORT="${CRYO_DEV_INGEST_PORT:-8000}"

export CRYO_DB_DSN="postgresql://cryo@127.0.0.1:$PORT/cryo"
export CRYO_TOKENS='{"dev-token-blackfridge":"blackfridge"}'
export CRYO_MAINTENANCE_TOKENS='["dev-maintenance-token"]'
export TOKEN="dev-token-blackfridge"
# Exported so verify_phase0.sh works whether invoked via `verify` or directly.
export INGEST_URL="http://$INGEST_HOST:$INGEST_PORT"

pg_running() { pg_isready -h 127.0.0.1 -p "$PORT" -q; }

# Kill anything bound to the ingest port (a stale uvicorn from a prior run);
# otherwise the health poll could succeed against old code while the new
# process exits silently.
free_ingest_port() {
    local pids
    pids="$(lsof -ti "tcp:$INGEST_PORT" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        sleep 1
    fi
}

# Bring up only the database (cluster + schema + grants) — no ingest service.
# Both `up` and `run_tests` build on this; tests need the DB but not uvicorn.
up_db() {
    mkdir -p "$SOCK" "$WORKDIR"
    if [ ! -d "$PGDATA" ]; then
        initdb -D "$PGDATA" -U "${USER:-postgres}" --auth=trust >/dev/null
    fi
    if ! pg_running; then
        pg_ctl -D "$PGDATA" -o "-p $PORT -k $SOCK -c listen_addresses='127.0.0.1'" \
            -l "$PGDATA/server.log" start
        for _ in $(seq 1 30); do pg_running && break; sleep 1; done
    fi
    pg_running || { echo "postgres failed to start; see $PGDATA/server.log" >&2; return 1; }

    createdb -h 127.0.0.1 -p "$PORT" cryo 2>/dev/null || true
    psql -h 127.0.0.1 -p "$PORT" -d cryo -c \
        "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='cryo') THEN CREATE ROLE cryo LOGIN; END IF; END \$\$;" >/dev/null
    psql -h 127.0.0.1 -p "$PORT" -d cryo -f "$REPO/server/db/schema.sql" >/dev/null
    # Production grants the app role only SELECT/INSERT/UPDATE (see README); the
    # dev/test cluster also grants DELETE so integration tests can clean up rows.
    psql -h 127.0.0.1 -p "$PORT" -d cryo -c \
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cryo;" >/dev/null
}

# Create the venv if missing/incomplete and (re)install base deps when
# requirements.txt has changed since the last successful install. The stamp is
# written only after a successful install, so an interrupted build self-heals.
ensure_venv() {
    if [ ! -x "$VENV/bin/python" ]; then
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install -q --upgrade pip
    fi
    if [ ! -f "$STAMP" ] || [ "$REPO/server/requirements.txt" -nt "$STAMP" ]; then
        "$VENV/bin/pip" install -q -r "$REPO/server/requirements.txt"
        touch "$STAMP"
    fi
}

# (Re)install dev deps only when requirements-dev.txt changed since last install.
ensure_dev_deps() {
    if [ ! -f "$DEVSTAMP" ] || [ "$REPO/server/requirements-dev.txt" -nt "$DEVSTAMP" ]; then
        "$VENV/bin/pip" install -q -r "$REPO/server/requirements-dev.txt"
        touch "$DEVSTAMP"
    fi
}

up() {
    up_db
    ensure_venv
    free_ingest_port
    # Start without a subshell so we can capture the PID and watch it. --app-dir
    # lets uvicorn import ingest.app without a cd.
    "$VENV/bin/uvicorn" --app-dir "$REPO/server" ingest.app:app \
        --host "$INGEST_HOST" --port "$INGEST_PORT" > "$WORKDIR/uvicorn.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"

    # Poll /health, but bail immediately if our process died (don't wait 30s).
    for _ in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "ingest process exited during startup; see $WORKDIR/uvicorn.log" >&2
            tail -20 "$WORKDIR/uvicorn.log" >&2 || true
            return 1
        fi
        if curl -fsS "$INGEST_URL/health" >/dev/null 2>&1; then
            echo "ingest up on $INGEST_URL (pid $pid, db port $PORT)"
            return 0
        fi
        sleep 1
    done
    echo "ingest failed to come up; see $WORKDIR/uvicorn.log" >&2
    tail -20 "$WORKDIR/uvicorn.log" >&2 || true
    return 1
}

down() {
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
    fi
    free_ingest_port
    pg_ctl -D "$PGDATA" stop -m fast 2>/dev/null || true
    echo "stopped"
}

verify() { "$REPO/scripts/verify_phase0.sh"; }

# Run pytest for one suite directory, forwarding extra args. Exit code 5 ("no
# tests collected") is tolerated so a `-k`/path filter that scopes to the other
# suite doesn't fail the run.
_run_pytest() {
    local dir="$1"; shift
    local rc=0
    ( cd "$REPO/$dir" && CRYO_TEST_DSN="$CRYO_DB_DSN" "$VENV/bin/pytest" "$@" ) || rc=$?
    [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ] || return "$rc"
}

# Run the full pytest suite (server unit+integration, plus host-daemon parsers)
# against the dev cluster. Only the DB is needed — no uvicorn — so this uses
# up_db, not up, and leaves no service running. Extra args forward to pytest
# (e.g. `test -k maintenance`, `test tests/test_app.py`), resolved per suite dir.
run_tests() {
    up_db
    ensure_venv
    ensure_dev_deps
    _run_pytest server "$@"
    _run_pytest host-daemon "$@"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    verify) verify ;;
    test) shift; run_tests "$@" ;;
    *) echo "usage: $0 {up|verify|test|down}"; exit 1 ;;
esac
