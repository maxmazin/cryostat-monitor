"""Watchdog — the safety core (§8). Plain Python, deterministic, NO LLM.

Loop every check_interval:
  - staleness check per fridge (PRIMARY: do not infer lifecycle from stale data)
  - lifecycle checks per fridge (cooling/base/warming/room notifications), with
    per-channel STALE detection — a frozen lifecycle reading must not keep the
    phase (and the operators) believing nothing changed — and clear-hold flap
    damping on STALE recovery (CLEAR_HOLD_SECONDS)
  - maintenance mute (duration-capped, set via the /maintenance ingest endpoint)
  - persisted alert_state so a restart neither re-spams nor forgets (§8)
  - heartbeat to healthchecks.io every loop (dead-man's switch, §5/§8),
    suppressed after consecutive failed Slack sends so a dead webhook pages
    instead of dropping alerts behind a green heartbeat

Startup fails fast (ConfigError) on a missing Slack webhook env var, a missing
healthchecks URL, or a config that parses to zero fridges: under systemd that
crash-loops, the heartbeat goes silent, and healthchecks.io pages (§8).

Two pure state machines, both fully tested: decide_lifecycle_transition for
operational milestones and decide_transition for fault alerts (per-channel
STALE). All I/O lives at the edges: db.* for reads/writes, send_slack/heartbeat
for HTTP.

Run as a module so relative imports resolve:  python -m watchdog.watchdog
(see systemd/cryo-watchdog.service).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import db
from .db import AlertRow

log = logging.getLogger("cryo.watchdog")

# Notification actions emitted by the fault state machine (STALE alerts).
RAISE = "raise"
REMIND = "remind"
CLEAR = "clear"

LIFECYCLE_KEY = "LIFECYCLE"
PHASE_ROOM = "ROOM"
PHASE_COOLING = "COOLING"
PHASE_BASE = "BASE"
PHASE_WARMING = "WARMING"
_LIFECYCLE_PHASES = {PHASE_ROOM, PHASE_COOLING, PHASE_BASE, PHASE_WARMING}

STARTED_COOLING = "started_cooling"
REACHED_BASE = "reached_base"
STARTED_WARMING = "started_warming"
REACHED_ROOM = "reached_room"

# A configured healthchecks_url still holding the template placeholder is treated
# as unset, so we never ping a bogus check URL.
_HC_PLACEHOLDER = "REPLACE-WITH-UUID"

_HTTP_TIMEOUT = 10  # seconds, for Slack/healthchecks calls

# Alert-key prefix for per-channel staleness ("frozen reading") alerts on the
# lifecycle channel, distinct from the fridge-level SILENT key. Fits the
# existing alert_state key scheme (alert_key is free text) — no schema change.
STALE_PREFIX = "STALE:"

# Flap damping: an ALERTING STALE alert only CLEARs after the channel has been
# continuously fresh for this long. Without it, a channel reporting near the
# staleness-window cadence would emit a RAISE/RESOLVED pair every few passes,
# burying real alerts. 300 s spans many check intervals (15 s default): long
# enough to absorb boundary flapping, short enough that a genuine recovery is
# announced promptly.
CLEAR_HOLD_SECONDS = 300

# After this many CONSECUTIVE failed Slack sends, the heartbeat is suppressed so
# healthchecks.io pages: alerts are being dropped (dead webhook, Slack outage)
# and the watchdog must not keep reporting itself healthy (§8). Only attempted
# sends count — quiet passes neither increment nor reset the counter.
SLACK_FAILURES_TO_SUPPRESS_HEARTBEAT = 3

# While the heartbeat is suppressed for Slack failures and nothing is pending to
# retry the webhook, a probe message is attempted at most this often so the
# suppression can self-resolve once Slack recovers (see _probe_slack).
SLACK_PROBE_INTERVAL_SECONDS = 300


class WatchdogError(Exception):
    """Raised to abort a loop iteration when the watchdog cannot do its job
    (e.g. the database is unreachable for every fridge). Aborting suppresses the
    heartbeat so the healthchecks.io dead-man's switch fires (§8)."""


