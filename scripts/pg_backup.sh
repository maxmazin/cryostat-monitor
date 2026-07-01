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
# Connection uses standard libpq env (PGHOST/PGPORT/PGUSER) or, by default, the
# local socket as the invoking OS user (peer auth as `cryo` on labmanager).
set -euo pipefail

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

# Prune only our own dumps older than the retention window.
find "$DIR" -maxdepth 1 -type f -name 'cryo-*.dump' -mtime "+$KEEP_DAYS" -print -delete
