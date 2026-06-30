"""Host daemon (Windows, one instance per fridge). See §6.2.

Run as a service via NSSM wrapping `python daemon.py --config config.toml`.
NSSM gives auto-restart, so the daemon itself has a watchdog.

Every poll_interval seconds:
  1. Read NEW bytes from the active log file(s). Track byte offset AND
     inode/file-id; if it changes (midnight rotation -> new dated file), reset
     offset and pick up the new file. Handle a partial final line.
  2. parser.parse_new(...) -> Readings. Convert local ts -> UTC.
  3. Append readings to the local SQLite spool, marked un-acked.
  4. POST ALL un-acked readings as one batch. On HTTP 2xx, mark acked; on
     failure, leave them (they go out next cycle).
  5. Periodically prune acked rows older than N days.

Hard requirements (§6.2): a malformed line logs a warning and is skipped (no
crash); a network outage grows the spool and backfills on recovery; duplicate
sends are safe (server dedups).

Skeleton: loop and tailing contract are laid out; IO is marked TODO.
"""
from __future__ import annotations

import argparse
import time
import tomllib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from spool import Spool


def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_parser(name: str):
    """Import parsers/<name>.py and return its Parser instance."""
    # TODO: importlib the per-fridge parser module by name.
    raise NotImplementedError


class LogTailer:
    """Tracks (path, inode/file-id, byte offset) across rotation (§6.2 step 1)."""

    def __init__(self, log_glob: str) -> None:
        self.log_glob = log_glob
        self.offset = 0
        self.file_id = None

    def read_new_lines(self) -> list[tuple[str, list[str]]]:
        # Returns (source_basename, new_lines) per active file, because a fridge
        # logs to many files (BlueFors: CH* T, maxigauge, …) and the parser needs
        # the source to interpret each line.
        # TODO: resolve active files from glob; track byte offset AND inode/
        # file-id per file; on file-id change (midnight rotation) reset offset
        # and pick up the new file; hold back a partial final line. Never raise.
        return []


def to_utc(ts: datetime, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=tz)
    return ts.astimezone(timezone.utc)


def post_batch(server_url: str, token: str, fridge: str, rows) -> bool:
    """POST un-acked readings as one batch. Return True on HTTP 2xx."""
    # TODO: requests.post(server_url, json={"fridge": fridge, "readings": [...]},
    #       headers={"Authorization": f"Bearer {token}"})
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    fridge = cfg["fridge"]
    tz = ZoneInfo(cfg["timezone"])
    poll_interval = cfg.get("poll_interval", 30)

    parser = load_parser(cfg["parser"])
    tailer = LogTailer(cfg["log_glob"])
    spool = Spool(cfg.get("spool_path", "spool.sqlite"))

    while True:
        try:
            readings = []
            for source, lines in tailer.read_new_lines():   # 1 (per file)
                readings.extend(parser.parse_new(source, lines))   # 2
            for r in readings:
                r.ts = to_utc(r.ts, tz)
            spool.append(readings)                        # 3

            pending = spool.unacked()                     # 4
            if pending:
                ids = [rowid for rowid, _ in pending]
                rows = [r for _, r in pending]
                if post_batch(cfg["server_url"], cfg["token"], fridge, rows):
                    spool.mark_acked(ids)
            # 5: prune handled on a slower cadence — TODO
        except Exception as exc:
            # Never crash the loop over one bad cycle (§12).
            print(f"daemon loop error: {exc}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
