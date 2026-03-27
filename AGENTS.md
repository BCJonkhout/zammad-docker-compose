# Zammad Agent Guide

This guide applies inside the `zammad/` repository.

## Purpose

- This repository runs PrudAI's customer support stack at `https://support.prudai.com`.
- It is used for customer tickets, the public knowledge base, PrudAI SSO login, and AI-assisted first-response support.
- The goal is to let customers self-serve or receive fast AI help first, while escalating to human staff when the AI detects risk, security, billing, account-access, outage, or insufficient documentation coverage.
- The public service health page for PrudAI is `https://status.prudai.com`.

## Main Services

- `zammad-railsserver`, `zammad-scheduler`, `zammad-websocket`, and `zammad-nginx` run the core Zammad app.
- `zammad-autoreply` receives Zammad webhooks and writes AI-generated ticket replies and internal notes.
- `litellm` powers the AI decision and response layer used by `zammad-autoreply`.
- PostgreSQL, Redis, and Memcached provide storage, queues, and caching.

## How It Works

### Login and portal

- Customers log in at `https://support.prudai.com/#login`.
- PrudAI SSO is the primary login path.
- Dutch (`nl-nl`) and light mode are the intended defaults.
- Custom login, portal, knowledge-base, and ticket UI behavior lives in:
  - `docker/zammad-assets/application.html.erb`
  - `docker/zammad-assets/knowledge_base.html.erb`
  - `docker/zammad-assets/prudai-support.css`

### Knowledge base

- Zammad hosts PrudAI's public docs in Dutch and English knowledge bases.
- The docs sync flow pulls content from PrudAI docs into Zammad.
- Main files:
  - `bin/docs-sync.py`
  - `bin/run-docs-sync.sh`
  - `systemd/zammad-docs-sync.service`
  - `systemd/zammad-docs-sync.timer`

### Ticket flow

- A customer creates a ticket from the portal or sends a follow-up on an existing ticket.
- Zammad triggers a webhook to `zammad-autoreply`.
- `zammad-autoreply` retrieves the ticket articles, searches the PrudAI knowledge base, asks the model for a decision, and then:
  - posts a public reply if the docs clearly answer the question
  - posts a public escalation notice plus an internal note if a human should take over
  - posts an internal handoff note if the AI cannot safely answer from docs
- Follow-up customer messages should continue the AI conversation until escalation is needed.

## Notification Rules

- Staff email notifications are intended for:
  - new ticket creation
  - AI escalation to a human
- Staff should not receive an email for every routine follow-up message in an AI conversation.
- AI-written public replies set `send-auto-response: false` to avoid noisy auto-response behavior.
- Escalations must be visible to the customer with a public message, not only an internal note.

## Provisioning

- `bin/provision-zammad.sh` is the main PrudAI-specific setup entrypoint.
- It configures:
  - PrudAI branding and logos
  - Dutch locale defaults
  - PrudAI OIDC / SSO
  - knowledge bases
  - service users and API tokens
  - SMTP sender configuration
  - AI webhook triggers
  - escalation notification triggers
  - agent notification defaults
- After changing Zammad setup logic, this script usually needs to be re-run.

## Important Files

- `bin/provision-zammad.sh`
  - Main configuration and trigger provisioning logic.
- `docker/autoreply/app.py`
  - AI webhook handler and ticket reply logic.
- `docker-compose.yml`
  - Base service definitions.
- `docker-compose.override.yml`
  - PrudAI-specific runtime overrides and asset versioning.
- `docker/zammad-assets/application.html.erb`
  - Global app shell customization, localization helpers, and ticket/login UI tweaks.
- `docker/zammad-assets/knowledge_base.html.erb`
  - Knowledge base shell customization.
- `docker/zammad-assets/prudai-support.css`
  - PrudAI-specific styling.

## Common Operations

- Rebuild and restart the support stack:
  - `docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build --force-recreate`
- Rebuild only the AI autoreply service:
  - `docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build --force-recreate zammad-autoreply`
- Apply PrudAI provisioning again:
  - `bash bin/provision-zammad.sh`
- Check autoreply logs:
  - `docker logs -f zammad-zammad-autoreply-1`
- Check scheduler logs:
  - `docker logs -f zammad-zammad-scheduler-1`
- Check Rails logs:
  - `docker logs -f zammad-zammad-railsserver-1`

## Working Notes

- The repo may have uncommitted local customization work. Do not reset unrelated changes.
- Asset changes often require both a container rebuild and a cache-busting asset version bump in `docker-compose.override.yml`.
- If a ticket-page or login-page UI change appears not to apply, verify the live asset bundle version before assuming the code is wrong.
- If AI replies stop appearing, check:
  - Zammad trigger provisioning
  - scheduler jobs for `TriggerWebhookJob`
  - `zammad-autoreply` logs
  - whether the ticket article came from a customer
- If the UI appears in English or dark mode unexpectedly, verify both:
  - `Setting.get('locale_default')`
  - user preferences in Zammad

## Git

- If you create a commit from this repo, use the format:
  - `type(scope): message`
- Example:
  - `fix(zammad): suppress routine ticket update emails`
