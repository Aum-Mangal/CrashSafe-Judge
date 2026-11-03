#!/usr/bin/env bash
# Force-removes a container whose gVisor sandbox is wedged and ignores docker rm -f.
# Usage: unstick.sh CONTAINER_NAME
set -uo pipefail

id=$(docker inspect -f '{{.Id}}' "$1") || exit 1
pids=$(pgrep -f "runsc.*$id")
echo "killing sandbox processes: $pids"
kill -9 $pids
sleep 3
timeout 30 docker rm -f "$1" > /dev/null && echo "removed $1"
