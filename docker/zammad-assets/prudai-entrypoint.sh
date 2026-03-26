#!/usr/bin/env bash

set -o errexit
set -o pipefail

inject_prudai_styles() {
  local css_file="/opt/zammad/app/assets/stylesheets/custom/prudai-support.css"
  local asset
  local matched=0

  if [[ ! -f "${css_file}" ]]; then
    echo "Missing PrudAI stylesheet at ${css_file}." >&2
    return 1
  fi

  while IFS= read -r asset; do
    matched=1
    ruby - "${asset}" "${css_file}" <<'RUBY'
asset_path, css_path = ARGV
marker_start = "/* PRUDAI SUPPORT START */"
marker_end = "/* PRUDAI SUPPORT END */"

asset = File.read(asset_path)
css = File.read(css_path).rstrip
pattern = /#{Regexp.escape(marker_start)}.*?#{Regexp.escape(marker_end)}\n?/m
asset = asset.sub(pattern, "").rstrip
asset << "\n\n#{marker_start}\n#{css}\n#{marker_end}\n"
File.write(asset_path, asset)
RUBY
    gzip -n -c "${asset}" > "${asset}.gz"
  done < <(find /opt/zammad/public/assets -maxdepth 1 -type f \( -name 'application-*.css' -o -name 'knowledge_base-*.css' \) | sort)

  if [[ "${matched}" -eq 0 ]]; then
    echo "No compiled Zammad CSS bundles found to patch." >&2
    return 1
  fi

  echo "Injected PrudAI styles into compiled Zammad CSS bundles."
}

if [[ "${1:-}" == "zammad-nginx" ]]; then
  inject_prudai_styles
fi

exec /opt/zammad/bin/docker-entrypoint "$@"
