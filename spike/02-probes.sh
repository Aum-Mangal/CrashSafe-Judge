#!/usr/bin/env bash
# Spike probes 6, 7, 9, 10 and follow-up 3b from ARCHITECTURE.md §14. Run as root in WSL.
# Probe 8 and follow-ups 1b and 4b test the runner itself, so they live in 03-runner.sh.
set -uo pipefail

SPIKE="$(cd "$(dirname "$0")" && pwd)"
. "$SPIKE/lib.sh"

swapoff -a
build_probes memhog forker
docker pull -q "$IMAGE" > /dev/null

echo "=== Probe 6: where is a sandbox's cgroup, and can a worker container read it?"
start p6 runsc
cg=$(cgroup_of p6)
echo "host path: $cg"
rel=${cg#/sys/fs/cgroup}
echo "seen from another container: $(docker run --rm -v /sys/fs/cgroup:/host-cgroup:ro "$IMAGE" head -n 1 "/host-cgroup$rel/cpu.stat" 2>&1)"
docker rm -f p6 > /dev/null

echo
echo "=== Probe 7: how much memory does gVisor itself use?"
for runtime in runc runsc; do
  start p7 "$runtime" --memory 512m --memory-swap 512m
  cg=$(cgroup_of p7)
  sleep 2
  idle_kb=$(( $(cat "$cg/memory.current") / 1024 ))
  timeout 60 docker exec p7 /probe/memhog 128 > /dev/null
  peak_kb=$(( $(cat "$cg/memory.peak") / 1024 ))
  echo "$runtime: idle ${idle_kb} KB, peak with a 128 MB program ${peak_kb} KB, extra beyond the program $(( peak_kb - 131072 )) KB"
  docker rm -f p7 > /dev/null
done

echo
echo "=== Follow-up 3b: host pids used while the program runs 64 processes"
start p3b runsc --ulimit nproc=64:64
cg=$(cgroup_of p3b)
timeout 60 docker exec p3b /probe/forker 200
echo "host pids.peak: $(cat "$cg/pids.peak")"
docker rm -f p3b > /dev/null

echo
echo "=== Probe 9: C++ compile time and memory, with and without the precompiled header"
docker build -q --target cpp -t judge-cpp:spike -f "$SPIKE/../deploy/images/Dockerfile" "$SPIKE/.." > /dev/null || exit 1
for runtime in runc runsc; do
  IMAGE=judge-cpp:spike start p9 "$runtime" --memory 1g --memory-swap 1g --cpus 1 \
    --tmpfs /work:rw,exec,size=64m,uid=65534,gid=65534,mode=0700
  cg=$(cgroup_of p9)
  docker exec -i p9 sh -c 'cat > /work/main.cpp' < "$SPIKE/src/sample.cpp"
  # A copy of the header with no .gch beside it, so g++ can't use the precompiled one
  docker exec p9 sh -c 'mkdir -p /work/nopch/bits && cp "$(find /usr/include -path "*/bits/stdc++.h" | head -n 1)" /work/nopch/bits/'
  for mode in pch nopch; do
    extra=()
    [ "$mode" = nopch ] && extra=(-I/work/nopch)
    first=$(docker exec -e TMPDIR=/work p9 g++ -std=c++20 -O2 -pipe "${extra[@]}" -H -fsyntax-only /work/main.cpp 2>&1 | head -n 1)
    times=""
    for i in 1 2 3; do
      t0=$(date +%s%N)
      docker exec -e TMPDIR=/work p9 g++ -std=c++20 -O2 -pipe "${extra[@]}" -o /work/a.out /work/main.cpp
      times="$times $(ms_since "$t0")"
    done
    echo "$runtime $mode: compile ms:$times | first header line: $first"
  done
  echo "$runtime peak memory during compiles: $(( $(cat "$cg/memory.peak") / 1048576 )) MB"
  echo "$runtime program output: $(echo '5 3 1 4 1 5' | docker exec -i p9 /work/a.out)"
  docker rm -f p9 > /dev/null
done

echo
echo "=== Probe 10: cost of starting a sandbox and of one docker exec"
for runtime in runc runsc; do
  t0=$(date +%s%N)
  for i in 1 2 3 4 5; do
    docker run -d --name "p10-$i" --runtime="$runtime" --network none "$IMAGE" sleep infinity > /dev/null
  done
  start_ms=$(( $(ms_since "$t0") / 5 ))
  t0=$(date +%s%N)
  for i in $(seq 10); do
    docker exec p10-1 true
  done
  exec_ms=$(( $(ms_since "$t0") / 10 ))
  echo "$runtime: start ${start_ms} ms per container, exec ${exec_ms} ms per command"
  docker rm -f p10-1 p10-2 p10-3 p10-4 p10-5 > /dev/null
done
