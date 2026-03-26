#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

set -a
source "${ROOT_DIR}/.env"
set +a

logo_source_path="${ROOT_DIR}/docker/zammad-assets/prudai-logo.png"
logo_container_path="/tmp/prudai-logo.png"

read_env_file_value() {
  local file_path="$1"
  local key="$2"

  [[ -f "${file_path}" ]] || return 1

  sed -n "s/^${key}=//p" "${file_path}" | head -n 1
}

discover_sendgrid_api_key() {
  local candidate key

  if [[ -n "${SENDGRID_API_KEY:-}" ]]; then
    printf '%s' "${SENDGRID_API_KEY}"
    return 0
  fi

  if [[ -n "${SENDGRID_KEY:-}" ]]; then
    printf '%s' "${SENDGRID_KEY}"
    return 0
  fi

  for candidate in \
    "${ROOT_DIR}/.env.local" \
    "/root/alex/.env" \
    "/root/Prudai/odoo-careers/.env"
  do
    key="$(read_env_file_value "${candidate}" "SENDGRID_API_KEY" || true)"
    if [[ -n "${key}" ]]; then
      printf '%s' "${key}"
      return 0
    fi

    key="$(read_env_file_value "${candidate}" "SENDGRID_KEY" || true)"
    if [[ -n "${key}" ]]; then
      printf '%s' "${key}"
      return 0
    fi
  done

  return 1
}

ensure_secret_file() {
  local file_path="$1"

  mkdir -p "$(dirname "${file_path}")"
  chmod 700 "$(dirname "${file_path}")"

  if [[ ! -s "${file_path}" ]]; then
    python3 - <<'PY' > "${file_path}"
import secrets

print(secrets.token_urlsafe(48))
PY
  fi

  chmod 600 "${file_path}"
}

wait_for_rails() {
  local attempt
  for attempt in $(seq 1 90); do
    if docker compose exec -T zammad-railsserver bash -lc 'bundle exec rails runner "puts User.count"' >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done

  echo "Zammad railsserver did not become ready in time." >&2
  return 1
}

wait_for_rails

sendgrid_api_key="$(discover_sendgrid_api_key || true)"
docs_sync_service_email="${DOCS_SYNC_SERVICE_EMAIL:-docs-sync@support.prudai.com}"
autoreply_service_email="${AUTOREPLY_SERVICE_EMAIL:-ai-agent@support.prudai.com}"
autoreply_webhook_token_path="${ROOT_DIR}/secrets/autoreply-webhook.token"

ensure_secret_file "${autoreply_webhook_token_path}"
autoreply_webhook_bearer_token="$(<"${autoreply_webhook_token_path}")"

if [[ -f "${logo_source_path}" ]]; then
  docker cp "${logo_source_path}" "$(docker compose ps -q zammad-railsserver):${logo_container_path}"
fi

echo "Configuring Zammad settings, KB roots, and sync credentials..."
raw_output="$(
  docker compose exec -T \
    -e LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY}" \
    -e KC_CLIENT_ID="${KC_CLIENT_ID}" \
    -e KC_REALM="${KC_REALM}" \
    -e AUTOREPLY_SERVICE_EMAIL="${autoreply_service_email}" \
    -e AUTOREPLY_WEBHOOK_BEARER_TOKEN="${autoreply_webhook_bearer_token}" \
    -e DOCS_SYNC_SERVICE_EMAIL="${docs_sync_service_email}" \
    -e SENDGRID_API_KEY="${sendgrid_api_key}" \
    -e ZAMMAD_BOOTSTRAP_ADMIN_EMAIL="${ZAMMAD_BOOTSTRAP_ADMIN_EMAIL}" \
    -e ZAMMAD_FQDN="${ZAMMAD_FQDN}" \
    zammad-railsserver \
    bash -lc 'cat > /tmp/prudai-provision.rb && bundle exec rails runner /tmp/prudai-provision.rb' <<'RUBY'
require 'json'
require 'securerandom'

UserInfo.current_user_id = 1

