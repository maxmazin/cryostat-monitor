"""Unit tests for the watchdog: lifecycle alert policy + robustness machinery.

The two pure state machines (decide_lifecycle_transition for milestones,
decide_transition for STALE fault alerts) are tested directly. The loop
(check_once) is tested with the db layer faked and Slack/heartbeat captured, so
we exercise staleness, lifecycle transitions, STALE detection, clear-hold flap
damping, muting, restart-no-respam, Slack-failure escalation, and the
dead-man's-switch suppression without a live PostgreSQL or HTTP.
"""
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


# --------------------------------------------------------------------------- fault state machine (STALE)
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


def test_clear_while_muted_is_deferred_until_unmute():
    # A RAISE was paged (last_notified set) before the mute; recovery WHILE muted
    # must not announce mid-maintenance. State is held ALERTING (no write, no
    # notify) so the CLEAR is delivered once the mute lifts.
    row = AlertRow("ALERTING", since=T0, last_notified=T0)
    now = T0 + timedelta(seconds=60)
    d = wd.decide_transition(row, bad=False, muted=True, now=now, reminder_interval=REMINDER)
    assert d.state == "ALERTING" and d.notify is None and not d.write
    # Once unmuted and still recovered, the held incident finally clears.
    d2 = wd.decide_transition(row, bad=False, muted=False, now=now, reminder_interval=REMINDER)
    assert d2.state == "OK" and d2.notify == wd.CLEAR and d2.write


def test_clear_while_muted_is_silent_if_never_paged():
    # Alert only ever existed during the mute (never paged) -> clear silently.
    row = AlertRow("ALERTING", since=T0, last_notified=None)
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


def test_stale_message_names_channel_and_age():
    ctx = wd.AlertContext("blackfridge", "STALE:MXC", age_seconds=600, data_ts=T0)
    msg = wd.format_alert(ctx, wd.RAISE)
    assert "STALE" in msg and "MXC" in msg and "600s" in msg
    assert "RESOLVED" not in msg
    assert "(reminder)" in wd.format_alert(ctx, wd.REMIND)
    assert "RESOLVED" in wd.format_alert(ctx, wd.CLEAR)


def test_stale_message_tolerates_missing_age_and_ts():
    # Defense in depth: a sparse context must degrade, not raise (a crash in
    # format_alert would drop every later pending notification that pass).
    ctx = wd.AlertContext("blackfridge", "STALE:MXC")
    msg = wd.format_alert(ctx, wd.RAISE)
    assert "STALE" in msg and "unknown" in msg and "never" in msg


# --------------------------------------------------------------------------- loop with fake DB
class FakeDB:
    """In-memory stand-in for watchdog.db."""

    def __init__(self):
        self.muted: set[str] = set()
        self.seen: dict[str, datetime] = {}       # received_at (staleness basis)
        self.data_ts: dict[str, datetime] = {}    # last_ts override (defaults to seen)
        self.readings: dict[tuple[str, str], LatestReading] = {}
        self.state: dict[tuple[str, str], AlertRow] = {}
        self.fail = False  # raise on every read to simulate a DB outage
        self.broken: set[str] = set()  # raise only for these fridges

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

    def list_alert_keys(self):
        return list(self.state.keys())


