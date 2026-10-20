# E-mailkanaal support@prudai.com in Zammad — ontwerp, uitrol, terugrol, testplan

Status: **ontwerp + code (draft-PR), niets live.** Besluit Beau 19-10-2026: "E-mailkanaal inrichten".
Code: `bin/provision-email-channel.sh` (config als code), `bin/email_channel_signature.py`,
kleine aanpassingen in `bin/provision-zammad.sh` en `docker/autoreply/app.py`, tests in
`docker/autoreply/tests/test_email_channel.py`, sandbox-run `bin/test-email-channel-sandbox.sh`.

## 1. Aanleiding en doel

Zammad heeft geen in- of uitgaand e-mailkanaal. Een e-mailartikel wordt opgeslagen maar nooit
afgeleverd, en een portalantwoord gaat stil: er is geen trigger die de klant mailt. Antwoorden
gaan nu via agent-mail of Outlook, buiten Zammad om (het antwoord op #59024 van 28-08 is daardoor
waarschijnlijk nooit gezien).

Doel: support@prudai.com wordt een echt kanaal.

- (a) Mail van een klant aan support@ wordt een ticket, of hangt aan het bestaande ticket.
- (b) Een antwoord vanuit Zammad komt per mail bij de klant aan, met de huisstijl-handtekening
  "Prudai Support" en de vertrouwelijkheidsvoettekst.

## 2. Huidige stand (gemeten 20-10-2026, alleen metadata)

| Wat | Stand |
|---|---|
| Zammad-kanalen | alleen `Email::Notification` (SendGrid, afzender `"Prudai Support" <support@prudai.com>`) |
| Zammad-config | 1 groep (`Users`) zonder e-mailadres; 0 e-mailadressen; 0 postmasterfilters; handtekening `default`; seed-trigger `customer notification (on owner change)` staat **uit** |
| AI-triggers | `prudai ai first response …` en `… follow-up …` staan aan, op artikeltypes 1 (email), 5 (phone), 11 (web), afzender Customer |
| support@ in M365 | gedeelde mailbox (Full Access + Send As: Beau, Geert). Inbox: 37 berichten, alle 37 ongelezen, maart t/m oktober 2026. 19 daarvan komen van support@ zelf (de AI-escalatiemails van de autoreply), 5 van eigen medewerkers, 12 van buiten, 1 systeemmail |
| Wie leest support@ | Niemand rechtstreeks. Mail wordt doorgestuurd naar Beau en Geert (de gedeelde box houdt een ongelezen kopie). `inbox-verwerken` leest Beau's inbox en verwerkt die kopieën dus wél (`SHARED_SYSTEM` in `inbox_tools.py`). n8n: 0 verwijzingen naar support@, geen Outlook-nodes. Cron: niets. agent-mail verstuurt via SendGrid (standaard vanaf agent@, `--from support@` kan) en leest niets |
| DNS | SPF `include:spf.protection.outlook.com include:sendgrid.net`; DKIM voor M365 (`selector1`) én SendGrid (`s1`); DMARC `p=quarantine` |

## 3. Keuze: Microsoft Graph voor in én uit

**Gekozen: het Zammad-kanaal "Microsoft 365 Graph Email" (Zammad 7.0), met OAuth via een eigen
app-registratie in Entra, voor zowel inkomend als uitgaand.** SendGrid blijft voor de
systeemnotificaties aan medewerkers (`Email::Notification`) en voor de escalatiemail van de autoreply.

Waarom:

1. **Voor inkomend is er geen route zonder OAuth.** Basic-auth IMAP/POP staat uit in M365, en
   Zammad 7 kent geen app-only Graph-toegang: de Graph-driver (`Channel::Driver::MicrosoftGraphInbound`)
   werkt met een gedelegeerd token (scopes `offline_access openid profile email mail.readwrite
   mail.readwrite.shared mail.send mail.send.shared`, zie `lib/external_credential/microsoft_graph.rb`).
   De app-registratie in Entra is dus hoe dan ook nodig.
2. **Dan kost uitgaand via Graph niets extra**, en het levert meer op dan SendGrid: zelfde
   identiteit als inkomend, DKIM van M365 (al ingericht), verzonden mail staat in "Verzonden items"
   van support@, geen tweede geheim.
