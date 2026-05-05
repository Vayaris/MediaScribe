#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${MEDIASCRIBE_REPO_URL:-https://github.com/Vayaris/MediaScribe.git}"
REF="${MEDIASCRIBE_REF:-main}"
WORKDIR="$(mktemp -d)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: curl -fsSL <url>/scripts/bootstrap.sh | sudo bash"
  exit 1
fi

apt-get update
apt-get install -y git ca-certificates

git clone --depth 1 --branch "${REF}" "${REPO_URL}" "${WORKDIR}/mediascribe"
cd "${WORKDIR}/mediascribe"
./scripts/install.sh
