#!/usr/bin/env bash
# Builds the sandbox images the worker uses. Run as root in a WSL distro.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

for target in cpp python; do
  docker build -q --target "$target" -t "judge-$target" -f "$ROOT/deploy/images/Dockerfile" "$ROOT" > /dev/null
  echo "built judge-$target"
done
