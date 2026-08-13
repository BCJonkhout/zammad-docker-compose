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

# DOCS_KC_BOT_CLIENT_ID / DOCS_KC_BOT_CLIENT_SECRET (for the SSO-gated docs
# pages) arrive via .env, rendered from OpenBao kv/prod/zammad/app by
# `bao-fetch zammad` -- the same convention every other service here uses.  The
# previous secret-file mechanism (ZAMMAD_DOCS_KC_SECRET_FILE) is gone: it never
# had a file to read, and a second source of truth only invites drift.
if [[ -z "${DOCS_KC_BOT_CLIENT_ID:-}" || -z "${DOCS_KC_BOT_CLIENT_SECRET:-}" ]]; then
  echo "[run-docs-sync] Geen DOCS_KC_BOT_CLIENT_ID/SECRET in .env — de 4 SSO-afgeschermde" >&2
  echo "[run-docs-sync] pagina's worden overgeslagen. Herstel: bao kv patch kv/prod/zammad/app" >&2
  echo "[run-docs-sync] DOCS_KC_BOT_CLIENT_ID=... DOCS_KC_BOT_CLIENT_SECRET=... && bao-fetch zammad" >&2
fi

exec python3 "${ROOT_DIR}/bin/docs-sync.py"