def find_locale(*codes)
  codes.flatten.compact.each do |code|
    locale = Locale.find_by(locale: code)
    return locale if locale
  end
  nil
end

def ensure_kb(title:, locale:, color_highlight:, color_header:, color_header_link:)
  existing = KnowledgeBase
    .joins(:translations)
    .find_by(knowledge_base_translations: { title: title })

  kb = existing || KnowledgeBase.new
  kb.active = true
  kb.category_layout = 'grid'
  kb.homepage_layout = 'grid'
  kb.iconset = 'FontAwesome'
  kb.color_highlight = color_highlight
  kb.color_header = color_header
  kb.color_header_link = color_header_link

  if kb.new_record?
    kb.kb_locales.build(system_locale: locale, primary: true)
    kb.save!
  else
    kb.save!
  end

  locale_record = kb.kb_locales.find_by(system_locale_id: locale.id) || kb.kb_locales.first
  translation = kb.translations.find_or_initialize_by(kb_locale_id: locale_record.id)
  translation.title = title
  translation.footer_note = 'PrudAI Support'
  translation.save!

  kb
end

def ensure_persistent_api_token(user:, name:, permissions: nil)
  token = Token.where(action: 'api', user_id: user.id, persistent: true).find_by(name: name)
  attributes = {
    action:     'api',
    persistent: true,
    user_id:    user.id,
    name:       name
  }
  attributes[:preferences] = { permission: permissions } if permissions.present?

  if token.nil?
    Token.create!(attributes)
  else
    token.update!(attributes.except(:action, :persistent, :user_id, :name))
    token
  end
end

fqdn = ENV.fetch('ZAMMAD_FQDN')
realm = ENV.fetch('KC_REALM', 'prudai')
client_id = ENV.fetch('KC_CLIENT_ID', 'zammad-support')
litellm_master_key = ENV.fetch('LITELLM_MASTER_KEY')
docs_sync_email = ENV.fetch('DOCS_SYNC_SERVICE_EMAIL')
autoreply_email = ENV.fetch('AUTOREPLY_SERVICE_EMAIL')
autoreply_webhook_bearer_token = ENV.fetch('AUTOREPLY_WEBHOOK_BEARER_TOKEN')
bootstrap_admin_email = ENV.fetch('ZAMMAD_BOOTSTRAP_ADMIN_EMAIL')
sendgrid_api_key = ENV.fetch('SENDGRID_API_KEY', '').strip

users_group = Group.find_by!(name: 'Users')

nl_locale = find_locale('nl-nl', 'nl')
en_locale = find_locale('en-us', 'en')

raise 'Missing nl locale in Zammad seed data.' if nl_locale.nil?
raise 'Missing en locale in Zammad seed data.' if en_locale.nil?

Setting.set('fqdn', fqdn)
Setting.set('http_type', 'https')
Setting.set('organization', 'PrudAI')
Setting.set('product_name', 'PrudAI Support')

logo_path = '/tmp/prudai-logo.png'
if File.exist?(logo_path)
  if (logo_timestamp = Service::SystemAssets::ProductLogo.store(File.binread(logo_path)))
    Setting.set('product_logo', logo_timestamp)
  end
end

Setting.set('auth_openid_connect', true)
Setting.set(
  'auth_openid_connect_credentials',
  {
    'display_name' => 'PrudAI SSO',
    'identifier'   => client_id,
    'issuer'       => "https://login.prudai.com/realms/#{realm}",
    'uid_field'    => 'sub',
    'scope'        => 'openid email profile',
    'pkce'         => true
  }
)

Setting.set('ai_provider', true)
Setting.set(
  'ai_provider_config',
  {
    'provider'   => 'custom_open_ai',
    'url'        => 'http://litellm:4000/v1',
    'token'      => litellm_master_key,
    'model'      => 'gemini-support',
    'ocr_active' => true,
    'ocr_model'  => 'gemini-support'
  }
)
Setting.set('ai_assistance_ticket_summary', true)
Setting.set('ai_assistance_text_tools', true)

