"""End-to-end acceptance tests for the watchdog (Phase 2, §10).

Unlike test_watchdog.py (which fakes the DB), these run check_once() through the
REAL db layer against a live PostgreSQL AND real HTTP: the Slack webhook and the
healthchecks.io heartbeat are pointed at a local capture server. This exercises
the whole loop — config, SQL reads/writes, the state machines, Slack delivery,
and the heartbeat — the way it will run on labmanager.

Each lifecycle acceptance criterion maps to a test below:
  - stale daemon -> SILENT state, no Slack : test_silent_state_when_data_goes_stale
  - room -> cooling Slack event           : test_lifecycle_cooling_started
  - cooling -> base Slack event           : test_lifecycle_reaches_base
  - base -> warming Slack event           : test_lifecycle_warming_started
  - warming -> room Slack event           : test_lifecycle_reaches_room
  - maintenance mute -> Slack suppressed  : test_maintenance_mute_suppresses_lifecycle
  - restart mid-state -> no re-spam       : test_restart_mid_lifecycle_does_not_respam
  - frozen lifecycle channel -> STALE     : test_stale_lifecycle_channel_alerts_and_clears
  - heartbeat every loop (dead-man)       : test_heartbeat_pings_every_loop

Gated on CRYO_TEST_DSN, same as the other integration tests.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from watchdog import db
from watchdog import watchdog as wd

DSN = os.environ.get("CRYO_TEST_DSN")

if not DSN and os.environ.get("CRYO_REQUIRE_INTEGRATION"):
    raise RuntimeError(
        "CRYO_REQUIRE_INTEGRATION is set but CRYO_TEST_DSN is unset — integration "
        "tests would skip. Set CRYO_TEST_DSN to a schema-applied test database."
    )

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set CRYO_TEST_DSN to run DB integration tests"),
]

UTC = timezone.utc
SLACK_ENV = "CRYO_TEST_SLACK_WEBHOOK"


# --------------------------------------------------------------------------- capture server
class _Capture:
    """Records Slack POSTs (as message text) and heartbeat GETs."""

    def __init__(self) -> None:
        self.url = ""
        self.slack: list[str] = []
        self.beats = 0

    def reset(self) -> None:
        self.slack.clear()
        self.beats = 0


@pytest.fixture(scope="module")
def capture():
    cap = _Capture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence the default stderr logging
            pass

        def do_POST(self):  # Slack incoming webhook
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                cap.slack.append(json.loads(body)["text"])
            except (ValueError, KeyError):
                cap.slack.append(body.decode("utf-8", "replace"))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):  # healthchecks.io ping
            cap.beats += 1
            self.send_response(200)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    cap.url = f"http://{host}:{port}"
    os.environ[SLACK_ENV] = cap.url
    yield cap
    srv.shutdown()
    os.environ.pop(SLACK_ENV, None)


# --------------------------------------------------------------------------- db fixtures
@pytest.fixture(scope="module")
def _pool():
    db.init_pool(DSN)
    yield
    db.close_pool()


@pytest.fixture
def fridge(_pool):
    name = f"e2e_{uuid.uuid4().hex[:8]}"
    yield name
    with db._get_pool().connection() as conn:
        for table in ("readings", "last_seen", "maintenance", "alert_state"):
            conn.execute(f"DELETE FROM {table} WHERE fridge = %s", (name,))


def _cfg(capture, fridge: str, *, reminder: float = 1800) -> wd.WatchdogConfig:
    return wd.WatchdogConfig(
        check_interval=15,
        reminder_interval=reminder,
        healthchecks_url=capture.url,
        slack_webhook_env=SLACK_ENV,
        fridges=[
            wd.FridgeConfig(
                name=fridge, poll_interval=60, staleness_factor=4,
                lifecycle=wd.LifecycleConfig(
                    channel="MXC",
                    cooling_start_k=280.0,
                    base_temperature_k=0.050,
                    warming_start_k=0.100,
                    room_temperature_k=285.0,
                    unit="K",
                ),
            )
        ],
    )


def _set_last_seen(fridge: str, *, age_seconds: float) -> None:
    """Record that data last ARRIVED age_seconds ago (staleness basis)."""
    ts = datetime.now(UTC) - timedelta(seconds=age_seconds)
    with db._get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO last_seen (fridge, last_ts, received_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (fridge) DO UPDATE SET last_ts = EXCLUDED.last_ts, "
            "received_at = EXCLUDED.received_at",
            (fridge, ts, ts),
        )


def _add_reading(fridge: str, value: float, ts: datetime, *, channel: str = "MXC") -> None:
    with db._get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO readings (ts, fridge, channel, value, unit) "
            "VALUES (%s, %s, %s, %s, 'K') ON CONFLICT DO NOTHING",
            (ts, fridge, channel, value),
        )


def _mute(fridge: str, *, minutes: int) -> None:
    with db._get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO maintenance (fridge, until_ts) "
            "VALUES (%s, now() + make_interval(mins => %s))",
            (fridge, minutes),
        )


def _preset_lifecycle(fridge: str, state: str, *, notified_age_seconds: float = 60) -> None:
    ln = datetime.now(UTC) - timedelta(seconds=notified_age_seconds)
    with db._get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO alert_state (fridge, alert_key, state, since, last_notified) "
            "VALUES (%s, %s, %s, %s, %s)",
            (fridge, wd.LIFECYCLE_KEY, state, ln, ln),
        )


# --------------------------------------------------------------------------- acceptance tests
def test_silent_state_when_data_goes_stale(capture, fridge):
    capture.reset()
    # Last data arrived well past staleness_factor * poll_interval (4 * 60 s).
    _set_last_seen(fridge, age_seconds=1000)
    wd.check_once(_cfg(capture, fridge))
    assert capture.slack == []
    assert capture.beats == 1
    assert db.get_alert_state(fridge, "SILENT").state == "ALERTING"


def test_lifecycle_cooling_started(capture, fridge):
    now = datetime.now(UTC)
    cfg = _cfg(capture, fridge)
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_ROOM)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 279.0, now - timedelta(minutes=1))
    wd.check_once(cfg)
    assert any("COOLING STARTED" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, wd.LIFECYCLE_KEY).state == wd.PHASE_COOLING


def test_lifecycle_reaches_base(capture, fridge):
    now = datetime.now(UTC)
    cfg = _cfg(capture, fridge)
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_COOLING)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.025, now - timedelta(minutes=1))
    wd.check_once(cfg)
    assert any("BASE TEMPERATURE" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, wd.LIFECYCLE_KEY).state == wd.PHASE_BASE


def test_lifecycle_warming_started(capture, fridge):
    now = datetime.now(UTC)
    cfg = _cfg(capture, fridge)
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_BASE)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.15, now - timedelta(minutes=1))
    wd.check_once(cfg)
    assert any("WARMING STARTED" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, wd.LIFECYCLE_KEY).state == wd.PHASE_WARMING


def test_lifecycle_reaches_room(capture, fridge):
    now = datetime.now(UTC)
    cfg = _cfg(capture, fridge)
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_WARMING)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 293.0, now - timedelta(minutes=1))
    wd.check_once(cfg)
    assert any("ROOM TEMPERATURE" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, wd.LIFECYCLE_KEY).state == wd.PHASE_ROOM


def test_maintenance_mute_suppresses_lifecycle(capture, fridge):
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_ROOM)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 279.0, datetime.now(UTC) - timedelta(minutes=1))
    _mute(fridge, minutes=60)
    wd.check_once(_cfg(capture, fridge))
    assert capture.slack == []                # suppressed
    assert capture.beats == 1                 # but still heartbeats
    assert db.get_alert_state(fridge, wd.LIFECYCLE_KEY).state == wd.PHASE_COOLING


def test_restart_mid_lifecycle_does_not_respam(capture, fridge):
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_COOLING, notified_age_seconds=60)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 279.0, datetime.now(UTC) - timedelta(minutes=1))
    wd.check_once(_cfg(capture, fridge))
    assert capture.slack == []                # no re-page
    assert capture.beats == 1


def test_stale_lifecycle_channel_alerts_and_clears(capture, fridge):
    cfg = _cfg(capture, fridge)
    # The fridge is reporting (fresh last_seen) but its lifecycle channel froze
    # 20+ minutes ago: STALE pages, and the phase is NOT inferred from the
    # frozen value.
    capture.reset()
    _preset_lifecycle(fridge, wd.PHASE_BASE)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.025, datetime.now(UTC) - timedelta(minutes=20))
    wd.check_once(cfg)
    assert any("STALE" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, wd.STALE_PREFIX + "MXC").state == "ALERTING"
    assert db.get_alert_state(fridge, wd.LIFECYCLE_KEY).state == wd.PHASE_BASE

    # Fresh data returns: the RESOLVED is held (flap damping) — no message yet.
    capture.reset()
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.025, datetime.now(UTC) - timedelta(seconds=30))
    wd.check_once(cfg)
    assert capture.slack == []
    assert db.get_alert_state(fridge, wd.STALE_PREFIX + "MXC").state == "ALERTING"

    # Once continuously fresh past the hold, a single RESOLVED goes out. Shift
    # the watchdog clock instead of sleeping; move last_seen and the data with
    # it so nothing looks stale at the shifted now.
    capture.reset()
    later = datetime.now(UTC) + timedelta(seconds=wd.CLEAR_HOLD_SECONDS + 10)
    _set_last_seen(fridge, age_seconds=-(wd.CLEAR_HOLD_SECONDS + 5))
    _add_reading(fridge, 0.025, later - timedelta(seconds=30))
    wd.check_once(cfg, now=later)
    assert any("RESOLVED" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, wd.STALE_PREFIX + "MXC").state == "OK"


def test_heartbeat_pings_every_loop(capture, fridge):
    capture.reset()
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 293.0, datetime.now(UTC) - timedelta(minutes=1))
    wd.check_once(_cfg(capture, fridge))
    wd.check_once(_cfg(capture, fridge))
    assert capture.beats == 2
    assert capture.slack == []                # nothing to alert on
