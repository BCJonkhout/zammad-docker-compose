# Scenario checks for the support@ e-mail channel, run with `rails runner` inside a
# THROWAWAY Zammad (never production): bin/test-email-channel-sandbox.sh starts one.
#
# Phase "link" mimics what the OAuth browser step leaves behind (ExternalCredential
# .link_account): an inactive MicrosoftGraph::Account channel plus an EmailAddress.
# Phase "mail" feeds RFC 822 messages through the real Channel::EmailParser and the
# real triggers, and checks what Zammad would do. Nothing leaves the sandbox: the
# channel carries a fake token and no scheduler or job worker runs.
require 'json'

UserInfo.current_user_id = 1
PHASE = ENV.fetch('SANDBOX_PHASE')
ADDRESS = 'support@prudai.com'.freeze
CUSTOMER = 'klant@example.org'.freeze

def check(results, name, ok, detail = nil)
  results << { 'check' => name, 'ok' => ok ? true : false, 'detail' => detail }.compact
end

def channel
  Channel.where(area: 'MicrosoftGraph::Account').find { |c| c.options.dig('inbound', 'options', 'shared_mailbox') == ADDRESS }
end

def deliver(raw)
  Channel::EmailParser.new.process(channel, raw)
ensure
  UserInfo.current_user_id = 1 # the parser resets it
end

def mail(from:, subject:, body: 'Testbericht.', headers: {})
  extra = headers.map { |k, v| "#{k}: #{v}\n" }.join
  "From: #{from}\nTo: #{ADDRESS}\nSubject: #{subject}\nMessage-ID: <#{SecureRandom.hex(8)}@example.org>\n" \
    "Date: #{Time.zone.now.rfc2822}\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n#{extra}\n#{body}\n"
end

def jobs_like(pattern)
  Delayed::Job.all.count { |job| job.handler.to_s.include?(pattern) }
end

results = []

case PHASE
when 'fixture'
  # What production has before this work (verified 20-10-2026): one group Users, two
  # staff agents, the two AI webhook triggers from provision-zammad.sh with e-mail in.
  Setting.set('fqdn', 'support.example.test')
  agent_role = Role.find_by!(name: 'Agent')
  admin_role = Role.find_by!(name: 'Admin')
  %w[jonkhout@prudai.com haisma@prudai.com].each do |email|
    user = User.find_or_initialize_by(email: email)
    user.update!(login: email, firstname: email.split('@').first, lastname: 'Staff', roles: [admin_role, agent_role], active: true)
    user.group_names_access_map = { 'Users' => 'full' }
    user.save!
  end
  customer = User.find_or_initialize_by(email: CUSTOMER)
  customer.update!(login: CUSTOMER, firstname: 'Test', lastname: 'Klant', roles: [Role.find_by!(name: 'Customer')])
  webhook = Webhook.find_or_initialize_by(name: 'prudai bm25 docs autoreply')
  webhook.update!(endpoint: 'http://127.0.0.1:9/sandbox', ssl_verify: false, active: true)
  [['prudai ai first response on new tickets', 'create'], ['prudai ai follow-up on customer messages', 'update']].each do |name, action|
    trigger = Trigger.find_or_initialize_by(name: name)
    trigger.update!(
      active:    true,
      condition: { 'ticket.action' => { 'operator' => 'is', 'value' => action }, 'ticket.state_id' => { 'operator' => 'is not', 'value' => 4 },
                   'article.type_id' => { 'operator' => 'is', 'value' => [1, 5, 11] }, 'article.sender_id' => { 'operator' => 'is', 'value' => 2 } },
      perform:   { 'notification.webhook' => { 'webhook_id' => webhook.id } }
    )
  end
  # provision-zammad.sh switches Zammad's seeded auto replies off; so does production.
  Trigger.where(name: ['auto reply (on new tickets)', 'auto reply (on follow-up of tickets)']).find_each { |t| t.update!(active: false) }
  check(results, 'fixture: single group without e-mail address', Group.count == 1 && Group.first.email_address_id.nil?)

when 'binding'
  # SANDBOX_EXPECT=unbound: Users has no e-mail address and its original signature;
  # bound: Users sends as support@ with "Prudai Support".
  group = Group.find_by!(name: 'Users')
  if ENV.fetch('SANDBOX_EXPECT') == 'bound'
    check(results, 'binding: Users bound to support@ + Prudai Support', group.email_address&.email == ADDRESS && group.signature&.name == 'Prudai Support',
          [group.email_address&.email, group.signature&.name].inspect)
  else
    check(results, 'binding: Users unbound, original signature', group.email_address_id.nil? && group.signature&.name == 'default',
          [group.email_address&.email, group.signature&.name].inspect)
  end

