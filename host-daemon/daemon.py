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
import math
import os
import re
import sqlite3
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from parsers.base import Parser, Reading
from spool import Spool, open_or_recover

log = logging.getLogger("cryo.daemon")

# The lab's timezone. Used when a config omits `timezone`, so hosts don't each
# have to set it; still overridable per config for a fridge elsewhere.
DEFAULT_TIMEZONE = "America/Los_Angeles"

# A newly-seen log file not modified within this many days is treated as historical
# backlog and skipped (unless backfill is on) — see LogTailer. The window is
# anchored to the last successful cycle (persisted), so an outage longer than the
# window still catches up everything written while the daemon was down.
DEFAULT_BACKFILL_WINDOW_DAYS = 2

# Per-cycle read caps. Without them, backfill=true would accumulate a multi-year
# log tree in memory in a single cycle (MemoryError -> rollback -> identical retry
# forever). Bounding the bytes consumed per cycle drains any backlog incrementally;
# normal operation (daily ~1/min appends) never comes near these limits.
READ_BUDGET_BYTES = 16 * 1024 * 1024   # total per cycle
FILE_CHUNK_BYTES = 4 * 1024 * 1024     # per file per cycle

# BlueFors dated day folders: "26-06-30" == 2026-06-30 (YY-MM-DD).
_DATED_FOLDER_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})$")


