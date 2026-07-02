"""Host daemon (Windows, one instance per fridge). See §6.2.

Run as a service via NSSM wrapping `python daemon.py --config config.toml`.
NSSM gives auto-restart, so the daemon itself has a watchdog.

Before going live on a new host, `python daemon.py --config config.toml --dry-run`
parses the current logs and prints what it WOULD post (per-channel counts, value
ranges, latest timestamp) without spooling, committing offsets, or contacting the
server — a safe way to validate the parser + log-path config first.

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

# The lab's timezone. Used when a config omits `timezone`, so hosts don't each
# have to set it; still overridable per config for a fridge elsewhere.
DEFAULT_TIMEZONE = "America/Los_Angeles"


class ConfigError(Exception):
    """A config file is missing a required key or has an invalid value. Raised at
    startup so a typo fails loud (visible to the service manager) instead of
    silently wedging the daemon — a fridge that never posts is an invisible
    outage."""


def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def validate_config(cfg: dict, *, require_network: bool) -> None:
    """Fail fast on a missing/invalid config. `require_network` adds the keys the
    live daemon needs to POST (server_url, token); a --dry-run doesn't need them."""
    required = ["fridge", "parser"]
    if require_network:
        required += ["server_url", "token"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ConfigError(f"config missing required key(s): {', '.join(missing)}")
    if not (cfg.get("log_globs") or cfg.get("log_glob")):
        raise ConfigError("config must set 'log_globs' (or 'log_glob')")
    tz_name = cfg.get("timezone", DEFAULT_TIMEZONE)
    try:
        ZoneInfo(tz_name)
    except Exception as exc:
        raise ConfigError(
            f"invalid timezone {tz_name!r} ({exc}); on Windows ensure the "
            "'tzdata' package is installed"
        ) from exc


def load_parser(name: str) -> Parser:
    """Import parsers/<name>.py and return its Parser subclass instance."""
    module = importlib.import_module(f"parsers.{name}")
    for obj in vars(module).values():
        # Only a Parser subclass DEFINED in this module — not one merely imported
        # into it (e.g. the shared BlueforsParser base), which would otherwise be
        # returned in place of the fridge-specific subclass.
        if (isinstance(obj, type) and issubclass(obj, Parser) and obj is not Parser
                and obj.__module__ == module.__name__):
            return obj()
    raise ValueError(f"no Parser subclass found in parsers.{name}")


def to_utc(ts: datetime, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=tz)
    return ts.astimezone(timezone.utc)


class LogTailer:
    """Tracks (path -> file signature, byte offset) across rotation (§6.2 step 1).

    State is persisted to JSON so a restart resumes where it left off and reads
    whatever was appended while the daemon was down. A newly-seen file is read
    from the start; an in-place rotation (same path, new file signature — see
    _signature) or a truncation resets the offset to 0.
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
        """Return newly-appended lines per file. Advances the offsets IN MEMORY
        only — the caller must call commit() after the returned lines have been
        durably spooled, so a crash in between re-reads them (the spool dedups)
        rather than losing them (§3.2)."""
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
        return results

    def commit(self) -> None:
        """Persist the advanced read offsets. Call this only AFTER the lines from
        read_new_lines() are durably spooled: persisting the cursor first would,
        on a crash before the spool commit, skip past never-buffered lines and
        lose them permanently."""
        self._save_state()

    def rollback(self) -> None:
        """Discard in-memory offset advances not yet commit()ed. Call after a
        failed cycle so the next cycle re-reads the uncommitted lines instead of
        skipping them until a restart reloads the persisted state."""
        self.state = self._load_state()

    @staticmethod
    def _signature(st: os.stat_result) -> str:
        """Identity token for rotation detection. st_ino uniquely identifies a
        file on POSIX and local NTFS, but is 0 on Windows network/mapped drives
        and some non-NTFS volumes — collapsing every file to the same identity, so
        an in-place rotation (same path, replaced file) would go undetected and
        the stale offset would seek into the new file. When st_ino is unavailable,
        fall back to the creation time, which is st_ctime on Windows and still
        changes when a new file replaces one at the same path. (On POSIX st_ino is
        always populated, so the ctime branch — where st_ctime is instead the
        metadata-change time and shifts on every append — is never taken.)"""
        if st.st_ino:
            return f"ino:{st.st_ino}"
        return f"ctime:{st.st_ctime_ns}"

    def _read_file(self, path: str) -> list[str]:
        st = os.stat(path)
        sig = self._signature(st)
        prev = self.state.get(path)
        if prev is None or prev.get("sig") != sig or st.st_size < prev.get("offset", 0):
            offset = 0   # new file, rotated-in-place, or truncated
        else:
            offset = prev["offset"]

        if offset >= st.st_size:
            self.state[path] = {"sig": sig, "offset": st.st_size}
            return []

        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(st.st_size - offset)

        # Hold back a partial final line (no trailing newline) until it completes.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            self.state[path] = {"sig": sig, "offset": offset}
            return []
        complete = data[: last_nl + 1]
        self.state[path] = {"sig": sig, "offset": offset + len(complete)}
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
    try:
        for source, lines in tailer.read_new_lines():
            for r in parser.parse_new(source, lines):
                r.ts = to_utc(r.ts, tz)
                readings.append(r)
        spool.append(readings)
    except Exception:
        # read_new_lines() already advanced the in-memory offsets; a failure
        # before the spool is durable must roll them back so the next cycle
        # re-reads these lines rather than skipping them until a restart.
        tailer.rollback()
        raise
    # Advance the persisted read cursor only now that the readings are durably
    # spooled — see LogTailer.commit(). A crash before this re-reads the lines
    # next start and the spool dedups them; a crash after loses nothing.
    tailer.commit()

    pending = spool.unacked()
    if pending and post(pending):
        spool.mark_acked(pending)
    return len(readings)


def run_dry(cfg: dict) -> list[Reading]:
    """Parse the current logs once and return the readings that WOULD be posted,
    printing a summary. No spool, no offset commit, no network — for validating a
    host's parser + log-path config BEFORE going live (`daemon.py --dry-run`)."""
    tz = ZoneInfo(cfg.get("timezone", DEFAULT_TIMEZONE))
    globs = cfg.get("log_globs") or [cfg["log_glob"]]
    parser = load_parser(cfg["parser"])
    # Empty, throwaway offset state (os.devnull reads as {}), so every current
    # file is read from the start and nothing is ever persisted — commit() is
    # never called, so the null path is never written.
    tailer = LogTailer(globs, os.devnull)

    readings: list[Reading] = []
    for source, lines in tailer.read_new_lines():
        for r in parser.parse_new(source, lines):
            r.ts = to_utc(r.ts, tz)
            readings.append(r)
    _print_dry_run_summary(cfg, globs, readings)
    return readings


def _print_dry_run_summary(cfg: dict, globs: list[str], readings: list[Reading]) -> None:
    files = sorted({p for pattern in globs for p in glob.glob(pattern)})
    tz_name = cfg.get("timezone", DEFAULT_TIMEZONE)
    print(f"DRY RUN — fridge={cfg['fridge']} parser={cfg['parser']} tz={tz_name}")
    print(f"log_globs: {globs}")
    print(f"matched {len(files)} file(s):")
    for path in files[:20]:
        print(f"  {path}")
    if len(files) > 20:
        print(f"  ... (+{len(files) - 20} more)")
    print(f"parsed {len(readings)} reading(s) that WOULD be posted")
    if not readings:
        print("NO READINGS — check log_globs path/pattern and that the logger is writing.")
        return
    by_channel: dict[str, list[Reading]] = {}
    for r in readings:
        by_channel.setdefault(r.channel, []).append(r)
    print(f"{'channel':<10}{'count':>7}  {'unit':<5}{'min':>14}{'max':>14}  latest_ts (UTC)")
    for channel in sorted(by_channel):
        rows = by_channel[channel]
        values = [r.value for r in rows]
        latest = max(r.ts for r in rows)
        print(f"{channel:<10}{len(rows):>7}  {rows[0].unit:<5}"
              f"{min(values):>14.6g}{max(values):>14.6g}  {latest.isoformat()}")
    print("check: expected channels all present? values physically plausible? "
          "timestamps recent and in UTC?")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="parse the current logs and print what would be posted, "
                         "then exit — no spool, no offset commit, no network")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        validate_config(cfg, require_network=False)
        run_dry(cfg)
        return
    validate_config(cfg, require_network=True)
    fridge = cfg["fridge"]
    tz_name = cfg.get("timezone", DEFAULT_TIMEZONE)
    tz = ZoneInfo(tz_name)
    poll_interval = cfg.get("poll_interval", 60)
    globs = cfg.get("log_globs") or [cfg["log_glob"]]
    retain_days = cfg.get("spool_retain_days", 7)

    parser = load_parser(cfg["parser"])
    spool = Spool(cfg.get("spool_path", "spool.sqlite"))
    tailer = LogTailer(globs, cfg.get("offset_state_path", "offsets.json"))
    log.info("daemon starting: fridge=%s tz=%s poll=%ss globs=%s -> %s",
             fridge, tz_name, poll_interval, globs, cfg["server_url"])

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
    try:
        main()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        raise SystemExit(2)
