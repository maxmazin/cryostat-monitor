"""Unit tests for the watchdog lifecycle alert policy."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from watchdog import watchdog as wd
from watchdog.db import AlertRow, LastSeen, LatestReading

REMINDER = 1800.0
T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(fridges=None, healthchecks_url="https://hc-ping.com/real"):
    return wd.WatchdogConfig(
        check_interval=15,
        reminder_interval=REMINDER,
        healthchecks_url=healthchecks_url,
        slack_webhook_env="CRYO_ALERT_SLACK_WEBHOOK",
        fridges=fridges or [],
    )


def _lifecycle():
    return wd.LifecycleConfig(
        channel="MXC",
        cooling_start_k=280.0,
        base_temperature_k=0.050,
        warming_start_k=0.100,
        room_temperature_k=285.0,
        unit="K",
    )


def _bluefors(staleness_factor=4, poll_interval=60):
    return wd.FridgeConfig(
        name="blackfridge",
        poll_interval=poll_interval,
        staleness_factor=staleness_factor,
        lifecycle=_lifecycle(),
    )


# --------------------------------------------------------------------------- lifecycle state machine
def test_lifecycle_first_observation_baselines_silently():
    d = wd.decide_lifecycle_transition(None, 293.0, _lifecycle(), False, T0)
    assert d.state == wd.PHASE_ROOM
    assert d.notify is None
    assert d.write


def test_lifecycle_room_to_cooling_notifies():
    row = AlertRow(wd.PHASE_ROOM, since=T0, last_notified=None)
    now = T0 + timedelta(minutes=1)
    d = wd.decide_lifecycle_transition(row, 279.0, _lifecycle(), False, now)
    assert d.state == wd.PHASE_COOLING
    assert d.notify == wd.STARTED_COOLING
    assert d.since == now and d.last_notified == now


def test_lifecycle_cooling_to_base_notifies():
    row = AlertRow(wd.PHASE_COOLING, since=T0, last_notified=T0)
    now = T0 + timedelta(hours=8)
    d = wd.decide_lifecycle_transition(row, 0.025, _lifecycle(), False, now)
    assert d.state == wd.PHASE_BASE
    assert d.notify == wd.REACHED_BASE


def test_lifecycle_base_to_warming_notifies():
    row = AlertRow(wd.PHASE_BASE, since=T0, last_notified=T0)
    now = T0 + timedelta(hours=1)
    d = wd.decide_lifecycle_transition(row, 0.15, _lifecycle(), False, now)
    assert d.state == wd.PHASE_WARMING
    assert d.notify == wd.STARTED_WARMING


def test_lifecycle_warming_to_room_notifies():
    row = AlertRow(wd.PHASE_WARMING, since=T0, last_notified=T0)
    now = T0 + timedelta(days=1)
    d = wd.decide_lifecycle_transition(row, 293.0, _lifecycle(), False, now)
    assert d.state == wd.PHASE_ROOM
    assert d.notify == wd.REACHED_ROOM


def test_lifecycle_muted_transition_updates_state_without_slack():
    row = AlertRow(wd.PHASE_ROOM, since=T0, last_notified=None)
    now = T0 + timedelta(minutes=1)
    d = wd.decide_lifecycle_transition(row, 279.0, _lifecycle(), True, now)
    assert d.state == wd.PHASE_COOLING
    assert d.notify is None
    assert d.write


# --------------------------------------------------------------------------- formatting
def test_lifecycle_messages_are_named_milestones():
    ctx = wd.AlertContext(
        "blackfridge",
        wd.LIFECYCLE_KEY,
        value=279.0,
        unit="K",
        data_ts=T0,
        phase="MXC",
    )
    assert "COOLING STARTED" in wd.format_alert(ctx, wd.STARTED_COOLING)
    assert "BASE TEMPERATURE" in wd.format_alert(ctx, wd.REACHED_BASE)
    assert "WARMING STARTED" in wd.format_alert(ctx, wd.STARTED_WARMING)
    assert "ROOM TEMPERATURE" in wd.format_alert(ctx, wd.REACHED_ROOM)


def test_silent_message_format_still_exists_for_status_debugging():
    ctx = wd.AlertContext("blackfridge", "SILENT", age_seconds=300, data_ts=T0)
    msg = wd.format_alert(ctx, wd.RAISE)
    assert "SILENT" in msg and "blackfridge" in msg and "300s" in msg
    assert "2026-06-30 12:00:00 UTC" in msg


# --------------------------------------------------------------------------- loop with fake DB
class FakeDB:
    """In-memory stand-in for watchdog.db."""

    def __init__(self):
        self.muted: set[str] = set()
        self.seen: dict[str, datetime] = {}
        self.data_ts: dict[str, datetime] = {}
        self.readings: dict[tuple[str, str], LatestReading] = {}
        self.state: dict[tuple[str, str], AlertRow] = {}
        self.fail = False
        self.broken: set[str] = set()

    def ping(self):
        if self.fail:
            raise RuntimeError("db down")

    def is_muted(self, fridge):
        if self.fail or fridge in self.broken:
            raise RuntimeError("db down")
        return fridge in self.muted

    def last_seen(self, fridge):
        if self.fail:
            raise RuntimeError("db down")
        received_at = self.seen.get(fridge)
        if received_at is None:
            return None
        return LastSeen(last_ts=self.data_ts.get(fridge, received_at), received_at=received_at)

    def latest_reading(self, fridge, channel):
        return self.readings.get((fridge, channel))

    def get_alert_state(self, fridge, key):
        return self.state.get((fridge, key))

    def upsert_alert_state(self, fridge, key, state, since, last_notified):
        self.state[(fridge, key)] = AlertRow(state, since, last_notified)


@pytest.fixture
def env(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(wd.db, "ping", fake.ping)
    monkeypatch.setattr(wd.db, "is_muted", fake.is_muted)
    monkeypatch.setattr(wd.db, "last_seen", fake.last_seen)
    monkeypatch.setattr(wd.db, "latest_reading", fake.latest_reading)
    monkeypatch.setattr(wd.db, "get_alert_state", fake.get_alert_state)
    monkeypatch.setattr(wd.db, "upsert_alert_state", fake.upsert_alert_state)

    sent: list[str] = []
    monkeypatch.setattr(wd, "send_slack", lambda msg, cfg: sent.append(msg) or True)

    pings: list[bool] = []
    monkeypatch.setattr(wd, "heartbeat", lambda cfg: pings.append(True))
    return fake, sent, pings


def test_fresh_room_temperature_baselines_no_slack_and_heartbeats(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.readings[("blackfridge", "MXC")] = LatestReading(293.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert sent == [] and pings == [True]
    assert fake.state[("blackfridge", wd.LIFECYCLE_KEY)].state == wd.PHASE_ROOM


def test_room_to_cooling_sends_only_lifecycle_slack(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert len(sent) == 1 and "COOLING STARTED" in sent[0]
    assert "THRESHOLD" not in sent[0] and "SILENT" not in sent[0]
    assert fake.state[("blackfridge", wd.LIFECYCLE_KEY)].state == wd.PHASE_COOLING


def test_stale_fridge_records_silent_but_sends_no_slack(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    now = T0 + timedelta(seconds=4 * 60 + 1)
    wd.check_once(_cfg([_bluefors()]), now=now)
    assert sent == []
    assert pings == [True]
    assert fake.state[("blackfridge", "SILENT")].state == "ALERTING"
    assert ("blackfridge", wd.LIFECYCLE_KEY) not in fake.state


def test_never_seen_fridge_records_silent_but_sends_no_slack(env):
    fake, sent, pings = env
    wd.check_once(_cfg([_bluefors()]), now=T0)
    assert sent == []
    assert fake.state[("blackfridge", "SILENT")].state == "ALERTING"


def test_silent_state_clears_without_slack_even_if_old_row_was_notified(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.state[("blackfridge", "SILENT")] = AlertRow(
        "ALERTING", since=T0 - timedelta(hours=1), last_notified=T0 - timedelta(hours=1)
    )
    fake.readings[("blackfridge", "MXC")] = LatestReading(293.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert sent == []
    assert fake.state[("blackfridge", "SILENT")].state == "OK"
    assert fake.state[("blackfridge", "SILENT")].last_notified is None


def test_mute_suppresses_lifecycle_notification_but_updates_phase(env):
    fake, sent, pings = env
    fake.muted.add("blackfridge")
    fake.seen["blackfridge"] = T0
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert sent == []
    assert fake.state[("blackfridge", wd.LIFECYCLE_KEY)].state == wd.PHASE_COOLING


def test_restart_mid_lifecycle_state_does_not_respam(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_COOLING, since=T0, last_notified=T0
    )
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=60))
    assert sent == []


def test_total_db_failure_suppresses_heartbeat(env):
    fake, sent, pings = env
    fake.fail = True
    with pytest.raises(wd.WatchdogError):
        wd.check_once(_cfg([_bluefors()]), now=T0)
    assert pings == []


def test_flaky_fridge_suppresses_heartbeat_but_sends_other_lifecycle_events(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    fake.broken.add("adr_2")
    bad = wd.FridgeConfig("adr_2", 30, 4, lifecycle=_lifecycle())
    wd.check_once(_cfg([_bluefors(), bad]), now=T0 + timedelta(seconds=30))
    assert pings == []
    assert len(sent) == 1 and "COOLING STARTED" in sent[0]


def test_staleness_uses_received_at_not_data_timestamp(env):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.data_ts["blackfridge"] = T0 + timedelta(hours=1)
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    now = T0 + timedelta(seconds=4 * 60 + 1)
    wd.check_once(_cfg([_bluefors()]), now=now)
    assert sent == []
    assert fake.state[("blackfridge", "SILENT")].state == "ALERTING"


def test_missing_lifecycle_channel_warns(env, caplog):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    with caplog.at_level(logging.WARNING, logger="cryo.watchdog"):
        wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert sent == [] and pings == [True]
    assert any("lifecycle channel" in r.message for r in caplog.records)


def test_lifecycle_unit_mismatch_warns(env, caplog):
    fake, sent, pings = env
    fake.seen["blackfridge"] = T0
    fake.readings[("blackfridge", "MXC")] = LatestReading(293.0, T0, "mK")
    with caplog.at_level(logging.WARNING, logger="cryo.watchdog"):
        wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert any("lifecycle unit" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- slack-failure persistence
def test_failed_slack_send_does_not_persist_lifecycle_transition(env, monkeypatch):
    fake, sent, pings = env
    monkeypatch.setattr(wd, "send_slack", lambda msg, cfg: False)
    fake.seen["blackfridge"] = T0
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert fake.state[("blackfridge", wd.LIFECYCLE_KEY)].state == wd.PHASE_ROOM


# --------------------------------------------------------------------------- config
def test_load_config_parses_real_yaml():
    cfg = wd.load_config()
    assert cfg.check_interval == 15 and cfg.reminder_interval == 1800
    names = {f.name for f in cfg.fridges}
    assert names == {"blackfridge", "whitefridge"}
    bf = next(f for f in cfg.fridges if f.name == "blackfridge")
    assert bf.poll_interval == 60 and bf.staleness_factor == 4
    assert bf.lifecycle is not None
    assert bf.lifecycle.channel == "MXC"
    assert bf.lifecycle.cooling_start_k == 280.0
    assert bf.lifecycle.base_temperature_k == 0.050
    assert bf.lifecycle.warming_start_k == 0.100
    assert bf.lifecycle.room_temperature_k == 285.0
    assert cfg.healthchecks_url is None
