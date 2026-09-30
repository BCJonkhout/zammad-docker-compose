# Security readiness Q4-2026 — Zammad (support.prudai.com)

Remediations for the internal security-readiness review of 2026-09-30
(findings ZAM-1 … ZAM-7; method: `/root/docs/runbooks/pentest-methodology.md`).
Branch `pentest-readiness-2026-09-30`. Code fixes carry their own regression
tests in `docker/autoreply/tests/`; this file holds the parts that are ops
steps or design limitations rather than code.

| ID | Severity | What | Where it landed |
|---|---|---|---|
| ZAM-1 | Medium | autoreply service held an `admin` API token | `bin/provision-zammad.sh` + ops step below |
| ZAM-2 | Medium | no tenant primitive; `organizations.shared` defaulted to true | `bin/provision-zammad.sh` + design note below |
| ZAM-3 | Low | inbound e-mail unfenced in the LLM prompt | `docker/autoreply/app.py` (`fence_customer_input`) |
| ZAM-4 | Low | regex "sanitizer" for model HTML | `docker/autoreply/app.py` (`sanitize_html_fragment`, allowlist parser) |
| ZAM-5 | Low | Elasticsearch without authentication | `docker-compose.yml` env gate + rollout below |
| ZAM-6 | Info | shared realm signup lands as Customer (correct) | no change, see below |
| ZAM-7 | Info | bearer compare not constant-time | `docker/autoreply/app.py` (`hmac.compare_digest`) |

## ZAM-1 — least-privilege autoreply service account (ops step required)

Before: the `ai-agent@…` service user had roles `Admin + Agent` and its
persistent API token `autoreply-agent` was minted with *all* of the user's
permissions (`admin`, `report`, `knowledge_base.editor`, …). That token is
mounted read-only into `zammad-autoreply`, the one container that parses
attacker-controlled inbound e-mail and makes outbound HTTP.

After (`bin/provision-zammad.sh`, ZAM-1 block):

- Service user roles: **`Agent` only**, group access `Users: full`. The
  script raises if the user still resolves `admin`.
- Token `autoreply-agent` permission list, exactly what `docker/autoreply/app.py`
  calls and nothing more:

  | Permission | Endpoints in `app.py` (`ZammadClient`) |
  |---|---|
  | `ticket.agent` | `GET /api/v1/ticket_articles/by_ticket/:id`, `GET /api/v1/tags?object=Ticket`, `POST /api/v1/tags/add`, `PUT /api/v1/tickets/:id` (priority), `POST /api/v1/ticket_articles` (public reply, internal note) |
  | `knowledge_base.reader` | `POST /api/v1/knowledge_bases/search` (flavor `agent`; the `public` flavor needs no permission), `GET /api/v1/knowledge_bases/:kb/answers/:id` |

  Zammad evaluates a token as *user permissions AND token permission list*
  (`Token::Permissions#permissions?`), so both halves are the ceiling.
  `test_provision_contract.py` fails if `app.py` starts calling other Zammad
  endpoints, so the list and the code cannot drift apart silently.

### Ops step — apply and re-mint

The next provisioning run narrows the existing token in place (same token
value, smaller scope). Because the old value lived for months inside the
e-mail-exposed container, also rotate it once:

```sh
cd /root/zammad
agent-bus claim zammad "ZAM-1 autoreply token re-mint"   # coordinate, it recreates zammad-autoreply
AUTOREPLY_TOKEN_ROTATE=1 bash bin/provision-zammad.sh
```

What that does: destroys token `autoreply-agent`, creates a fresh one with the
two permissions above, writes it to `secrets/autoreply.token` (mode 600), and
recreates `zammad-autoreply` (the script already does that at the end).
Verify afterwards, read-only:

```sh
docker exec zammad-zammad-postgresql-1 psql -U zammad -d zammad_production -Atc \
  "select preferences from tokens where name='autoreply-agent' and action='api';"
# expect only: ticket.agent, knowledge_base.reader
docker exec zammad-zammad-postgresql-1 psql -U zammad -d zammad_production -Atc \
  "select r.name from users u join roles_users ru on ru.user_id=u.id join roles r on r.id=ru.role_id where u.email='ai-agent@support.prudai.com';"
# expect only: Agent
```

Then send one test ticket from a customer account and confirm the autoreply
still posts its public reply + internal note (`docker logs zammad-zammad-autoreply-1`);
a `403` on any `/api/v1/...` call there means the scope is too narrow — the
table above is the contract to compare against.

Same class, not changed here: the `docs-sync` service user also carries
`Admin` (its token *is* scoped to `knowledge_base.editor`, and it runs from a
host systemd unit rather than an internet-facing container). A dedicated
"knowledge-base editor" role would remove that residual; queued as follow-up,
not part of this change.

## ZAM-2 — Zammad has no tenant primitive (design limitation)

Zammad is a single-tenant helpdesk: one `zammad_production` database, no
row-level security, one Elasticsearch namespace, and one role model
(`Customer` / `Agent` / `Admin`) shared by everyone who logs in. The only
customer grouping is the **Organization**, and its `shared` flag is seeded by
Zammad as default **true** — meaning every member of a shared organization can
read every other member's tickets through the normal customer portal. That is
intended for one company running its own helpdesk; Prudai runs several
customer companies on one instance, so it is the wrong default here.