3. **SMTP-relay via SendGrid voor uitgaand past niet in het kanaalmodel.** Een Graph-account in
   Zammad heeft altijd een Graph-outbound. Die met de hand omzetten naar SMTP is een niet-ondersteunde
   wijziging die bij elke herkoppeling verdwijnt.

Nadelen, met de maatregel:

- **Het token is van de persoon die koppelt.** Wordt dat account geblokkeerd, verandert het
  wachtwoord of wordt het token ingetrokken, dan valt het kanaal uit. Zammad toont dat in
  Admin > Kanalen. Een gedelegeerd `Mail.ReadWrite` geeft Zammad bovendien toegang tot de eigen
  mailbox van die persoon (en `Mail.ReadWrite.Shared` tot elke box waar die persoon Full Access op
  heeft). **Advies:** koppel met een apart serviceaccount (bv. `zammad-mail@prudai.com`) dat alleen
  Full Access + Send As op support@ krijgt. Kan dat niet op korte termijn, dan koppelt Beau met zijn
  eigen account en zetten we het serviceaccount als vervolgstap op de lijst. Of zo'n account een
  Exchange-licentie nodig heeft voor Graph-toegang tot een gedeelde box, is **niet geverifieerd**:
  dat zoekt de browseragent uit (prompt A, stap 7).
- **Het clientgeheim verloopt** (advies: 24 maanden). Zammad kopieert het geheim bij het koppelen
  in de kanaalopties, dus na vernieuwen moet het account opnieuw gekoppeld worden. Zet de
  vervaldatum als taak in CATO. **Let op bij herkoppelen:** Zammad neemt dan `keep_on_server` over,
  maar `folder_id` niet, en het kanaal blijft aan. De volgende ophaalronde leest dan de hele Inbox
  (`ExternalCredential::MicrosoftBase.link_account`, pad met `channel_id`). Daarom gaat
  herkoppelen altijd zo:
  1. `deactivate`;
  2. in de UI opnieuw koppelen en de inbound-dialoog annuleren;
  3. `apply`, dat de map opnieuw opzoekt;
  4. `activate`.

  Draait er toch een `apply` of `plan` op een actief kanaal zonder map, dan zet het script het
  kanaal eerst uit (guard `active_channel_has_folder`).

## 4. Ontwerp

| Onderdeel | Waar | Wanneer actief |
|---|---|---|
| Map `Zammad` in support@ + inboxregel "kopie naar Zammad" | M365 (browser) | regel pas vlak vóór `activate` |
| Kanaal haalt alleen uit map `Zammad`, `keep_on_server = true` | `provision-email-channel.sh apply` | met het kanaal |
| Postmasterfilter: mail **van** support@ negeren | idem | meteen (raakt alleen inkomende kanaalmail) |
| Postmasterfilter: header `X-Prudai-Mail-Kind: ai-escalation` negeren | idem + `app.py` | meteen |
| AI-autoreply niet op e-mailartikelen (fase 1) | `provision-zammad.sh` + `provision-email-channel.sh`, `ZAMMAD_AI_ON_EMAIL` | meteen (er bestaan nog geen e-mailartikelen) |
| Trigger "klant mailen bij publiek antwoord" (types note/web, afzender Agent) | `provision-email-channel.sh` | alleen als het kanaal aan staat |
| Trigger "staf mailen bij klantreactie per e-mail" (fase 1) | idem | kanaal aan én fase 1 |
| Handtekening "Prudai Support" (huisstijl + vertrouwelijkheidsvoettekst) | idem, bron `prudai-huisstijl/signature/out/support.html` | groep gebruikt hem als het kanaal aan staat |
| Groep `Users` ↔ support@ | idem, afgeleid van kanaalstatus | kanaal aan = gekoppeld, uit = ontkoppeld |
| Autoreply negeert auto-replies (`is-auto-response`) | `app.py` | na rebuild autoreply |
| Escalatiemail draagt `X-Prudai-Mail-Kind`, `Auto-Submitted`, `X-Auto-Response-Suppress` | `app.py` | na rebuild autoreply |

Toelichting bij de keuzes die niet vanzelf spreken:

- **Een aparte map in plaats van de Inbox.** Met `keep_on_server` haalt Zammad alle *ongelezen*
  berichten uit de gekozen map. Op de Inbox zou dat de 37 oude berichten (maart–oktober) in één
  keer als nieuwe tickets importeren, inclusief de 19 escalatiemails. De kopieerregel stuurt alleen
  nieuwe mail naar `Zammad`; de Inbox en het doorsturen naar Beau en Geert blijven zoals ze zijn.
  Zammad markeert wat het verwerkt als gelezen, alleen in die map.
