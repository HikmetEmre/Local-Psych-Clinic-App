#!/usr/bin/env bash
# One encrypted backup of the office database.
#
#   klinik-backup daily|hourly|cloud
#
# Environment:
#   PGHOST, PGDATABASE, PGUSER (kasa_backup), PGPASSWORD
#   AGE_RECIPIENT     public key (age1...). Only the matching private key, kept offline,
#                     can decrypt the files.
#   BACKUP_DIR        default /backups
#   RCLONE_REMOTE     e.g. "gdrive:klinik-yedek" (optional; rclone config in /config/rclone)
#   CLOUD_DUMP_URL    (cloud only) postgresql://... of the Supabase database, read access
#
# Retention: 30 daily, 12 monthly (first daily of each month), 48 hourly, 8 weekly cloud.
set -euo pipefail

kind="${1:-daily}"
case "$kind" in daily|hourly|cloud) ;; *) echo "usage: $0 daily|hourly|cloud" >&2; exit 2 ;; esac

: "${AGE_RECIPIENT:?AGE_RECIPIENT is not set}"
backup_dir="${BACKUP_DIR:-/backups}"
stamp="$(date +%Y%m%d-%H%M)"
mkdir -p "$backup_dir/daily" "$backup_dir/monthly" "$backup_dir/hourly" "$backup_dir/cloud"

status() {
  # Report to the database so Klinik Kasa can show it (Ayarlar -> Yedekleme).
  psql -v ON_ERROR_STOP=1 -q -c "INSERT INTO backup_status (key, value, updated_at)
    VALUES ('$1', '$2', now())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()" >/dev/null || true
}

fail() {
  echo "backup FAILED: $1" >&2
  status last_error "$(date -Iseconds) $kind: $1"
  exit 1
}

if [ "$kind" = cloud ]; then
  : "${CLOUD_DUMP_URL:?CLOUD_DUMP_URL is not set}"
  target="$backup_dir/cloud/klinik-web-$stamp.dump.age"
  dump=(pg_dump "$CLOUD_DUMP_URL" --schema=app --format=custom --compress=6 --no-owner --no-privileges)
else
  target="$backup_dir/$kind/kasa-$stamp.dump.age"
  # kasa_backup bypasses row-level security, so every row is included.
  dump=(pg_dump --format=custom --compress=6 --no-owner --no-privileges)
fi
partial="$target.partial"

if ! "${dump[@]}" \
     | age --encrypt --recipient "$AGE_RECIPIENT" --output "$partial"; then
  rm -f "$partial"
  fail "pg_dump or age failed"
fi
mv "$partial" "$target"
size="$(stat -c %s "$target")"
[ "$size" -gt 100 ] || fail "backup file is suspiciously small ($size bytes)"

if [ "$kind" = daily ] && [ "$(date +%d)" = "01" ]; then
  cp "$target" "$backup_dir/monthly/kasa-$(date +%Y%m).dump.age"
fi

prune() { # keep the newest N files of a folder (an empty folder is fine)
  local files
  shopt -s nullglob
  files=("$1"/*.dump.age)
  shopt -u nullglob
  [ "${#files[@]}" -gt "$2" ] || return 0
  ls -1t "${files[@]}" | tail -n +"$(( $2 + 1 ))" | xargs -r rm -f
}
prune "$backup_dir/daily" 30
prune "$backup_dir/monthly" 12
prune "$backup_dir/hourly" 48
prune "$backup_dir/cloud" 8

if [ -n "${RCLONE_REMOTE:-}" ]; then
  # sync mirrors the local retention: what is pruned here is pruned in Drive too.
  if ! rclone sync "$backup_dir" "$RCLONE_REMOTE" --config /config/rclone/rclone.conf \
       --include "*.dump.age" --transfers 2 --retries 5 --low-level-retries 10; then
    status last_error "$(date -Iseconds) upload to $RCLONE_REMOTE failed"
    echo "upload failed; the local copy is kept" >&2
  else
    status last_upload_ok "$(date -Iseconds)"
  fi
fi

status "last_${kind}_ok" "$(date -Iseconds)"
status last_file "$(basename "$target") ($size bytes)"
echo "backup ok: $target ($size bytes)"
