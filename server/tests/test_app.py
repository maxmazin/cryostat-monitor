"""Unit tests for the ingest service endpoints (DB layer faked).

Covers auth, the timezone contract (§3.6), non-finite filtering (§8), batch
and string caps, timestamp plausibility, health degradation, and the
maintenance endpoint's auth/validation/cap/fail-closed behavior (§2.1, §7).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ingest import db

HOST_AUTH = {"Authorization": "Bearer host-token"}
MAINT_AUTH = {"Authorization": "Bearer maint-token"}


def _reading(ts="2026-06-29T19:00:00Z", channel="MXC", value=0.0102, unit="K"):
    return {"ts": ts, "channel": channel, "value": value, "unit": unit}


# --------------------------------------------------------------------------- health
def test_health_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_health_returns_503_degraded_when_db_unreachable(client, monkeypatch):
    def _boom() -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "ping", _boom)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


# --------------------------------------------------------------------------- ingest auth
def test_ingest_missing_auth_header_returns_401(client):
    r = client.post("/ingest", json={"fridge": "blackfridge", "readings": []})
    assert r.status_code == 401


def test_ingest_invalid_token_returns_401(client):
    r = client.post("/ingest", headers={"Authorization": "Bearer nope"},
                    json={"fridge": "blackfridge", "readings": []})
    assert r.status_code == 401


def test_ingest_non_bearer_scheme_returns_401(client):
    r = client.post("/ingest", headers={"Authorization": "Token host-token"},
                    json={"fridge": "blackfridge", "readings": []})
    assert r.status_code == 401


def test_ingest_fridge_must_match_token_returns_403(client):
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "adr_2", "readings": [_reading()]})
    assert r.status_code == 403


# --------------------------------------------------------------------------- ingest happy path
def test_ingest_accepts_aware_timestamp(client, fake_db):
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "blackfridge", "readings": [_reading()]})
    assert r.status_code == 200
    assert r.json() == {"received": 1, "inserted": 1, "dropped": 0}
    assert len(fake_db.readings_calls) == 1


def test_ingest_converts_offset_timestamp_to_utc(client, fake_db):
    # 21:00 at +02:00 is 19:00 UTC.
    client.post("/ingest", headers=HOST_AUTH, json={
        "fridge": "blackfridge",
        "readings": [_reading(ts="2026-06-29T21:00:00+02:00")],
    })
    _, rows = fake_db.readings_calls[0]
    stored_ts = rows[0][0]
    assert stored_ts == datetime(2026, 6, 29, 19, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- ingest validation
def test_ingest_rejects_naive_timestamp_422(client, fake_db):
    # No offset -> naive datetime. Must be rejected loudly, not coerced (§3.6/§12).
    r = client.post("/ingest", headers=HOST_AUTH, json={
        "fridge": "blackfridge",
        "readings": [_reading(ts="2026-06-29T19:00:00")],
    })
    assert r.status_code == 422
    assert fake_db.readings_calls == []  # nothing stored


# NaN/Infinity are sent as raw JSON tokens: Python's json (used by Starlette)
# accepts them, but httpx's `json=` encoder refuses to produce them, so a real
# client posts them in the raw body. We mirror that with `content=`.
_JSON = {**HOST_AUTH, "Content-Type": "application/json"}


def test_ingest_drops_nonfinite_value_keeps_finite(client, fake_db):
    body = (
        '{"fridge": "blackfridge", "readings": ['
        '{"ts": "2026-06-29T19:00:00Z", "channel": "MXC", "value": NaN, "unit": "K"},'
        '{"ts": "2026-06-29T19:00:00Z", "channel": "4K", "value": 3.9, "unit": "K"}]}'
    )
    r = client.post("/ingest", headers=_JSON, content=body)
    assert r.status_code == 200
    assert r.json() == {"received": 2, "inserted": 1, "dropped": 1}
    # Only the finite row reached the DB layer.
    _, rows = fake_db.readings_calls[0]
    assert [row[2] for row in rows] == ["4K"]


def test_ingest_drops_infinity_value(client, fake_db):
    body = (
        '{"fridge": "blackfridge", "readings": ['
        '{"ts": "2026-06-29T19:00:00Z", "channel": "MXC", "value": Infinity, "unit": "K"}]}'
    )
    r = client.post("/ingest", headers=_JSON, content=body)
    assert r.json() == {"received": 1, "inserted": 0, "dropped": 1}
    _, rows = fake_db.readings_calls[0]
    assert rows == []


# --------------------------------------------------------------------------- ingest caps
def test_ingest_rejects_batch_over_cap_returns_413(client, fake_db):
    # 10,001 readings: one over the daemon's spool-drain contract. List of one
    # shared dict — cheap to build and serialize.
    readings = [_reading()] * 10_001
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "blackfridge", "readings": readings})
    assert r.status_code == 413
    assert "10000" in r.json()["detail"]  # detail names the cap so clients chunk
    assert fake_db.readings_calls == []


def test_ingest_accepts_batch_at_cap(client, fake_db):
    readings = [_reading()] * 10_000
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "blackfridge", "readings": readings})
    assert r.status_code == 200
    assert r.json() == {"received": 10_000, "inserted": 10_000, "dropped": 0}


@pytest.mark.parametrize("field,value", [
    ("channel", "c" * 65),   # cap 64
    ("unit", "u" * 17),      # cap 16
])
def test_ingest_rejects_oversized_reading_string_422(client, fake_db, field, value):
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "blackfridge", "readings": [_reading(**{field: value})]})
    assert r.status_code == 422
    assert fake_db.readings_calls == []


def test_ingest_rejects_oversized_fridge_name_422(client, fake_db):
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "f" * 65, "readings": [_reading()]})
    assert r.status_code == 422
    assert fake_db.readings_calls == []


# --------------------------------------------------------------------------- ingest ts sanity
def test_ingest_drops_far_future_ts_keeps_rest_of_batch(client, fake_db):
    # A year-3000 ts (broken host clock) must be dropped — not stored, where it
    # would wedge last_seen via GREATEST — and not 4xx the whole batch, which
    # would poison the daemon's retry loop.
    r = client.post("/ingest", headers=HOST_AUTH, json={
        "fridge": "blackfridge",
        "readings": [_reading(ts="3000-01-01T00:00:00Z"),
                     _reading(channel="4K", value=3.9)],
    })
    assert r.status_code == 200
    assert r.json() == {"received": 2, "inserted": 1, "dropped": 1}
    _, rows = fake_db.readings_calls[0]
    assert [row[2] for row in rows] == ["4K"]  # only the sane row reached the DB


def test_ingest_drops_pre_2020_ts(client, fake_db):
    r = client.post("/ingest", headers=HOST_AUTH, json={
        "fridge": "blackfridge",
        "readings": [_reading(ts="1970-01-01T00:00:01Z")],
    })
    assert r.json() == {"received": 1, "inserted": 0, "dropped": 1}
    _, rows = fake_db.readings_calls[0]
    assert rows == []


def test_ingest_accepts_slightly_future_ts(client, fake_db):
    # Ordinary clock skew (< 24 h ahead) must still be accepted.
    ts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/ingest", headers=HOST_AUTH,
                    json={"fridge": "blackfridge", "readings": [_reading(ts=ts)]})
    assert r.json() == {"received": 1, "inserted": 1, "dropped": 0}


# --------------------------------------------------------------------------- maintenance auth
def test_maintenance_missing_auth_header_returns_401(client):
    r = client.post("/maintenance", json={"fridge": "blackfridge", "minutes": 60})
    assert r.status_code == 401


def test_maintenance_invalid_token_returns_401(client):
    r = client.post("/maintenance", headers={"Authorization": "Bearer nope"},
                    json={"fridge": "blackfridge", "minutes": 60})
    assert r.status_code == 401


def test_maintenance_fail_closed_when_unconfigured_returns_503(client_no_maint):
    # No maintenance tokens configured -> endpoint must refuse, never open (§2.1).
    r = client_no_maint.post("/maintenance", headers={"Authorization": "Bearer anything"},
                             json={"fridge": "blackfridge", "minutes": 60})
    assert r.status_code == 503


# --------------------------------------------------------------------------- maintenance behavior
def test_maintenance_valid_request_succeeds(client, fake_db):
    r = client.post("/maintenance", headers=MAINT_AUTH,
                    json={"fridge": "blackfridge", "minutes": 60, "reason": "regen", "set_by": "ben"})
    assert r.status_code == 200
    assert fake_db.maintenance_calls == [("blackfridge", 60, "regen", "ben")]


def test_maintenance_unknown_fridge_returns_404(client, fake_db):
    r = client.post("/maintenance", headers=MAINT_AUTH,
                    json={"fridge": "bluefors_typo", "minutes": 60})
    assert r.status_code == 404
    assert fake_db.maintenance_calls == []  # no row written for a bad name


def test_maintenance_caps_duration_at_default_max(make_client, fake_db):
    # Default cap is 720 minutes when CRYO_MAX_MAINTENANCE_MINUTES is unset.
    client = make_client(max_minutes=None)
    r = client.post("/maintenance", headers=MAINT_AUTH,
                    json={"fridge": "blackfridge", "minutes": 9999})
    body = r.json()
    assert body["minutes_granted"] == 720
    assert body["capped"] is True
    assert fake_db.maintenance_calls[0][1] == 720  # capped value reaches the DB


def test_maintenance_cap_is_configurable(make_client, fake_db):
    # A non-default cap from the environment is honored (read at startup).
    client = make_client(max_minutes="30")
    r = client.post("/maintenance", headers=MAINT_AUTH,
                    json={"fridge": "blackfridge", "minutes": 9999})
    body = r.json()
    assert body["minutes_granted"] == 30
    assert body["capped"] is True
    assert fake_db.maintenance_calls[0][1] == 30


def test_maintenance_under_cap_not_capped(make_client, fake_db):
    client = make_client(max_minutes="720")
    r = client.post("/maintenance", headers=MAINT_AUTH,
                    json={"fridge": "blackfridge", "minutes": 60})
    body = r.json()
    assert body["minutes_granted"] == 60
    assert body["capped"] is False


def test_maintenance_rejects_nonpositive_minutes_422(client):
    r = client.post("/maintenance", headers=MAINT_AUTH,
                    json={"fridge": "blackfridge", "minutes": 0})
    assert r.status_code == 422