- **Alleen ongelezen mail wordt opgehaald.** Met `keep_on_server` vraagt de driver alleen ongelezen
  berichten op (`list_messages(unread_only: true)`). Open in de map `Zammad` dus niets met de hand:
  een bericht dat daar als gelezen gemarkeerd is, wordt nooit een ticket. De Inbox is voor mensen.
- **`keep_on_server = true` is een harde poort.** De standaard in de Zammad-UI is "nee", en dan
  *verwijdert* Zammad elk opgehaald bericht van de server (`MicrosoftGraph#delete_message`).
  `activate` weigert zonder.
- **Fase 1: de AI beantwoordt geen e-mail.** Klanten die nu mailen, zitten vaak in een gesprek met
  een mens (Outlook, agent-mail). Een AI-antwoord daar tussendoor is dubbele afhandeling. In fase 1
  komen e-mailtickets bij een mens: nieuwe tickets via de bestaande melding aan medewerkers, een
  klantreactie op een bestaand ticket via de staftrigger (de standaardmatrix mailt medewerkers
  bewust niet bij updates, om de AI-gesprekken stil te houden). Fase 2 (AI ook op e-mail) is één
  variabele (`ZAMMAD_AI_ON_EMAIL=1`) en een `apply`. Dan gaat de staftrigger vanzelf uit.
- **Portal- en AI-antwoorden gaan per mail via een trigger, niet als e-mailartikel.** Dit is de
  oorzaak van het stille antwoord op #59024: een publiek `web`-artikel of een publieke notitie mailt
  niemand. De trigger stuurt het antwoord met de handtekening naar de klant, en legt het verstuurde
  bericht vast als *intern* e-mailartikel, zodat het niet dubbel in het portaal staat. Antwoorden
  die zelf al een e-mail zijn (een medewerker die in Zammad "per e-mail" antwoordt), slaat de
  trigger over. Die verstuurt Zammad zelf, met de groepshandtekening.
- **De groepskoppeling volgt de kanaalstatus.** Met één groep koppelt Zammad die groep automatisch
  aan support@ zodra de OAuth-stap het e-mailadres aanmaakt (`EmailAddress#update_email_address_id`),
  terwijl het kanaal nog uit staat. `apply` maakt dat ongedaan. Pas `activate` koppelt de groep en
  zet de handtekening. `deactivate` zet de oude handtekening terug.

Wat Zammad zelf al tegen lussen doet (geverifieerd in de 7.0-broncode):
`OwnNotificationLoopDetection` laat eigen notificaties vallen (Message-ID op de fqdn + `Precedence: bulk`).
`AutoResponseCheck` markeert `Auto-Submitted`, `X-Loop`, `Precedence: bulk/list/junk` en
`X-Auto-Response-Suppress: All` als auto-reply. `OutOfOfficeCheck` herkent Outlook-, Gmail- en
Zimbra-afwezigheidsmeldingen. Uitgaande trigger- en systeemmail krijgt `X-Loop`, `Precedence: bulk`,
`Auto-Submitted: auto-generated` en `X-Auto-Response-Suppress: All` (`Channel::EmailBuild`).
Triggermail gaat nooit naar een eigen systeemadres en respecteert `send_no_auto_response_reg_exp`.

## 5. Risico's