class ConfigError(Exception):
    """Raised for invalid or unsafe configuration. Startup lets this propagate
    so a misconfigured watchdog crash-loops under systemd instead of running
    with disarmed alerts — the heartbeat goes silent and healthchecks pages (§8)."""


@dataclass
class _RuntimeState:
    """In-memory loop state. Deliberately NOT persisted: a restart resets the
    Slack-failure counter (the next failed sends re-trip it) and restarts any
    clear-hold window (errs toward staying alerted) — both safe and deterministic."""

    consecutive_slack_failures: int = 0
    # (fridge, alert_key) -> when the channel was first observed fresh again.
    in_range_since: dict[tuple[str, str], datetime] = field(default_factory=dict)
    heartbeat_attempted: bool = False  # did the CURRENT pass reach the heartbeat call?
    last_slack_probe_at: datetime | None = None  # throttles _probe_slack


_runtime = _RuntimeState()


# --------------------------------------------------------------------------- config
@dataclass
class LifecycleConfig:
    channel: str = "MXC"
    cooling_start_k: float = 280.0
    base_temperature_k: float = 0.050
    warming_start_k: float = 0.100
    room_temperature_k: float = 285.0
    unit: str = "K"


_LIFECYCLE_CONFIG_KEYS = {
    "channel", "cooling_start_k", "base_temperature_k",
    "warming_start_k", "room_temperature_k", "unit",
}


@dataclass
class FridgeConfig:
    name: str
    poll_interval: float
    staleness_factor: float
    lifecycle: LifecycleConfig | None = None


@dataclass
class WatchdogConfig:
    check_interval: float
    reminder_interval: float
    healthchecks_url: str | None
    slack_webhook_env: str
    fridges: list[FridgeConfig]


def _config_path() -> Path:
    override = os.environ.get("CRYO_FRIDGES_CONFIG")
    if override:
        return Path(override)
    # server/watchdog/watchdog.py -> server/config/fridges.yaml
    return Path(__file__).resolve().parent.parent / "config" / "fridges.yaml"


def load_config(path: Path | None = None) -> WatchdogConfig:
    """Parse fridges.yaml into a WatchdogConfig (§3.3: tuning lives in config)."""
    path = path or _config_path()
    raw = yaml.safe_load(path.read_text())

    wd = raw.get("watchdog", {})
    hc_url = os.environ.get("CRYO_HEALTHCHECKS_URL") or wd.get("healthchecks_url")
    if hc_url and _HC_PLACEHOLDER in hc_url:
        hc_url = None

    slack_env = raw.get("slack", {}).get("webhook_url_env", "CRYO_ALERT_SLACK_WEBHOOK")

    fridges: list[FridgeConfig] = []
    for name, spec in (raw.get("fridges") or {}).items():
        lifecycle = None
        if spec.get("lifecycle"):
            lc = spec["lifecycle"]
            # A misspelled key ({"warming_stat_k": 0.1}) would otherwise silently
            # fall back to the dataclass default and disarm the intended trigger.
            unknown = set(lc) - _LIFECYCLE_CONFIG_KEYS
            if unknown:
                raise ConfigError(
                    f"{path}: fridge {name!r} lifecycle has unknown key(s) "
                    f"{sorted(unknown)} (allowed: {sorted(_LIFECYCLE_CONFIG_KEYS)})"
                )
            lifecycle = LifecycleConfig(
                channel=lc.get("channel", "MXC"),
                cooling_start_k=float(lc.get("cooling_start_k", 280.0)),
                base_temperature_k=float(lc.get("base_temperature_k", 0.050)),
                warming_start_k=float(lc.get("warming_start_k", 0.100)),
                room_temperature_k=float(lc.get("room_temperature_k", 285.0)),
                unit=lc.get("unit", "K"),
            )
            # A disordered set of trigger temperatures makes transitions
            # unreachable or permanently flapping — refuse to run disarmed.
            if not (lifecycle.base_temperature_k < lifecycle.warming_start_k
                    < lifecycle.cooling_start_k < lifecycle.room_temperature_k):
                raise ConfigError(
                    f"{path}: fridge {name!r} lifecycle temperatures must satisfy "
                    "base_temperature_k < warming_start_k < cooling_start_k < "
                    "room_temperature_k"
                )
        fridges.append(
            FridgeConfig(
                name=name,
                poll_interval=float(spec["poll_interval"]),
                staleness_factor=float(spec["staleness_factor"]),
                lifecycle=lifecycle,
            )
        )

    if not fridges:
        # An indentation slip or misspelled top-level key parses to zero fridges;
        # a watchdog monitoring nothing must not heartbeat as healthy (§8).
        raise ConfigError(
            f"{path}: no fridges configured — check the top-level 'fridges:' key "
            "and its indentation"
        )

    return WatchdogConfig(
        check_interval=float(wd.get("check_interval", 15)),
        reminder_interval=float(wd.get("reminder_interval", 1800)),
        healthchecks_url=hc_url,
        slack_webhook_env=slack_env,
        fridges=fridges,
    )


