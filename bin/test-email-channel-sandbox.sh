#!/usr/bin/env bash
# End-to-end check of bin/provision-email-channel.sh against a THROWAWAY Zammad 7.0:
# Postgres (tmpfs) + Redis + one Rails container, sharing one network namespace, no
# published ports, all started with --rm. Production is never touched.
#
#   bin/test-email-channel-sandbox.sh [suffix]
#
# Steps: fixture (prod-like start state) -> fake OAuth link -> apply x2 (idempotent)
# -> activate without go (must be refused) -> activate -> mail scenarios through the
# real Channel::EmailParser and triggers -> deactivate -> plan (must be 0 changes).
# The containers keep running for inspection; stop them with the command printed at
# the end (stopping removes them, --rm).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
suffix="${1:-$$}"
image="${ZAMMAD_SANDBOX_IMAGE:-prudai/zammad-support:7.0.0-0042}"
pg="zammad-mailtest-pg-${suffix}"
redis="zammad-mailtest-redis-${suffix}"
rails="zammad-mailtest-rails-${suffix}"
zammad_env=(-e POSTGRESQL_HOST=127.0.0.1 -e POSTGRESQL_USER=zammad -e POSTGRESQL_PASS=zammad
            -e POSTGRESQL_DB=zammad_production -e POSTGRESQL_PORT=5432 -e REDIS_URL=redis://127.0.0.1:6379
            -e ELASTICSEARCH_ENABLED=false -e MEMCACHE_SERVERS=)

docker run --rm -d --name "${pg}" --memory 1g --tmpfs /var/lib/postgresql/data:size=2g \
  -e POSTGRES_USER=zammad -e POSTGRES_PASSWORD=zammad -e POSTGRES_DB=zammad_production postgres:17.9-alpine >/dev/null
docker run --rm -d --name "${redis}" --memory 256m --network "container:${pg}" redis:8.6.1-alpine >/dev/null
sleep 5
echo "init ${image} (takes a few minutes)..."
docker run --rm --name "zammad-mailtest-init-${suffix}" --network "container:${pg}" "${zammad_env[@]}" \
  -e POSTGRESQL_DB_CREATE=true "${image}" zammad-init >/dev/null 2>&1
docker run --rm -d --name "${rails}" --memory 3g --network "container:${pg}" "${zammad_env[@]}" \
  --entrypoint sleep "${image}" infinity >/dev/null

scenario() {
  local out
  out="$(docker exec -i -e SANDBOX_PHASE="$1" -e SANDBOX_EXPECT="${2:-}" "${rails}" bash -lc \
    'cat > /tmp/scenarios.rb && cd /opt/zammad && bundle exec rails runner /tmp/scenarios.rb' \
    < "${ROOT_DIR}/bin/email-channel-sandbox-scenarios.rb" 2>&1 | sed -n 's/^__SANDBOX__//p')"
  SANDBOX_JSON="${out:-[]}" python3 -c '
import json, os, sys
rows = json.loads(os.environ["SANDBOX_JSON"])
for r in rows:
    print(("PASS" if r["ok"] else "FAIL"), r["check"], r.get("detail", ""))
sys.exit(0 if rows and all(r["ok"] for r in rows) else 1)'
}

provision() {
  ZAMMAD_RAILS_CONTAINER="${rails}" ZAMMAD_EMAIL_FOLDER_ID="${SANDBOX_FOLDER_ID-sandbox-folder}" \
    bash "${ROOT_DIR}/bin/provision-email-channel.sh" "$@" 2>&1
}

changes() { python3 -c 'import json,sys; t=sys.stdin.read(); print(len(json.loads(t[t.index("{"):t.rindex("}")+1])["changes"]))'; }
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; exit 1; }

scenario fixture
scenario link
ZAMMAD_GRAPH_CLIENT_ID=sandbox-app ZAMMAD_GRAPH_CLIENT_SECRET=sandbox-secret ZAMMAD_GRAPH_TENANT_ID=sandbox-tenant \
  provision apply >/dev/null || fail "apply"
[[ "$(provision apply | changes)" == 0 ]] && pass "apply is idempotent" || fail "apply not idempotent"
scenario binding unbound
if out="$(provision activate)"; then fail "activate without go marker succeeded"; fi
grep -q "activate refused, failing guards: go_marker" <<<"${out}" && pass "activate without go marker refused (go_marker only)" \
  || fail "activate failed for another reason: $(grep -m1 -i 'refused\|error' <<<"${out}")"
ZAMMAD_EMAIL_CHANNEL_GO="sandbox ${suffix}" provision activate >/dev/null && pass "activate with go marker" || fail "activate"
scenario binding bound
scenario mail
provision deactivate >/dev/null && pass "deactivate" || fail "deactivate"
scenario binding unbound
[[ "$(provision plan | changes)" == 0 ]] && pass "plan after deactivate: 0 changes" || fail "plan after deactivate"
# Re-authentication in the UI drops folder_id on a live channel; apply must switch it off.
ZAMMAD_EMAIL_CHANNEL_GO="sandbox ${suffix}" provision activate >/dev/null || fail "re-activate"
scenario reconnect
SANDBOX_FOLDER_ID= provision apply >/dev/null || fail "apply on folderless channel"
scenario reconnected
provision apply >/dev/null && ZAMMAD_EMAIL_CHANNEL_GO="sandbox ${suffix}" provision activate >/dev/null \
  && pass "folder restored and re-activated" || fail "re-activate after reconnect"
provision deactivate >/dev/null || fail "final deactivate"
echo "sandbox containers: ${pg} ${redis} ${rails}"
echo "stop (removes them): docker stop ${rails} ${redis} ${pg}"
