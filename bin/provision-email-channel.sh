#!/usr/bin/env bash
# E-mailkanaal support@prudai.com (Microsoft 365 Graph) in Zammad: config als code.
#
#   bin/provision-email-channel.sh plan        # leest alleen: toont wat apply/activate zou doen
#   bin/provision-email-channel.sh apply       # zet alles klaar, activeert NIETS
#   bin/provision-email-channel.sh activate    # zet het kanaal aan (vereist ZAMMAD_EMAIL_CHANNEL_GO)
#   bin/provision-email-channel.sh deactivate  # terugrol: kanaal uit, groepskoppeling terug
#
# Ontwerp, risico's, uitrol en terugrol: docs/email-channel-support.md.
#
# Elke modus is idempotent en draait in één databasetransactie; `plan` is dezelfde run
# met een rollback aan het eind, en `activate` rolt alles terug als één poortcheck faalt.
# Dit script herstart geen enkele container (in tegenstelling tot provision-zammad.sh).
#
# Wat het NIET kan: het OAuth-account koppelen. Dat is een browserstap in Zammad
# (Admin > Kanalen > Microsoft 365 Graph Email > Account toevoegen), zie de
# browseragent-prompt in docs/email-channel-support.md. Het kanaal dat Zammad daarbij
# aanmaakt staat uit (Channel.create! active: false) tot `activate`.
set -euo pipefail

mode="${1:-plan}"
case "${mode}" in
  plan|apply|activate|deactivate) ;;
  *) echo "usage: $0 plan|apply|activate|deactivate" >&2; exit 2 ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

display_name="${ZAMMAD_EMAIL_DISPLAY_NAME:-Prudai Support}"
signature_file="${ZAMMAD_EMAIL_SIGNATURE_FILE:-/root/prudai-huisstijl/signature/out/support.html}"
signature_html="$(python3 "${ROOT_DIR}/bin/email_channel_signature.py" "${signature_file}" "${display_name}")"

# ZAMMAD_RAILS_CONTAINER richt het script op een losse container (wegwerp-testinstantie);
# zonder die variabele is het de railsserver van deze compose-stack.
if [[ -n "${ZAMMAD_RAILS_CONTAINER:-}" ]]; then
  rails_exec=(docker exec -i)
  rails_target=("${ZAMMAD_RAILS_CONTAINER}")
else
  rails_exec=(docker compose exec -T)
  rails_target=(zammad-railsserver)
fi

raw_output="$(
  "${rails_exec[@]}" \
    -e PRUDAI_EMAIL_MODE="${mode}" \
    -e PRUDAI_SIGNATURE_HTML="${signature_html}" \
    -e ZAMMAD_EMAIL_ADDRESS="${ZAMMAD_EMAIL_ADDRESS:-support@prudai.com}" \
    -e ZAMMAD_EMAIL_DISPLAY_NAME="${display_name}" \
    -e ZAMMAD_EMAIL_FOLDER="${ZAMMAD_EMAIL_FOLDER:-Zammad}" \
    -e ZAMMAD_EMAIL_FOLDER_ID="${ZAMMAD_EMAIL_FOLDER_ID:-}" \
    -e ZAMMAD_EMAIL_STAFF="${ZAMMAD_EMAIL_STAFF:-jonkhout@prudai.com,haisma@prudai.com}" \
    -e ZAMMAD_EMAIL_CHANNEL_GO="${ZAMMAD_EMAIL_CHANNEL_GO:-}" \
    -e ZAMMAD_AI_ON_EMAIL="${ZAMMAD_AI_ON_EMAIL:-0}" \
    -e ZAMMAD_GRAPH_CLIENT_ID="${ZAMMAD_GRAPH_CLIENT_ID:-}" \
    -e ZAMMAD_GRAPH_CLIENT_SECRET="${ZAMMAD_GRAPH_CLIENT_SECRET:-}" \
    -e ZAMMAD_GRAPH_TENANT_ID="${ZAMMAD_GRAPH_TENANT_ID:-}" \
    "${rails_target[@]}" \
    bash -lc 'cat > /tmp/prudai-email-channel.rb && cd /opt/zammad && bundle exec rails runner /tmp/prudai-email-channel.rb' <<'RUBY'
require 'json'

UserInfo.current_user_id = 1

