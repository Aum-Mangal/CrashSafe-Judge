#!/usr/bin/env bash
# Everything the organiser checks on contest day, in one screen (deploy/CONTEST.md). Run as root
# inside judge-prod; from Windows, as the judge account:
#     wsl -d judge-prod -u root -- bash /opt/judge/deploy/status.sh
# Reads only. The host half is here (services, readiness, backups, disk); the database half is
# `python -m admin status`. Exits 1 if anything needs a person.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
set -a; . /etc/judge/judge.env; set +a
problems=0
warn() { echo "WARNING: $*"; problems=1; }

units="judge-api judge-backup.timer $(seq -f 'judge-worker@%g' 1 "${WORKERS:-2}")"
for unit in $units; do
  state=$(systemctl is-active "$unit")
  restarts=$(systemctl show "$unit" -p NRestarts --value 2> /dev/null)
  echo "$unit: $state${restarts:+, restarted $restarts times}"
  [ "$state" = active ] || warn "$unit is $state: journalctl -u $unit -n 50"
done

[ "$(docker inspect -f '{{.State.Running}}' judge-postgres 2> /dev/null)" = true ] || warn "judge-postgres isn't running"
ready=$(curl -s -m 5 "http://127.0.0.1:${API_PORT:-8000}/health/ready")
echo "API readiness: ${ready:-no answer}"
[ "$ready" = '{"ok":true}' ] || warn "the API isn't ready"

newest=$(ls -1t /var/backups/judge/judge-*.dump.age 2> /dev/null | head -1)
if [ -n "$newest" ]; then
  hours=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 ))
  echo "Newest backup: $(basename "$newest"), $hours h old"
  [ "$hours" -lt 26 ] || warn "no backup in $hours h: systemctl status judge-backup.service"
else
  warn "no backup at all yet"
fi
free=$(df --output=pcent / | tail -1 | tr -dc 0-9)
echo "Disk: ${free}% used"
[ "$free" -lt 90 ] || warn "the disk is ${free}% full"

echo
if db=$(cd "$ROOT" && /opt/judge-venv/bin/python -m admin status 2>&1); then
  echo "$db"
  grep -q '^WARNING' <<< "$db" && problems=1
else
  echo "$db" | tail -3
  warn "couldn't read the database"
fi
exit $problems
