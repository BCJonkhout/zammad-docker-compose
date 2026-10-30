# Prudai Support-chat: lokale oplevering en latere uitrol

Dit betreft alleen SPEC-punt 8, CATO 8baf5345 / feedback #1547. Geen
provisioning, OpenBao-write, productieherstart, deploy of echte mail uitgevoerd.
De backend en frontend worden elders gebouwd. De onderstaande commando's zijn
een draaiboek voor een later geautoriseerd uitrolmoment.

## Contract en bestanden

- `bin/provision-zammad.sh` maakt idempotent groep `Support`, tag `prudai-chat`,
  Agent-servicegebruiker met login `prudai-platform` en e-mail
  `prudai-platform@support.prudai.com`, token `secrets/prudai-platform.token`,
  webhook en trigger `Prudai-chat: antwoord naar de app`.
- De servicegebruiker heeft uitsluitend Support-rechten `read/create/change`.
  Het token heeft alleen `ticket.agent`. Zammad kent geen aparte machtiging
  voor alleen artikelen toevoegen: `change` is ook nodig. Geen Admin, andere
  groepen, `full`, `delete` of KB-tokenrecht. Klant opzoeken/aanmaken kan in
  `POST /api/v1/tickets` met `customer_id: "guess:<email>"`, zonder `admin.user`.
- Webhook: POST `https://api.prudai.com/support/zammad/webhook`, TLS-verificatie
  aan, standaard Zammad-JSON met `ticket` en `article`. Zammad zet zijn native
  `bearer_token` om in `Authorization: Bearer <token>`. Override:
  `PRUDAI_CHAT_WEBHOOK_URL`; uitsluitend HTTPS zonder credentials in de URL.
  Secret: `secrets/prudai-chat-webhook.token`, 0600, directory 0700, hergebruikt.
- De trigger vereist tag, nieuw openbaar artikel en geen System-afzender.
  Een menselijke Agent is toegestaan; API-/systeemgebruikers worden daarbij
  uitgesloten. Daarnaast mag een e-mailartikel van een exact
  `@prudai.com`-adres door, ook als Zammad dat als Customer registreert.
  `support@prudai.com` en `no-reply@prudai.com` zijn in beide takken uitgesloten.
- Alle bestaande `notification.email`-triggers en triggers naar de bestaande
  AI-webhook krijgen een AND-taguitsluiting; hun oude selectie blijft bestaan,
  ook bij expert-OR-selecties. De al uitgeschakelde standaardautoresponders
  blijven uitgeschakeld. Gewone supporttickets behouden hun bestaande flow.
- `docker/zammad-assets/prudai_chat.rb` bewaakt daarnaast de daadwerkelijke
  agentmail en automatische trigger-/schedulermail, voor alle agents, ook
  eigenaars, vermeldingen, vervangers en toekomstige agents. Online-notificaties
  blijven bestaan. De gebruikersmatrix heeft in Zammad 7.0 geen tagfilter.
  De bestaande image neemt deze initializer op; provisioning weigert zonder
  deze geladen agentmailguard. Alle Rails-processen moeten de nieuwe image laden.
- `docker/autoreply/app.py` leest de actuele tags via de API vóór artikelreads,
  AI, shadow, mail en schrijfacties. Een chat-ticket wordt overgeslagen, ook
  als een oude/gequeue-de webhookpayload de tag nog niet bevat. Taglookup-fouten
  stoppen verwerking. Er wordt geen klantinhoud gelogd door deze guard.
- Secret-output wordt uit het provisioningresultaat gefilterd. Tokenbestanden
  worden relatief aan de checkout geschreven, niet naar een vaste andere repo.

## Volgorde voor de latere productie-uitrol

1. Backend voorbereiden met webhook-auth, API-adapter en idempotentie. Laat de
   echte Zammad-adapter nog geen cases aanmaken tot stap 6 gecontroleerd is.
   Eerst backend live, daarna frontend. Lees ook
   `docs/security-readiness-q4-2026.md`; provisioning wijzigt meer dan chat,
   waaronder organisatie-sharing en bestaande instellingen. Inventariseer de
   commits tussen draaiende en gewenste versie en controleer de actuele backup.