@pytest.fixture
def env(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(wd.db, "ping", fake.ping)
    monkeypatch.setattr(wd.db, "is_muted", fake.is_muted)
    monkeypatch.setattr(wd.db, "last_seen", fake.last_seen)
    monkeypatch.setattr(wd.db, "latest_reading", fake.latest_reading)
    monkeypatch.setattr(wd.db, "get_alert_state", fake.get_alert_state)
    monkeypatch.setattr(wd.db, "upsert_alert_state", fake.upsert_alert_state)
    monkeypatch.setattr(wd.db, "list_alert_keys", fake.list_alert_keys)

    # Fresh in-memory loop state (Slack-failure counter, clear-hold markers).
    monkeypatch.setattr(wd, "_runtime", wd._RuntimeState())

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
    assert pings == []  # dead-man's switch will fire


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


def _write_yaml(tmp_path, body: str):
    path = tmp_path / "fridges.yaml"
    path.write_text(body)
    return path


LIFECYCLE_YAML = """\
fridges:
  blackfridge:
    poll_interval: 60
    staleness_factor: 4
    lifecycle:
%s
"""


def test_load_config_raises_on_zero_fridges(tmp_path):
    # An indentation slip or misspelled 'fridges:' key must not yield a watchdog
    # that heartbeats while monitoring nothing.
    path = _write_yaml(tmp_path, "watchdog:\n  check_interval: 15\n")
    with pytest.raises(wd.ConfigError, match="no fridges"):
        wd.load_config(path)


def test_load_config_rejects_unknown_lifecycle_key(tmp_path):
    # A typo would silently fall back to the LifecycleConfig default and disarm
    # the intended trigger temperature.
    path = _write_yaml(tmp_path, LIFECYCLE_YAML % "      warming_stat_k: 0.1")
    with pytest.raises(wd.ConfigError, match=r"blackfridge.*warming_stat_k"):
        wd.load_config(path)


def test_load_config_rejects_disordered_lifecycle_temperatures(tmp_path):
    body = LIFECYCLE_YAML % "      base_temperature_k: 0.2\n      warming_start_k: 0.1"
    path = _write_yaml(tmp_path, body)
    with pytest.raises(wd.ConfigError, match=r"blackfridge.*base_temperature_k"):
        wd.load_config(path)


# --------------------------------------------------------------------------- startup fail-fast
def test_startup_raises_when_slack_webhook_env_unset(monkeypatch):
    monkeypatch.delenv("CRYO_ALERT_SLACK_WEBHOOK", raising=False)
    with pytest.raises(wd.ConfigError, match="CRYO_ALERT_SLACK_WEBHOOK"):
        wd.require_runtime_env(_cfg())


def test_startup_raises_when_healthchecks_url_missing(monkeypatch):
    monkeypatch.setenv("CRYO_ALERT_SLACK_WEBHOOK", "https://hooks.slack.example/x")
    with pytest.raises(wd.ConfigError, match="healthchecks"):
        wd.require_runtime_env(_cfg(healthchecks_url=None))


def test_startup_ok_when_endpoints_configured(monkeypatch):
    monkeypatch.setenv("CRYO_ALERT_SLACK_WEBHOOK", "https://hooks.slack.example/x")
    wd.require_runtime_env(_cfg())  # no raise


# --------------------------------------------------------------------------- slack-failure escalation
def test_consecutive_slack_failures_suppress_heartbeat_until_success(env, monkeypatch, caplog):
    fake, sent, pings = env
    # A ROOM -> COOLING event keeps retrying every pass while sends fail (the
    # transition is only persisted after a successful send).
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    fake.seen["blackfridge"] = T0
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    delivered = {"ok": False}
    monkeypatch.setattr(wd, "send_slack", lambda msg, cfg: delivered["ok"])
    cfg = _cfg([_bluefors()])
    now = T0 + timedelta(seconds=30)

    # Three failed sends. The beat fires on those passes (a failure is only known
    # after the pre-send heartbeat), but the counter reaches the threshold...
    for _ in range(3):
        wd.check_once(cfg, now=now)
    assert pings == [True, True, True]
    # ...so every following pass suppresses the heartbeat and says why.
    with caplog.at_level(logging.ERROR, logger="cryo.watchdog"):
        wd.check_once(cfg, now=now)
    assert pings == [True, True, True]
    assert any("consecutive Slack send failures" in r.message for r in caplog.records)

    # A successful send resets the counter; the heartbeat resumes next pass.
    delivered["ok"] = True
    wd.check_once(cfg, now=now)   # send succeeds, but this pass was already suppressed
    assert pings == [True, True, True]
    wd.check_once(cfg, now=now)
    assert pings == [True, True, True, True]


def test_suppressed_heartbeat_self_resolves_via_probe(env, monkeypatch):
    fake, sent, pings = env
    cfg = _cfg([_bluefors()])
    delivered = {"ok": False}
    sends: list[tuple[str, bool]] = []

    def fake_send(msg, _cfg):
        sends.append((msg, delivered["ok"]))
        return delivered["ok"]

    monkeypatch.setattr(wd, "send_slack", fake_send)
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )

    def one_pass(now, value):
        fake.seen["blackfridge"] = now - timedelta(seconds=5)
        fake.readings[("blackfridge", "MXC")] = LatestReading(
            value, now - timedelta(seconds=5), "K")
        wd.check_once(cfg, now=now)

    # Webhook dead during a ROOM -> COOLING event: three failed sends trip
    # suppression (the transition is never persisted, so each pass retries it).
    for i in range(3):
        one_pass(T0 + timedelta(seconds=15 * i), 279.0)
    assert pings == [True] * 3 and len(sends) == 3

    # The fridge warms back above cooling_start while the webhook is still down:
    # no transition pending anymore, so a probe is attempted; it fails ->
    # suppression (correctly) persists.
    t_probe1 = T0 + timedelta(seconds=60)
    one_pass(t_probe1, 293.0)
    assert pings == [True] * 3
    assert len(sends) == 4 and "restored" in sends[3][0]

    # Probes are throttled: a pass inside the probe interval sends nothing.
    one_pass(t_probe1 + timedelta(seconds=15), 293.0)
    assert len(sends) == 4 and pings == [True] * 3

    # Webhook recovers: the next due probe succeeds -> counter resets and the
    # heartbeat resumes on the same pass, with exactly one restored message.
    delivered["ok"] = True
    one_pass(t_probe1 + timedelta(seconds=wd.SLACK_PROBE_INTERVAL_SECONDS), 293.0)
    assert pings == [True] * 4
    assert len(sends) == 5
    assert [m for m, ok in sends if ok and "restored" in m] == [sends[4][0]]

    # Steady healthy state: no further probes.
    one_pass(t_probe1 + timedelta(seconds=wd.SLACK_PROBE_INTERVAL_SECONDS + 15), 293.0)
    assert pings == [True] * 5 and len(sends) == 5