| # | Risico | Maatregel |
|---|---|---|
| R1 | Oude inbox (37 ongelezen) komt bij activatie als tickets binnen, met mails aan klanten | eigen map `Zammad` + kopieerregel pas vlak vóór `activate`; `activate` weigert zonder map |
| R2 | Zammad verwijdert mail uit M365 | `keep_on_server` geforceerd op `true`; poort in `activate`; de Inbox wordt sowieso niet gelezen |
| R3 | Lus: de AI-escalatiemail (van support@ aan support@) wordt een ticket, de AI antwoordt, er volgt een nieuwe escalatie | postmasterfilter op afzender support@ + op `X-Prudai-Mail-Kind`; sandbox-scenario's 3 en 4 |
| R4 | Lus: afwezigheidsmelding van de klant → AI-antwoord → afwezigheidsmelding | fase 1: de AI slaat e-mail over; fase 2: `app.py` slaat `is-auto-response` over (getest) |
| R5 | Dubbele afhandeling: dezelfde klantmail komt als ticket in Zammad, als doorgestuurde kopie bij Beau/Geert, én via `inbox-verwerken` als concept-antwoord + CATO-taak | **voorwaarde vóór activatie:** `inbox-verwerken` behandelt mail aan support@ als "staat in Zammad" (alleen ter info, geen concept, geen taak). Dat is een wijziging in `/root/.claude/skills/inbox-verwerken/` en valt buiten deze PR. Na twee weken stabiel draaien: het doorsturen naar Beau/Geert uitzetten (EAC, browser) |
| R6 | De AI antwoordt in een gesprek dat een mens via Outlook of agent-mail voert | fase 1: geen AI op e-mail |
| R7 | De seed-trigger "customer notification (on owner change)" gaat klanten mailen zodra de groep een adres heeft | staat uit (gemeten); het script rapporteert de stand bij elke run (`owner_change_trigger_active`) en raakt hem niet aan |
| R8 | Het token of clientgeheim verloopt of wordt ingetrokken: het kanaal valt stil, en dan gaat het antwoord weer niet weg | Zammad toont kanaalfouten in Admin > Kanalen; CATO-taak voor de vervaldatum van het geheim; vervolgstap: een monitor op de kanaalstatus (status-page of `monitor-*.sh`) |
| R9 | Gedelegeerd token geeft Zammad toegang tot de eigen mailbox van wie koppelt | apart serviceaccount (§3); anders beperken tot Beau en als vervolgpunt op de lijst |
| R10 | Een medewerker antwoordt per mail op een staf-notificatie. Dat landt als notitie in het ticket, niet bij de klant | de notificatie zegt het zelf ("Beantwoord in Zammad, niet op deze mail") |
| R11 | Elke staf-notificatie en elke klantmail via de trigger wordt als intern artikel in het ticket bewaard | bewust: zo is zichtbaar wat er verstuurd is. Kost wat ruimte in de tijdlijn |
| R12 | Gespoofte mail met afzender support@ wordt genegeerd | gewenst. DMARC `p=quarantine` vangt het grootste deel al op |
| R13 | De handtekening verliest de kleur van de links en de `<sup>`-stijl (Zammad-sanitizer) | cosmetisch; logo, keurmerken en voettekst blijven (als inline-PNG; externe afbeeldingen zou de sanitizer verwijderen) |
| R14 | `provision-zammad.sh` herstartte bij de autoreply-recreate ook railsserver + nginx | in deze PR gerepareerd (`--no-deps`). Voor deze uitrol hoeft `provision-zammad.sh` niet te draaien |
| R16 | Herkoppelen in de UI laat `folder_id` vallen terwijl het kanaal aan blijft: Zammad leest dan de Inbox (R1 opnieuw) | vaste procedure in §3 (eerst `deactivate`); elke `plan`/`apply` zet een actief kanaal zonder map direct uit; vervolgstap: een monitor die alarmeert bij een actief kanaal zonder map |
| R17 | Fase 1: een afwezigheidsmelding van de klant geeft ook een staf-notificatie. Fase 2: klantmail die als auto-reply gemarkeerd is (`Precedence: bulk`, `Auto-Submitted`) krijgt geen AI-antwoord én geen staf-notificatie | fase 1 is alleen ruis. Voor fase 2 beoordelen we bij de fase-2-beslissing hoeveel van zulke mails er binnenkwamen (aantal tickets met `is-auto-response`) |
| R18 | De seed-triggers `auto reply (on new tickets / follow-up)` gaan weer aan en mailen dan elke klant | `provision-zammad.sh` zet ze uit (in productie uit, gemeten); `activate` weigert als er één aan staat (guard `seed_auto_replies_inactive`) |
| R15 | Klantmail staat nu ook in Zammad (bewaartermijn, ISMS) | zelfde regime als portaltickets; geen nieuwe categorie gegevens. Geen inhoud in commits, CATO of logs (alleen metaniveau) |

## 6. Uitrolstappen

Elke stap die iets live verandert, wacht op een expliciete go van Beau.

