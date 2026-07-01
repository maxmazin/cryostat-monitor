"""End-to-end acceptance tests for the watchdog (Phase 2, §10).

Unlike test_watchdog.py (which fakes the DB), these run check_once() through the
REAL db layer against a live PostgreSQL AND real HTTP: the Slack webhook and the
healthchecks.io heartbeat are pointed at a local capture server. This exercises
the whole loop — config, SQL reads/writes, the state machine, Slack delivery,
and the heartbeat — the way it will run on labmanager.

Each §10 acceptance criterion maps to a test below:
  - kill daemon -> SILENT alert          : test_silent_alert_when_data_goes_stale
  - out-of-range value -> THRESHOLD/clear : test_threshold_alert_then_clear
  - maintenance mute -> both suppressed   : test_maintenance_mute_suppresses_alerts
  - restart mid-alert -> no re-spam       : test_restart_mid_alert_does_not_respam
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
                channels={"MXC": wd.ChannelLimits(high=0.05)},
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


def _preset_alert(fridge: str, key: str, *, notified_age_seconds: float) -> None:
    ln = datetime.now(UTC) - timedelta(seconds=notified_age_seconds)
    with db._get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO alert_state (fridge, alert_key, state, since, last_notified) "
            "VALUES (%s, %s, 'ALERTING', %s, %s)",
            (fridge, key, ln, ln),
        )


# --------------------------------------------------------------------------- acceptance tests
def test_silent_alert_when_data_goes_stale(capture, fridge):
    capture.reset()
    # Last data arrived well past staleness_factor * poll_interval (4 * 60 s).
    _set_last_seen(fridge, age_seconds=1000)
    wd.check_once(_cfg(capture, fridge))
    assert any("SILENT" in m for m in capture.slack), capture.slack
    assert capture.beats == 1
    assert db.get_alert_state(fridge, "SILENT").state == "ALERTING"


def test_threshold_alert_then_clear(capture, fridge):
    now = datetime.now(UTC)
    cfg = _cfg(capture, fridge)

    # Fresh data, breaching value -> THRESHOLD alert.
    capture.reset()
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.09, now - timedelta(minutes=2))  # > high=0.05
    wd.check_once(cfg)
    assert any("THRESHOLD" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, "MXC").state == "ALERTING"

    # A newer, in-range reading -> RESOLVED.
    capture.reset()
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.01, now - timedelta(minutes=1))  # newer, within limit
    wd.check_once(cfg)
    assert any("RESOLVED" in m for m in capture.slack), capture.slack
    assert db.get_alert_state(fridge, "MXC").state == "OK"


def test_maintenance_mute_suppresses_alerts(capture, fridge):
    capture.reset()
    _set_last_seen(fridge, age_seconds=1000)  # would be SILENT
    _mute(fridge, minutes=60)
    wd.check_once(_cfg(capture, fridge))
    assert capture.slack == []                # suppressed
    assert capture.beats == 1                 # but still heartbeats
    assert db.get_alert_state(fridge, "SILENT").state == "ALERTING"  # recorded, not sent


def test_restart_mid_alert_does_not_respam(capture, fridge):
    capture.reset()
    # As if a RAISE was paged just before a watchdog restart, still within reminder.
    _preset_alert(fridge, "MXC", notified_age_seconds=60)
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.09, datetime.now(UTC) - timedelta(minutes=1))  # still breaching
    wd.check_once(_cfg(capture, fridge))
    assert capture.slack == []                # no re-page
    assert capture.beats == 1


def test_heartbeat_pings_every_loop(capture, fridge):
    capture.reset()
    _set_last_seen(fridge, age_seconds=5)
    _add_reading(fridge, 0.01, datetime.now(UTC) - timedelta(minutes=1))  # healthy
    wd.check_once(_cfg(capture, fridge))
    wd.check_once(_cfg(capture, fridge))
    assert capture.beats == 2
    assert capture.slack == []                # nothing to alert on