# --------------------------------------------------------------------------- per-channel STALE
def test_frozen_lifecycle_channel_raises_stale_and_skips_lifecycle(env):
    fake, sent, pings = env
    now = T0 + timedelta(seconds=1000)
    fake.seen["blackfridge"] = now - timedelta(seconds=5)  # fridge itself is fresh
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    # The lifecycle channel froze past the SILENT window (4*60 s) at a value that
    # WOULD page STARTED_COOLING: it must page STALE instead — a transition
    # inferred from a frozen reading would be fiction.
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")
    wd.check_once(_cfg([_bluefors()]), now=now)
    assert len(sent) == 1 and "STALE" in sent[0] and "COOLING" not in sent[0]
    assert fake.state[("blackfridge", "STALE:MXC")].state == "ALERTING"
    assert fake.state[("blackfridge", wd.LIFECYCLE_KEY)].state == wd.PHASE_ROOM  # unchanged


def test_stale_clears_after_hold_when_fresh_data_returns(env):
    fake, sent, pings = env
    cfg = _cfg([_bluefors()])
    fake.state[("blackfridge", "STALE:MXC")] = AlertRow("ALERTING", since=T0, last_notified=T0)
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_BASE, since=T0, last_notified=None)
    # Fresh data returns: the RESOLVED is held (flap damping), state untouched...
    t1 = T0 + timedelta(seconds=60)
    fake.seen["blackfridge"] = t1 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t1 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t1)
    assert sent == []
    assert fake.state[("blackfridge", "STALE:MXC")].state == "ALERTING"
    # ...and a single RESOLVED goes out once continuously fresh past the hold.
    t2 = t1 + timedelta(seconds=wd.CLEAR_HOLD_SECONDS)
    fake.seen["blackfridge"] = t2 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t2 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t2)
    assert len(sent) == 1 and "RESOLVED" in sent[0]
    assert fake.state[("blackfridge", "STALE:MXC")].state == "OK"


