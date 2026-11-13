#!/usr/bin/env bash
# Starts the dev database, applies migrations and runs the tests. Run as root in the dev distro.
# Extra arguments go to pytest, e.g. scripts/dev-test.sh -k idempotency
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV=/root/.venvs/judge
# Keep Python's cache files out of the Windows folder
export PYTHONDONTWRITEBYTECODE=1

dpkg -s python3-venv > /dev/null 2>&1 || apt-get install -y -q python3-venv > /dev/null
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -e "$ROOT[dev]"

docker compose --progress quiet -f "$ROOT/deploy/compose.dev.yml" up -d --wait

bash "$ROOT/scripts/build-images.sh"

cd "$ROOT"
"$VENV/bin/python" -m db.migrate
"$VENV/bin/pytest" -v -p no:cacheprovider "$@"