def _parse_folder_date(name: str) -> date | None:
    """Parse a BlueFors YY-MM-DD day-folder name; None if it isn't one."""
    m = _DATED_FOLDER_RE.match(name)
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


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

    On a log tree with years of dated folders, reading all of it on first start
    would flood the server, so by default a newly-seen file not modified within
    `backfill_window_days` BEFORE THE LAST SUCCESSFUL CYCLE is skipped as
    historical backlog (the current day's file is recent, so live data flows
    immediately; anchoring the window to the persisted last_run means an outage
    of any length still catches up on restart). Set backfill=True to ingest the
    whole history — a per-cycle byte budget drains it incrementally with bounded
    memory. Offsets for old/gone files are pruned so the state stays bounded.
    """

    def __init__(self, globs: list[str], state_path: str, *, backfill: bool = False,
                 backfill_window_days: float = DEFAULT_BACKFILL_WINDOW_DAYS) -> None:
        self.globs = globs
        self.state_path = state_path
        self.backfill = backfill
        self.window_seconds = backfill_window_days * 86400
        self.state, self.last_run = self._load_state()
        # Consecutive read-failure count per path, for rate-limited warnings.
        self._read_failures: dict[str, int] = {}
        # True when the last read_new_lines() left data behind — budget hit,
        # a file only partially drained, or a read error — see commit().
        self._incomplete_read = False

    def _read_state_file(self) -> dict | None:
        # Windows AV can hold offsets.json briefly at service start; retry a
        # PermissionError a few times before giving up loudly.
        for attempt in range(3):
            try:
                with open(self.state_path) as fh:
                    return json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError):
                return None
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.5)
        return None   # unreachable; keeps the type checker happy

    def _load_state(self) -> tuple[dict[str, dict], float | None]:
        """Load {"files": {path: {sig, offset}}, "last_run": epoch}. A pre-existing
        flat file (the format deployed on whitefridge before last_run was added)
        is the files map itself; it loads with last_run=None, which falls back to
        first-run behavior for the backlog cutoff."""
        raw = self._read_state_file()
        if raw is None or not isinstance(raw, dict):
            return {}, None
        if "files" in raw:
            return raw["files"], raw.get("last_run")
        return raw, None

    def _save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"files": self.state, "last_run": self.last_run}, fh)
            # fsync before the rename: on power loss the rename can otherwise
            # survive while the data doesn't, leaving an empty offsets.json.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)   # atomic on POSIX and Windows

    def read_new_lines(self) -> list[tuple[str, list[str]]]:
        """Return newly-appended lines per file. Advances the offsets IN MEMORY
        only — the caller must call commit() after the returned lines have been
        durably spooled, so a crash in between re-reads them (the spool dedups)
        rather than losing them (§3.2)."""
        results: list[tuple[str, list[str]]] = []
        now = time.time()
        # Anchor the backlog cutoff to the last successful cycle, not the current
        # clock: a host powered off longer than the window must still ship the gap
        # days on restart, and a forward clock jump must not blind the daemon.
        # min() guards against a backward clock jump making the cutoff futuristic.
        anchor = min(now, self.last_run) if self.last_run is not None else now
        cutoff = anchor - self.window_seconds
        self._incomplete_read = False
        paths = sorted({p for pattern in self.globs for p in glob.glob(pattern)})
        paths = self._filter_by_folder_date(paths, cutoff)
        budget = READ_BUDGET_BYTES
        for path in paths:
            if budget <= 0:
                self._incomplete_read = True
                break
            try:
                lines, consumed, drained = self._read_file(
                    path, cutoff, min(budget, FILE_CHUNK_BYTES))
            except OSError as exc:
                # File vanished or unreadable mid-poll; try again next cycle. Warn
                # on the first failure, then roughly hourly (every 60th consecutive
                # failure at the default 60 s poll) — a channel that stays dark
                # must leave host-side evidence without flooding the log.
                count = self._read_failures.get(path, 0) + 1
                self._read_failures[path] = count
                if count % 60 == 1:
                    log.warning("cannot read %s (consecutive failure #%d): %s",
                                path, count, exc)
                # An unread file must hold last_run back like an unvisited one:
                # advancing it would age the file past the cutoff and strand it.
                self._incomplete_read = True
                continue
            self._read_failures.pop(path, None)
            budget -= consumed
            if not drained:
                self._incomplete_read = True
            if lines:
                results.append((os.path.basename(path), lines))
        self._prune_state(paths, cutoff)
        return results

    def _filter_by_folder_date(self, paths: list[str], cutoff: float) -> list[str]:
        """Cheap pre-filter before statting: BlueFors nests files in dated
        YY-MM-DD day folders, so on a years-deep tree most paths can be dismissed
        by folder name alone instead of os.stat()ing every file every cycle. A
        folder name that doesn't parse as a date is kept (conservative); one day
        of margin absorbs timezone/rounding at the cutoff boundary."""
        if self.backfill:
            return paths
        cutoff_date = datetime.fromtimestamp(cutoff).date() - timedelta(days=1)
        kept = []
        for path in paths:
            folder = _parse_folder_date(os.path.basename(os.path.dirname(path)))
            if folder is None or folder >= cutoff_date:
                kept.append(path)
        return kept

    def _prune_state(self, current_paths: list[str], cutoff: float) -> None:
        """Bound offsets.json on a years-deep tree: drop entries for files that are
        gone, or (outside full-backfill mode) not modified since the cutoff. A
        dropped old file re-encountered later is skipped by mtime, so pruning never
        causes a re-read. In backfill mode nothing is pruned — dropping an entry
        there would re-ship the whole file."""
        if self.backfill:
            return
        if not current_paths and self.state:
            # A transient share hiccup can make the glob come back empty; wiping
            # every offset then would re-read all files when it recovers.
            log.warning("log globs matched no files while %d are tracked; "
                        "skipping offset prune this cycle", len(self.state))
            return
        current = set(current_paths)
        for path in list(self.state):
            try:
                st = os.stat(path)
            except FileNotFoundError:
                del self.state[path]   # file is gone; nothing left to ship
                continue
            except OSError:
                # Transient stat error (share hiccup, AV lock): keep the entry —
                # deleting it would reset a live file's offset.
                continue
            if st.st_size > self.state[path].get("offset", 0):
                # Unshipped bytes remain (e.g. a gap file mid-drain that aged out
                # of the candidate list); dropping the entry would orphan them.
                continue
            if path not in current or st.st_mtime < cutoff:
                del self.state[path]

    def commit(self) -> None:
        """Persist the advanced read offsets. Call this only AFTER the lines from
        read_new_lines() are durably spooled: persisting the cursor first would,
        on a crash before the spool commit, skip past never-buffered lines and
        lose them permanently.

        last_run (the backlog-cutoff anchor) only advances when the last read
        fully drained every candidate file — while a catch-up is still draining
        (budget hit, a file chunk-truncated, or a read error), advancing it could
        strand the leftover data outside the window."""
        if not self._incomplete_read:
            self.last_run = time.time()
        self._save_state()

    def rollback(self) -> None:
        """Discard in-memory offset advances not yet commit()ed. Call after a
        failed cycle so the next cycle re-reads the uncommitted lines instead of
        skipping them until a restart reloads the persisted state."""
        self.state, self.last_run = self._load_state()

    @staticmethod
    def _signature(st: os.stat_result) -> dict:
        """Identity for rotation detection: BOTH st_ino and st_ctime_ns. st_ino
        uniquely identifies a file on POSIX and local NTFS but is 0 on Windows
        network/mapped drives and under some AV interference; ctime is the
        creation time on Windows and still changes when a new file replaces one
        at the same path. Storing both means a flap in stat quality (ino
        appearing/disappearing between polls) still matches on the other field
        instead of looking like a replaced file and resetting the offset. (On
        POSIX st_ino is always populated, so the ctime comparison — where
        st_ctime is the metadata-change time and shifts on every append — never
        decides.) Known, accepted blind spot: Windows file tunneling can reuse
        the ctime of a file recreated within ~15 s — BlueFors never recreates
        its logs."""
        return {"ino": st.st_ino, "ctime": st.st_ctime_ns}

    @staticmethod
    def _parse_signature(raw) -> dict:
        """Normalize a persisted signature. Old deployments (whitefridge) stored
        strings "ino:X" or "ctime:Y"; parse those into the dict form with the
        missing field 0 (= unknown) so upgrading doesn't re-read every file."""
        if isinstance(raw, dict):
            return {"ino": raw.get("ino", 0), "ctime": raw.get("ctime", 0)}
        if isinstance(raw, str):
            kind, _, val = raw.partition(":")
            try:
                num = int(val)
            except ValueError:
                num = 0
            if kind == "ino":
                return {"ino": num, "ctime": 0}
            if kind == "ctime":
                return {"ino": 0, "ctime": num}
        return {"ino": 0, "ctime": 0}

    @staticmethod
    def _same_file(old: dict, new: dict) -> bool:
        """Same file iff both inos are known (nonzero) and equal; when either ino
        is unavailable, fall back to comparing ctimes."""
        if old["ino"] and new["ino"]:
            return old["ino"] == new["ino"]
        return old["ctime"] == new["ctime"]

    def _read_file(self, path: str, cutoff: float,
                   max_bytes: int) -> tuple[list[str], int, bool]:
        """Read up to max_bytes of new data from `path`. Returns (complete lines,
        bytes consumed, drained). The offset advances only to the last newline
        within what was actually read, so a budget-truncated read resumes exactly
        where it left off next cycle. `drained` is False when the max_bytes cap
        cut the read short of the file's end — the caller must then hold last_run
        back so the leftover doesn't age out of the backlog window."""
        st = os.stat(path)
        sig = self._signature(st)
        prev = self.state.get(path)
        if prev is None:
            # Not yet tracked. Skip a historical-backlog file (unless full backfill
            # is on): reading years of dated folders on first start would flood the
            # server. The cutoff is anchored to the last successful cycle, so files
            # written during ANY outage are newer than it and still picked up.
            if not self.backfill and st.st_mtime < cutoff:
                return [], 0, True
            offset = 0
        elif (not self._same_file(self._parse_signature(prev.get("sig")), sig)
                or st.st_size < prev.get("offset", 0)):
            offset = 0   # rotated-in-place, or truncated
        else:
            offset = prev["offset"]

        if offset >= st.st_size:
            self.state[path] = {"sig": sig, "offset": st.st_size}
            return [], 0, True

        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(min(st.st_size - offset, max_bytes))
        drained = offset + len(data) >= st.st_size

        # Hold back a partial final line (no trailing newline) until it completes.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            self.state[path] = {"sig": sig, "offset": offset}
            return [], len(data), drained
        complete = data[: last_nl + 1]
        self.state[path] = {"sig": sig, "offset": offset + len(complete)}
        return (complete.decode("ascii", errors="replace").splitlines(),
                len(data), drained)


