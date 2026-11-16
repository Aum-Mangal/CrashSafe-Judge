#!/usr/bin/env bash
# One encrypted backup of the judge database, kept in /var/backups/judge with the newest 14.
# Run as root in judge-prod, nightly by judge-backup.timer (ARCHITECTURE.md §12).
#
# The dump holds emails and password hashes, so it is encrypted with age before it touches the
# disk (DECISIONS.md, 27). judge-prod holds only the public key, in /etc/judge/backup-recipient.txt:
# it can write backups but not read them. The private key stays off this machine.
set -euo pipefail

RECIPIENT=${BACKUP_RECIPIENT:-/etc/judge/backup-recipient.txt}
DIR=${BACKUP_DIR:-/var/backups/judge}
KEEP=14
CONTAINER=${PG_CONTAINER:-judge-postgres}

# Only the key itself: a trailing space or carriage return copied in from Windows makes age
# refuse the whole file (checked 16 Nov)
KEY=$(grep -o -m1 "age1[0-9a-z]*" "$RECIPIENT" 2> /dev/null || true)
if [ -z "$KEY" ]; then
  echo "No age public key in $RECIPIENT. Make a key pair where the private key will live," >&2
  echo "with age-keygen, and put the public key (age1...) in that file." >&2
  exit 1
fi

umask 077
install -d -m 700 "$DIR"
name="judge-$(date -u +%Y%m%d-%H%M%S).dump.age"
part="$DIR/.$name.part"
trap 'rm -f "$part"' EXIT
# pipefail makes a failed pg_dump fail the backup, instead of keeping a short file
docker exec "$CONTAINER" pg_dump --format=custom -U judge judge | age --encrypt -r "$KEY" > "$part"
mv "$part" "$DIR/$name"
echo "backup: $DIR/$name ($(stat -c %s "$DIR/$name") bytes)"

# Keep the newest $KEEP. Names sort by time, so the oldest come first
ls -1 "$DIR"/judge-*.dump.age | head -n -"$KEEP" | while read -r old; do
  rm -f -- "$old"
  echo "removed the old backup $old"
done