Isolation between Prudai's customers therefore rests entirely on the
application role check: a Customer sees only their own tickets (or their
organization's, when shared). There is no defence in depth below that layer.
This is the same class of limitation the Q4-2026 tenancy companion documents
for the other pooled services (no RLS, app-level predicates only) — see the
isolation/tenancy companion of the same review and the runbook above. For a
customer that needs a hard boundary (contractual or regulatory), the answer is
a dedicated Zammad instance, not more roles on the pooled one.

What provisioning now enforces (`bin/provision-zammad.sh`, ZAM-2 block):

- The object-manager attribute `Organization.shared` gets `default: false`, so
  an organization created in the admin UI starts **unshared**.
- Every organization with `shared = true` whose id is **not** listed in
  `ZAMMAD_SHARED_ORGANIZATION_IDS` (comma-separated ids, default empty) is set
  to `shared = false`. Sharing is an explicit, per-organization opt-in that
  survives re-provisioning only while the id stays in that variable. The
  result payload lists `organizations_unshared` so the operator sees what
  changed.
- Not covered: an organization created through the REST API without an
  explicit `shared` field still takes the database column default (`true`)
  until the next provisioning run. Nothing in this repo creates organizations
  that way.

Standing rules that follow from this:

- Never put users of two different customer companies in one organization.
- Customer-role users must never receive `Agent` (that role reads all tickets
  in its groups). Self-service signup and SSO both land on `Customer`
  (ZAM-6), keep it that way.
- A cross-tenant requirement stronger than "trust the role check" ⇒ dedicated
  instance.

## ZAM-5 — Elasticsearch authentication (gated rollout)

`zammad-elasticsearch` indexes every ticket, article, user and KB answer and
ran with `xpack.security.enabled=false`. It publishes no host port (only the
opt-in `scenarios/add-hostport-to-elasticsearch.yml` does, and the contract
test keeps it that way), but any container on the compose network
(`zammad-autoreply`, `litellm`, …) could read all customer tickets from it
without credentials.

`docker-compose.yml` now reads the flag from the environment:

| Variable (names only — values live in OpenBao `kv/prod/zammad/app`, rendered into `.env` by `bao-fetch zammad`) | Role |
|---|---|
| `ELASTICSEARCH_SECURITY_ENABLED` | `true` turns on `xpack.security.enabled`; unset/`false` = today's behaviour |
| `ELASTICSEARCH_USER` | must be `elastic` (the built-in superuser ES bootstraps) |
| `ELASTICSEARCH_PASS` | used twice: as `ELASTIC_PASSWORD` (ES bootstrap password for `elastic`) and by Zammad, whose image entrypoint writes `es_user`/`es_password` settings from `ELASTICSEARCH_USER`/`ELASTICSEARCH_PASS` when both are set |
| `ELASTICSEARCH_SCHEMA` | stays `http`: TLS is explicitly off on the HTTP and transport layers, the container is only reachable inside the compose network |

Merging this compose change is itself a config change for the ES container,
so the next `docker compose up -d` recreates `zammad-elasticsearch` even with
the gate off. Do the whole thing in one window:

1. Add the three values to `kv/prod/zammad/app` (`ELASTICSEARCH_SECURITY_ENABLED=true`,
   `ELASTICSEARCH_USER=elastic`, a fresh `ELASTICSEARCH_PASS`), then `bao-fetch zammad`
   and confirm the names appear in `/root/zammad/.env` (`grep -c '^ELASTICSEARCH_' .env`).
2. `agent-bus claim zammad "ZAM-5 ES auth"`; announce a short search-index blip.
3. `docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate zammad-elasticsearch`
   and wait until `docker logs zammad-zammad-elasticsearch-1` shows the node
   started (`"started"` line, no `bootstrap checks failed`).
4. Push the credentials into Zammad's settings: `docker compose … up -d --force-recreate zammad-init`
   (its entrypoint sets `es_user`/`es_password`, then waits for ES and runs
   the search-index check). Follow with `… up -d --force-recreate zammad-railsserver zammad-scheduler zammad-websocket`
   so the app processes pick up the new env.
5. Verify, all read-only:
   - `docker exec zammad-zammad-autoreply-1 python -c "import urllib.request;urllib.request.urlopen('http://zammad-elasticsearch:9200/zammad*/_search')"`
     must now fail with HTTP 401 (that was the finding).
   - `docker exec zammad-zammad-railsserver-1 bundle exec rails r 'puts SearchIndexBackend.info.present?'`
     prints `true`, and a ticket search in the agent UI returns results.
   - post-deploy runbook §1/§2 (`/root/docs/runbooks/post-deploy.md`).
6. Rollback = set `ELASTICSEARCH_SECURITY_ENABLED=false` (or remove it),
   `bao-fetch zammad`, repeat steps 3–4. The index data is unaffected either
   way; only the auth layer flips.

Not done here: the ES container is not moved to a separate internal network.
That is the stronger cut (autoreply/litellm would then have no route to ES at
all) and needs the nginx/railsserver network layout reviewed first; the auth
gate is the step that can ship inside one window.

## ZAM-6 — shared `prudai` realm signup (no change)

OIDC is configured without role/group mapping, `Customer` is the only role
with `default_at_signup`, and third-party auto-link at first login is off. A
realm principal (any LEO/dashboard user) can create a support-portal Customer
account, which is the intended public-portal behaviour, and cannot become an
agent through that path. No code change; re-audit if agent roles are ever
mapped from the IdP or the realm is split per tenant.
