#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

set -a
source "${ROOT_DIR}/.env"
source "${ROOT_DIR}/docs-sync.env"
set +a

if [[ ! -f "${ZAMMAD_DOCS_SYNC_TOKEN_FILE}" ]]; then
  echo "Missing docs sync token file at ${ZAMMAD_DOCS_SYNC_TOKEN_FILE}." >&2
  exit 1
fi

export ZAMMAD_DOCS_SYNC_TOKEN
ZAMMAD_DOCS_SYNC_TOKEN="$(<"${ZAMMAD_DOCS_SYNC_TOKEN_FILE}")"

exec python3 "${ROOT_DIR}/bin/docs-sync.py"