# --------------------------------------------------------------------------- state machines
@dataclass
class AlertContext:
    """Everything needed to describe one Slack/status message. The alert kind is
    derived from the key ('SILENT', 'LIFECYCLE', or 'STALE:<channel>'), so there
    is no separate `kind` field to keep in sync."""

    fridge: str
    key: str                      # 'SILENT', 'LIFECYCLE', or 'STALE:<channel>'
    value: float | None = None
    unit: str | None = None
    data_ts: datetime | None = None
    age_seconds: float | None = None
    phase: str | None = None

    @property
    def is_silent(self) -> bool:
        return self.key == "SILENT"

    @property
    def is_lifecycle(self) -> bool:
        return self.key == LIFECYCLE_KEY

    @property
    def is_stale(self) -> bool:
        return self.key.startswith(STALE_PREFIX)

    @property
    def channel(self) -> str:
        """Channel name for STALE keys (not meaningful for SILENT/LIFECYCLE)."""
        return self.key[len(STALE_PREFIX):] if self.is_stale else self.key


@dataclass
class Decision:
    state: str
    since: datetime
    last_notified: datetime | None
    notify: str | None
    write: bool                   # whether alert_state needs persisting


def _initial_lifecycle_phase(value: float, cfg: LifecycleConfig) -> str:
    if value >= cfg.room_temperature_k:
        return PHASE_ROOM
    if value <= cfg.base_temperature_k:
        return PHASE_BASE
    return PHASE_COOLING


def decide_lifecycle_transition(
    row: AlertRow | None,
    value: float,
    cfg: LifecycleConfig,
    muted: bool,
    now: datetime,
) -> Decision:
    """Persist one lifecycle phase per fridge and notify only on named
    operational milestones. First observation establishes a baseline silently so
    restarting the watchdog does not announce an event that already happened."""
    current = row.state if row and row.state in _LIFECYCLE_PHASES else None
    last_notified = row.last_notified if row else None

    if current is None:
        return Decision(
            _initial_lifecycle_phase(value, cfg),
            now,
            last_notified,
            None,
            write=True,
        )

    target = current
    event = None

    if current == PHASE_ROOM:
        if value < cfg.cooling_start_k:
            target, event = PHASE_COOLING, STARTED_COOLING
    elif current == PHASE_COOLING:
        if value <= cfg.base_temperature_k:
            target, event = PHASE_BASE, REACHED_BASE
        elif value >= cfg.room_temperature_k:
            target, event = PHASE_ROOM, REACHED_ROOM
    elif current == PHASE_BASE:
        if value > cfg.warming_start_k:
            target, event = PHASE_WARMING, STARTED_WARMING
    elif current == PHASE_WARMING:
        if value >= cfg.room_temperature_k:
            target, event = PHASE_ROOM, REACHED_ROOM
        elif value <= cfg.base_temperature_k:
            target, event = PHASE_BASE, REACHED_BASE

    if target == current:
        return Decision(current, row.since, last_notified, None, write=False)
    if muted:
        return Decision(target, now, last_notified, None, write=True)
    return Decision(target, now, now, event, write=True)