2. Ga naar de **deploymentcheckout**, niet deze headless worktree. Controleer
   de tak/SHA en bewaar een beperkte configuratiesnapshot en oude images vóór
   de wijziging. Gebruik een besloten map; de snapshot bevat geen ticketinhoud
   of tokens. Doe dit samen met de uitrol onder een geslaagde lock:

   ```bash
   cd /root/zammad
   agent-bus ps
   agent-bus locks
   agent-bus claim zammad "Prudai-chat uitrol en controle" && bash -euo pipefail <<'SH'
   umask 077
   test ! -e secrets/prudai-chat-rollback
   for rollback_image in prudai/zammad-support:pre-prudai-chat prudai/zammad-autoreply:pre-prudai-chat; do
     if docker image inspect "${rollback_image}" >/dev/null 2>&1; then
       echo "Rollbackimage bestaat al; bewaar de bestaande set en stop." >&2
       exit 1
     fi
   done
   mkdir secrets/prudai-chat-rollback
   docker compose exec -T zammad-railsserver bundle exec rails runner \
     'require "json"; puts JSON.generate(triggers: Trigger.all.map { |t| {id: t.id, condition: t.condition, active: t.active} }, agents: User.joins(:roles).where(roles: {name: "Agent"}).distinct.map { |u| {id: u.id, groups: u.group_ids_access_map} })' \
     | grep '^{' > secrets/prudai-chat-rollback/config.json
   docker image tag "$(docker compose images -q zammad-railsserver)" prudai/zammad-support:pre-prudai-chat
   docker image tag "$(docker compose images -q zammad-autoreply)" prudai/zammad-autoreply:pre-prudai-chat
   bash -n bin/provision-zammad.sh
   .venv/bin/pytest docker/autoreply -q
   SH
   ```

   Als één stap faalt: stop de uitrol. De lock is geen deploygoedkeuring.
   Bewaar een bestaande rollbackset; overschrijf die niet bij een herhaalrun.

3. Bouw de nieuwe images. Meet dienstverkeer vlak vóór de gerichte recreates.
   Voer na de afgesproken uitrolpoort deze stappen uit onder dezelfde lock:

   ```bash
   agent-bus claim zammad "Prudai-chat images laden" && bash -euo pipefail <<'SH'
   docker compose -f docker-compose.yml -f docker-compose.override.yml build zammad-railsserver zammad-autoreply
   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --no-deps zammad-railsserver zammad-scheduler zammad-websocket
   docker compose -f docker-compose.yml -f docker-compose.override.yml exec -T zammad-railsserver bundle exec rails runner \
     'raise "chat guard missing" unless Transaction::Notification < PrudaiChat::AgentEmail; raise "automatic mail guard missing" unless Ticket::PerformChanges::Action::NotificationEmail < PrudaiChat::AutomaticEmail; puts "chat guards loaded"'
   PRUDAI_PROVISION_RESTART_AUTOREPLY=0 bash bin/provision-zammad.sh
   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --no-deps zammad-autoreply
   SH
   ```

   De automatische recreate aan het eind van provisioning is hier expliciet
   uitgezet. Bij gewone provisioning is hij standaard aan, nu met `--no-deps`.
   Bij staging: gebruik een aparte Zammad-instantie en stel
   `PRUDAI_CHAT_WEBHOOK_URL=https://<staging-api-host>/support/zammad/webhook` in
   vóór provisioning. Wijzig daarvoor nooit de productie-webhook. Platform-
   staging gebruikt volgens de SPEC zijn dry-run-adapter en verstuurt geen mail.

