#!/usr/bin/env bash
# Restores an encrypted backup into the judge database. Run as root in judge-prod:
#   bash deploy/restore.sh /var/backups/judge/judge-20261123-023000.dump.age /root/backup-key.txt
# The second file is the age private key. judge-prod can't see C:, so copy it in through stdin
# and delete it afterwards (see bootstrap.sh's notes).
#
# It refuses to overwrite a database that already holds users or jobs unless REPLACE=1, since
# restoring throws away everything newer than the backup.
set -euo pipefail

BACKUP=$1
IDENTITY=$2
CONTAINER=${PG_CONTAINER:-judge-postgres}
psql() { docker exec -i "$CONTAINER" psql -U judge -v ON_ERROR_STOP=1 -qtA "$@"; }

[ -r "$BACKUP" ] || { echo "can't read $BACKUP" >&2; exit 1; }
[ -r "$IDENTITY" ] || { echo "can't read $IDENTITY" >&2; exit 1; }

has_data=$(psql -d judge -c "SELECT to_regclass('public.users') IS NOT NULL AND (EXISTS (SELECT FROM users)
    OR EXISTS (SELECT FROM jobs) OR EXISTS (SELECT FROM api_clients) OR EXISTS (SELECT FROM problems))" 2> /dev/null || echo f)
if [ "$has_data" = t ] && [ "${REPLACE:-0}" != 1 ]; then
  echo "The database already holds data. To replace it with the backup, run again with REPLACE=1." >&2
  exit 1
fi

# Nothing may write while the database is replaced
services=$(systemctl list-units --plain --no-legend --state=active 'judge-api.service' 'judge-worker@*.service' | awk '{print $1}')
[ -z "$services" ] || systemctl stop $services

trap '[ -z "$services" ] || systemctl start $services' EXIT

# Decrypt once to nowhere first. age authenticates every chunk, so a wrong key or a damaged file
# fails here, before the database is dropped. The second pass streams straight into pg_restore,
# so the plain dump never touches the disk.
age --decrypt -i "$IDENTITY" "$BACKUP" > /dev/null

psql -d postgres -c "DROP DATABASE IF EXISTS judge WITH (FORCE)" -c "CREATE DATABASE judge OWNER judge"
age --decrypt -i "$IDENTITY" "$BACKUP" | docker exec -i "$CONTAINER" pg_restore -U judge -d judge --no-owner --exit-on-error
echo "restored $BACKUP: $(psql -d judge -c 'SELECT count(*) FROM users') users, $(psql -d judge -c 'SELECT count(*) FROM jobs') jobs"
