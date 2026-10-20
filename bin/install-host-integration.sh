#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_TARGET="/etc/nginx/sites-available/support.prudai.com"

if [[ ! -f "${NGINX_TARGET}" ]]; then
  install -m 644 "${ROOT_DIR}/nginx/support.prudai.com.conf" "${NGINX_TARGET}"
else
  echo "Preserving existing ${NGINX_TARGET}."
fi

ln -sfn "${NGINX_TARGET}" /etc/nginx/sites-enabled/support.prudai.com

install -m 644 "${ROOT_DIR}/systemd/zammad-docs-sync.service" /etc/systemd/system/zammad-docs-sync.service
install -m 644 "${ROOT_DIR}/systemd/zammad-docs-sync.timer" /etc/systemd/system/zammad-docs-sync.timer
install -m 644 "${ROOT_DIR}/systemd/zammad-cato-meldingen.service" /etc/systemd/system/zammad-cato-meldingen.service
install -m 644 "${ROOT_DIR}/systemd/zammad-cato-meldingen.timer" /etc/systemd/system/zammad-cato-meldingen.timer

nginx -t
systemctl reload nginx
systemctl daemon-reload
systemctl enable --now zammad-docs-sync.timer
systemctl enable --now zammad-cato-meldingen.timer