def test_flapping_staleness_raises_once_and_never_clears_mid_flap(env):
    fake, sent, pings = env
    cfg = _cfg([_bluefors()])
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_BASE, since=T0, last_notified=None)
    # Reading age hovering around the 240 s staleness window every pass: one
    # STALE RAISE, zero RESOLVED (the clear-hold absorbs the fresh blips).
    for i, age in enumerate([300, 30, 300, 30, 300, 30]):
        now = T0 + timedelta(seconds=600 + 15 * i)
        fake.seen["blackfridge"] = now - timedelta(seconds=5)
        fake.readings[("blackfridge", "MXC")] = LatestReading(
            0.01, now - timedelta(seconds=age), "K")
        wd.check_once(cfg, now=now)
    assert len(sent) == 1 and "STALE" in sent[0]
    assert fake.state[("blackfridge", "STALE:MXC")].state == "ALERTING"


def test_fridge_silence_voids_stale_clear_hold(env):
    fake, sent, pings = env
    cfg = _cfg([_bluefors()])
    fake.state[("blackfridge", "STALE:MXC")] = AlertRow("ALERTING", since=T0, last_notified=T0)
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_BASE, since=T0, last_notified=None)
    # 1) Fresh pass: the clear-hold starts.
    t1 = T0 + timedelta(seconds=15)
    fake.seen["blackfridge"] = t1 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t1 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t1)
    assert sent == []
    # 2) The WHOLE fridge goes silent for 20 minutes (blind window).
    t2 = t1 + timedelta(seconds=1200)
    wd.check_once(cfg, now=t2)  # last_seen still ~t1 -> fridge stale
    assert fake.state[("blackfridge", "SILENT")].state == "ALERTING"
    # 3) The fridge resumes with fresh data: the RESOLVED must NOT fire yet —
    # the hold restarts from the resume; blind time earns no credit.
    t3 = t2 + timedelta(seconds=15)
    fake.seen["blackfridge"] = t3 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t3 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t3)
    assert sent == []
    assert fake.state[("blackfridge", "STALE:MXC")].state == "ALERTING"
    # 4) The hold elapses from the resume -> single RESOLVED.
    t4 = t3 + timedelta(seconds=wd.CLEAR_HOLD_SECONDS)
    fake.seen["blackfridge"] = t4 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t4 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t4)
    assert len(sent) == 1 and "RESOLVED" in sent[0]
    assert fake.state[("blackfridge", "STALE:MXC")].state == "OK"


# --------------------------------------------------------------------------- held clear must not notify
def test_held_stale_clear_past_reminder_stays_silent_and_others_deliver(env):
    fake, sent, pings = env
    other = wd.FridgeConfig("whitefridge", 60, 4, lifecycle=_lifecycle())
    cfg = _cfg([_bluefors(), other])
    # blackfridge's STALE:MXC alerted long ago (past a reminder window) and fresh
    # data just returned: the held clear must not REMIND (nothing is stale right
    # now) nor crash — whitefridge's genuine lifecycle event must still deliver.
    now = T0 + timedelta(seconds=REMINDER + 60)
    fake.state[("blackfridge", "STALE:MXC")] = AlertRow("ALERTING", since=T0, last_notified=T0)
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_BASE, since=T0, last_notified=None)
    fake.seen["blackfridge"] = now - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, now - timedelta(seconds=5), "K")
    fake.state[("whitefridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_ROOM, since=T0, last_notified=None)
    fake.seen["whitefridge"] = now - timedelta(seconds=5)
    fake.readings[("whitefridge", "MXC")] = LatestReading(279.0, now - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=now)
    assert len(sent) == 1 and "COOLING STARTED" in sent[0] and "whitefridge" in sent[0]
    assert fake.state[("blackfridge", "STALE:MXC")].state == "ALERTING"  # held, untouched


def test_held_stale_clear_after_mute_lifts_does_not_belatedly_raise(env):
    fake, sent, pings = env
    cfg = _cfg([_bluefors()])
    # STALE raised under mute (never announced: last_notified None), data went
    # fresh, and the mute lifted inside the hold window: a fresh channel must
    # NOT be paged as a belated RAISE.
    fake.state[("blackfridge", "STALE:MXC")] = AlertRow("ALERTING", since=T0, last_notified=None)
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_BASE, since=T0, last_notified=None)
    t1 = T0 + timedelta(seconds=60)
    fake.seen["blackfridge"] = t1 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t1 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t1)
    assert sent == []
    assert fake.state[("blackfridge", "STALE:MXC")].state == "ALERTING"
    # Once the hold elapses, the never-announced alert clears silently.
    t2 = t1 + timedelta(seconds=wd.CLEAR_HOLD_SECONDS)
    fake.seen["blackfridge"] = t2 - timedelta(seconds=5)
    fake.readings[("blackfridge", "MXC")] = LatestReading(0.01, t2 - timedelta(seconds=5), "K")
    wd.check_once(cfg, now=t2)
    assert sent == []
    assert fake.state[("blackfridge", "STALE:MXC")].state == "OK"