4. Zet na succesvolle provisioning de twee waarden in OpenBao. **Niet uitgevoerd
   in deze opdracht.** Exacte commando's voor `kv/prod/platform-backend/app`:

   ```bash
   set +x
   agent-bus claim platform-backend "Prudai-chat tokens publiceren" && \
     bao-set --env prod platform-backend \
       "ZAMMAD_WEBHOOK_TOKEN=$(<secrets/prudai-chat-webhook.token)" \
       "ZAMMAD_API_TOKEN=$(<secrets/prudai-platform.token)"
   ```

   Geen `cat` van tokens of shell tracing. `bao-set` toont alleen namen/lengtes;
   het rendert ook de productie-`.env`. Het vernieuwen van de backendprocessen
   blijft onderdeel van de aparte backenduitrol, met `--no-deps` en juiste
   servicenaam. Beheer andere backendinstellingen daar. Staging gebruikt nooit
   deze productietokens of een productie-webhook-URL. Een aparte staging-
   Zammad kan overeenkomstig publiceren met `bao-set --env staging
   platform-backend ...`, waarna de staging-render nodig is.

5. Controleer in railsserver én scheduler dat de twee mailguards geladen zijn.
   Controleer idempotentie met een tweede provisioningrun: dezelfde user,
   webhook, trigger en tokens; geen extra AND-lagen. Lees alleen aantallen,
   scopes en ids terug, nooit tokenwaarden. Controleer bij de servicegebruiker
   ook de effectieve `group_ids_access`: rollen kunnen groepsrechten erven;
   provisioning stopt wanneer de Agent-rol extra groepen of full/overview-recht
   oplevert. De directe `group_ids_access_map` alleen toont die erfenis niet.

6. Activeer pas daarna de echte backendadapter en voer de SPEC-e2e uit met
   het testaccount en `[TEST]`, zonder echte klantdata. Test minimaal:
   chatvraag die LEO kan beantwoorden maakt geen ticket; escalatie maakt één
   Support-ticket; herhaalbericht blijft daarin; teammail komt alleen via
   Prudai; Outlook-antwoord gaat via webhook naar dezelfde chat en levert een
   korte klantmail; interne notitie, System, support@, no-reply@ en externe
   klantmail sturen geen staff-antwoordwebhook; geen Zammad-agentmail bij create,
   reply, mentions, eigenaarschap, reminders of escalatie; een gewoon support-
   mailticket behoudt zijn eerdere notificatie/AI-gedrag. Sluit het testticket.
   Controleer rechten met dit token: artikelen lezen/toevoegen en tags in
   Support toegestaan, tickets in andere groepen en admin-endpoints verboden.
   Rapporteer alleen aantallen/status. Volg de normale post-deploy-controle en
   geef daarna de locks vrij. CATO blijft open tot deze e2e is bewezen.

## Backendvoorwaarden die maillekken voorkomen

`POST /api/v1/tickets` moet **in dezelfde create-request** `group: "Support"`
en `tags: "prudai-chat"` bevatten. Zammad 7.0 verwacht tags daar als
kommagescheiden string. Achteraf taggen kan de create-notificatie al laten
ontsnappen. Klantberichten worden een `note`-artikel met `sender: "Customer"`,
`internal: false` en `preferences: {"send-auto-response": false}`. Nooit een
uitgaand `email`-artikel maken: dat is directe verzending, buiten de automatische
mailpaden. De webhook ontvangt onvertrouwde mailmetadata: de backend moet auth,
ticket-tag, artikel-id, afzenderautorisatie, quotes/handtekeningen, bijlagen en
idempotentie zelf blijven controleren. Domeinmatching alleen bewijst geen
authenticiteit van een inkomend e-mailbericht.

## Terugdraaien zonder tickets of bestanden te verwijderen

1. Pauzeer eerst nieuwe cases en webhookverwerking in de backend. Laat de
   chat-tag en de mailguards staan zolang bestaande chat-tickets bestaan;
   volledige rollback van de guard zou die tickets weer kunnen laten mailen.
   Bewaar tokens in OpenBao; blind verwijderen/roteren breekt lopende requests.
