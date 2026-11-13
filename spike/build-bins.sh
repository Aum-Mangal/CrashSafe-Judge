#!/usr/bin/env bash
# Builds every probe and the runner into spike/bin, so they can be copied into judge-prod,
# which has no compiler. Run as root in the dev distro.
set -euo pipefail

SPIKE="$(cd "$(dirname "$0")" && pwd)"
BIN="$SPIKE/bin"
. "$SPIKE/lib.sh"

build_probes memhog forker killall burn measure forge crash
gcc -O2 -static -Wall -Wextra -o "$BIN/judge-runner" "$SPIKE/../runner/judge_runner.c"
ls "$BIN"
