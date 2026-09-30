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

## ZAM-6 — shared `prudai` realm signup (no change)

OIDC is configured without role/group mapping, `Customer` is the only role
with `default_at_signup`, and third-party auto-link at first login is off. A
realm principal (any LEO/dashboard user) can create a support-portal Customer
account, which is the intended public-portal behaviour, and cannot become an
agent through that path. No code change; re-audit if agent roles are ever
mapped from the IdP or the realm is split per tenant.
