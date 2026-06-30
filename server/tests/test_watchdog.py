"""Unit tests for the watchdog (§8).

The state machine (decide_transition) is pure and tested directly. The loop
(check_once / transition) is tested with the db layer faked and Slack/heartbeat
captured, so we exercise staleness, thresholds, muting, restart-no-respam, and
the dead-man's-switch suppression without a live PostgreSQL or HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from watchdog import watchdog as wd
from watchdog.db import AlertRow, LatestReading

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


# --------------------------------------------------------------------------- state machine
def test_ok_to_bad_raises():
    d = wd.decide_transition(None, bad=True, muted=False, now=T0, reminder_interval=REMINDER)
    assert d.state == "ALERTING" and d.notify == wd.RAISE and d.write
    assert d.since == T0 and d.last_notified == T0


def test_ok_to_bad_while_muted_records_state_but_does_not_notify():
    d = wd.decide_transition(None, bad=True, muted=True, now=T0, reminder_interval=REMINDER)
    assert d.state == "ALERTING" and d.notify is None and d.write
    assert d.last_notified is None  # never announced


def test_alerting_within_reminder_interval_is_silent():
    row = AlertRow("ALERTING", since=T0, last_notified=T0)
    now = T0 + timedelta(seconds=REMINDER - 1)
    d = wd.decide_transition(row, bad=True, muted=False, now=now, reminder_interval=REMINDER)
    assert d.notify is None and not d.write
    assert d.last_notified == T0  # unchanged


def test_alerting_past_reminder_interval_reminds():
    row = AlertRow("ALERTING", since=T0, last_notified=T0)
    now = T0 + timedelta(seconds=REMINDER + 1)
    d = wd.decide_transition(row, bad=True, muted=False, now=now, reminder_interval=REMINDER)
    assert d.notify == wd.REMIND and d.write
    assert d.last_notified == now and d.since == T0  # since preserved across reminders


def test_alerting_to_ok_clears():
    row = AlertRow("ALERTING", since=T0, last_notified=T0)
    now = T0 + timedelta(seconds=60)
    d = wd.decide_transition(row, bad=False, muted=False, now=now, reminder_interval=REMINDER)
    assert d.state == "OK" and d.notify == wd.CLEAR and d.write and d.last_notified is None


def test_clear_while_muted_is_silent():
    row = AlertRow("ALERTING", since=T0, last_notified=T0)
    now = T0 + timedelta(seconds=60)
    d = wd.decide_transition(row, bad=False, muted=True, now=now, reminder_interval=REMINDER)
    assert d.state == "OK" and d.notify is None and d.write


def test_mute_then_unmute_belated_raise_not_reminder():
    # Went bad while muted (last_notified None), mute now lifted, still bad.
    row = AlertRow("ALERTING", since=T0, last_notified=None)
    now = T0 + timedelta(seconds=5)
    d = wd.decide_transition(row, bad=True, muted=False, now=now, reminder_interval=REMINDER)
    assert d.notify == wd.RAISE and d.last_notified == now


def test_alerting_still_bad_while_muted_no_write_no_notify():
    row = AlertRow("ALERTING", since=T0, last_notified=T0)
    now = T0 + timedelta(seconds=REMINDER + 1)
    d = wd.decide_transition(row, bad=True, muted=True, now=now, reminder_interval=REMINDER)
    assert d.notify is None and not d.write


def test_steady_ok_no_row_is_noop():
    d = wd.decide_transition(None, bad=False, muted=False, now=T0, reminder_interval=REMINDER)
    assert d.state == "OK" and d.notify is None and not d.write


# --------------------------------------------------------------------------- formatting
def test_silent_message_is_distinct_and_has_details():
    ctx = wd.AlertContext("bluefors_1", "SILENT", "SILENT", age_seconds=300, data_ts=T0)
    msg = wd.format_alert(ctx, wd.RAISE)
    assert "SILENT" in msg and "bluefors_1" in msg and "300s" in msg
    assert "2026-06-30 12:00:00 UTC" in msg


def test_threshold_message_has_value_limit_and_ts():
    ctx = wd.AlertContext("bluefors_1", "MXC", "THRESHOLD", value=0.08,
                          limit=0.05, bound="high", unit="K", data_ts=T0)
    msg = wd.format_alert(ctx, wd.RAISE)
    assert "MXC" in msg and "0.08" in msg and "0.05" in msg and "high" in msg


def test_reminder_message_marked():
    ctx = wd.AlertContext("adr_2", "4K", "THRESHOLD", value=9.0, limit=5.5,
                          bound="high", unit="K", data_ts=T0)
    assert "(reminder)" in wd.format_alert(ctx, wd.REMIND)


def test_clear_message():
    ctx = wd.AlertContext("adr_2", "4K", "THRESHOLD", value=4.0, unit="K")
    assert "RESOLVED" in wd.format_alert(ctx, wd.CLEAR)


# --------------------------------------------------------------------------- loop with fake DB
class FakeDB:
    """In-memory stand-in for watchdog.db."""

    def __init__(self):
        self.muted: set[str] = set()
        self.seen: dict[str, datetime] = {}
        self.readings: dict[tuple[str, str], LatestReading] = {}
        self.state: dict[tuple[str, str], AlertRow] = {}
        self.fail = False  # raise on every read to simulate a DB outage
        self.broken: set[str] = set()  # raise only for these fridges

    def is_muted(self, fridge):
        if self.fail or fridge in self.broken:
            raise RuntimeError("db down")
        return fridge in self.muted

    def last_seen(self, fridge):
        if self.fail:
            raise RuntimeError("db down")
        return self.seen.get(fridge)

    def latest_reading(self, fridge, channel):
        return self.readings.get((fridge, channel))

    def get_alert_state(self, fridge, key):
        return self.state.get((fridge, key))

    def upsert_alert_state(self, fridge, key, state, since, last_notified):
        self.state[(fridge, key)] = AlertRow(state, since, last_notified)


@pytest.fixture
def env(monkeypatch):
    fake = FakeDB()
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


def _bluefors(staleness_factor=4, poll_interval=60):
    return wd.FridgeConfig(
        name="bluefors_1", poll_interval=poll_interval, staleness_factor=staleness_factor,
        channels={"MXC": wd.ChannelLimits(high=0.05)},
    )


def test_fresh_data_in_range_no_alert_and_heartbeats(env):
    fake, sent, pings = env
    fake.seen["bluefors_1"] = T0
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.01, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert sent == [] and pings == [True]


def test_stale_fridge_raises_silent_and_skips_thresholds(env):
    fake, sent, pings = env
    fake.seen["bluefors_1"] = T0
    # Even though MXC is breaching, a stale fridge only pages SILENT.
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.09, T0, "K")
    now = T0 + timedelta(seconds=4 * 60 + 1)  # past staleness_factor * poll_interval
    wd.check_once(_cfg([_bluefors()]), now=now)
    assert len(sent) == 1 and "SILENT" in sent[0]
    assert fake.state[("bluefors_1", "SILENT")].state == "ALERTING"
    assert ("bluefors_1", "MXC") not in fake.state  # threshold skipped while silent


def test_never_seen_fridge_is_silent(env):
    fake, sent, pings = env
    wd.check_once(_cfg([_bluefors()]), now=T0)
    assert len(sent) == 1 and "SILENT" in sent[0]


def test_threshold_breach_raises(env):
    fake, sent, pings = env
    fake.seen["bluefors_1"] = T0
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.09, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert len(sent) == 1 and "THRESHOLD" in sent[0]
    assert fake.state[("bluefors_1", "MXC")].state == "ALERTING"


def test_mute_suppresses_both_alert_types(env):
    fake, sent, pings = env
    fake.muted.add("bluefors_1")
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.09, T0, "K")
    # never seen -> would be SILENT, but muted
    wd.check_once(_cfg([_bluefors()]), now=T0)
    assert sent == []
    assert fake.state[("bluefors_1", "SILENT")].state == "ALERTING"  # recorded, not sent


def test_restart_mid_alert_does_not_respam(env):
    fake, sent, pings = env
    # Persisted ALERTING from before a restart; still breaching, within reminder.
    fake.state[("bluefors_1", "MXC")] = AlertRow("ALERTING", since=T0, last_notified=T0)
    fake.seen["bluefors_1"] = T0
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.09, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=60))
    assert sent == []  # no re-page


def test_recovery_after_breach_clears(env):
    fake, sent, pings = env
    fake.state[("bluefors_1", "MXC")] = AlertRow("ALERTING", since=T0, last_notified=T0)
    fake.seen["bluefors_1"] = T0
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.01, T0, "K")  # back in range
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=60))
    assert len(sent) == 1 and "RESOLVED" in sent[0]
    assert fake.state[("bluefors_1", "MXC")].state == "OK"


def test_total_db_failure_suppresses_heartbeat(env):
    fake, sent, pings = env
    fake.fail = True
    with pytest.raises(wd.WatchdogError):
        wd.check_once(_cfg([_bluefors()]), now=T0)
    assert pings == []  # dead-man's switch will fire


def test_one_flaky_fridge_does_not_stop_heartbeat(env):
    fake, sent, pings = env
    fake.seen["bluefors_1"] = T0
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.01, T0, "K")
    fake.broken.add("adr_2")  # adr_2's reads raise; bluefors_1 is fine
    bad = wd.FridgeConfig("adr_2", 30, 4, {"4K": wd.ChannelLimits(high=5.5)})
    wd.check_once(_cfg([_bluefors(), bad]), now=T0 + timedelta(seconds=30))
    assert pings == [True]  # one fridge failed, heartbeat still fired


# --------------------------------------------------------------------------- slack-failure persistence
def test_failed_slack_send_does_not_persist_state(env, monkeypatch):
    fake, sent, pings = env
    monkeypatch.setattr(wd, "send_slack", lambda msg, cfg: False)  # delivery fails
    fake.seen["bluefors_1"] = T0
    fake.readings[("bluefors_1", "MXC")] = LatestReading(0.09, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    # state NOT written, so the next loop retries the RAISE
    assert ("bluefors_1", "MXC") not in fake.state


# --------------------------------------------------------------------------- config
def test_load_config_parses_real_yaml():
    cfg = wd.load_config()
    assert cfg.check_interval == 15 and cfg.reminder_interval == 1800
    names = {f.name for f in cfg.fridges}
    assert {"bluefors_1", "adr_2"} <= names
    bf = next(f for f in cfg.fridges if f.name == "bluefors_1")
    assert bf.poll_interval == 60 and bf.staleness_factor == 4
    assert bf.channels["MXC"].high == 0.05
    # placeholder healthchecks URL is treated as unset
    assert cfg.healthchecks_url is None
