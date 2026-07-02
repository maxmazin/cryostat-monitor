"""Watchdog — the safety core (§8). Plain Python, deterministic, NO LLM.

Loop every check_interval:
  - staleness check per fridge (PRIMARY: do not infer lifecycle from stale data)
  - lifecycle checks per fridge (cooling/base/warming/room notifications)
  - maintenance mute (duration-capped, set via the /maintenance ingest endpoint)
  - persisted alert_state so a restart neither re-spams nor forgets (§8)
  - heartbeat to healthchecks.io every loop (dead-man's switch, §5/§8)

The lifecycle state machine is pure and fully tested. All I/O lives at the
edges: db.* for reads/writes, send_slack/heartbeat for HTTP.

Run as a module so relative imports resolve:  python -m watchdog.watchdog
(see systemd/cryo-watchdog.service).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import db
from .db import AlertRow

log = logging.getLogger("cryo.watchdog")

# Notification actions emitted by the state machine.
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


class WatchdogError(Exception):
    """Raised to abort a loop iteration when the watchdog cannot do its job
    (e.g. the database is unreachable for every fridge). Aborting suppresses the
    heartbeat so the healthchecks.io dead-man's switch fires (§8)."""


# --------------------------------------------------------------------------- config
@dataclass
class LifecycleConfig:
    channel: str = "MXC"
    cooling_start_k: float = 280.0
    base_temperature_k: float = 0.050
    warming_start_k: float = 0.100
    room_temperature_k: float = 285.0
    unit: str = "K"


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
            lifecycle = LifecycleConfig(
                channel=lc.get("channel", "MXC"),
                cooling_start_k=float(lc.get("cooling_start_k", 280.0)),
                base_temperature_k=float(lc.get("base_temperature_k", 0.050)),
                warming_start_k=float(lc.get("warming_start_k", 0.100)),
                room_temperature_k=float(lc.get("room_temperature_k", 285.0)),
                unit=lc.get("unit", "K"),
            )
        fridges.append(
            FridgeConfig(
                name=name,
                poll_interval=float(spec["poll_interval"]),
                staleness_factor=float(spec["staleness_factor"]),
                lifecycle=lifecycle,
            )
        )

    return WatchdogConfig(
        check_interval=float(wd.get("check_interval", 15)),
        reminder_interval=float(wd.get("reminder_interval", 1800)),
        healthchecks_url=hc_url,
        slack_webhook_env=slack_env,
        fridges=fridges,
    )


# --------------------------------------------------------------------------- state machine
@dataclass
class AlertContext:
    """Everything needed to describe one Slack/status message."""

    fridge: str
    key: str                      # 'SILENT' or 'LIFECYCLE'
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


# --------------------------------------------------------------------------- formatting
def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_alert(ctx: AlertContext, action: str) -> str:
    """Build Slack/status messages for lifecycle events and silent state."""
    if ctx.is_lifecycle:
        return format_lifecycle_alert(ctx, action)

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
def _send_pending(pending: list[tuple[AlertContext, Decision]], cfg: WatchdogConfig) -> None:
    """Deliver queued notifications, persisting each only after a successful send
    so a Slack outage retries next loop rather than marking the alert handled."""
    for ctx, decision in pending:
        if not send_slack(format_alert(ctx, decision.notify), cfg):
            continue  # leave alert_state untouched; retry on the next loop
        db.upsert_alert_state(
            ctx.fridge, ctx.key, decision.state, decision.since, decision.last_notified
        )


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


def _check_lifecycle(f: FridgeConfig, muted: bool, now: datetime,
                     pending: list[tuple[AlertContext, Decision]]) -> None:
    if f.lifecycle is None:
        return
    lc = f.lifecycle
    reading = db.latest_reading(f.name, lc.channel)
    if reading is None:
        log.warning("%s: lifecycle channel %r has no reading — cannot infer "
                    "cooling/base/warming/room state", f.name, lc.channel)
        return
    if lc.unit and reading.unit and lc.unit != reading.unit:
        log.warning("%s/%s: lifecycle unit %r != stored unit %r",
                    f.name, lc.channel, lc.unit, reading.unit)

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
        stale, age, last_ts = True, None, None
    else:
        age = (now - seen.received_at).total_seconds()
        stale = age > f.staleness_factor * f.poll_interval
        last_ts = seen.last_ts
    # Staleness is still recorded, but Slack is reserved for lifecycle events per
    # the lab alert policy.
    _record_silent_state(f.name, stale, now)
    if stale:
        return  # silent -> data is untrustworthy, skip lifecycle checks

    _check_lifecycle(f, muted, now, pending)


def check_once(cfg: WatchdogConfig, now: datetime | None = None) -> None:
    """One watchdog pass: evaluate every fridge, confirm the DB is reachable,
    heartbeat, then deliver notifications.

    Per-fridge errors are caught so one flaky fridge can't stop the others, but
    they DO suppress the heartbeat: a fridge left unevaluated is a monitoring
    blind spot, and the dead-man's switch must reflect that, not just raw DB
    reachability. The heartbeat is therefore gated on both a direct DB probe
    (db.ping — a watchdog that can't read the DB must not report healthy) and a
    clean sweep of every fridge. It runs before the Slack sends so a Slack outage
    cannot starve it (§8)."""
    now = now or now_utc()
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
    if all_checked:
        heartbeat(cfg)
    else:
        log.error("not all fridges evaluated this pass; suppressing heartbeat")
    _send_pending(pending, cfg)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config()
    db.init_pool()
    log.info("watchdog starting: %d fridge(s), check_interval=%ss, heartbeat=%s",
             len(cfg.fridges), cfg.check_interval,
             "on" if cfg.healthchecks_url else "OFF")
    try:
        while True:
            try:
                check_once(cfg)
            except Exception as exc:  # never let one bad loop kill the watchdog
                log.error("watchdog loop error (heartbeat skipped this pass): %s", exc)
            time.sleep(cfg.check_interval)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
