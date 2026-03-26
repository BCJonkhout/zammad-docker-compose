#!/bin/sh
set -eu

wait_for_secret() {
  secret_path="$1"
  while [ ! -s "$secret_path" ]; do
    echo "Waiting for secret file: $secret_path" >&2
    sleep 2
  done
}

wait_for_secret "${ZAMMAD_AUTOREPLY_TOKEN_FILE:-/run/prudai-secrets/autoreply.token}"
wait_for_secret "${ZAMMAD_AUTOREPLY_WEBHOOK_TOKEN_FILE:-/run/prudai-secrets/autoreply-webhook.token}"

exec python /app/app.py