when 'reconnect'
  # What the UI's re-authentication does (MicrosoftBase.link_account, channel_id path):
  # options are rebuilt, folder_id is not carried over, the channel stays active.
  c = channel
  options = c.options.deep_dup
  options['inbound']['options'].delete('folder_id')
  c.update!(options: options)
  check(results, 'reconnect: active channel lost its folder_id', c.reload.active && c.options.dig('inbound', 'options', 'folder_id').blank?)

when 'reconnected'
  check(results, 'reconnected: apply switched the folderless channel off', channel && !channel.active)

when 'link'
  c = Channel.create!(
    area:          'MicrosoftGraph::Account',
    group_id:      Group.first.id,
    active:        false,
    options:       {
      inbound:  { adapter: 'microsoft_graph_inbound', options: { user: 'jonkhout@prudai.com', shared_mailbox: ADDRESS } },
      outbound: { adapter: 'microsoft_graph_outbound', options: { user: 'jonkhout@prudai.com', shared_mailbox: ADDRESS } },
      auth:     { provider: 'microsoft_graph', type: 'XOAUTH2', access_token: 'sandbox-fake', refresh_token: 'sandbox-fake',
                  created_at: Time.zone.now, expires_in: 360_000, client_id: 'sandbox', client_secret: 'sandbox' },
    },
    created_by_id: 1, updated_by_id: 1
  )
  EmailAddress.create_or_update(channel_id: c.id, name: "#{Setting.get('product_name')} Support", email: ADDRESS, active: true, created_by_id: 1, updated_by_id: 1)
  check(results, 'link: channel created inactive like the OAuth callback', !c.active)

