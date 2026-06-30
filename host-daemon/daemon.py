"""Host daemon (Windows, one instance per fridge). See §6.2.

Run as a service via NSSM wrapping `python daemon.py --config config.toml`.
NSSM gives auto-restart, so the daemon itself has a watchdog.

Every poll_interval seconds:
  1. Read NEW lines from each active log file (LogTailer tracks per-file byte
     offset + inode; handles midnight rotation to a new dated file, partial
     final lines, and truncation).
  2. parse_new(source, lines) -> Readings; convert local ts -> UTC.
  3. Append to the local SQLite spool (idempotent on (channel, ts)).
  4. POST ALL un-acked readings as one batch; on HTTP 2xx, mark them acked.
     On failure, leave them — they go out next cycle (backfill on recovery).
  5. Periodically prune acked rows older than the retention window.

Outage behavior (Phase 1 acceptance): persisted offsets mean a daemon restart
reads the gap written while it was down; un-acked spool rows mean a network
outage backfills on recovery — both with zero duplicate rows, because the spool
is idempotent on (channel, ts) and the server dedups on (fridge, channel, ts).
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import logging
import os
import time
import tomllib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from parsers.base import Parser, Reading
from spool import Spool

log = logging.getLogger("cryo.daemon")


def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_parser(name: str) -> Parser:
    """Import parsers/<name>.py and return its Parser subclass instance."""
    module = importlib.import_module(f"parsers.{name}")
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, Parser) and obj is not Parser:
            return obj()
    raise ValueError(f"no Parser subclass found in parsers.{name}")


def to_utc(ts: datetime, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=tz)
    return ts.astimezone(timezone.utc)


class LogTailer:
    """Tracks (path -> inode, byte offset) across rotation (§6.2 step 1).

    State is persisted to JSON so a restart resumes where it left off and reads
    whatever was appended while the daemon was down. A newly-seen file is read
    from the start; an in-place rotation (same path, new inode) or a truncation
    resets the offset to 0.
    """

    def __init__(self, globs: list[str], state_path: str) -> None:
        self.globs = globs
        self.state_path = state_path
        self.state: dict[str, dict] = self._load_state()

    def _load_state(self) -> dict[str, dict]:
        try:
            with open(self.state_path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh)
        os.replace(tmp, self.state_path)   # atomic on POSIX and Windows

    def read_new_lines(self) -> list[tuple[str, list[str]]]:
        results: list[tuple[str, list[str]]] = []
        paths = sorted({p for pattern in self.globs for p in glob.glob(pattern)})
        for path in paths:
            try:
                lines = self._read_file(path)
            except OSError:
                # File vanished or unreadable mid-poll; try again next cycle.
                continue
            if lines:
                results.append((os.path.basename(path), lines))
        self._save_state()
        return results

    def _read_file(self, path: str) -> list[str]:
        st = os.stat(path)
        prev = self.state.get(path)
        if prev is None or prev.get("inode") != st.st_ino or st.st_size < prev.get("offset", 0):
            offset = 0   # new file, rotated-in-place, or truncated
        else:
            offset = prev["offset"]

        if offset >= st.st_size:
            self.state[path] = {"inode": st.st_ino, "offset": st.st_size}
            return []

        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(st.st_size - offset)

        # Hold back a partial final line (no trailing newline) until it completes.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            self.state[path] = {"inode": st.st_ino, "offset": offset}
            return []
        complete = data[: last_nl + 1]
        self.state[path] = {"inode": st.st_ino, "offset": offset + len(complete)}
        return complete.decode("ascii", errors="replace").splitlines()


def post_batch(server_url: str, token: str, fridge: str, rows: list[dict],
               timeout: float = 30.0) -> bool:
    """POST un-acked readings as one batch. Return True on HTTP 2xx.

    Network/HTTP errors return False (not raise): the rows stay un-acked and go
    out next cycle.
    """
    try:
        resp = requests.post(
            server_url,
            json={"fridge": fridge, "readings": rows},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.warning("POST to %s failed: %s", server_url, exc)
        return False
    if 200 <= resp.status_code < 300:
        return True
    log.warning("POST to %s returned HTTP %s", server_url, resp.status_code)
    return False


def run_cycle(tailer: LogTailer, parser: Parser, spool: Spool, tz: ZoneInfo,
              post) -> int:
    """One poll cycle. `post(rows) -> bool` is injected so it can be faked in
    tests. Returns the number of readings parsed this cycle."""
    readings: list[Reading] = []
    for source, lines in tailer.read_new_lines():
        for r in parser.parse_new(source, lines):
            r.ts = to_utc(r.ts, tz)
            readings.append(r)
    spool.append(readings)

    pending = spool.unacked()
    if pending and post(pending):
        spool.mark_acked(pending)
    return len(readings)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    fridge = cfg["fridge"]
    tz = ZoneInfo(cfg["timezone"])
    poll_interval = cfg.get("poll_interval", 60)
    globs = cfg.get("log_globs") or [cfg["log_glob"]]
    retain_days = cfg.get("spool_retain_days", 7)

    parser = load_parser(cfg["parser"])
    spool = Spool(cfg.get("spool_path", "spool.sqlite"))
    tailer = LogTailer(globs, cfg.get("offset_state_path", "offsets.json"))

    def post(rows: list[dict]) -> bool:
        return post_batch(cfg["server_url"], cfg["token"], fridge, rows)

    while True:
        try:
            run_cycle(tailer, parser, spool, tz, post)
            spool.prune(datetime.now(timezone.utc) - timedelta(days=retain_days))
        except Exception:
            # Never let one bad cycle kill the daemon (§12).
            log.exception("daemon cycle error")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
