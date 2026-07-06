#!/usr/bin/env bash
# Nightly logical backup of the cryostat PostgreSQL database (Phase 4).
#
# Writes a compressed custom-format dump (pg_dump -Fc, restore with pg_restore)
# to a local directory and prunes dumps older than the retention window. Run on
# labmanager via the cryo-backup.timer systemd unit. The local backup dir is
# meant to be picked up by the site's existing Restic->NAS job for offsite copies
# (§10 Phase 4); this script only produces the local dump + rotation.
#
# Config (env, all optional):
#   CRYO_DB               database name           (default: cryo)
#   CRYO_BACKUP_DIR       output directory        (default: /var/backups/cryostat)
#   CRYO_BACKUP_KEEP_DAYS days of dumps to keep    (default: 14)
#   BACKUP_PING_URL       healthchecks.io check URL for the backup job. If set,
#                         the script pings it on success and $BACKUP_PING_URL/fail
#                         on failure, so a silently failing nightly backup pages
#                         instead of being discovered at restore time. If unset,
#                         no ping is attempted (keeps the secret out of the repo).
#                         Set it in /etc/cryostat-monitor/backup.env — the
#                         cryo-backup.service unit loads that file.
# Connection uses standard libpq env (PGHOST/PGPORT/PGUSER) or, by default, the
# local socket as the invoking OS user (peer auth as `cryo` on labmanager).
set -euo pipefail

# Report the outcome to healthchecks.io if configured. The ping itself must
# never change the unit's result — a flaky network shouldn't fail a good backup
# (or mask a bad one behind a curl error), hence `|| true`.
ping_healthchecks() {
    local suffix="$1"    # "" on success, "/fail" on failure
    [[ -n "${BACKUP_PING_URL:-}" ]] || return 0
    curl -fsS --max-time 10 --retry 3 -o /dev/null "$BACKUP_PING_URL$suffix" || true
}
trap '[[ $? -eq 0 ]] || ping_healthchecks /fail' EXIT

DB="${CRYO_DB:-cryo}"
DIR="${CRYO_BACKUP_DIR:-/var/backups/cryostat}"
KEEP_DAYS="${CRYO_BACKUP_KEEP_DAYS:-14}"

mkdir -p "$DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"    # UTC, colon-free for portable filenames
out="$DIR/cryo-$stamp.dump"

# Dump to a .partial name and rename on success, so a dump interrupted mid-write
# never leaves a file that looks complete.
pg_dump -Fc "$DB" -f "$out.partial"
mv "$out.partial" "$out"
echo "wrote $out"

# Prune only our own dumps older than the retention window. The dump above
# already succeeded, so a prune hiccup (a file vanishing mid-scan, a lock, a
# permission blip) must not fail the unit under `set -e` and mask a good backup
# — warn and move on.
if ! find "$DIR" -maxdepth 1 -type f -name 'cryo-*.dump' -mtime "+$KEEP_DAYS" -print -delete; then
    echo "warning: pruning old dumps had errors (the new dump is intact)" >&2
fi

ping_healthchecks ""
