#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! grep -q '^vm.max_map_count=262144$' /etc/sysctl.conf; then
  printf '\nvm.max_map_count=262144\n' >> /etc/sysctl.conf
fi
sysctl -w vm.max_map_count=262144 >/dev/null

"${ROOT_DIR}/bin/install-host-integration.sh"

docker compose up -d --build

"${ROOT_DIR}/bin/provision-keycloak-client.sh"
"${ROOT_DIR}/bin/provision-zammad.sh"