2. Zet alleen de chat-trigger/webhook en servicegebruiker inactief onder de lock:

   ```bash
   agent-bus claim zammad "Prudai-chat pauzeren" && \
     docker compose exec -T zammad-railsserver bundle exec rails runner \
       'UserInfo.current_user_id = 1; Trigger.find_by(name: "Prudai-chat: antwoord naar de app")&.update!(active: false); Webhook.find_by(name: "Prudai-chat: antwoord naar de app")&.update!(active: false); User.find_by(login: "prudai-platform")&.update!(active: false); puts "chat integration paused"'
   ```

   Dit is de veilige functionele rollback: normale supportflow blijft draaien,
   chat-ticketmails blijven geblokkeerd en er worden geen tickets verwijderd.
3. Alleen voor een volledige code/config-rollback, nadat bewezen is dat er geen
   chat-tickets (ook gesloten) of mail-/webhookjobs meer zijn: herstel de oorspronkelijke
   triggerselecties/actiefstatus en groepsrechten uit de snapshot, en zet de
   oude images terug. De nieuwe integratie blijft inactief; Support en tickets
   worden behouden. Herstel nooit de hele database over nieuwere tickets.

   ```bash
   agent-bus claim zammad "Prudai-chat configuratie herstellen" && bash -euo pipefail <<'SH'
   docker compose exec -T zammad-railsserver bash -lc \
     'cat > /tmp/prudai-chat-rollback.json && bundle exec rails runner '\''require "json"; UserInfo.current_user_id = 1; snapshot = JSON.parse(File.read("/tmp/prudai-chat-rollback.json")); ActiveRecord::Base.transaction do; snapshot.fetch("triggers").each { |r| Trigger.find(r.fetch("id")).update!(condition: r.fetch("condition"), active: r.fetch("active")) }; snapshot.fetch("agents").each { |r| u = User.find(r.fetch("id")); u.group_ids_access_map = r.fetch("groups"); u.save! }; end; puts "configuration restored"'\''' \
     < secrets/prudai-chat-rollback/config.json
   docker image tag prudai/zammad-support:pre-prudai-chat prudai/zammad-support:7.0.0-0042
   docker image tag prudai/zammad-autoreply:pre-prudai-chat prudai/zammad-autoreply:7.0.0-0042
   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --no-deps zammad-railsserver zammad-scheduler zammad-websocket zammad-autoreply
   SH
   ```

   Pas de doeltags aan als de deployment een andere `VERSION` gebruikt.
   Controleer gewone ticketflow opnieuw. Provisioning opnieuw draaien activeert
   de integratie weer; gebruik dat niet als rollback. De provisioning bevat
   bestaande bredere instellingen die deze chatsnapshot bewust niet herstelt.

## Bewijs en grenzen van deze oplevering

Lokale controles: volledige `.venv/bin/pytest docker/autoreply -q`, Bash-
syntax, embedded Ruby-syntax en initializer-syntax, `git diff --check` en
`audit-lint`. De nieuwe uitvoerbare tests toetsen beide mail-entrypoints,
afzenders, tagfilters, idempotente selectorverrijking en gewone ticketflow.
De tests draaien zonder Rails/DB/mail. De bestaande Docker-image-syntaxcheck
kan worden overgeslagen als de basisimage lokaal ontbreekt; de nieuwe host-
Ruby-syntaxcheck draait dan alsnog. Echte API-autorisatie, mailaflevering,
Rails-boot en de volledige Outlook/chat-e2e zijn pas na de latere uitrol bewezen.

Zammad 7.0-bronnen gebruikt voor het contract:
[notificatie](https://github.com/zammad/zammad/blob/7.0.0/app/models/transaction/notification.rb),
[mailmatrix](https://github.com/zammad/zammad/blob/7.0.0/lib/notification_factory/mailer.rb),
[automatische mail](https://github.com/zammad/zammad/blob/7.0.0/app/models/ticket/perform_changes/action/notification_email.rb),
[selectors](https://github.com/zammad/zammad/blob/7.0.0/lib/selector/sql.rb),
[ticket-aanmaak](https://github.com/zammad/zammad/blob/7.0.0/app/controllers/tickets_controller.rb),
[groepsrechten](https://github.com/zammad/zammad/blob/7.0.0/app/models/concerns/has_groups.rb).
