#!/usr/bin/env bash
# Turns a distro holding this code into a running judge: Docker and gVisor, the sandbox images,
# Postgres, the API, two workers and the nightly backup (ARCHITECTURE.md §12). Run as root, from
# the code's folder (/opt/judge in judge-prod). Safe to run again: it keeps the secrets and the
# data it finds, and only fills in what is missing.
#
#   bash deploy/bootstrap.sh                          a fresh judge, or bring an existing one up to date
#   bash deploy/bootstrap.sh BACKUP.dump.age KEY.txt  a fresh judge holding a backup's data
#
# Settings live in /etc/judge/judge.env (written on the first run, readable only by root):
# database password, JWT secret, ports and the number of workers.
# Backups need an age public key in /etc/judge/backup-recipient.txt; without it the judge runs,
# but the nightly backup fails and says why.
set -euo pipefail

started=$(date +%s)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV=${VENV:-/opt/judge-venv}
ETC=/etc/judge
ENV_FILE=$ETC/judge.env
step() { echo; echo "=== $* ($(( $(date +%s) - started )) s)"; }

[ "$(id -u)" -eq 0 ] || { echo "Run this as root." >&2; exit 1; }
if [ $# -ne 0 ] && [ $# -ne 2 ]; then
  echo "usage: bootstrap.sh [BACKUP.dump.age KEY.txt]" >&2
  exit 1
fi

step "Docker and gVisor"
if command -v runsc > /dev/null && docker info --format '{{.Runtimes}}' 2> /dev/null | grep -q runsc; then
  echo "already installed"
else
  bash "$ROOT/deploy/setup-host.sh"
fi
swapoff -a  # a sandbox could use swap to go past its memory limit

step "Packages and the Python environment"
apt-get update -q > /dev/null
apt-get install -y -q python3-venv age curl > /dev/null
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -e "$ROOT"

step "Settings in $ENV_FILE"
install -d -m 755 "$ETC"
if [ -f "$ENV_FILE" ]; then
  echo "kept the existing file: a new database password or JWT secret would lock out the data and every session"
else
  password=$(openssl rand -hex 24)
  umask 077
  cat > "$ENV_FILE" <<SETTINGS
# Written by bootstrap.sh on $(date -u '+%Y-%m-%d %H:%M UTC'). Holds secrets: root only.
POSTGRES_PASSWORD=$password
PGPORT=5432
# sslmode=disable: Postgres is on this machine, and asyncpg would otherwise look for client keys
# in the home folder, which the API's service can't see (ProtectHome)
DATABASE_URL=postgresql://judge:$password@127.0.0.1:5432/judge?sslmode=disable
JWT_SECRET=$(openssl rand -hex 32)
API_PORT=8000
WORKERS=2
SETTINGS
  umask 022
  echo "wrote new settings"
fi
chmod 600 "$ENV_FILE"
set -a; . "$ENV_FILE"; set +a

step "Postgres"
docker compose -f "$ROOT/deploy/docker-compose.yml" --env-file "$ENV_FILE" up -d --wait

step "Sandbox images"
bash "$ROOT/scripts/build-images.sh"

if [ $# -eq 2 ]; then
  step "Restoring $1"
  REPLACE=${REPLACE:-0} bash "$ROOT/deploy/restore.sh" "$1" "$2"
fi

step "Database migrations"
(cd "$ROOT" && "$VENV/bin/python" -m db.migrate)

step "Services"
id judge-api > /dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin judge-api
for unit in judge-api.service judge-worker@.service judge-backup.service judge-backup.timer; do
  sed -e "s#@ROOT@#$ROOT#g" -e "s#@VENV@#$VENV#g" "$ROOT/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
workers=$(seq -f 'judge-worker@%g.service' 1 "$WORKERS")
systemctl enable -q judge-api.service judge-backup.timer $workers
# restart, not start: a second run picks up new code
systemctl restart judge-api.service $workers
systemctl start judge-backup.timer

step "Checking"
for _ in $(seq 60); do
  curl -fs "http://127.0.0.1:$API_PORT/health/ready" > /dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$API_PORT/health/ready"; echo
systemctl is-active judge-api.service $workers | paste -sd ' '
grep -q '^age1' "$ETC/backup-recipient.txt" 2> /dev/null \
  || echo "note: no age public key in $ETC/backup-recipient.txt yet, so nightly backups will fail"
echo
echo "Ready in $(( $(date +%s) - started )) s. The API is on 127.0.0.1:$API_PORT."