MODE          = ENV.fetch('PRUDAI_EMAIL_MODE')
ADDRESS       = ENV.fetch('ZAMMAD_EMAIL_ADDRESS').strip.downcase
DISPLAY_NAME  = ENV.fetch('ZAMMAD_EMAIL_DISPLAY_NAME').strip
FOLDER_NAME   = ENV.fetch('ZAMMAD_EMAIL_FOLDER').strip
FOLDER_ID     = ENV.fetch('ZAMMAD_EMAIL_FOLDER_ID', '').strip
GO_MARKER     = ENV.fetch('ZAMMAD_EMAIL_CHANNEL_GO', '').strip
AI_ON_EMAIL   = ENV.fetch('ZAMMAD_AI_ON_EMAIL', '0') == '1'
SIGNATURE     = ENV.fetch('PRUDAI_SIGNATURE_HTML')
STAFF_EMAILS  = ENV.fetch('ZAMMAD_EMAIL_STAFF', '').split(',').map { |e| e.strip.downcase }.reject(&:empty?)
GRAPH_APP     = {
  'client_id'     => ENV.fetch('ZAMMAD_GRAPH_CLIENT_ID', '').strip,
  'client_secret' => ENV.fetch('ZAMMAD_GRAPH_CLIENT_SECRET', '').strip,
  'client_tenant' => ENV.fetch('ZAMMAD_GRAPH_TENANT_ID', '').strip,
}.freeze

AREA = 'MicrosoftGraph::Account'.freeze
SENDER_AGENT    = Ticket::Article::Sender.lookup(name: 'Agent').id
SENDER_CUSTOMER = Ticket::Article::Sender.lookup(name: 'Customer').id
TYPE_EMAIL = Ticket::Article::Type.lookup(name: 'email').id
TYPE_PHONE = Ticket::Article::Type.lookup(name: 'phone').id
TYPE_NOTE  = Ticket::Article::Type.lookup(name: 'note').id
TYPE_WEB   = Ticket::Article::Type.lookup(name: 'web').id

# Dezelfde regel als in provision-zammad.sh (ai_article_type_ids): de AI-autoreply
# draait in fase 1 niet op e-mail. Beide scripts schrijven deze ene sleutel, uit
# dezelfde variabele, zodat ze niet uit elkaar kunnen lopen.
AI_TRIGGER_NAMES = ['prudai ai first response on new tickets', 'prudai ai follow-up on customer messages'].freeze
AI_ARTICLE_TYPE_IDS = (AI_ON_EMAIL ? [TYPE_EMAIL, TYPE_PHONE, TYPE_WEB] : [TYPE_PHONE, TYPE_WEB]).freeze

CUSTOMER_TRIGGER = 'prudai notify customer on public agent reply'.freeze
STAFF_TRIGGER    = 'prudai notify staff on customer email follow-up'.freeze
SIGNATURE_NAME   = 'Prudai Support'.freeze
BINDING_KEY      = 'prudai_previous_group_binding'.freeze

@changes = []
@guards  = {}

def ignored_fields
  %w[updated_at updated_by_id created_at created_by_id]
end

def track(label, record)
  return record unless record.new_record? || record.changed?

  @changes << {
    'object' => label,
    'action' => record.new_record? ? 'create' : 'update',
    'fields' => (record.changed - ignored_fields).sort,
  }
  record.updated_by_id = 1 if record.respond_to?(:updated_by_id=)
  record.created_by_id ||= 1 if record.respond_to?(:created_by_id=)
  record.save!
  record
end

def guard(name, ok, detail = nil)
  @guards[name] = { 'ok' => ok ? true : false, 'detail' => detail }.compact
end

def channel_mailbox(channel)
  inbound = channel.options.dig('inbound', 'options') || {}
  (inbound['shared_mailbox'].presence || inbound['user']).to_s.downcase
end

def flatten_folders(folders, path = [])
  folders.flat_map do |folder|
    name = path + [folder[:displayName] || folder['displayName']]
    children = folder[:childFolders] || folder['childFolders'] || []
    [{ 'id' => folder[:id] || folder['id'], 'path' => name.join('/') }] + flatten_folders(children, name)
  end
end