kb_nl = ensure_kb(
  title:             'PrudAI Docs - NL',
  locale:            nl_locale,
  color_highlight:   '#345CF3',
  color_header:      '#FFFFFF',
  color_header_link: '#0F172A'
)
kb_en = ensure_kb(
  title:             'PrudAI Docs - EN',
  locale:            en_locale,
  color_highlight:   '#345CF3',
  color_header:      '#FFFFFF',
  color_header_link: '#0F172A'
)

admin_role = Role.find_by!(name: 'Admin')
agent_role = Role.find_by!(name: 'Agent')

docs_sync_user = User.find_or_initialize_by(email: docs_sync_email)
docs_sync_user.login = docs_sync_email
docs_sync_user.firstname = 'Docs'
docs_sync_user.lastname = 'Sync'
docs_sync_user.active = true
docs_sync_user.created_by_id ||= 1
docs_sync_user.updated_by_id = 1
docs_sync_user.password = SecureRandom.urlsafe_base64(32) if docs_sync_user.new_record?
docs_sync_user.roles = [admin_role]
docs_sync_user.save!

docs_sync_token = ensure_persistent_api_token(
  user:        docs_sync_user,
  name:        'docs-sync',
  permissions: ['knowledge_base.editor']
)

autoreply_user = User.find_or_initialize_by(email: autoreply_email)
autoreply_user.login = autoreply_email
autoreply_user.firstname = 'AI'
autoreply_user.lastname = 'Agent'
autoreply_user.active = true
autoreply_user.created_by_id ||= 1
autoreply_user.updated_by_id = 1
autoreply_user.password = SecureRandom.urlsafe_base64(32) if autoreply_user.new_record?
autoreply_user.roles = [admin_role, agent_role]
autoreply_user.group_names_access_map = { users_group.name => 'full' }
autoreply_user.save!

autoreply_token = ensure_persistent_api_token(
  user:        autoreply_user,
  name:        'autoreply-agent',
  permissions: autoreply_user.permissions.pluck(:name).uniq
)

staff_emails = [bootstrap_admin_email, 'haisma@prudai.com'].map(&:downcase).uniq
staff_configured = []

staff_emails.each do |email|
  user = User.find_by(email: email)
  next if user.nil?

  user.roles = [admin_role, agent_role]
  user.group_names_access_map = { users_group.name => 'full' }
  user.save!

  staff_configured << email
end

Setting.set('notification_sender', '"PrudAI Support" <support@prudai.com>')

smtp_configured = false
unless sendgrid_api_key.empty?
  Service::System::SetEmailNotificationConfiguration
    .new(
      adapter: 'smtp',
      new_configuration: {
        host:                 'smtp.sendgrid.net',
        port:                 587,
        user:                 'apikey',
        password:             sendgrid_api_key,
        enable_starttls_auto: true,
        ssl_verify:           true
      }
    )
    .execute

  smtp_configured = true
end

ticket_trigger = Trigger.find_or_initialize_by(name: 'prudai notify support on new tickets')
ticket_trigger.active = true
ticket_trigger.created_by_id ||= 1
ticket_trigger.updated_by_id = 1
ticket_trigger.condition = {
  'ticket.action'    => { 'operator' => 'is', 'value' => 'create' },
  'ticket.state_id'  => { 'operator' => 'is not', 'value' => 4 },
  'article.type_id'  => { 'operator' => 'is', 'value' => [1, 5, 11] },
  'article.sender_id'=> { 'operator' => 'is', 'value' => 2 }
}
ticket_trigger.perform = {
  'notification.email' => {
    'subject'   => 'New support ticket (#{ticket.title})',
    'recipient' => ['support@prudai.com'],
    'body'      => '<div>A new support ticket <b>(#{config.ticket_hook}#{ticket.number})</b> was created.</div><br/><div><b>Title:</b> #{ticket.title}</div><div><b>Customer:</b> #{ticket.customer&.email}</div><div><b>Group:</b> #{ticket.group&.name}</div><div><b>Link:</b> <a href="#{config.http_type}://#{config.fqdn}/#ticket/zoom/#{ticket.id}">#{config.http_type}://#{config.fqdn}/#ticket/zoom/#{ticket.id}</a></div><br/><div>#{config.product_name}</div>'
  }
}
ticket_trigger.save!