0. **Voorwaarden.**
   - PR gemerged.
   - `inbox-verwerken` aangepast (R5).
   - Besluit: met welk account wordt gekoppeld (§3).
   - Autoreply herbouwd met de nieuwe `app.py`:
     `docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build --force-recreate --no-deps zammad-autoreply`.
     Dat is een productieherstart van alleen de autoreply: ~seconden geen AI-antwoorden, de webhook
     herhaalt. Daarna de post-deploy-check.
1. **Op de host** (go: config-wijziging): `bin/provision-email-channel.sh plan` → beoordelen →
   `bin/provision-email-channel.sh apply`.
   Gedrag in productie verandert hierdoor niet: de filters werken alleen op een e-mailkanaal dat
   nog niet bestaat, de nieuwe triggers staan uit, de AI-types verliezen alleen "email" (dat komt
   nog niet voor) en de handtekening hangt nog aan niets. `provision-zammad.sh` hoeft niet te draaien.
2. **Browser, prompt A:** de app-registratie in Entra.
3. **Browser, prompt B:**
   - app-gegevens invullen in Zammad;
   - map `Zammad` aanmaken in support@ (zonder regel);
   - het account koppelen en de dialoog met de inbound-instellingen **annuleren**;
   - het bestaande doorsturen en de regels van support@ uitlezen.
4. **Op de host:** `bin/provision-email-channel.sh apply`. Dit zoekt de map op via Graph, forceert
   `keep_on_server`, hernoemt het adres naar "Prudai Support" en ontkoppelt de groep weer. Daarna
   `plan`: alle guards in de uitvoer staan op `ok`, en `keep_on_server: true` en
   `folder_id_set: true`. De go-marker en `shared_mailbox` controleert `activate` zelf; die staan
   niet in de guardlijst.
5. **Activatie, in één sessie:**
   - prompt C (browser): de kopieerregel aanmaken;
   - op de host: `ZAMMAD_EMAIL_CHANNEL_GO="Beau <datum> <tijd>" bin/provision-email-channel.sh activate`.
     De go-marker wordt in de kanaalvoorkeuren bewaard.
6. **Het testplan (§8), live deel**, direct na de activatie.
7. **Na twee weken:**
   - fase 2 beoordelen (AI ook op e-mail);
   - het doorsturen naar Beau/Geert uitzetten;
   - het serviceaccount regelen, als dat nog niet gebeurd is.

## 7. Terugrol

Elke stap is los terug te draaien. Niets in deze uitrol verwijdert data.

- **Direct (seconden):** `bin/provision-email-channel.sh deactivate`. Het kanaal stopt met ophalen
  en versturen, de groep is ontkoppeld en heeft weer de oude handtekening, en beide nieuwe triggers
  staan uit. Herhaalbaar, en in de sandbox getest.
- Kopieerregel in support@ uitzetten (OWA, rechtstreeks in de regel).
- Volledig: het kanaal verwijderen in Admin > Kanalen > Microsoft 365 Graph Email, de app-consent
  intrekken en het geheim verwijderen in Entra.
- Fase 2 terugdraaien: `ZAMMAD_AI_ON_EMAIL` weghalen uit OpenBao (`kv/prod/zammad/app`),
  `bao-fetch zammad`, daarna `apply`.
- Al aangemaakte tickets blijven bestaan. Dat is gewenst: het zijn echte klantvragen.
- Code: de PR reverten. De `app.py`-wijziging is los veilig: twee extra headers en een overslag op
  auto-replies.

## 8. Testplan

### 8a. Sandbox (geautomatiseerd, gedraaid op 20-10-2026)

`bin/test-email-channel-sandbox.sh <suffix>` start een wegwerp-Zammad 7.0 (Postgres op tmpfs,
Redis, Rails; alles `--rm`, één netwerk-namespace, geen poorten) met de productie-uitgangstoestand.
Daarna: een nep-OAuth-koppeling, `apply` twee keer (de tweede keer 0 wijzigingen), `activate`
zonder go-marker (geweigerd), `activate`, en daarna de mailscenario's door de echte
`Channel::EmailParser` en de echte triggers:

1. Nieuwe klantmail → ticket, afzender Customer, type email. Geen AI-webhook (fase 1).
2. Klantreactie per mail → hangt aan hetzelfde ticket; staf krijgt een intern e-mailartikel, de klant niet.
3. Mail van support@ zelf → genegeerd.
4. Mail met `X-Prudai-Mail-Kind: ai-escalation` → genegeerd.
5. Outlook-afwezigheidsmelding → opgeslagen met `is-auto-response: true`.
6. Portalticket + publiek `web`-antwoord → AI-webhook voor web blijft werken. De klant krijgt een
   (intern vastgelegde) mail van support@: de antwoordtekst, handtekening en voettekst erin, geen
   losse placeholders, en bij verzending het ticketnummer in het onderwerp.
7. Antwoord als e-mailartikel → geen extra notificatie; het artikel staat klaar om verstuurd te worden.
8. Interne notitie → niets naar de klant.

Ook getest:

- 3b: `it-support@prudai.com` valt niet onder het eigen-adresfilter.
- De groepskoppeling na `apply`, `activate` en `deactivate`.
- Het herkoppelen: een actief kanaal raakt zijn map kwijt, `apply` zet het uit, en daarna komt de
  map terug en kan het kanaal weer aan.
- `deactivate`, waarna `plan` 0 wijzigingen toont.

Uitkomst 20-10-2026 (run `aae0c`, ná de auditfixes): **35/35 PASS**.

Dekking eerlijk benoemd:

- `docker/autoreply/tests/test_email_channel.py` test de `app.py`-wijzigingen en de handtekening
  op gedrag (breek-de-test-probe: zonder de auto-reply-overslag wordt hij rood).
- Voor de provisioning zijn het **contracttests op tekst**: ze vangen het weghalen van een regel,
  niet een logicafout.
- Het gedrag van de provisioning (poorten, koppeling, triggers, filters) dekt alleen de
  sandbox-harness. Draai die na elke wijziging aan `provision-email-channel.sh`.

### 8b. Live (na activatie; testadres = een extern adres van Beau, bv. privé-Gmail)

| # | Richting | Actie | Verwacht | Waar kijken |
|---|---|---|---|---|
| L1 | in | testadres mailt support@ ("Test kanaal L1") | binnen ~1 min een nieuw ticket, klant = testadres, géén AI-antwoord; medewerkers krijgen de nieuw-ticketmelding | Zammad-overzicht; scheduler-log `fetching Microsoft Graph (support@prudai.com folder_id=…,keep_on_server=true)` |
| L2 | uit | Beau antwoordt in Zammad "per e-mail" | testadres ontvangt het antwoord van "Prudai Support" <support@prudai.com>, onderwerp met `[Ticket#…]`, handtekening + voettekst; DMARC/DKIM pass in de headers | testadres; Verzonden items van support@ |
| L3 | in | testadres antwoordt op L2 | hangt aan hetzelfde ticket; staf-notificatie bij de eigenaar + Beau/Geert | ticket-tijdlijn |
| L4 | uit | publieke notitie (of portalantwoord) op een portalticket van het testaccount | testadres krijgt de trigger-mail met antwoord + link naar het portaal | testadres |
| L5 | in (lus) | afwezigheidsmelding aanzetten op het testadres, Beau antwoordt nog een keer | de afwezigheidsmelding landt als artikel met `is-auto-response`; geen nieuwe mail naar het testadres | ticket; testadres |
| L6 | in (lus) | een AI-escalatie op een portalticket uitlokken | de escalatiemail naar support@ wordt **geen** ticket (filter); hij staat wel in de Inbox **en komt nog steeds doorgestuurd aan bij Beau/Geert** (de nieuwe header `Auto-Submitted: auto-generated` kan doorsturen beïnvloeden; dat is niet geverifieerd) | aantal tickets; Inbox support@; inbox Beau |
| L7 | M365 | na de tests | de berichten in map `Zammad` zijn gelezen en **niet** verwijderd; de Inbox is onveranderd | OWA support@ |
| L8 | terugrol | `deactivate` + `plan`, daarna weer `activate` | kanaal uit/aan zonder fouten; groepskoppeling volgt | uitvoer van het script |

Klaar is: L1–L8 groen, de uitkomst als opmerking op de CATO-taak, het testticket gesloten.

## 9. Browseragent-prompts

### Prompt A — Entra-app-registratie