def decide_transition(
    row: AlertRow | None,
    bad: bool,
    muted: bool,
    now: datetime,
    reminder_interval: float,
) -> Decision:
    """Pure fault-alert state machine (§8), used for per-channel STALE alerts.
    A missing row is treated as OK.

    OK   -> bad & not muted  : ALERTING, notify RAISE
    ALERTING & still bad     : notify REMIND once now - last_notified > reminder
    ALERTING -> not bad      : OK, notify CLEAR
    muted                    : record the state change but suppress notifications

    A key that went bad while muted carries last_notified=None; once the mute
    lifts and it is still bad, that None triggers a (belated) RAISE rather than a
    REMIND, so the first thing humans see is the alert, not a reminder.

    Recovery while muted: muting suppresses ALL notifications (§3), CLEAR
    included. An alert that only ever existed during the mute (last_notified is
    None) clears silently. An alert that WAS paged before the mute (last_notified
    set) is not announced mid-maintenance either — its ALERTING state is held
    (not cleared) so the CLEAR is delivered once the mute lifts and the key is
    still recovered, closing the announced incident then rather than during the
    window operators asked to stay quiet.
    """
    state = row.state if row else "OK"
    since = row.since if row else now
    last_notified = row.last_notified if row else None

    if bad:
        if state == "OK":
            if muted:
                return Decision("ALERTING", now, None, None, write=True)
            return Decision("ALERTING", now, now, RAISE, write=True)
        # already ALERTING
        if muted:
            return Decision("ALERTING", since, last_notified, None, write=False)
        if last_notified is None:
            return Decision("ALERTING", since, now, RAISE, write=True)
        if (now - last_notified).total_seconds() > reminder_interval:
            return Decision("ALERTING", since, now, REMIND, write=True)
        return Decision("ALERTING", since, last_notified, None, write=False)

    # not bad
    if state == "ALERTING":
        if last_notified is None:
            # Only ever alerted while muted (the RAISE was suppressed) -> nothing
            # was ever announced, so clear silently.
            return Decision("OK", now, None, None, write=True)
        if muted:
            # A real RAISE was paged before the mute. Don't announce recovery
            # mid-maintenance (§3) and don't clear the state yet: hold ALERTING so
            # the CLEAR goes out once the mute lifts and it is still recovered.
            return Decision("ALERTING", since, last_notified, None, write=False)
        # Not muted and previously paged -> announce the recovery.
        return Decision("OK", now, None, CLEAR, write=True)
    # OK and not bad: steady state. Never create a row for a healthy key.
    return Decision("OK", since, last_notified, None, write=False)


# --------------------------------------------------------------------------- formatting
def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_alert(ctx: AlertContext, action: str) -> str:
    """Build Slack/status messages for lifecycle events, STALE faults, and
    silent state. Every field formatted here has a None fallback: a formatting
    crash would drop every later pending notification in the same pass."""
    if ctx.is_lifecycle:
        return format_lifecycle_alert(ctx, action)

    if ctx.is_stale:
        if action == CLEAR:
            return (f"🟢 RESOLVED — *{ctx.fridge}* / {ctx.channel} is reporting "
                    f"fresh data again; lifecycle tracking is back in force.")
        again = " (reminder)" if action == REMIND else ""
        age = "unknown" if ctx.age_seconds is None else f"{ctx.age_seconds:.0f}s"
        return (
            f"🟠 STALE{again} — *{ctx.fridge}* / {ctx.channel} reading is FROZEN: "
            f"newest sample is {age} old (ts {_fmt_ts(ctx.data_ts)}) while the fridge "
            f"itself is still reporting — dead sensor/cable? Lifecycle tracking is "
            f"suspended until fresh data returns."
        )

    if ctx.is_silent:
        if action == CLEAR:
            return f"🟢 RESOLVED — *{ctx.fridge}* is reporting again."
        again = " (reminder)" if action == REMIND else ""
        age = "unknown" if ctx.age_seconds is None else f"{ctx.age_seconds:.0f}s"
        return (
            f"🔴 *SILENT*{again} — *{ctx.fridge}* has STOPPED reporting. "
            f"No data for {age}; last seen {_fmt_ts(ctx.data_ts)}. "
            f"The fridge may be warming unseen — investigate now."
        )
    raise ValueError(f"unknown alert context: {ctx.key}")