when 'mail'
  check(results, 'precondition: channel active', channel&.active)
  Delayed::Job.delete_all

  # 1. New customer mail -> ticket; AI webhook must NOT run on e-mail in phase 1.
  ticket, article, = deliver(mail(from: "Test Klant <#{CUSTOMER}>", subject: 'Vraag over facturen'))
  check(results, '1 new mail becomes a ticket', ticket.present? && article.sender.name == 'Customer' && article.type.name == 'email')
  check(results, '1 customer is the sender', ticket&.customer&.email == CUSTOMER)
  check(results, '1 AI webhook not queued for e-mail (phase 1)', jobs_like('TriggerWebhookJob').zero?, "jobs=#{jobs_like('TriggerWebhookJob')}")

  # 2. Customer follow-up by mail on the same ticket -> staff notification (internal e-mail article).
  ticket.update!(owner_id: User.find_by(email: 'haisma@prudai.com').id)
  before = ticket.articles.count
  followup_ticket, followup, = deliver(mail(from: CUSTOMER, subject: "Re: #{ticket.subject_build('Vraag over facturen')}", body: 'Aanvulling.'))
  staff_note = Ticket::Article.where(ticket_id: ticket.id, sender: Ticket::Article::Sender.lookup(name: 'System')).last
  check(results, '2 follow-up lands on the existing ticket', followup_ticket&.id == ticket.id && followup.sender.name == 'Customer')
  check(results, '2 staff notified by internal e-mail article', staff_note.present? && staff_note.internal && staff_note.type.name == 'email' && staff_note.to.to_s.include?('haisma@prudai.com'), "to=#{staff_note&.to}")
  check(results, '2 staff notification not sent to the customer', !staff_note&.to.to_s.include?(CUSTOMER))
  check(results, '2 follow-up plus exactly one staff notice', ticket.articles.count == before + 2, "delta=#{ticket.articles.count - before}")

  # 3. Mail from support@ itself (AI escalation notice, Outlook copy) -> ignored.
  tickets_before = Ticket.count
  deliver(mail(from: "Prudai Support <#{ADDRESS}>", subject: 'Prudai AI escalation (iets)'))
  check(results, '3 mail from own address is ignored', Ticket.count == tickets_before)

  # 3b. A different address that merely contains support@prudai.com -> not ignored.
  deliver(mail(from: 'it-support@prudai.com', subject: 'Andere afzender'))
  check(results, '3b it-support@ is not caught by the own-address filter', Ticket.count == tickets_before + 1)
  tickets_before = Ticket.count

  # 4. Escalation notice marked by header, even from another sender -> ignored.
  deliver(mail(from: 'alerts@prudai.com', subject: 'Prudai AI escalation (x)', headers: { 'X-Prudai-Mail-Kind' => 'ai-escalation' }))
  check(results, '4 ai-escalation header is ignored', Ticket.count == tickets_before)

  # 5. Outlook out-of-office on the ticket -> stored, flagged as auto-response.
  _, oof, = deliver(mail(from: CUSTOMER, subject: "Automatisch antwoord: #{ticket.subject_build('Vraag over facturen')}",
                           headers: { 'X-Auto-Response-Suppress' => 'All', 'X-MS-Exchange-Inbox-Rules-Loop' => CUSTOMER, 'Auto-Submitted' => 'auto-replied' }))
  check(results, '5 out-of-office flagged is-auto-response', oof&.preferences&.dig('is-auto-response') == true, oof&.preferences&.slice('is-auto-response', 'send-auto-response').inspect)

  # 6. Portal ticket + public web reply by an agent (what the AI posts) -> customer gets a mail.
  customer = User.find_by(email: CUSTOMER)
  portal = Ticket.create!(title: 'Portalvraag', group: Group.find_by(name: 'Users'), customer: customer, state: Ticket::State.find_by(name: 'new'))
  Ticket::Article.create!(ticket: portal, type: Ticket::Article::Type.lookup(name: 'web'), sender: Ticket::Article::Sender.lookup(name: 'Customer'),
                          from: CUSTOMER, body: 'Hoe werkt X?', internal: false, created_by_id: customer.id, updated_by_id: customer.id)
  TransactionDispatcher.commit
  check(results, '6 AI webhook still queued for portal (web) articles', jobs_like('TriggerWebhookJob') >= 1, "jobs=#{jobs_like('TriggerWebhookJob')}")
  agent = User.find_by(email: 'jonkhout@prudai.com')
  Ticket::Article.create!(ticket: portal, type: Ticket::Article::Type.lookup(name: 'web'), sender: Ticket::Article::Sender.lookup(name: 'Agent'),
                          body: '<p>Zo werkt X: stap 1.</p>', content_type: 'text/html', internal: false, created_by_id: agent.id, updated_by_id: agent.id)
  TransactionDispatcher.commit
  notice = Ticket::Article.where(ticket_id: portal.id, sender: Ticket::Article::Sender.lookup(name: 'System')).last
  check(results, '6 customer mailed on public web reply', notice.present? && notice.type.name == 'email' && notice.to.to_s.include?(CUSTOMER), "to=#{notice&.to}")
  check(results, '6 notice is internal (no duplicate in the portal)', notice&.internal == true)
  body = notice&.body.to_s
  check(results, '6 notice carries the reply text', body.include?('Zo werkt X: stap 1.'))
  check(results, '6 notice carries signature + confidentiality footer', body.include?('Prudai Support') && body.include?('vertrouwelijk en uitsluitend bestemd voor'))
  check(results, '6 notice has no unrendered placeholders', !body.include?('#{'))
  # Stored subject is the rendered title; TicketArticleCommunicateEmailJob adds the
  # [Ticket#…] hook on send (app/jobs/ticket_article_communicate_email_job.rb:31).
  check(results, '6 notice subject is the rendered ticket title', notice&.subject == 'Portalvraag', notice&.subject)
  check(results, '6 notice is sent from the support address', notice&.from.to_s.include?(ADDRESS), notice&.from)

  # 7. Agent replies by e-mail -> Zammad sends that article itself; no extra notice.
  system_before = Ticket::Article.where(ticket_id: portal.id, sender: Ticket::Article::Sender.lookup(name: 'System')).count
  jobs_before = jobs_like('TicketArticleCommunicateEmailJob')
  Ticket::Article.create!(ticket: portal, type: Ticket::Article::Type.lookup(name: 'email'), sender: Ticket::Article::Sender.lookup(name: 'Agent'),
                          from: "Prudai Support <#{ADDRESS}>", to: CUSTOMER, subject: 'Re: Portalvraag', body: 'Per mail.', internal: false,
                          created_by_id: agent.id, updated_by_id: agent.id)
  # 8. Internal note -> nothing to the customer.
  Ticket::Article.create!(ticket: portal, type: Ticket::Article::Type.lookup(name: 'note'), sender: Ticket::Article::Sender.lookup(name: 'Agent'),
                          body: 'Intern.', internal: true, created_by_id: agent.id, updated_by_id: agent.id)
  TransactionDispatcher.commit
  system_after = Ticket::Article.where(ticket_id: portal.id, sender: Ticket::Article::Sender.lookup(name: 'System')).count
  check(results, '7+8 no notice for e-mail replies or internal notes', system_after == system_before, "delta=#{system_after - system_before}")
  check(results, '7 agent e-mail reply queued for sending', jobs_like('TicketArticleCommunicateEmailJob') == jobs_before + 1, "delta=#{jobs_like('TicketArticleCommunicateEmailJob') - jobs_before}")

  Delayed::Job.delete_all # nothing in the sandbox may try to deliver anything
end

puts "__SANDBOX__#{JSON.generate(results)}"