# --------------------------------------------------------------------------- dangling alert_state
def test_dangling_alert_state_rows_are_warned(env, caplog):
    fake, sent, pings = env
    fake.state[("ghostfridge", "SILENT")] = AlertRow("ALERTING", T0, T0)       # fridge gone
    fake.state[("blackfridge", "MXC")] = AlertRow("OK", T0, None)              # threshold-era key
    fake.state[("blackfridge", "STALE:OLD_CH")] = AlertRow("OK", T0, None)     # not the lifecycle channel
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(wd.PHASE_BASE, T0, None)  # valid
    fake.state[("blackfridge", "STALE:MXC")] = AlertRow("OK", T0, None)        # valid
    fake.state[("blackfridge", "SILENT")] = AlertRow("OK", T0, None)           # valid
    with caplog.at_level(logging.WARNING, logger="cryo.watchdog"):
        wd.warn_dangling_alert_state(_cfg([_bluefors()]))
    msgs = [r.getMessage() for r in caplog.records if "alert_state row" in r.message]
    assert len(msgs) == 3
    assert any("ghostfridge" in m for m in msgs)
    assert any("(blackfridge, MXC)" in m for m in msgs)
    assert any("STALE:OLD_CH" in m for m in msgs)


# --------------------------------------------------------------------------- loop-error logging
def test_loop_error_message_reflects_whether_heartbeat_fired(monkeypatch, caplog):
    monkeypatch.setattr(wd, "_runtime", wd._RuntimeState())
    with caplog.at_level(logging.ERROR, logger="cryo.watchdog"):
        wd._runtime.heartbeat_attempted = False
        wd._log_loop_error(RuntimeError("pre-beat boom"))
        wd._runtime.heartbeat_attempted = True
        wd._log_loop_error(RuntimeError("post-beat boom"))
    pre, post = [r.getMessage() for r in caplog.records]
    assert "heartbeat skipped" in pre
    assert "skipped" not in post and "after the heartbeat" in post


def test_error_after_heartbeat_is_not_reported_as_skipped(env, monkeypatch):
    fake, sent, pings = env
    fake.state[("blackfridge", wd.LIFECYCLE_KEY)] = AlertRow(
        wd.PHASE_ROOM, since=T0, last_notified=None
    )
    fake.seen["blackfridge"] = T0
    fake.readings[("blackfridge", "MXC")] = LatestReading(279.0, T0, "K")  # -> event pending

    def boom(*args, **kwargs):
        raise RuntimeError("alert_state upsert failed")

    # The upsert in _send_pending happens AFTER the heartbeat; the loop-level
    # log must not then claim the beat was skipped.
    monkeypatch.setattr(wd.db, "upsert_alert_state", boom)
    with pytest.raises(RuntimeError):
        wd.check_once(_cfg([_bluefors()]), now=T0 + timedelta(seconds=30))
    assert pings == [True]
    assert wd._runtime.heartbeat_attempted
