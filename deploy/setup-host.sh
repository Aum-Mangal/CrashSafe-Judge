#!/usr/bin/env bash
# Installs Docker Engine and gVisor on Ubuntu 24.04.
# Runs on amd64 (WSL2) and arm64 (Oracle A1), and is safe to run again.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

ARCH="$(dpkg --print-architecture)"
. /etc/os-release

apt-get update
apt-get install -y ca-certificates curl gnupg

# Docker's package source, signed with Docker's key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

# gVisor's package source, signed with gVisor's key
curl -fsSL https://gvisor.dev/archive.key | gpg --batch --yes --dearmor -o /etc/apt/keyrings/gvisor.gpg
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/gvisor.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io runsc

# Adds runsc as an extra Docker runtime. The default stays runc.
runsc install
systemctl restart docker

# Swap can let a sandbox escape its memory limit
swapoff -a

echo "--- installed"
docker version --format 'docker {{.Server.Version}} {{.Server.Arch}}'
runsc --version | head -n 1
docker info --format 'default runtime: {{.DefaultRuntime}}, cgroup: {{.CgroupDriver}} v{{.CgroupVersion}}'
docker info --format 'runtimes:{{range $name, $_ := .Runtimes}} {{$name}}{{end}}'
free -m | grep -i swap
