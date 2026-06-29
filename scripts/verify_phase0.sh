#!/usr/bin/env bash
# Phase 0 acceptance check (§10): a POST of one fake reading lands a row in
# `readings`. Assumes:
#   - PostgreSQL reachable via $CRYO_DB_DSN (db `cryo`, schema applied)
#   - ingest service running at $INGEST_URL (default http://127.0.0.1:8000)
#   - a token for fridge bluefors_1 in $TOKEN
#
# Usage:
#   CRYO_DB_DSN=postgresql://cryo@127.0.0.1:5432/cryo \
#   TOKEN=dev-token-bluefors_1 \
#   ./scripts/verify_phase0.sh
set -euo pipefail

# Fail early with a clear message rather than an opaque 'unbound variable' from
# `set -u` at the psql step after the POSTs have already run.
: "${CRYO_DB_DSN:?set CRYO_DB_DSN, e.g. postgresql://cryo@127.0.0.1:5432/cryo}"

INGEST_URL="${INGEST_URL:-http://127.0.0.1:8000}"
TOKEN="${TOKEN:-dev-token-bluefors_1}"
TS="2026-06-29T19:00:00Z"

echo "1. health check"
curl -fsS "$INGEST_URL/health"; echo

echo "2. POST one fake reading"
curl -fsS -X POST "$INGEST_URL/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"fridge\":\"bluefors_1\",\"readings\":[{\"ts\":\"$TS\",\"channel\":\"MXC\",\"value\":0.0102,\"unit\":\"K\"}]}"
echo

echo "3. POST it again (idempotency: should insert 0)"
curl -fsS -X POST "$INGEST_URL/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"fridge\":\"bluefors_1\",\"readings\":[{\"ts\":\"$TS\",\"channel\":\"MXC\",\"value\":0.0102,\"unit\":\"K\"}]}"
echo

echo "4. verify the row landed"
psql "$CRYO_DB_DSN" -c "SELECT ts, fridge, channel, value, unit FROM readings WHERE fridge='bluefors_1';"
psql "$CRYO_DB_DSN" -c "SELECT * FROM last_seen WHERE fridge='bluefors_1';"

echo "PHASE 0 OK: reading present, last_seen advanced."
