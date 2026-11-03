#!/usr/bin/env bash
# Shows that runsc containers talk to gVisor's kernel, not the host kernel.
set -euo pipefail

IMAGE=alpine:3.20
docker pull -q "$IMAGE" > /dev/null

echo "host kernel:  $(uname -r)"
echo "runc kernel:  $(docker run --rm --runtime=runc "$IMAGE" uname -r)"
echo "runsc kernel: $(docker run --rm --runtime=runsc "$IMAGE" uname -r)"

echo
echo "--- runsc dmesg (gVisor's own boot log)"
docker run --rm --runtime=runsc "$IMAGE" dmesg | head -n 6

echo
echo "--- runsc with the locked-down flags from ARCHITECTURE.md"
docker run --rm --runtime=runsc \
  --network none --read-only \
  --tmpfs /work:rw,exec,size=64m,uid=65534,gid=65534,mode=0700 \
  --memory 256m --memory-swap 256m --cpus 1 \
  --cap-drop ALL --security-opt no-new-privileges --user 65534 \
  "$IMAGE" sh -c '
    echo "uid: $(id -u)"
    echo hello > /work/ok && echo "write /work: allowed"
    if echo x > /etc/x 2>/dev/null; then echo "write /etc: ALLOWED (bad)"; else echo "write /etc: blocked"; fi
    if wget -q -T 3 -O /dev/null http://example.com 2>/dev/null; then echo "network: ALLOWED (bad)"; else echo "network: blocked"; fi
  '

echo
echo "--- start time, 5 runs each"
for runtime in runc runsc; do
  start=$(date +%s%N)
  for _ in 1 2 3 4 5; do
    docker run --rm --runtime="$runtime" "$IMAGE" true
  done
  end=$(date +%s%N)
  echo "$runtime: $(( (end - start) / 5000000 )) ms per container"
done
