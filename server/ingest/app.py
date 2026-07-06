"""Ingest service (FastAPI on labmanager). See §7 of the spec.

Single data endpoint plus a constrained maintenance endpoint. Phase 0: the
data path is fully wired to PostgreSQL with idempotent inserts.

Run behind systemd via uvicorn, bound to the LAN/tailnet interface only
(not the public internet).

Configuration (env vars):
  CRYO_DB_DSN                 postgresql://cryo@127.0.0.1:5432/cryo
  CRYO_TOKENS                 JSON object {"<bearer-token>": "<fridge>"}   (or)
  CRYO_TOKENS_FILE            path to a JSON file with the same shape
  CRYO_MAINTENANCE_TOKENS     JSON array ["<token>", ...] allowed to set mutes.
                              If empty, /maintenance is refused (fail-closed).
  CRYO_MAINTENANCE_TOKENS_FILE  path to a JSON file with the same shape
  CRYO_MAX_MAINTENANCE_MINUTES  cap on maintenance duration (default 720)
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import db

log = logging.getLogger("cryo.ingest")


def _load_json_env(env_var: str, file_env_var: str) -> object | None:
    """Read JSON config from an env var, or from a file named by another env var."""
    raw = os.environ.get(env_var)
    if not raw:
        path = os.environ.get(file_env_var)
        if path:
            with open(path) as fh:
                raw = fh.read()
    if not raw:
        return None
    return json.loads(raw)


def _load_tokens() -> dict[str, str]:
    """Per-host bearer tokens -> fridge name. A host can only write its own data."""
    return _load_json_env("CRYO_TOKENS", "CRYO_TOKENS_FILE") or {}


def _load_maintenance_tokens() -> set[str]:
    """Bearer tokens allowed to set maintenance mutes (OpenClaw, humans)."""
    return set(_load_json_env("CRYO_MAINTENANCE_TOKENS", "CRYO_MAINTENANCE_TOKENS_FILE") or [])


# All configuration is loaded at startup (lifespan), so a fresh process — or a
# fresh TestClient in tests — picks up the current environment. Defaults here
# apply only until lifespan runs.
TOKENS: dict[str, str] = {}
MAINTENANCE_TOKENS: set[str] = set()
KNOWN_FRIDGES: set[str] = set()
# Cap on accepted maintenance duration (§7): OpenClaw can request a mute but
# cannot disable the watchdog indefinitely.
MAX_MAINTENANCE_MINUTES = 720


def _load_max_maintenance_minutes() -> int:
    return int(os.environ.get("CRYO_MAX_MAINTENANCE_MINUTES", "720"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global TOKENS, MAINTENANCE_TOKENS, KNOWN_FRIDGES, MAX_MAINTENANCE_MINUTES
    TOKENS = _load_tokens()
    MAINTENANCE_TOKENS = _load_maintenance_tokens()
    # The configured fridges are exactly the targets of the per-host tokens.
    KNOWN_FRIDGES = set(TOKENS.values())
    MAX_MAINTENANCE_MINUTES = _load_max_maintenance_minutes()
    if not TOKENS:
        log.warning("CRYO_TOKENS is empty: every /ingest request will be rejected (401).")
    if not MAINTENANCE_TOKENS:
        log.warning("CRYO_MAINTENANCE_TOKENS is empty: /maintenance is disabled (503).")
    db.init_pool()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="cryostat-monitor ingest", lifespan=lifespan)


# --------------------------------------------------------------------------- models
# The daemon drains at most 10,000 spool rows per POST (host-daemon/spool.py,
# unacked(limit=10000)) — that is the contract. A larger batch is a client bug,
# not a bigger backlog, so it is rejected with 413 rather than buffered.
MAX_BATCH_READINGS = 10_000

# Timestamp plausibility window. A reading far in the future (broken host
# clock, seconds-vs-milliseconds epoch bug) would permanently wedge
# last_seen.last_ts via GREATEST and freeze the watchdog's ORDER BY ts DESC
# view. Such readings are DROPPED, not rejected: a 4xx would poison the
# daemon's retry loop over one bad row.
TS_FLOOR = datetime(2020, 1, 1, tzinfo=timezone.utc)
TS_MAX_FUTURE_SKEW = timedelta(hours=24)


class Reading(BaseModel):
    ts: datetime
    channel: str = Field(max_length=64)
    value: float
    unit: str = Field(max_length=16)

    @field_validator("ts")
    @classmethod
    def _require_tzaware(cls, v: datetime) -> datetime:
        # A naive datetime would be coerced using the SERVER's local tz, shifting
        # the reading by labmanager's UTC offset and breaking staleness math
        # (§3.6, §12). The daemon must send tz-aware (UTC) timestamps.
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware (e.g. end with 'Z')")
        return v


class IngestBody(BaseModel):
    fridge: str = Field(max_length=64)
    readings: list[Reading]


class MaintenanceBody(BaseModel):
    fridge: str
    minutes: int = Field(gt=0)
    reason: str | None = None
    set_by: str | None = None


# --------------------------------------------------------------------------- auth
def _bearer(authorization: str | None) -> str:
    # Header optional so a missing credential is a clean 401, not FastAPI's 422.
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing or malformed bearer token")
    return token


def _match_token(presented: str, known: Iterable[str]) -> str | None:
    """Constant-time token lookup: compare against EVERY known token with
    secrets.compare_digest (a dict lookup short-circuits and leaks timing).
    There are ~5 tokens, so the full scan is negligible. Encoded to bytes
    because compare_digest on str rejects non-ASCII input."""
    presented_bytes = presented.encode()
    matched: str | None = None
    for candidate in known:
        if secrets.compare_digest(presented_bytes, candidate.encode()):
            matched = candidate
    return matched


def fridge_for_token(authorization: str | None = Header(default=None)) -> str:
    """Resolve a per-host Bearer token to its fridge, or 401."""
    token = _bearer(authorization)
    matched = _match_token(token, TOKENS)
    if matched is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return TOKENS[matched]


def require_maintenance_auth(authorization: str | None = Header(default=None)) -> None:
    """Authorize a /maintenance caller. Fail closed if no tokens are configured."""
    if not MAINTENANCE_TOKENS:
        raise HTTPException(status_code=503, detail="maintenance endpoint not configured")
    token = _bearer(authorization)
    if _match_token(token, MAINTENANCE_TOKENS) is None:
        raise HTTPException(status_code=401, detail="invalid maintenance token")


# --------------------------------------------------------------------------- endpoints
# response_model=None: the union return type (dict | JSONResponse) is not a
# valid response-model annotation for FastAPI.
@app.get("/health", response_model=None)
def health() -> dict | JSONResponse:
    # The pool opens non-blocking, so startup "succeeds" even with a bad DSN.
    # /health must prove a real DB round-trip, or every /ingest 500s while the
    # monitoring stack reports green and all five fridges drift into SILENT.
    try:
        db.ping()
    except Exception:
        log.exception("health check failed: database unreachable")
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "detail": "database unreachable"},
        )
    return {"status": "ok"}


@app.post("/ingest")
def ingest(body: IngestBody, fridge: str = Depends(fridge_for_token)) -> dict:
    # A host can only write its own data (§7.1).
    if body.fridge != fridge:
        raise HTTPException(status_code=403, detail="fridge does not match token")

    if len(body.readings) > MAX_BATCH_READINGS:
        # 413 (not a generic 422) so a client author knows the fix is to chunk,
        # matching the daemon's 10,000-row spool drain per POST.
        raise HTTPException(
            status_code=413,
            detail=(
                f"batch has {len(body.readings)} readings; the maximum is "
                f"{MAX_BATCH_READINGS} per POST — split into smaller batches"
            ),
        )

    # Drop non-finite values (NaN/Inf from a flaky sensor) rather than storing
    # them: stored NaN silently evades the watchdog's threshold checks (§8), and
    # rejecting the whole batch would wedge the host's spool over one bad row.
    # Implausible timestamps (outside TS_FLOOR..now+TS_MAX_FUTURE_SKEW) are
    # dropped for the same reason. ts is validated tz-aware on the model;
    # normalize to UTC before storing (§3.6).
    rows = []
    dropped_nonfinite = 0
    dropped_ts = 0
    first_bad_ts: tuple[str, str] | None = None
    ts_ceiling = datetime.now(timezone.utc) + TS_MAX_FUTURE_SKEW
    for r in body.readings:
        if not math.isfinite(r.value):
            dropped_nonfinite += 1
            continue
        ts_utc = r.ts.astimezone(timezone.utc)
        if not (TS_FLOOR <= ts_utc <= ts_ceiling):
            dropped_ts += 1
            if first_bad_ts is None:
                first_bad_ts = (ts_utc.isoformat(), r.channel)
            continue
        rows.append((ts_utc, body.fridge, r.channel, r.value, r.unit))

    if dropped_nonfinite:
        log.warning("ingest %s: dropped %d non-finite reading(s)", body.fridge, dropped_nonfinite)
    if dropped_ts:
        # One aggregated line per batch: a host with a broken clock would
        # otherwise emit a warning per reading, 10k lines per POST.
        log.warning(
            "ingest %s: dropped %d reading(s) with implausible ts (first: %s channel=%s)",
            body.fridge, dropped_ts, first_bad_ts[0], first_bad_ts[1],
        )

    dropped = dropped_nonfinite + dropped_ts
    inserted = db.insert_readings(body.fridge, rows)
    return {"received": len(body.readings), "inserted": inserted, "dropped": dropped}


@app.post("/maintenance", dependencies=[Depends(require_maintenance_auth)])
def maintenance(body: MaintenanceBody) -> dict:
    # Reject mutes for fridges we don't know about — a typo'd name would write a
    # row that mutes nothing while the real fridge keeps paging (or vice versa).
    if body.fridge not in KNOWN_FRIDGES:
        raise HTTPException(status_code=404, detail=f"unknown fridge: {body.fridge}")

    # Duration is capped server-side (§7). Note: an authorized caller can still
    # extend a mute by re-issuing; the cap bounds a single window, and auth keeps
    # the endpoint off the unauthenticated attack surface (§2.1).
    minutes = min(body.minutes, MAX_MAINTENANCE_MINUTES)
    db.insert_maintenance(body.fridge, minutes, body.reason, body.set_by)
    return {
        "fridge": body.fridge,
        "minutes_granted": minutes,
        "capped": minutes < body.minutes,
    }