def post_batch(server_url: str, token: str, fridge: str, rows: list[dict],
               timeout: float = 30.0) -> bool:
    """POST un-acked readings as one batch. Return True only on a genuine ingest
    ack: HTTP 2xx from the endpoint itself with a JSON body containing
    "received". Anything else returns False (not raise) — the rows stay un-acked
    and go out next cycle — with a log that tells the operator what to fix.

    Redirects are NOT followed: 301/302/303 would convert the POST to a GET and
    a landing page's 200 would ack (and later prune) rows that never reached the
    server — silent permanent data loss.
    """
    try:
        resp = requests.post(
            server_url,
            json={"fridge": fridge, "readings": rows},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        log.warning("POST to %s failed: %s", server_url, exc)
        return False
    if 300 <= resp.status_code < 400:
        log.error("POST to %s redirected (HTTP %s to %r); refusing to follow — "
                  "check that server_url points directly at the ingest endpoint",
                  server_url, resp.status_code, resp.headers.get("Location"))
        return False
    if 200 <= resp.status_code < 300:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or "received" not in body:
            log.error("POST to %s returned HTTP %s but not an ingest ack "
                      "(body starts %r); not acking — check that server_url "
                      "points at the ingest endpoint",
                      server_url, resp.status_code, resp.text[:200])
            return False
        return True
    if resp.status_code in (401, 403):
        log.error("POST to %s rejected (HTTP %s): the bearer token was refused — "
                  "was it rotated on the server or mistyped in config.toml?",
                  server_url, resp.status_code)
    elif 400 <= resp.status_code < 500:
        log.error("POST to %s returned HTTP %s (body starts %r); the server is "
                  "rejecting this batch — it will retry until fixed",
                  server_url, resp.status_code, resp.text[:200])
    else:
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
                # A non-finite value would poison the pipeline: NaN becomes SQL
                # NULL (NOT NULL violation silently swallowed by INSERT OR
                # IGNORE) and inf makes json.dumps emit invalid JSON the server
                # 400s forever — a permanent poison-pill stall.
                if not math.isfinite(r.value):
                    log.warning("dropping non-finite reading: channel=%s value=%r "
                                "ts=%s (from %s)", r.channel, r.value, r.ts, source)
                    continue
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
    matched = sorted({p for pattern in globs for p in glob.glob(pattern)})
    # Validate against only the most recent dated folder — enough to confirm the
    # parser + path + timezone, and safe when the tree holds years of history.
    sampled: list[str] = []
    if matched:
        newest_dir = max({os.path.dirname(p) for p in matched}, key=os.path.getmtime)
        sampled = [p for p in matched if os.path.dirname(p) == newest_dir]

    readings: list[Reading] = []
    for path in sampled:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        lines = data.decode("ascii", errors="replace").splitlines()
        for r in parser.parse_new(os.path.basename(path), lines):
            r.ts = to_utc(r.ts, tz)
            readings.append(r)
    _print_dry_run_summary(cfg, matched, sampled, readings)
    return readings


def _print_dry_run_summary(cfg: dict, matched: list[str], sampled: list[str],
                           readings: list[Reading]) -> None:
    tz_name = cfg.get("timezone", DEFAULT_TIMEZONE)
    print(f"DRY RUN — fridge={cfg['fridge']} parser={cfg['parser']} tz={tz_name}")
    print(f"matched {len(matched)} file(s) across the tree; "
          f"validating the most recent day ({len(sampled)} file(s)):")
    for path in sampled:
        print(f"  {path}")
    print(f"parsed {len(readings)} reading(s) from that day that WOULD be posted")
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

    backfill = cfg.get("backfill", False)
    window_days = cfg.get("backfill_window_days", DEFAULT_BACKFILL_WINDOW_DAYS)
    parser = load_parser(cfg["parser"])
    spool_path = cfg.get("spool_path", "spool.sqlite")
    # A corrupt spool must not crash-loop the service: quarantine + start fresh.
    spool = open_or_recover(spool_path)
    tailer = LogTailer(globs, cfg.get("offset_state_path", "offsets.json"),
                       backfill=backfill, backfill_window_days=window_days)
    log.info("daemon starting: fridge=%s tz=%s poll=%ss backfill=%s(window=%sd) globs=%s -> %s",
             fridge, tz_name, poll_interval, backfill, window_days, globs, cfg["server_url"])

    def post(rows: list[dict]) -> bool:
        return post_batch(cfg["server_url"], cfg["token"], fridge, rows)

    while True:
        try:
            run_cycle(tailer, parser, spool, tz, post)
            spool.prune(datetime.now(timezone.utc) - timedelta(days=retain_days))
        except sqlite3.OperationalError as exc:
            # Transient ("database is locked" from AV/backup holding the file,
            # "disk I/O error"): NOT corruption — quarantining a healthy spool
            # would pull its un-acked rows out of the pipeline. Retry next cycle.
            log.warning("spool temporarily unavailable (%s); retrying next cycle", exc)
        except sqlite3.DatabaseError:
            # Corruption-shaped ("database disk image is malformed") would fail
            # every subsequent cycle; quarantine and re-open instead of retrying
            # doomed operations forever.
            log.exception("spool database error; attempting quarantine + fresh spool")
            try:
                spool.close()
            except sqlite3.Error:
                pass   # connection already unusable; the file gets quarantined
            try:
                spool = open_or_recover(spool_path)
            except Exception:
                # Rename blocked (Windows lock) or re-open failed: keep the old
                # object and retry next cycle rather than killing the daemon.
                log.exception("spool recovery failed; retrying next cycle")
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
