"""Watchdog — the safety core (§8). Plain Python, deterministic, NO LLM.

Loop every CHECK_INTERVAL:
  - staleness check per fridge (PRIMARY: silence is the alarm, §3.1)
  - threshold checks per channel (secondary)
  - maintenance mute (duration-capped, set elsewhere)
  - persisted alert_state so a restart neither re-spams nor forgets
  - heartbeat to healthchecks.io EVERY loop, unconditionally (dead-man's switch)

This is a skeleton: the control flow and state machine are spelled out;
DB reads/writes and the Slack/heartbeat HTTP calls are marked TODO.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_muted(fridge: str) -> bool:
    """True if an active maintenance row exists (now() < until_ts)."""
    # TODO: SELECT 1 FROM maintenance WHERE fridge=%s AND until_ts > now()
    return False


def last_seen(fridge: str) -> datetime | None:
    # TODO: SELECT last_ts FROM last_seen WHERE fridge=%s
    return None


def latest_value(fridge: str, channel: str) -> float | None:
    # TODO: most recent reading for (fridge, channel) from `readings`
    return None


def transition(fridge: str, key: str, bad: bool, muted: bool) -> None:
    """State machine over alert_state (§8).

    OK -> bad & not muted     : set ALERTING, Slack "🔴 raised", record last_notified
    ALERTING & still bad      : if now - last_notified > reminder_interval, remind
    ALERTING -> not bad       : set OK, Slack "🟢 cleared"
    muted                     : record state but suppress notifications
    """
    # TODO: load current row from alert_state, apply transitions above,
    #       persist, and call send_slack(...) only when not muted.
    ...


def send_slack(message: str) -> None:
    # Dedicated incoming webhook, separate from OpenClaw (§2.1). Make SILENT
    # alerts visually distinct — they are the scary ones.
    # TODO: POST to CRYO_ALERT_SLACK_WEBHOOK
    ...


def heartbeat() -> None:
    # Ping healthchecks.io every loop, unconditionally. This is what catches
    # labmanager / the watchdog itself dying (§8 dead-man's switch).
    # TODO: GET the configured hc-ping URL
    ...


def check_once(fridges: list[FridgeConfig], check_interval: float) -> None:
    for f in fridges:
        muted = is_muted(f.name)

        # --- staleness (primary) ---
        seen = last_seen(f.name)
        if seen is None:
            stale = True
        else:
            age = (now_utc() - seen).total_seconds()
            stale = age > f.staleness_factor * f.poll_interval
        transition(f.name, "SILENT", bad=stale, muted=muted)
        if stale:
            continue  # if it's silent, skip threshold checks

        # --- thresholds (secondary) ---
        for channel, limits in f.channels.items():
            v = latest_value(f.name, channel)
            if v is None:
                continue
            breach = (limits.high is not None and v > limits.high) or (
                limits.low is not None and v < limits.low
            )
            transition(f.name, channel, bad=breach, muted=muted)

    heartbeat()  # EVERY loop, unconditionally


def load_config() -> tuple[list[FridgeConfig], float]:
    # TODO: parse server/config/fridges.yaml into FridgeConfig objects.
    return [], 15.0


def main() -> None:
    fridges, check_interval = load_config()
    while True:
        try:
            check_once(fridges, check_interval)
        except Exception as exc:  # never let one bad loop kill the watchdog
            # TODO: log the exception; keep looping so heartbeat survives.
            print(f"watchdog loop error: {exc}")
        time.sleep(check_interval)


if __name__ == "__main__":
    main()
