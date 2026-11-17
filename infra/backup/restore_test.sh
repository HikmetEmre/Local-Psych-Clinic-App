#!/usr/bin/env bash
# Monthly restore test (brief §12). Run on the office laptop, from the repository folder:
#
#   bash infra/backup/restore_test.sh /path/to/age-identity.txt [backup-file]
#
# The identity file is the OFFLINE private key (USB stick from the safe). Remove the stick
# afterwards. Without a backup-file argument the newest daily backup is used.
#
# What it does, all inside Docker (the laptop needs only Docker and bash):
#   1. decrypts the backup with the private key;
#   2. restores it into a throwaway PostgreSQL that is deleted at the end;
#   3. checks schema version, row counts, and that client names and session notes
#      decrypt with LOCAL_FERNET_KEYS from the env file;
#   4. records the result in Ayarlar -> Yedekleme.
set -euo pipefail

# Windows (Git Bash): keep container paths as they are, and give Docker Windows host paths.
export MSYS_NO_PATHCONV=1
hostpath() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

identity="${1:?usage: restore_test.sh <age-identity-file> [backup-file]}"
env_file="${ENV_FILE:-.env}"
backups="${BACKUP_HOST_DIR:-$PWD/backups}"
compose=(docker compose --env-file "$env_file" -f infra/docker-compose.yml)
network="klinik_default"
scratch="klinik-restore-test"

[ -f "$identity" ] || { echo "identity file not found: $identity" >&2; exit 2; }
file="${2:-$(ls -1t "$backups"/daily/*.dump.age 2>/dev/null | head -n 1 || true)}"
[ -n "$file" ] && [ -f "$file" ] || { echo "no backup file found in $backups/daily" >&2; exit 2; }
echo "restore test of: $file"

workdir="$(mktemp -d "${TMPDIR:-/tmp}/klinik-restore.XXXXXX")"
cleanup() {
  docker rm -f "$scratch" >/dev/null 2>&1 || true
  rm -rf "$workdir"
}
trap cleanup EXIT

cp "$identity" "$workdir/identity.txt"
cp "$file" "$workdir/backup.dump.age"

echo "1/4 decrypting"
docker run --rm -v "$(hostpath "$workdir"):/work" --entrypoint age klinik-backup \
  --decrypt --identity /work/identity.txt --output /work/backup.dump /work/backup.dump.age
rm -f "$workdir/identity.txt"

echo "2/4 restoring into a throwaway database"
docker rm -f "$scratch" >/dev/null 2>&1 || true
docker run -d --name "$scratch" --network "$network" \
  -e POSTGRES_PASSWORD=restore-test -e POSTGRES_DB=kasa postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do
  docker exec "$scratch" pg_isready -U postgres -d kasa >/dev/null 2>&1 && break
  sleep 1
done
docker cp "$(hostpath "$workdir/backup.dump")" "$scratch:/tmp/backup.dump"
docker cp "$(hostpath "$PWD/infra/backup/roles.sql")" "$scratch:/tmp/roles.sql"
# Server-wide roles are not part of a database dump; create them first, as on a new laptop.
docker exec "$scratch" psql -q -v ON_ERROR_STOP=1 -U postgres -d kasa -f /tmp/roles.sql
docker exec "$scratch" pg_restore --exit-on-error --no-owner -U postgres -d kasa /tmp/backup.dump

echo "3/4 checking contents and keys"
result=0
"${compose[@]}" run --rm --no-deps -T \
  -e RESTORE_DSN="postgresql://postgres:restore-test@$scratch:5432/kasa" \
  local-migrate python -m klinik_local.manage verify-restore || result=$?

echo "4/4 recording the result"
when="$(date -Iseconds)"
outcome=$([ "$result" -eq 0 ] && echo "ok" || echo "FAILED")
"${compose[@]}" exec -T local-db psql -U kasa_migrator -d kasa -q -c \
  "INSERT INTO backup_status (key, value) VALUES ('last_restore_test', '$when $outcome $(basename "$file")')
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"

if [ "$result" -eq 0 ]; then
  echo "RESTORE TEST PASSED"
else
  echo "RESTORE TEST FAILED (exit $result)" >&2
fi
exit "$result"