```text
Doel: een app-registratie in Microsoft Entra voor het Zammad-e-mailkanaal van support@prudai.com.
Bekende stand (bevestig of corrigeer): tenant prudai.com; support@prudai.com is een GEDEELDE
mailbox; Beau en Geert hebben er Full Access + Send As op; er bestaat nog GEEN app-registratie
voor Zammad. Zammad draait op https://support.prudai.com (versie 7.0).

1. Open https://entra.microsoft.com → Identity → Applications → App registrations → "New registration".
2. Name: "Zammad support.prudai.com (Graph mail)".
   Supported account types: "Accounts in this organizational directory only (single tenant)".
   Redirect URI: platform "Web", URL exact:
   https://support.prudai.com/api/v1/external_credentials/microsoft_graph/callback
   Klik Register.
3. Noteer van de Overview-pagina: "Application (client) ID" en "Directory (tenant) ID".
4. API permissions → Add a permission → Microsoft Graph → Delegated permissions. Voeg toe:
   offline_access, openid, profile, email, Mail.ReadWrite, Mail.ReadWrite.Shared, Mail.Send,
   Mail.Send.Shared. GEEN Application permissions toevoegen. Laat User.Read staan.
   Klik "Grant admin consent for Prudai" en bevestig. Alle regels moeten "Granted" tonen.
5. Certificates & secrets → Client secrets → New client secret. Description "zammad-graph-2026-10",
   Expires: 24 months. Kopieer de VALUE meteen (die is daarna nooit meer te zien).
   Zet de value NIET in je eindrapport of in een chat. Bewaar hem alleen tijdelijk voor prompt B
   (bij voorkeur geeft Beau hem daar zelf in).
6. Authentication: controleer dat de redirect-URI exact klopt en dat "Access tokens" en
   "ID tokens" bij Implicit grant UIT staan (niet nodig).
7. Alleen uitzoeken, niets aanmaken: bestaat er een gebruiker zammad-mail@prudai.com? Kan een
   gebruiker zonder Exchange-licentie via Graph bij een gedeelde mailbox waarop hij Full Access
   heeft? Noteer de bron (Microsoft Learn-pagina) of "onbekend".
Veiligheid: niets verwijderen; geen andere apps wijzigen; geen extra permissies; geen
Conditional Access aanpassen. Wijkt een scherm af van deze beschrijving: stop en beschrijf wat je ziet.
Rapporteer: app-naam, client ID, tenant ID, vervaldatum van het geheim, lijst van permissies +
consentstatus, de redirect-URI zoals opgeslagen, en de bevindingen uit stap 7. NIET de secret-value.
```

### Prompt B — Zammad koppelen (kanaal blijft uit) + map + uitlezen

```text
Doel: het Microsoft 365-account in Zammad koppelen ZONDER het kanaal aan te zetten, en de map
"Zammad" in support@ aanmaken. Bekende stand (bevestig of corrigeer): de app-registratie
"Zammad support.prudai.com (Graph mail)" bestaat (client ID <uit prompt A>, tenant ID <uit
prompt A>). Zammad heeft nog GEEN Microsoft 365 Graph-kanaal. support@ is een gedeelde mailbox.
Er is nog GEEN map "Zammad" en GEEN regel die ernaar kopieert.

1. Outlook op het web: open https://outlook.office.com/mail/support@prudai.com/ (de gedeelde
   mailbox, niet je eigen). Maak een nieuwe map "Zammad" op het hoogste niveau (naast Inbox, dus
   niet erin). Maak GEEN regel aan.
2. Lees alleen, wijzig niets: Instellingen (tandwiel) → E-mail → "Doorsturen" en "Regels" van
   deze mailbox. Noteer: staat doorsturen aan (naar welk adres, wordt er een kopie bewaard)?
   Welke regels zijn er (naam, voorwaarde, actie, "stoppen met verwerken")?
3. Zammad: log in op https://support.prudai.com met "Login met Prudai" (account Beau, admin).
   Ga naar https://support.prudai.com/#channels/microsoft_graph.
4. Klik op het instellen van de app ("Connect Microsoft 365 App" of vergelijkbaar). Vul in: Client ID,
   Client Secret (de value uit prompt A; laat Beau die zelf plakken als dat kan) en Tenant ID.
   Opslaan.
5. Klik "Add Account". Kies voor een gedeelde mailbox ("shared mailbox") en vul support@prudai.com
   in. Meld je bij Microsoft aan met het account dat Beau aanwijst (standaard jonkhout@prudai.com;
   als zammad-mail@prudai.com bestaat en Beau dat zegt, dat account). Het consentscherm hoort al
   door de admin goedgekeurd te zijn.
6. Terug in Zammad opent een dialoog met de inbound-instellingen (map, "Keep messages on server",
   doelgroep). Belangrijk: klik NIET op opslaan/doorgaan/activeren. Sluit de dialoog met Cancel of
   het kruisje. Dit is bewust: het kanaal wordt later met een script aangezet, met de juiste
   instellingen.
7. Controleer in de lijst: er staat één account support@prudai.com en het staat UIT ("disabled" of
   "inactive"). Staat het AAN: klik meteen op Disable en meld dat.
Veiligheid: geen mail versturen; geen berichten in support@ verplaatsen, lezen of verwijderen;
geen regels aanmaken of wijzigen; het kanaal niet activeren. Wijkt een scherm af: stop en beschrijf.
Rapporteer: de map is aangemaakt (exacte naam en plek), doorsturen + regels uit stap 2, het account
waarmee gekoppeld is, de status van het kanaal in Zammad (aan/uit), eventuele foutmeldingen.
```

