"""Ingest service (FastAPI on labmanager). See §7 of the spec.

Single data endpoint plus a constrained maintenance endpoint. Phase 0: the
data path is fully wired to PostgreSQL with idempotent inserts.

Run behind systemd via uvicorn, bound to the LAN/tailnet interface only
(not the public internet).

Configuration (env vars):
  CRYO_DB_DSN                 postgresql://cryo@127.0.0.1:5432/cryo
  CRYO_TOKENS                 JSON object {"<bearer-token>": "<fridge>"}   (or)
  CRYO_TOKENS_FILE            path to a JSON file with the same shape
  CRYO_MAX_MAINTENANCE_MINUTES  cap on maintenance duration (default 720)
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import db


def _load_tokens() -> dict[str, str]:
    """Per-host bearer tokens -> fridge name. A host can only write its own data."""
    raw = os.environ.get("CRYO_TOKENS")
    if not raw:
        path = os.environ.get("CRYO_TOKENS_FILE")
        if path:
            with open(path) as fh:
                raw = fh.read()
    if not raw:
        return {}
    return json.loads(raw)


TOKENS: dict[str, str] = {}

# Cap accepted maintenance duration (§7). OpenClaw can request a mute but cannot
# disable the watchdog indefinitely.
MAX_MAINTENANCE_MINUTES = int(os.environ.get("CRYO_MAX_MAINTENANCE_MINUTES", "720"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global TOKENS
    TOKENS = _load_tokens()
    db.init_pool()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="cryostat-monitor ingest", lifespan=lifespan)


# --------------------------------------------------------------------------- models
class Reading(BaseModel):
    ts: datetime
    channel: str
    value: float
    unit: str


class IngestBody(BaseModel):
    fridge: str
    readings: list[Reading]


class MaintenanceBody(BaseModel):
    fridge: str
    minutes: int = Field(gt=0)
    reason: str | None = None
    set_by: str | None = None


# --------------------------------------------------------------------------- auth
def fridge_for_token(authorization: str = Header(...)) -> str:
    """Resolve a Bearer token to its fridge, or 401."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token not in TOKENS:
        raise HTTPException(status_code=401, detail="invalid token")
    return TOKENS[token]


# --------------------------------------------------------------------------- endpoints
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest")
def ingest(body: IngestBody, fridge: str = Depends(fridge_for_token)) -> dict:
    # A host can only write its own data (§7.1).
    if body.fridge != fridge:
        raise HTTPException(status_code=403, detail="fridge does not match token")

    # Normalize every timestamp to UTC before storing (§3.6).
    rows = [
        (
            r.ts.astimezone(timezone.utc),
            body.fridge,
            r.channel,
            r.value,
            r.unit,
        )
        for r in body.readings
    ]

    inserted = db.insert_readings(body.fridge, rows)
    return {"received": len(rows), "inserted": inserted}


@app.post("/maintenance")
def maintenance(body: MaintenanceBody) -> dict:
    # The only write OpenClaw is allowed; duration is capped server-side (§7).
    minutes = min(body.minutes, MAX_MAINTENANCE_MINUTES)
    db.insert_maintenance(body.fridge, minutes, body.reason, body.set_by)
    return {
        "fridge": body.fridge,
        "minutes_granted": minutes,
        "capped": minutes < body.minutes,
    }