autoreply_webhook = Webhook.find_or_initialize_by(name: 'prudai bm25 docs autoreply')
autoreply_webhook.active = true
autoreply_webhook.endpoint = 'http://zammad-autoreply:8081/webhooks/zammad/new-ticket'
autoreply_webhook.http_method = 'post'
autoreply_webhook.ssl_verify = false
autoreply_webhook.customized_payload = false
autoreply_webhook.custom_payload = nil
autoreply_webhook.bearer_token = autoreply_webhook_bearer_token
autoreply_webhook.note = 'PrudAI BM25-grounded first-response automation for new support tickets.'
autoreply_webhook.save!

autoreply_trigger = Trigger.find_or_initialize_by(name: 'prudai ai first response on new tickets')
autoreply_trigger.active = true
autoreply_trigger.created_by_id ||= 1
autoreply_trigger.updated_by_id = 1
autoreply_trigger.condition = {
  'ticket.action'    => { 'operator' => 'is', 'value' => 'create' },
  'ticket.state_id'  => { 'operator' => 'is not', 'value' => 4 },
  'article.type_id'  => { 'operator' => 'is', 'value' => [1, 5, 11] },
  'article.sender_id'=> { 'operator' => 'is', 'value' => 2 }
}
autoreply_trigger.perform = {
  'notification.webhook' => {
    'webhook_id' => autoreply_webhook.id
  }
}
autoreply_trigger.save!

puts "__RESULT__#{JSON.generate(
  kb_nl_id: kb_nl.id,
  kb_en_id: kb_en.id,
  docs_sync_token: docs_sync_token.token,
  autoreply_token: autoreply_token.token,
  docs_sync_email: docs_sync_user.email,
  autoreply_email: autoreply_user.email,
  smtp_configured: smtp_configured,
  staff_configured: staff_configured,
  ticket_trigger_id: ticket_trigger.id,
  autoreply_trigger_id: autoreply_trigger.id,
  autoreply_webhook_id: autoreply_webhook.id
)}"
RUBY
)"

result_json="$(printf '%s\n' "${raw_output}" | sed -n 's/^__RESULT__//p' | tail -n 1)"
if [[ -z "${result_json}" ]]; then
  printf '%s\n' "${raw_output}" >&2
  echo "Failed to extract provisioning result payload." >&2
  exit 1
fi

RESULT_JSON="${result_json}" python3 - <<'PY'
import json
import os
from pathlib import Path

result = json.loads(os.environ["RESULT_JSON"])
root = Path("/root/zammad")
token_path = root / "secrets" / "docs-sync.token"
autoreply_token_path = root / "secrets" / "autoreply.token"
env_path = root / "docs-sync.env"

token_path.write_text(result["docs_sync_token"] + "\n", encoding="utf-8")
os.chmod(token_path, 0o600)

autoreply_token_path.write_text(result["autoreply_token"] + "\n", encoding="utf-8")
os.chmod(autoreply_token_path, 0o600)

env_path.write_text(
    "\n".join(
        [
            "ZAMMAD_BASE_URL=https://support.prudai.com",
            "ZAMMAD_DOCS_BASE_URL=https://docs.prudai.com",
            f"ZAMMAD_DOCS_KB_NL_ID={result['kb_nl_id']}",
            f"ZAMMAD_DOCS_KB_EN_ID={result['kb_en_id']}",
            "ZAMMAD_DOCS_SYNC_TOKEN_FILE=/root/zammad/secrets/docs-sync.token",
        ]
    )
    + "\n",
    encoding="utf-8",
)
os.chmod(env_path, 0o600)

sanitized = dict(result)
sanitized.pop("docs_sync_token", None)
sanitized.pop("autoreply_token", None)
print(json.dumps(sanitized))
PY

if docker compose config --services | grep -qx 'zammad-autoreply'; then
  echo "Recreating zammad-autoreply with fresh config..."
  docker compose up -d --build --force-recreate zammad-autoreply
fi
