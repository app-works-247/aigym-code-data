#!/usr/bin/env bash
# Encrypted database backup (docs/database.md section 6, layer 2).
#
#   scripts/backup.sh OUT_FILE
#
# Needs DATABASE_URL and BACKUP_PASSPHRASE in the environment, pg_dump and pg_restore at least
# as new as the server (Neon runs Postgres 18), and openssl. Writes a pg_dump custom-format
# archive encrypted with AES-256 (key from BACKUP_PASSPHRASE through PBKDF2), then decrypts it
# again and lists its contents, so a backup that cannot be read back fails here, not on the day
# it is needed. Nothing secret is printed.
set -euo pipefail

out="${1:?usage: scripts/backup.sh OUT_FILE}"
: "${DATABASE_URL:?DATABASE_URL is not set}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is not set}"

# Same settings in restore.sh: change both together.
enc=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass env:BACKUP_PASSPHRASE)

pg_dump --format=custom --no-owner --no-privileges --dbname="$DATABASE_URL" \
  | openssl enc -e -salt "${enc[@]}" -out "$out"

# Decrypt to a file: pg_restore --list stops reading a pipe after the table of contents, which
# makes openssl fail with a broken pipe. Decrypting all of it also checks the whole file.
check=$(mktemp)
trap 'rm -f "$check"' EXIT
openssl enc -d "${enc[@]}" -in "$out" -out "$check"
entries=$(pg_restore --list "$check" | grep -c ' TABLE DATA ')
size=$(wc -c <"$out" | tr -d ' ')
echo "backup ok: $out, $size bytes, $entries tables with data"
