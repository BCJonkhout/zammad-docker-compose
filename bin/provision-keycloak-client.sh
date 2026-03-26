#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCOUNT_ENV_FILE="/root/account-dashboard/.env"

set -a
source "${ROOT_DIR}/.env"
set +a

read_env_value() {
  local env_file="$1"
  local key="$2"

  python3 - "${env_file}" "${key}" <<'PY'
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
key = sys.argv[2]

for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    current_key, _, value = raw_line.partition("=")
    if current_key.strip() != key:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
}

KEYCLOAK_ADMIN_EXEC_CONTAINER="${KEYCLOAK_ADMIN_EXEC_CONTAINER:-account-dashboard-backend-1}"
KEYCLOAK_SERVER="${KEYCLOAK_SERVER_URL:-$(read_env_value "${ACCOUNT_ENV_FILE}" "KEYCLOAK_SERVER_URL")}"
REALM="${KC_REALM:-${KEYCLOAK_REALM:-$(read_env_value "${ACCOUNT_ENV_FILE}" "KEYCLOAK_REALM")}}"
CLIENT_ID="${KC_CLIENT_ID:-zammad-support}"
SUPPORT_URL="https://${ZAMMAD_FQDN}"
REDIRECT_URI="${SUPPORT_URL}/auth/openid_connect/callback"
VERIFY_SSL="${KEYCLOAK_VERIFY_SSL:-$(read_env_value "${ACCOUNT_ENV_FILE}" "KEYCLOAK_VERIFY_SSL")}"
KEYCLOAK_ADMIN_CLIENT_ID="${KEYCLOAK_ADMIN_CLIENT_ID:-$(read_env_value "${ACCOUNT_ENV_FILE}" "KEYCLOAK_ADMIN_CLIENT_ID")}"
KEYCLOAK_ADMIN_CLIENT_SECRET="${KEYCLOAK_ADMIN_CLIENT_SECRET:-$(read_env_value "${ACCOUNT_ENV_FILE}" "KEYCLOAK_ADMIN_CLIENT_SECRET")}"

if [[ "$(docker inspect -f '{{.State.Running}}' "${KEYCLOAK_ADMIN_EXEC_CONTAINER}")" != "true" ]]; then
  echo "Keycloak admin execution container ${KEYCLOAK_ADMIN_EXEC_CONTAINER} is not running." >&2
  exit 1
fi

echo "Provisioning Keycloak client ${CLIENT_ID} in realm ${REALM} via ${KEYCLOAK_ADMIN_EXEC_CONTAINER}..."
result_json="$(
  docker exec -i \
    -e KEYCLOAK_SERVER_URL="${KEYCLOAK_SERVER}" \
    -e KEYCLOAK_REALM="${REALM}" \
    -e KEYCLOAK_ADMIN_CLIENT_ID="${KEYCLOAK_ADMIN_CLIENT_ID}" \
    -e KEYCLOAK_ADMIN_CLIENT_SECRET="${KEYCLOAK_ADMIN_CLIENT_SECRET}" \
    -e KEYCLOAK_VERIFY_SSL="${VERIFY_SSL}" \
    -e KC_TARGET_CLIENT_ID="${CLIENT_ID}" \
    -e SUPPORT_URL="${SUPPORT_URL}" \
    -e REDIRECT_URI="${REDIRECT_URI}" \
    "${KEYCLOAK_ADMIN_EXEC_CONTAINER}" \
    python - <<'PY'
import json
import os

from keycloak import KeycloakAdmin


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


server_url = os.environ["KEYCLOAK_SERVER_URL"].rstrip("/") + "/"
realm = os.environ["KEYCLOAK_REALM"]
admin_client_id = os.environ["KEYCLOAK_ADMIN_CLIENT_ID"]
admin_client_secret = os.environ["KEYCLOAK_ADMIN_CLIENT_SECRET"]
target_client_id = os.environ["KC_TARGET_CLIENT_ID"]
support_url = os.environ["SUPPORT_URL"].rstrip("/")
redirect_uri = os.environ["REDIRECT_URI"]
verify_ssl = parse_bool(os.environ.get("KEYCLOAK_VERIFY_SSL", "true"))

kc = KeycloakAdmin(
    server_url=server_url,
    realm_name=realm,
    client_id=admin_client_id,
    client_secret_key=admin_client_secret,
    verify=verify_ssl,
    auto_refresh_token=["get", "post", "put", "delete"],
)

payload = {
    "clientId": target_client_id,
    "name": "Zammad Support",
    "protocol": "openid-connect",
    "enabled": True,
    "publicClient": True,
    "standardFlowEnabled": True,
    "implicitFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "serviceAccountsEnabled": False,
    "frontchannelLogout": True,
    "rootUrl": support_url,
    "baseUrl": support_url,
    "redirectUris": [redirect_uri],
    "webOrigins": [support_url],
    "attributes": {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": f"{support_url}/*",
    },
}

existing = next((client for client in kc.get_clients() if client.get("clientId") == target_client_id), None)
if existing:
    client_uuid = existing["id"]
    kc.update_client(client_uuid, payload)
    action = "updated"
else:
    kc.create_client(payload, skip_exists=False)
    client_uuid = kc.get_client_id(target_client_id)
    action = "created"

print(
    json.dumps(
        {
            "action": action,
            "realm": realm,
            "client_id": target_client_id,
            "client_uuid": client_uuid,
            "redirect_uri": redirect_uri,
        }
    )
)
PY
)"

RESULT_JSON="${result_json}" python3 - <<'PY'
import json
import os

result = json.loads(os.environ["RESULT_JSON"])
print(f"Keycloak client {result['action']}:")
print(f"  Realm: {result['realm']}")
print(f"  Client ID: {result['client_id']}")
print(f"  Client UUID: {result['client_uuid']}")
print(f"  Redirect URI: {result['redirect_uri']}")
PY