### Prompt C — kopieerregel (alleen op de dag van activatie, vlak vóór `activate`)

```text
Doel: nieuwe mail aan support@prudai.com ook in de map "Zammad" zetten. Bekende stand: de map
"Zammad" bestaat op het hoogste niveau van de gedeelde mailbox support@. Het Zammad-kanaal is
gekoppeld en staat UIT. Doorsturen en regels zoals gerapporteerd in prompt B: <invullen>.

1. Open https://outlook.office.com/mail/support@prudai.com/ → Instellingen → E-mail → Regels →
   "Nieuwe regel".
2. Naam: "Kopie naar Zammad". Voorwaarde: "Toepassen op alle berichten". Actie: "Kopiëren naar"
   → map "Zammad". Zet "Stoppen met verwerken van meer regels" UIT. Opslaan.
3. Staat er een regel met "stoppen met verwerken" vóór deze regel: zet "Kopie naar Zammad" bovenaan.
4. Is "Kopiëren naar" niet beschikbaar in OWA: stop en meld dat. Het alternatief is Exchange
   Online PowerShell (`New-InboxRule -Mailbox support@prudai.com -Name "Kopie naar Zammad"
   -CopyToFolder "support@prudai.com:\Zammad"`), en dat beslist Beau.
Veiligheid: bestaande regels en het doorsturen niet wijzigen of verwijderen; geen mail versturen.
Rapporteer: de regel zoals opgeslagen (voorwaarde, actie, volgorde) en het tijdstip.
```

## 10. Bediening

```sh
bin/provision-email-channel.sh plan          # alleen lezen (transactie met rollback)
bin/provision-email-channel.sh apply         # idempotent; zet niets aan
ZAMMAD_EMAIL_CHANNEL_GO="Beau 2026-10-2x 10:00" bin/provision-email-channel.sh activate
bin/provision-email-channel.sh deactivate    # terugrol
# fase 2: ZAMMAD_AI_ON_EMAIL=1 in OpenBao kv/prod/zammad/app zetten, `bao-fetch zammad`
# (.env wordt in zijn geheel gerenderd, nooit met de hand), daarna:
bin/provision-email-channel.sh apply   # en bij een volgende provision-zammad.sh-run blijft het zo
bin/test-email-channel-sandbox.sh <suffix>   # wegwerp-Zammad; stop daarna de drie containers
```

Variabelen (standaard tussen haakjes):

- `ZAMMAD_EMAIL_ADDRESS` (support@prudai.com)
- `ZAMMAD_EMAIL_DISPLAY_NAME` (Prudai Support)
- `ZAMMAD_EMAIL_FOLDER` (Zammad)
- `ZAMMAD_EMAIL_FOLDER_ID`: slaat de Graph-opzoeking over
- `ZAMMAD_EMAIL_STAFF` (jonkhout@, haisma@)
- `ZAMMAD_EMAIL_SIGNATURE_FILE`
- `ZAMMAD_AI_ON_EMAIL` (0)
- `ZAMMAD_GRAPH_CLIENT_ID`, `ZAMMAD_GRAPH_CLIENT_SECRET`, `ZAMMAD_GRAPH_TENANT_ID`: optioneel.
  Normaal vult prompt B ze in de UI in. Als ze gezet zijn, houdt het script de app-gegevens in
  Zammad gelijk aan deze waarden.
