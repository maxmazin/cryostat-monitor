"""Watchdog — the safety core (§8). Plain Python, deterministic, NO LLM.

Loop every check_interval:
  - staleness check per fridge (PRIMARY: silence is the alarm, §3.1)
  - threshold checks per channel (secondary)
  - maintenance mute (duration-capped, set via the /maintenance ingest endpoint)
  - persisted alert_state so a restart neither re-spams nor forgets (§8)
  - heartbeat to healthchecks.io every loop (dead-man's switch, §5/§8)

The alert state machine (decide_transition) is pure and fully tested. All I/O
lives at the edges: db.* for reads/writes, send_slack/heartbeat for HTTP.

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
class ChannelLimits:
    high: float | None = None
    low: float | None = None


@dataclass
class FridgeConfig:
    name: str
    poll_interval: float
    staleness_factor: float
    channels: dict[str, ChannelLimits]


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
        channels = {
            channel: ChannelLimits(high=limits.get("high"), low=limits.get("low"))
            for channel, limits in (spec.get("channels") or {}).items()
        }
        fridges.append(
            FridgeConfig(
                name=name,
                poll_interval=float(spec["poll_interval"]),
                staleness_factor=float(spec["staleness_factor"]),
                channels=channels,
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
    """Everything needed to describe one alert key in a Slack message."""

    fridge: str
    key: str                      # 'SILENT' or channel name
    kind: str                     # 'SILENT' | 'THRESHOLD'
    value: float | None = None
    limit: float | None = None    # the limit that was crossed
    bound: str | None = None      # 'high' | 'low'
    unit: str | None = None
    data_ts: datetime | None = None
    age_seconds: float | None = None


@dataclass
class Decision:
    state: str                    # 'OK' | 'ALERTING'
    since: datetime
    last_notified: datetime | None
    notify: str | None            # None | RAISE | REMIND | CLEAR
    write: bool                   # whether alert_state needs persisting


def decide_transition(
    row: AlertRow | None,
    bad: bool,
    muted: bool,
    now: datetime,
    reminder_interval: float,
) -> Decision:
    """Pure alert state machine (§8). A missing row is treated as OK.

    OK   -> bad & not muted  : ALERTING, notify RAISE
    ALERTING & still bad     : notify REMIND once now - last_notified > reminder
    ALERTING -> not bad      : OK, notify CLEAR
    muted                    : record the state change but suppress notifications

    A key that went bad while muted carries last_notified=None; once the mute
    lifts and it is still bad, that None triggers a (belated) RAISE rather than a
    REMIND, so the first thing humans see is the alert, not a reminder.
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
        if muted:
            return Decision("OK", now, None, None, write=True)
        return Decision("OK", now, None, CLEAR, write=True)
    # OK and not bad: steady state. Never create a row for a healthy key.
    return Decision("OK", since, last_notified, None, write=False)


# --------------------------------------------------------------------------- formatting
def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_alert(ctx: AlertContext, action: str) -> str:
    """Build the Slack message. SILENT is made visually distinct — it is the
    scary one (§8): no high reading accompanies a fridge that has gone dark."""
    if action == CLEAR:
        if ctx.kind == "SILENT":
            return f"🟢 RESOLVED — *{ctx.fridge}* is reporting again."
        value = f" (now {ctx.value:g} {ctx.unit})" if ctx.value is not None else ""
        return f"🟢 RESOLVED — *{ctx.fridge}* / {ctx.key} back within limits{value}."

    again = " (reminder)" if action == REMIND else ""
    if ctx.kind == "SILENT":
        age = "unknown" if ctx.age_seconds is None else f"{ctx.age_seconds:.0f}s"
        return (
            f"🔴 *SILENT*{again} — *{ctx.fridge}* has STOPPED reporting. "
            f"No data for {age}; last seen {_fmt_ts(ctx.data_ts)}. "
            f"The fridge may be warming unseen — investigate now."
        )
    return (
        f"🟠 THRESHOLD{again} — *{ctx.fridge}* / {ctx.key} = {ctx.value:g} {ctx.unit} "
        f"crossed {ctx.bound} limit {ctx.limit:g}; data ts {_fmt_ts(ctx.data_ts)}."
    )


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
def transition(ctx: AlertContext, bad: bool, muted: bool, cfg: WatchdogConfig,
               now: datetime) -> None:
    """Apply the state machine for one alert key: read state, decide, notify,
    then persist — but only persist after a successful notification, so a Slack
    outage retries rather than silently marking the alert as handled."""
    decision = decide_transition(
        db.get_alert_state(ctx.fridge, ctx.key), bad, muted, now, cfg.reminder_interval
    )
    if decision.notify is not None:
        if not send_slack(format_alert(ctx, decision.notify), cfg):
            return  # leave alert_state untouched; retry on the next loop
    if decision.write:
        db.upsert_alert_state(
            ctx.fridge, ctx.key, decision.state, decision.since, decision.last_notified
        )


def _check_fridge(f: FridgeConfig, cfg: WatchdogConfig, now: datetime) -> None:
    muted = db.is_muted(f.name)

    # --- staleness (primary, §3.1) ---
    seen = db.last_seen(f.name)
    if seen is None:
        stale, age = True, None
    else:
        age = (now - seen).total_seconds()
        stale = age > f.staleness_factor * f.poll_interval
    transition(
        AlertContext(f.name, "SILENT", "SILENT", data_ts=seen, age_seconds=age),
        bad=stale, muted=muted, cfg=cfg, now=now,
    )
    if stale:
        return  # silent -> data is untrustworthy, skip threshold checks

    # --- thresholds (secondary) ---
    for channel, limits in f.channels.items():
        reading = db.latest_reading(f.name, channel)
        if reading is None:
            continue
        if limits.high is not None and reading.value > limits.high:
            bound, limit = "high", limits.high
        elif limits.low is not None and reading.value < limits.low:
            bound, limit = "low", limits.low
        else:
            bound, limit = None, None
        transition(
            AlertContext(
                f.name, channel, "THRESHOLD",
                value=reading.value, limit=limit, bound=bound,
                unit=reading.unit, data_ts=reading.ts,
            ),
            bad=bound is not None, muted=muted, cfg=cfg, now=now,
        )


def check_once(cfg: WatchdogConfig, now: datetime | None = None) -> None:
    """One watchdog pass over all fridges, then the heartbeat.

    Per-fridge errors are caught so one flaky fridge can't stop the others or the
    heartbeat. But if EVERY fridge check fails (e.g. the DB is down), we abort
    before the heartbeat so the dead-man's switch fires — a watchdog that cannot
    read the DB must not keep reporting itself healthy (§8)."""
    now = now or now_utc()
    errors = 0
    for f in cfg.fridges:
        try:
            _check_fridge(f, cfg, now)
        except Exception:
            log.exception("error checking fridge %s", f.name)
            errors += 1
    if cfg.fridges and errors == len(cfg.fridges):
        raise WatchdogError("all fridge checks failed; suppressing heartbeat")
    heartbeat(cfg)


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
