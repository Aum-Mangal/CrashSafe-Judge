#!/usr/bin/env bash
# Runs the whole test suite inside judge-prod, on the code in /opt/judge. Run as root.
# Gate 1 (ROADMAP.md) is the crash test in it. The rest shows judge-prod behaves like dev.
# Extra arguments go to pytest.
set -uo pipefail

cd "$(dirname "$0")/.."
echo "=== judge-prod test run, $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "code: commit $(cat COMMIT 2> /dev/null || echo unknown)"

echo
echo "=== isolation (the evidence that this ran in judge-prod, not the dev distro)"
bash deploy/check-isolation.sh

echo
echo "=== swap (a sandbox could use it to go past its memory limit)"
swapoff -a
free -m | grep -i swap

echo
echo "=== tests"
bash scripts/dev-test.sh -s "$@"
code=$?
echo "=== pytest exit code: $code"
exit $code
