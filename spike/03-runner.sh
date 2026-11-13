#!/usr/bin/env bash
# Attacks judge-runner inside a sandbox built with the flags from ARCHITECTURE.md §7.
# Covers probe 8 (capabilities) and follow-ups 1b (memory limit) and 4b (cleanup and zombies).
set -uo pipefail

SPIKE="$(cd "$(dirname "$0")" && pwd)"
. "$SPIKE/lib.sh"
TMP=/tmp/judge-spike

swapoff -a
build_probes memhog forker burn forge crash
if [ -z "${SKIP_BUILD:-}" ]; then
  gcc -O2 -static -Wall -Wextra -o "$BIN/judge-runner" "$SPIKE/../runner/judge_runner.c" || exit 1
fi
mkdir -p "$TMP"
docker pull -q "$IMAGE" > /dev/null

SANDBOX_FLAGS=(
  --read-only --tmpfs /work:rw,exec,size=64m,uid=65534,gid=65534,mode=0700
  --pids-limit 512 --ulimit nproc=64:64 --cpus 1
  --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add SYS_ADMIN
  --security-opt no-new-privileges
)

# run_case NAME STDIN [runner options] -- PROGRAM [ARGS...]
run_case() {
  local name=$1 input=$2
  shift 2
  printf '%b' "$input" | timeout 60 docker exec -i --user 0 sbx /probe/judge-runner "$@" \
    > "$TMP/out.txt" 2> "$TMP/report.txt"
  echo "--- $name"
  echo "stdout ($(wc -c < "$TMP/out.txt") bytes): $(head -c 100 "$TMP/out.txt" | tr '\n' ' ')"
  echo "report: $(cut -c1-400 "$TMP/report.txt")"
}

echo "=== judge-runner against hostile programs"
start sbx runsc --memory 256m --memory-swap 256m "${SANDBOX_FLAGS[@]}"

run_case "normal program (echoes stdin)" '5 3 1 4 1 5\n' -- /bin/cat
run_case "non-zero exit" '' -- /bin/sh -c 'exit 3'
run_case "segfault" '' -- /probe/crash
run_case "uses more CPU than its limit" '' --cpu-ms 300 -- /probe/burn 1500000000
run_case "infinite loop" '' --cpu-ms 1000 --wall-ms 5000 -- /probe/burn 100000000000
run_case "sleeps forever" '' --wall-ms 2000 -- /bin/sleep 100
run_case "output flood" '' --out-kb 1024 -- /usr/bin/yes
run_case "stderr with quotes" '' -- /bin/sh -c 'echo "say \"hi\"" >&2; exit 1'
run_case "memory bomb with a 192 MB address-space limit (1b)" '' --mem-mb 192 -- /probe/memhog 1024
echo "sandbox after the memory bomb: $(state sbx)"
run_case "fork bomb" '' -- /probe/forker 200
run_case "leaves a background process (4b)" '' -- /bin/sh -c 'sleep 1000 > /dev/null 2>&1 & echo started'
echo "processes after cleanup:"
docker exec sbx ps -o pid,stat,user,args
run_case "attacks the runner (8)" '' -- /probe/forge
run_case "program that doesn't exist" '' -- /probe/missing
run_case "environment the program sees" '' -- /usr/bin/env
echo "sandbox at the end: $(state sbx)"
docker rm -f sbx > /dev/null

echo
echo "=== C++ compile and run through the runner, under a 512 MB compile limit"
IMAGE=judge-cpp:spike start sbx-cpp runsc --memory 512m --memory-swap 512m "${SANDBOX_FLAGS[@]}"
cg=$(cgroup_of sbx-cpp)
docker exec -i sbx-cpp sh -c 'cat > /work/main.cpp' < "$SPIKE/src/sample.cpp"
docker exec --user 0 sbx-cpp /probe/judge-runner --cpu-ms 10000 --wall-ms 10000 \
  -- /usr/bin/g++ -std=c++20 -O2 -pipe -o /work/a.out /work/main.cpp 2> "$TMP/report.txt"
echo "compile report: $(cut -c1-300 "$TMP/report.txt")"
echo "sandbox peak memory: $(( $(cat "$cg/memory.peak") / 1048576 )) MB, state: $(state sbx-cpp)"
echo '5 3 1 4 1 5' | docker exec -i --user 0 sbx-cpp /probe/judge-runner --mem-mb 256 -- /work/a.out 2> "$TMP/report.txt"
echo "run report: $(cut -c1-300 "$TMP/report.txt")"
docker rm -f sbx-cpp > /dev/null
