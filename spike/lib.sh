# Shared helpers for the spike scripts. Source this file; don't run it.
# Callers set SPIKE to the spike directory first.

BIN=${BIN:-/tmp/judge-spike/bin}
IMAGE=${IMAGE:-alpine:3.20}

# build_probes NAME...: compile spike/src/NAME.c into static binaries.
# SKIP_BUILD=1 uses binaries already in BIN, for judge-prod, which has no compiler.
build_probes() {
  [ -n "${SKIP_BUILD:-}" ] && return
  mkdir -p "$BIN"
  for p in "$@"; do
    gcc -O2 -static -o "$BIN/$p" "$SPIKE/src/$p.c" || exit 1
  done
}

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

ms_since() {
  echo $(( ($(date +%s%N) - $1) / 1000000 ))
}