def resolve_folder_id(channel)
  channel.refresh_xoauth2!(force: true)
  channel.reload
  graph = ::MicrosoftGraph.new(access_token: channel.options.dig('auth', 'access_token'), mailbox: ADDRESS)
  matches = flatten_folders(graph.get_message_folders_tree).select { |f| f['path'].casecmp?(FOLDER_NAME) || f['path'].split('/').last.casecmp?(FOLDER_NAME) }
  raise "mail folder '#{FOLDER_NAME}' not found in #{ADDRESS} (create it first, see docs/email-channel-support.md)" if matches.empty?
  raise "mail folder '#{FOLDER_NAME}' is ambiguous in #{ADDRESS}: #{matches.map { |m| m['path'] }.join(', ')}" if matches.size > 1

  matches.first['id']
end

def notification_trigger(name:, condition:, perform:, active:, note:)
  trigger = Trigger.find_or_initialize_by(name: name)
  trigger.condition = condition
  trigger.perform = perform
  trigger.active = active
  trigger.note = note
  track("trigger:#{name}", trigger)
end

result = {}

ActiveRecord::Base.transaction do
  users_group = Group.find_by!(name: 'Users')

  # --- 1. Microsoft Graph app credentials (Entra app registration) -----------------------
  credential = ExternalCredential.find_by(name: 'microsoft_graph')
  if GRAPH_APP['client_id'].present? && GRAPH_APP['client_secret'].present?
    credential ||= ExternalCredential.new(name: 'microsoft_graph')
    current = (credential.credentials || {}).to_h.stringify_keys.slice('client_id', 'client_secret', 'client_tenant')
    credential.credentials = GRAPH_APP.to_h if current != GRAPH_APP.to_h
    track('external_credential:microsoft_graph', credential)
  end
  guard('graph_app_configured', credential.present? && credential.credentials['client_id'].present?)

  # --- 2. Signature in house style ---------------------------------------------------------
  signature = Signature.find_or_initialize_by(name: SIGNATURE_NAME)
  # Store what the sanitizer keeps, so a re-run compares like with like (idempotent).
  # One pass reorders <a> attributes once more, the second pass is a fixpoint.
  sanitizer = HtmlSanitizer::Strict.new
  signature.body = sanitizer.sanitize(sanitizer.sanitize(SIGNATURE))
  signature.active = true
  signature.note = 'Huisstijl uit prudai-huisstijl/signature/out/support.html (bin/provision-email-channel.sh)'
  track("signature:#{SIGNATURE_NAME}", signature)
  # The sanitizer may rewrite the HTML on save; compare what is stored, not what we sent.
  stored_signature = signature.reload.body.to_s
  guard('signature_has_confidentiality_footer', stored_signature.include?('vertrouwelijk en uitsluitend bestemd voor'))
  guard('signature_has_display_name', stored_signature.include?(DISPLAY_NAME))

  # --- 3. Loop protection: never turn our own outgoing mail into tickets -----------------
  # support@ receives mail that support@ (or the autoreply on its behalf) sent itself:
  # the AI-escalation notices (19 of the 37 messages in the inbox on 20-10-2026) and
  # Outlook/agent-mail sends that copy support@. Zammad's own outgoing mail is already
  # dropped by Channel::Filter::OwnNotificationLoopDetection (Message-ID @fqdn + bulk).
  {
    'prudai ignore mail from own support address' => {
      'from' => { 'operator' => 'contains', 'value' => ADDRESS },
    },
    'prudai ignore ai escalation notices' => {
      'x-prudai-mail-kind' => { 'operator' => 'contains', 'value' => 'ai-escalation' },
    },
  }.each do |name, match|
    filter = PostmasterFilter.find_or_initialize_by(name: name)
    filter.channel = 'email'
    filter.match = match
    filter.perform = { 'x-zammad-ignore' => { 'value' => 'true' } }
    filter.active = true
    filter.note = 'Loopbeveiliging e-mailkanaal (bin/provision-email-channel.sh)'
    track("postmaster_filter:#{name}", filter)
  end
  guard('loop_filters_active', PostmasterFilter.where(channel: 'email', active: true, name: ['prudai ignore mail from own support address', 'prudai ignore ai escalation notices']).count == 2)

  # --- 4. AI autoreply article types (phase 1: not on e-mail) ----------------------------
  AI_TRIGGER_NAMES.each do |name|
    trigger = Trigger.find_by(name: name)
    next if trigger.nil?

    condition = trigger.condition.deep_dup
    condition['article.type_id'] = { 'operator' => 'is', 'value' => AI_ARTICLE_TYPE_IDS.dup }
    trigger.condition = condition
    track("trigger:#{name}", trigger)
  end
  ai_types_ok = AI_TRIGGER_NAMES.all? do |name|
    trigger = Trigger.find_by(name: name)
    trigger.nil? || Array(trigger.condition.dig('article.type_id', 'value')).map(&:to_i).sort == AI_ARTICLE_TYPE_IDS.sort
  end
  guard('ai_trigger_article_types', ai_types_ok, AI_ON_EMAIL ? 'phase 2: AI also answers e-mail' : 'phase 1: AI skips e-mail')

  # --- 5. The Graph channel (created by the OAuth step in the browser) -------------------
  channels = Channel.where(area: AREA).select { |c| channel_mailbox(c) == ADDRESS }
  raise "more than one #{AREA} channel for #{ADDRESS}: #{channels.map(&:id).inspect}" if channels.size > 1

  channel = channels.first
  guard('channel_linked', channel.present?, channel ? "channel #{channel.id}" : 'OAuth step not done yet')

  email_address = EmailAddress.find_by(email: ADDRESS)

  if channel
    options = channel.options.deep_dup
    inbound = options['inbound']['options']
    inbound['keep_on_server'] = true
    if FOLDER_ID.present?
      inbound['folder_id'] = FOLDER_ID
    elsif inbound['folder_id'].blank? && MODE != 'plan'
      inbound['folder_id'] = resolve_folder_id(channel)
      channel.reload
      options['auth'] = channel.options['auth']
    end
    channel.options = options
    channel.group_id = users_group.id

    case MODE
    when 'activate'
      bad = @guards.reject { |_name, g| g['ok'] }.keys
      bad << 'keep_on_server' unless channel.options.dig('inbound', 'options', 'keep_on_server') == true
      bad << 'folder_id' if channel.options.dig('inbound', 'options', 'folder_id').blank?
      bad << 'shared_mailbox' unless channel.options.dig('inbound', 'options', 'shared_mailbox').to_s.downcase == ADDRESS
      bad << 'go_marker' if GO_MARKER.empty?
      raise "activate refused, failing guards: #{bad.uniq.join(', ')}" if bad.any?

      preferences = (channel.preferences || {}).deep_dup
      preferences['prudai_go'] = GO_MARKER
      channel.preferences = preferences
      channel.active = true
    when 'deactivate'
      channel.active = false
    end
    track("channel:#{channel.id}", channel)

    email_address ||= EmailAddress.new(email: ADDRESS)
    email_address.name = DISPLAY_NAME
    email_address.channel_id = channel.id
    email_address.active = true
    track("email_address:#{ADDRESS}", email_address)
  elsif MODE == 'activate'
    raise 'activate refused: no Microsoft Graph channel linked yet'
  end

  channel_live = channel.present? && channel.active

  # --- 6. Group binding follows the channel state ----------------------------------------
  # With a single group, EmailAddress#update_email_address_id binds Users to support@ the
  # moment the OAuth step creates the address -- while the channel is still off. So the
  # binding is derived here on every run instead of being set once: live => Users sends
  # as support@ with the house signature; not live => Users has no e-mail address (the
  # state before this work, verified 20-10-2026) and its previous signature.
  if channel_live
    if users_group.signature_id != signature.id
      preferences = (channel.preferences || {}).deep_dup
      preferences[BINDING_KEY] = { 'signature_id' => users_group.signature_id }
      channel.preferences = preferences
      track("channel:#{channel.id}", channel)
    end
    users_group.email_address_id = email_address.id
    users_group.signature_id = signature.id
  else
    users_group.email_address_id = nil if email_address && users_group.email_address_id == email_address.id
    previous = channel && (channel.preferences || {}).dig(BINDING_KEY, 'signature_id')
    users_group.signature_id = previous if previous && users_group.signature_id == signature.id
  end
  track('group:Users', users_group)

  # --- 7. Notification triggers that only make sense while the channel is live ----------
  # (a) A public reply that is not itself an e-mail (AI reply, public note, portal reply)
  #     is mailed to the customer. E-mail replies are sent by Zammad itself, so type
  #     email is excluded -- otherwise the customer gets every answer twice.
  link = '#{config.http_type}://#{config.fqdn}/#ticket/zoom/#{ticket.id}'
  notification_trigger(
    name:      CUSTOMER_TRIGGER,
    active:    channel_live,
    note:      'Portal- en AI-antwoorden per mail naar de klant (bin/provision-email-channel.sh)',
    condition: {
      'ticket.action'     => { 'operator' => 'is', 'value' => 'update' },
      'article.sender_id' => { 'operator' => 'is', 'value' => [SENDER_AGENT] },
      'article.internal'  => { 'operator' => 'is', 'value' => ['false'] },
      'article.type_id'   => { 'operator' => 'is', 'value' => [TYPE_NOTE, TYPE_WEB] },
    },
    perform:   {
      'notification.email' => {
        'recipient'           => ['ticket_customer'],
        'subject'             => '#{ticket.title}',
        'internal'            => true,
        'include_attachments' => 'true',
        'body'                => "<div>#{'#{article.body_as_html}'}</div><br>" \
                                 '<div style="color:#5A6B85;font-size:13px;">U kunt op deze e-mail antwoorden, ' \
                                 "of uw vraag bekijken in het <a href=\"#{link}\">supportportaal</a>.</div>" \
                                 "#{SIGNATURE}",
      },
    }
  )

  # (b) Phase 1 only: a customer e-mail on an existing ticket. The staff matrix has
  #     update-mails off (AI conversations would flood it), but while the AI skips
  #     e-mail a human owns these tickets and must hear about the reply.
  staff_ids = User.where(email: STAFF_EMAILS).pluck(:id)
  notification_trigger(
    name:      STAFF_TRIGGER,
    active:    channel_live && !AI_ON_EMAIL,
    note:      'Fase 1: klantreactie per mail naar eigenaar en staf (bin/provision-email-channel.sh)',
    condition: {
      'ticket.action'     => { 'operator' => 'is', 'value' => 'update' },
      'article.sender_id' => { 'operator' => 'is', 'value' => [SENDER_CUSTOMER] },
      'article.type_id'   => { 'operator' => 'is', 'value' => [TYPE_EMAIL] },
    },
    perform:   {
      'notification.email' => {
        'recipient' => ['ticket_owner'] + staff_ids.map { |id| "userid_#{id}" },
        'subject'   => 'Klantreactie per e-mail (#{ticket.title})',
        'internal'  => true,
        'body'      => '<div>Nieuwe klantreactie per e-mail op ticket <b>#{config.ticket_hook}#{ticket.number}</b>.</div>' \
                       "<div><a href=\"#{link}\">#{link}</a></div><br>" \
                       '<div><b>Beantwoord in Zammad, niet op deze mail</b>: een antwoord op deze mail komt alleen als notitie in het ticket, niet bij de klant.</div>',
      },
    }
  )

  # Zammad's seeded "customer notification (on owner change)" would start mailing customers
  # the moment the group gets an e-mail address. Report its state; do not touch it.
  owner_change = Trigger.find_by(name: 'customer notification (on owner change)')

  group = users_group.reload
  result = {
    mode:                MODE,
    address:             ADDRESS,
    changes:             @changes,
    guards:              @guards,
    channel_id:          channel&.id,
    channel_active:      channel_live,
    keep_on_server:      channel&.options&.dig('inbound', 'options', 'keep_on_server'),
    folder_id_set:       channel&.options&.dig('inbound', 'options', 'folder_id').present?,
    group_email_address: group.email_address&.email,
    group_signature:     group.signature&.name,
    ai_on_email:         AI_ON_EMAIL,
    ai_article_type_ids: AI_ARTICLE_TYPE_IDS,
    staff_recipients:    staff_ids.size,
    owner_change_trigger_active: owner_change&.active,
    triggers_active:     Trigger.where(name: [CUSTOMER_TRIGGER, STAFF_TRIGGER]).to_h { |t| [t.name, t.active] },
  }

  raise ActiveRecord::Rollback if MODE == 'plan'
end

puts "__RESULT__#{JSON.generate(result)}"
RUBY
)"

result_json="$(printf '%s\n' "${raw_output}" | sed -n 's/^__RESULT__//p' | tail -n 1)"
if [[ -z "${result_json}" ]]; then
  printf '%s\n' "${raw_output}" | grep -v -i 'secret' >&2
  echo "provision-email-channel: no result payload (mode ${mode}); nothing was committed." >&2
  exit 1
fi

RESULT_JSON="${result_json}" python3 - <<'PY'
import json
import os

result = json.loads(os.environ["RESULT_JSON"])
print(json.dumps(result, indent=2, ensure_ascii=False))
if result["mode"] == "plan":
    print(f"plan: {len(result['changes'])} change(s) would be made; nothing was committed.")
PY
