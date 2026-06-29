"""Ingest service (FastAPI on labmanager).

Single data endpoint plus a constrained maintenance endpoint. See §7 of the
spec. This is a skeleton: the structure, data contract, and idempotency
guarantees are in place; DB wiring is marked TODO.

Run behind systemd via uvicorn, bound to the LAN/tailnet interface only
(not the public internet).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="cryostat-monitor ingest")

# Per-host bearer tokens → fridge name. Load from env/secret store in
# production; never hard-code real tokens. A host can only write its own data.
TOKENS: dict[str, str] = {
    # "<token>": "bluefors_1",
}

# Cap accepted maintenance duration (§7). OpenClaw can request a mute but
# cannot disable the watchdog indefinitely.
MAX_MAINTENANCE_MINUTES = int(os.environ.get("CRYO_MAX_MAINTENANCE_MINUTES", "720"))


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

    # TODO: bulk insert into `readings` with
    #   ON CONFLICT (fridge, channel, ts) DO NOTHING   -- idempotent (§3.2)
    # then UPDATE last_seen with max(ts) from the batch (§7.3).
    inserted = len(rows)  # placeholder until DB is wired

    return {"inserted": inserted}


@app.post("/maintenance")
def maintenance(body: MaintenanceBody) -> dict:
    # The only write OpenClaw is allowed; duration is capped server-side (§7).
    minutes = min(body.minutes, MAX_MAINTENANCE_MINUTES)
    # TODO: INSERT INTO maintenance (fridge, until_ts, reason, set_by)
    #       VALUES (body.fridge, now() + minutes, body.reason, body.set_by)
    return {
        "fridge": body.fridge,
        "minutes_granted": minutes,
        "capped": minutes < body.minutes,
    }
