#!/usr/bin/env bash
# Restore an encrypted backup made by backup.sh into an EMPTY database.
#
#   scripts/restore.sh BACKUP_FILE TARGET_DATABASE_URL
#
# Needs BACKUP_PASSPHRASE. Never point TARGET at the live database: restore into a new Neon
# branch or a scratch Postgres, check it, and only then decide what to do (docs/database.md
# section 6). The target must be empty; this script refuses one that already has tables.
set -euo pipefail

in="${1:?usage: scripts/restore.sh BACKUP_FILE TARGET_DATABASE_URL}"
target="${2:?usage: scripts/restore.sh BACKUP_FILE TARGET_DATABASE_URL}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is not set}"

# Same settings as backup.sh.
enc=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass env:BACKUP_PASSPHRASE)

existing=$(psql "$target" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [ "$existing" != "0" ]; then
  echo "refusing: the target already has $existing tables in public" >&2
  exit 1
fi

openssl enc -d "${enc[@]}" -in "$in" \
  | pg_restore --no-owner --no-privileges --exit-on-error --dbname="$target"
echo "restore ok"