def format_lifecycle_alert(ctx: AlertContext, action: str) -> str:
    unit = f" {ctx.unit}" if ctx.unit else ""
    value = "unknown" if ctx.value is None else f"{ctx.value:g}{unit}"
    ts = _fmt_ts(ctx.data_ts)
    if action == STARTED_COOLING:
        return f"🔵 COOLING STARTED — *{ctx.fridge}* / {ctx.phase} is {value}; data ts {ts}."
    if action == REACHED_BASE:
        return f"🟣 BASE TEMPERATURE — *{ctx.fridge}* / {ctx.phase} reached {value}; data ts {ts}."
    if action == STARTED_WARMING:
        return f"🟠 WARMING STARTED — *{ctx.fridge}* / {ctx.phase} is {value}; data ts {ts}."
    if action == REACHED_ROOM:
        return f"⚪ ROOM TEMPERATURE — *{ctx.fridge}* / {ctx.phase} reached {value}; data ts {ts}."
    raise ValueError(f"unknown lifecycle action: {action}")


# --------------------------------------------------------------------------- I/O edges
def send_slack(message: str, cfg: WatchdogConfig) -> bool:
    """POST to the dedicated alert webhook (separate from OpenClaw, §2.1).
    Returns True on success; on failure logs and returns False so the caller can
    leave alert_state unchanged and retry next loop."""
    webhook = os.environ.get(cfg.slack_webhook_env)
    if not webhook:
        log.error("Slack webhook env %s is unset; dropping alert: %s",
                  cfg.slack_webhook_env, message)
        return False
    try:
        resp = requests.post(webhook, json={"text": message}, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Slack POST failed (%s); will retry: %s", exc, message)
        return False


def heartbeat(cfg: WatchdogConfig) -> None:
    """Ping healthchecks.io. This is what catches labmanager / the watchdog
    itself dying (§8 dead-man's switch). A failed ping is logged, never raised."""
    if not cfg.healthchecks_url:
        log.debug("no healthchecks_url configured; skipping heartbeat")
        return
    try:
        requests.get(cfg.healthchecks_url, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("healthchecks ping failed: %s", exc)


# --------------------------------------------------------------------------- orchestration
def _clear_is_held(ctx: AlertContext, row: AlertRow | None, bad: bool,
                   now: datetime) -> bool:
    """Flap damping for STALE alerts: after an ALERTING channel starts reporting
    fresh data again, its CLEAR is held until it has been continuously fresh for
    CLEAR_HOLD_SECONDS. While held, the caller must leave the persisted ALERTING
    row untouched and emit NO notification — a held clear must never be fed back
    into decide_transition as bad, or it would RAISE/REMIND about a channel that
    is not stale right now. The fresh-since marker is in-memory only: a restart
    mid-hold restarts the hold, erring toward staying alerted — safe. The marker
    survives a queued CLEAR awaiting a successful Slack send; it is dropped when
    the row leaves ALERTING, the channel goes stale again, or the whole fridge
    goes SILENT (the blind window must not count as hold credit — see
    _check_fridge)."""
    key = (ctx.fridge, ctx.key)
    if bad or row is None or row.state != "ALERTING":
        _runtime.in_range_since.pop(key, None)
        return False
    in_range_since = _runtime.in_range_since.setdefault(key, now)
    return (now - in_range_since).total_seconds() < CLEAR_HOLD_SECONDS


def _void_clear_holds(fridge: str) -> None:
    """Drop all clear-hold markers for a fridge. Called when the fridge goes
    SILENT: its channels are unobservable, so time spent blind must not count
    toward any hold — the hold restarts from the first fresh pass after resume."""
    for key in [k for k in _runtime.in_range_since if k[0] == fridge]:
        del _runtime.in_range_since[key]


def _evaluate(ctx: AlertContext, bad: bool, muted: bool, cfg: WatchdogConfig,
              now: datetime, pending: list[tuple[AlertContext, Decision]],
              clear_hold: bool = False) -> None:
    """Read the persisted state and decide the fault transition. A non-notifying
    state change (e.g. a muted alert) is persisted immediately; a notification is
    queued in `pending` so it can be sent AFTER the heartbeat — a slow Slack
    endpoint must never delay the dead-man's switch (§8). With clear_hold=True,
    a fresh channel whose ALERTING row is still inside the clear-hold window is
    left exactly as it is: state stays ALERTING, nothing is written, nothing is
    announced."""
    row = db.get_alert_state(ctx.fridge, ctx.key)
    if clear_hold and _clear_is_held(ctx, row, bad, now):
        return
    decision = decide_transition(row, bad, muted, now, cfg.reminder_interval)
    if decision.notify is None:
        if decision.write:
            db.upsert_alert_state(
                ctx.fridge, ctx.key, decision.state, decision.since, decision.last_notified
            )
    else:
        pending.append((ctx, decision))


def _send_pending(pending: list[tuple[AlertContext, Decision]], cfg: WatchdogConfig) -> None:
    """Deliver queued notifications, persisting each only after a successful send
    so a Slack outage retries next loop rather than marking the alert handled.
    Every attempted send feeds the consecutive-failure counter that suppresses
    the heartbeat (see check_once); quiet passes leave it untouched."""
    for ctx, decision in pending:
        if not send_slack(format_alert(ctx, decision.notify), cfg):
            _runtime.consecutive_slack_failures += 1
            continue  # leave alert_state untouched; retry on the next loop
        _runtime.consecutive_slack_failures = 0
        db.upsert_alert_state(
            ctx.fridge, ctx.key, decision.state, decision.since, decision.last_notified
        )


def _probe_slack(cfg: WatchdogConfig, now: datetime) -> None:
    """Make heartbeat suppression self-resolving. With nothing pending, no send
    would ever exercise the webhook again (e.g. the transition that kept failing
    stopped being pending), pinning the failure counter — and the suppression —
    forever. Probe the webhook (throttled to SLACK_PROBE_INTERVAL_SECONDS); on
    success, reset the counter so the heartbeat resumes; on failure, stay
    suppressed — the alert path is genuinely dead and healthchecks SHOULD page."""
    last = _runtime.last_slack_probe_at
    if last is not None and (now - last).total_seconds() < SLACK_PROBE_INTERVAL_SECONDS:
        return
    _runtime.last_slack_probe_at = now
    failures = _runtime.consecutive_slack_failures
    restored = (f"🩺 Watchdog: Slack delivery restored after {failures} consecutive "
                f"send failures; alerting and heartbeat resumed.")
    if send_slack(restored, cfg):
        _runtime.consecutive_slack_failures = 0
        log.info("Slack probe delivered; heartbeat resumes after %d consecutive "
                 "send failures", failures)
    else:
        log.error("Slack probe failed; heartbeat stays suppressed "
                  "(%d consecutive send failures)", failures)


def _record_silent_state(fridge: str, stale: bool, now: datetime) -> None:
    """Track whether a fridge is stale without producing Slack notifications."""
    row = db.get_alert_state(fridge, "SILENT")
    if stale:
        since = row.since if row and row.state == "ALERTING" else now
        if row is None or row.state != "ALERTING" or row.last_notified is not None:
            db.upsert_alert_state(fridge, "SILENT", "ALERTING", since, None)
        return

    if row is not None and (row.state != "OK" or row.last_notified is not None):
        db.upsert_alert_state(fridge, "SILENT", "OK", now, None)


def _check_lifecycle(f: FridgeConfig, cfg: WatchdogConfig, muted: bool, now: datetime,
                     pending: list[tuple[AlertContext, Decision]]) -> None:
    if f.lifecycle is None:
        return
    lc = f.lifecycle
    stale_key = STALE_PREFIX + lc.channel
    reading = db.latest_reading(f.name, lc.channel)  # excludes far-future ts (db layer)
    if reading is None:
        log.warning("%s: lifecycle channel %r has no reading — cannot infer "
                    "cooling/base/warming/room state", f.name, lc.channel)
        # A dangling STALE alert must still be able to clear (e.g. retention
        # pruned the frozen reading) — evaluate it as not-stale through the
        # normal machinery.
        _evaluate(AlertContext(f.name, stale_key), False, muted, cfg, now, pending,
                  clear_hold=True)
        return
    if lc.unit and reading.unit and lc.unit != reading.unit:
        log.warning("%s/%s: lifecycle unit %r != stored unit %r",
                    f.name, lc.channel, lc.unit, reading.unit)

    # Per-channel staleness — checked in EVERY lifecycle phase (safety-first):
    # a dead sensor cable freezes the lifecycle channel while other channels
    # keep last_seen fresh, and the frozen value would pin the phase forever —
    # a fridge warming unseen would never page STARTED_WARMING. Unlike routine
    # fridge silence (recorded, not paged: the lab alert policy reserves Slack
    # for lifecycle events), a frozen lifecycle channel silently INVALIDATES
    # those lifecycle events, so it escalates like a fault: RAISE once, REMIND
    # per reminder_interval, mute-aware, and a flap-damped CLEAR when fresh
    # data returns. Age uses the data ts (latest_reading has no received_at);
    # db.latest_reading excludes far-future ts so a skewed clock cannot mask it.
    silent_after = f.staleness_factor * f.poll_interval
    reading_age = (now - reading.ts).total_seconds()
    channel_stale = reading_age > silent_after
    _evaluate(
        AlertContext(f.name, stale_key, data_ts=reading.ts, age_seconds=reading_age),
        channel_stale, muted, cfg, now, pending, clear_hold=True,
    )
    if channel_stale:
        return  # frozen value — do not infer lifecycle from it

    decision = decide_lifecycle_transition(
        db.get_alert_state(f.name, LIFECYCLE_KEY),
        reading.value,
        lc,
        muted,
        now,
    )
    if decision.notify is None:
        if decision.write:
            db.upsert_alert_state(
                f.name,
                LIFECYCLE_KEY,
                decision.state,
                decision.since,
                decision.last_notified,
            )
        return

    pending.append((
        AlertContext(
            f.name,
            LIFECYCLE_KEY,
            value=reading.value,
            unit=lc.unit or reading.unit,
            data_ts=reading.ts,
            phase=lc.channel,
        ),
        decision,
    ))


def _check_fridge(f: FridgeConfig, cfg: WatchdogConfig, now: datetime,
                  pending: list[tuple[AlertContext, Decision]]) -> None:
    muted = db.is_muted(f.name)

    # --- staleness (primary, §3.1) ---
    # Age is measured from received_at (server arrival time), not the data
    # timestamp, so a skewed fridge-host clock cannot mask real silence (§12).
    seen = db.last_seen(f.name)
    if seen is None:
        stale = True
    else:
        stale = (now - seen.received_at).total_seconds() > f.staleness_factor * f.poll_interval
    # Staleness is still recorded, but Slack is reserved for lifecycle events per
    # the lab alert policy.
    _record_silent_state(f.name, stale, now)
    if stale:
        # The whole fridge is unobservable: any clear-hold progress its STALE
        # alerts had accrued is void — blind time must not count toward a hold.
        _void_clear_holds(f.name)
        return  # silent -> data is untrustworthy, skip lifecycle checks

    _check_lifecycle(f, cfg, muted, now, pending)


def check_once(cfg: WatchdogConfig, now: datetime | None = None) -> None:
    """One watchdog pass: evaluate every fridge, confirm the DB is reachable,
    heartbeat, then deliver notifications.

    Per-fridge errors are caught so one flaky fridge can't stop the others, but
    they DO suppress the heartbeat: a fridge left unevaluated is a monitoring
    blind spot, and the dead-man's switch must reflect that, not just raw DB
    reachability. The heartbeat is therefore gated on a direct DB probe
    (db.ping — a watchdog that can't read the DB must not report healthy), a
    clean sweep of every fridge, AND the Slack path being alive: after
    SLACK_FAILURES_TO_SUPPRESS_HEARTBEAT consecutive failed sends the heartbeat
    is withheld until a send succeeds, so a dead webhook pages via healthchecks
    instead of dropping alerts behind a green heartbeat. When nothing is pending
    to retry the webhook naturally, a throttled probe (_probe_slack) keeps the
    suppression self-resolving. The heartbeat runs before the Slack sends so a
    slow webhook cannot starve it (§8)."""
    now = now or now_utc()
    _runtime.heartbeat_attempted = False
    pending: list[tuple[AlertContext, Decision]] = []
    all_checked = True
    for f in cfg.fridges:
        try:
            _check_fridge(f, cfg, now, pending)
        except Exception:
            log.exception("error checking fridge %s", f.name)
            all_checked = False

    try:
        db.ping()
    except Exception as exc:
        raise WatchdogError(f"database unreachable; suppressing heartbeat: {exc}")

    # Heartbeat BEFORE the Slack sends so a slow webhook can't starve the
    # dead-man's switch — but only if EVERY fridge was actually evaluated. A
    # fridge we failed to check is a blind spot, and a watchdog that heartbeats
    # while blind reports itself healthy when it isn't (§8). A one-off blip just
    # skips a single beat (within healthchecks.io's grace); a sustained blind
    # spot trips the switch, which is the correct escalation. Pending alerts for
    # the fridges that DID evaluate are still delivered below.
    if (_runtime.consecutive_slack_failures >= SLACK_FAILURES_TO_SUPPRESS_HEARTBEAT
            and not pending):
        # Nothing pending will retry the webhook, so the suppression could never
        # self-resolve; probe (throttled) and re-read the counter below.
        _probe_slack(cfg, now)
    failures = _runtime.consecutive_slack_failures
    if failures >= SLACK_FAILURES_TO_SUPPRESS_HEARTBEAT:
        log.error("%d consecutive Slack send failures; suppressing heartbeat so "
                  "healthchecks pages — alerts are being dropped", failures)
    elif all_checked:
        heartbeat(cfg)
        _runtime.heartbeat_attempted = True
    else:
        log.error("not all fridges evaluated this pass; suppressing heartbeat")
    _send_pending(pending, cfg)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_runtime_env(cfg: WatchdogConfig) -> None:
    """Fail fast at startup if the watchdog cannot page or heartbeat. Raising
    here makes the systemd unit crash-loop, the heartbeat goes silent, and
    healthchecks.io pages — instead of a green heartbeat over dropped alerts (§8)."""
    if not os.environ.get(cfg.slack_webhook_env):
        raise ConfigError(
            f"Slack webhook env var {cfg.slack_webhook_env} is unset or empty; the "
            "watchdog cannot deliver alerts. Export the webhook URL (see "
            "config/fridges.yaml 'slack:') and restart."
        )
    if not cfg.healthchecks_url:
        raise ConfigError(
            "healthchecks_url is unset (or still the REPLACE-WITH-UUID placeholder); "
            "the dead-man's switch would silently no-op. Set CRYO_HEALTHCHECKS_URL or "
            "watchdog.healthchecks_url in config/fridges.yaml and restart."
        )


def warn_dangling_alert_state(cfg: WatchdogConfig) -> None:
    """Log alert_state rows that no longer match any configured fridge/key
    (renamed or removed config entries, or keys from a previous alert scheme).
    Never auto-deleted — a human decides — but silent dangling state would hide
    e.g. an ALERTING row that can no longer clear."""
    configured: dict[str, set[str]] = {}
    for f in cfg.fridges:
        keys = {"SILENT"}
        if f.lifecycle is not None:
            keys |= {LIFECYCLE_KEY, STALE_PREFIX + f.lifecycle.channel}
        configured[f.name] = keys
    for fridge, key in db.list_alert_keys():
        keys = configured.get(fridge)
        if keys is None:
            log.warning("alert_state row (%s, %s) references a fridge not in config; "
                        "delete it manually if the fridge was removed", fridge, key)
        elif key not in keys:
            log.warning("alert_state row (%s, %s) references a key not produced by "
                        "the current config; delete it manually if it is obsolete",
                        fridge, key)


def _log_loop_error(exc: Exception) -> None:
    """Loop-level catch-all. Says whether THIS pass's heartbeat actually fired —
    an error in the post-heartbeat sends must not claim the beat was skipped."""
    if _runtime.heartbeat_attempted:
        log.error("watchdog loop error (after the heartbeat already fired this "
                  "pass): %s", exc)
    else:
        log.error("watchdog loop error (heartbeat skipped this pass): %s", exc)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config()
    require_runtime_env(cfg)
    db.init_pool()
    warn_dangling_alert_state(cfg)
    log.info("watchdog starting: %d fridge(s), check_interval=%ss",
             len(cfg.fridges), cfg.check_interval)
    try:
        while True:
            try:
                check_once(cfg)
            except Exception as exc:  # never let one bad loop kill the watchdog
                _log_loop_error(exc)
            time.sleep(cfg.check_interval)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
