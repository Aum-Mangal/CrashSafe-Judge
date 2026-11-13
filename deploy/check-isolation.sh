#!/usr/bin/env bash
# Checks, as root, that judge-prod can't reach Windows. Run inside judge-prod.
# PROTECTED_USER is the Windows account whose files must stay out of reach.
set -uo pipefail

PROTECTED_USER=${PROTECTED_USER:-Saran}

check() {
  local name=$1
  shift
  if "$@" > /dev/null 2>&1; then
    echo "$name: REACHABLE (bad)"
  else
    echo "$name: blocked"
  fi
}

check "C: drive mounted at /mnt/c" test -d /mnt/c/Windows
check "Windows folders on PATH" bash -c 'echo "$PATH" | grep -qi /mnt/'
# Root can always mount C: by hand (DECISIONS.md, 55). What matters is what the mount shows:
# it reads files as the Windows account that started WSL (DECISIONS.md, 62).
if mkdir -p /tmp/c && mount -t drvfs C: /tmp/c > /dev/null 2>&1 && test -d /tmp/c/Windows; then
  echo "mount C: by hand as root: works (expected; the next line is what counts)"
else
  echo "mount C: by hand as root: blocked"
fi
check "read $PROTECTED_USER's files through that mount" ls "/tmp/c/Users/$PROTECTED_USER/Documents"
umount /tmp/c 2> /dev/null
echo "kernel boot id (differs between WSL virtual machines): $(cat /proc/sys/kernel/random/boot_id)"

# Root can re-register the handler WSL uses to start Windows programs, so try running one
echo ":WSLProbe:M::MZ::/init:PF" > /proc/sys/fs/binfmt_misc/register 2> /dev/null
printf 'MZ' > /tmp/probe.exe && chmod +x /tmp/probe.exe
check "run a Windows program as root" timeout 20 /tmp/probe.exe
echo -1 > /proc/sys/fs/binfmt_misc/WSLProbe 2> /dev/null
rm -f /tmp/probe.exe

echo "docker: $(docker info --format '{{.ServerVersion}}, runtimes:{{range $name, $_ := .Runtimes}} {{$name}}{{end}}' 2>&1)"
