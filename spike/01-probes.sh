#!/usr/bin/env bash
# Spike probes 1 to 5 from ARCHITECTURE.md §14. Run as root in WSL.
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/src"
BIN=/tmp/judge-spike/bin
IMAGE=alpine:3.20

# Swap would let a sandbox go past its memory limit and hide what we're testing
swapoff -a

# Compile on the Linux filesystem; binaries from /mnt/c are slow and lose permissions
mkdir -p "$BIN"
for p in memhog forker killall burn measure; do
  gcc -O2 -static -o "$BIN/$p" "$SRC/$p.c" || exit 1
done
docker pull -q "$IMAGE" > /dev/null

# start NAME RUNTIME [docker flags...]: a sleeping container to exec probes into
start() {
  local name=$1 runtime=$2
  shift 2
  docker rm -f "$name" > /dev/null 2>&1
  docker run -d --name "$name" --runtime="$runtime" --network none \
    -v "$BIN:/probe:ro" --user 65534 "$@" "$IMAGE" sleep infinity > /dev/null
}

cgroup_of() {
  local id
  id=$(docker inspect -f '{{.Id}}' "$1" 2> /dev/null) || return
  find /sys/fs/cgroup -type d -name "*$id*" 2> /dev/null | head -n 1
}

state() {
  docker inspect -f 'status={{.State.Status}} oomkilled={{.State.OOMKilled}} exit={{.State.ExitCode}}' "$1" 2>&1
}

echo "=== Probe 1: memory bomb (256 MB limit, program asks for 1 GB)"
# -1000 wedged the sandbox and blocked Docker for about 10 minutes (DECISIONS.md, probe 1),
# so only the default is run now
for adj in default; do
  flags=(--memory 256m --memory-swap 256m)
  [ "$adj" != default ] && flags+=(--oom-score-adj "$adj")
  dmesg -C
  start p1 runsc "${flags[@]}"
  cg=$(cgroup_of p1)
  echo "--- oom-score-adj: $adj"
  echo "cgroup: ${cg:-not found}"
  timeout 60 docker exec p1 /probe/memhog 1024 | tail -n 1
  echo "exec exit code: ${PIPESTATUS[0]}"
  sleep 1
  echo "container after: $(state p1)"
  if [ -n "$cg" ] && [ -f "$cg/memory.events" ]; then
    echo "memory.events: $(grep oom_kill "$cg/memory.events" | tr '\n' ' ')"
  else
    echo "memory.events: cgroup gone"
  fi
  echo "next exec: $(timeout 20 docker exec p1 echo alive 2>&1)"
  echo "kernel OOM victim: $(dmesg | grep -i 'killed process' | tail -n 1 | sed 's/.*Killed process/Killed process/')"
  docker rm -f p1 > /dev/null
done

echo
echo "=== Probe 2: --ulimit nproc=64, program forks 200 children"
start p2 runsc --ulimit nproc=64:64
timeout 60 docker exec p2 /probe/forker 200
echo "exec exit code: $?"
echo "container after: $(state p2)"
docker rm -f p2 > /dev/null

echo
echo "=== Probe 3: --pids-limit, program forks 200 children"
start p3 runsc
cg=$(cgroup_of p3)
echo "idle sandbox uses $(cat "$cg/pids.current" 2> /dev/null || echo '?') host pids"
docker rm -f p3 > /dev/null
for limit in 32 64 128 256; do
  if ! start p3 runsc --pids-limit "$limit" 2> /tmp/judge-spike/start.err; then
    echo "limit $limit: container failed to start: $(tail -n 1 /tmp/judge-spike/start.err)"
    continue
  fi
  sleep 1
  out=$(timeout 60 docker exec p3 /probe/forker 200 2>&1)
  code=$?
  echo "limit $limit: exec exit $code, '$out', container $(state p3)"
  docker rm -f p3 > /dev/null
done

echo
echo "=== Probe 4: do background processes survive between tests?"
start p4 runsc
timeout 20 docker exec p4 sh -c 'sleep 1000 > /dev/null 2>&1 & echo "test 1 left background pid $!"'
echo "processes before cleanup:"
timeout 20 docker exec p4 ps -o pid,user,args
timeout 20 docker exec p4 /probe/killall
echo "processes after kill(-1):"
timeout 20 docker exec p4 ps -o pid,user,args
echo "container after: $(state p4)"
docker rm -f p4 > /dev/null

echo
echo "=== Probe 5: does wait4 report the real CPU time and memory?"
for runtime in runc runsc; do
  start p5 "$runtime" --cpus 1 --memory 512m --memory-swap 512m
  cg=$(cgroup_of p5)
  for i in 1 2 3 4 5; do
    before=$(awk '/^usage_usec/ {print $2}' "$cg/cpu.stat")
    out=$(timeout 60 docker exec p5 /probe/measure /probe/burn 1500000000)
    after=$(awk '/^usage_usec/ {print $2}' "$cg/cpu.stat")
    echo "$runtime cpu run $i: $out | cgroup_cpu_ms=$(( (after - before) / 1000 ))"
  done
  echo "$runtime memory (touches 128 MB): $(timeout 60 docker exec p5 /probe/measure /probe/memhog 128 | tail -n 1)"
  docker rm -f p5 > /dev/null
done
