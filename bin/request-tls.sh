#!/usr/bin/env bash
set -euo pipefail

certbot --nginx \
  --non-interactive \
  --agree-tos \
  -m jonkhout@prudai.com \
  -d support.prudai.com
