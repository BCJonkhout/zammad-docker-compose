#!/usr/bin/env python3
"""Report — and, only on explicit instruction, withdraw — knowledge-base
articles that correspond to SSO-gated docs pages.

Why this exists
---------------
docs.prudai.com puts a handful of competitive-edge pages behind Keycloak SSO
(the list is ``gated-pages.json`` in the docs repo).  The Zammad knowledge base
is anonymously readable and ``support.prudai.com/robots.txt`` is a 404, so an
article synced from such a page hands out exactly what the gate protects.
``docs-sync.py`` no longer publishes them, but articles that were already
published stay where they are: withdrawing customer-facing production content
is a human decision, not a side effect of a bug fix.

Default mode is a dry run: it reads, measures and prints the plan, and changes
nothing.  Withdrawal means *archiving* (``published`` -> ``archived``), never
deleting, and is reversible with a single ``unarchive`` call per article.

Usage
-----
    set -a; . /root/zammad/.env; . /root/zammad/docs-sync.env; set +a
    export ZAMMAD_DOCS_SYNC_TOKEN="$(<"$ZAMMAD_DOCS_SYNC_TOKEN_FILE")"

    python3 bin/docs-sync-gated-report.py              # dry run (default)
    python3 bin/docs-sync-gated-report.py --json       # dry run, machine-readable
    DOCS_GATED_WITHDRAW_CONFIRM=ja-intrekken \
        python3 bin/docs-sync-gated-report.py --apply  # actually archive
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("docs_sync", os.path.join(_HERE, "docs-sync.py"))
ds = importlib.util.module_from_spec(_spec)
sys.modules["docs_sync"] = ds
_spec.loader.exec_module(ds)

CONFIRM_ENV = "DOCS_GATED_WITHDRAW_CONFIRM"
CONFIRM_VALUE = "ja-intrekken"


def anonymous_status(url: str) -> int | str:
    """Status code an anonymous visitor gets — no token, no cookie."""
    try:
        response = requests.get(
            url,
            timeout=20,
            allow_redirects=False,
            headers={"User-Agent": "prudai-gated-report/1.0"},
        )
    except requests.RequestException as exc:
        return f"fout: {exc.__class__.__name__}"
    return response.status_code


def public_url(base_url: str, locale: str, category_id: int, answer_id: int) -> str:
    """Zammad matches on the numeric ids; the slug tail is cosmetic."""
    return f"{base_url.rstrip('/')}/help/{locale}/{category_id}/{answer_id}"


def collect(client: ds.ZammadClient, base_url: str, kb_id: int, language: str,
            gated_slugs: frozenset[str]) -> list[dict[str, Any]]:
    assets = ds.get_kb_snapshot(client, kb_id)
    kb_locale_id = ds.get_kb_locale_id(assets, kb_id)
    # Same key list as docs-sync.py's get_kb_locale_id(): Zammad's /init payload
    # keys this table as "KnowledgeBaseLocale".  Missing that first name made
    # asset_table() fall through to {} and the locale silently defaulted, which
    # put the wrong language segment in the public URL this report measures.
    locales = ds.asset_table(assets, "KnowledgeBaseLocale", "KnowledgeBase::Locale")
    locale_code = "nl-nl" if language == "nl" else "en-us"
    for locale in locales.values():
        if int(locale.get("id") or 0) == kb_locale_id:
            system_locales = ds.asset_table(assets, "Locale", "locale")
            for system_locale in system_locales.values():
                if int(system_locale.get("id") or 0) == int(locale.get("system_locale_id") or 0):
                    locale_code = str(system_locale.get("locale") or locale_code)
            break

    categories_by_id, _ = ds.build_category_state(assets, kb_locale_id, kb_id)
    answers_by_id, _ = ds.build_answer_state(assets, kb_locale_id, allowed_category_ids=set(categories_by_id))

    rows: list[dict[str, Any]] = []
    for answer in sorted(answers_by_id.values(), key=lambda item: item.id):
        if not answer.slug or answer.slug not in gated_slugs:
            continue
        category = categories_by_id.get(answer.category_id)
        url = public_url(base_url, locale_code, answer.category_id, answer.id)
        rows.append(
            {
                "kb_id": kb_id,
                "taal": language,
                "answer_id": answer.id,
                "slug": answer.slug,
                "titel": answer.title,
                "categorie_id": answer.category_id,
                "categorie": category.title if category else "?",
                "gepubliceerd": answer.published,
                "publieke_url": url,
                "anoniem_http": anonymous_status(url),
                "intrekken": f"POST /api/v1/knowledge_bases/{kb_id}/answers/{answer.id}/archive",
                "terugdraaien": f"POST /api/v1/knowledge_bases/{kb_id}/answers/{answer.id}/unarchive",
            }
        )
    return rows


def withdraw(client: ds.ZammadClient, row: dict[str, Any]) -> None:
    client.request(
        "POST",
        f"/api/v1/knowledge_bases/{row['kb_id']}/answers/{row['answer_id']}/archive",
        expected=(200,),
    )


def main() -> int:
    apply_changes = "--apply" in sys.argv[1:]
    as_json = "--json" in sys.argv[1:]

    base_url = ds.getenv("ZAMMAD_BASE_URL")
    token = ds.getenv("ZAMMAD_DOCS_SYNC_TOKEN")
    kb_nl_id = int(ds.getenv("ZAMMAD_DOCS_KB_NL_ID"))
    kb_en_id = int(ds.getenv("ZAMMAD_DOCS_KB_EN_ID"))
    gated_slugs = ds.load_gated_slugs()

    client = ds.ZammadClient(base_url, token)
    rows = collect(client, base_url, kb_nl_id, "nl", gated_slugs)
    rows += collect(client, base_url, kb_en_id, "en", gated_slugs)

    found = {(row["taal"], row["slug"]) for row in rows}
    missing = sorted(
        f"{language}:{slug}"
        for slug in gated_slugs
        for language in ("nl", "en")
        if (language, slug) not in found
    )

    if as_json:
        print(json.dumps({"gated_slugs": sorted(gated_slugs), "artikelen": rows,
                          "zonder_artikel": missing, "modus": "apply" if apply_changes else "droogloop"},
                         indent=2, ensure_ascii=False))
    else:
        print(f"Afgeschermde slugs ({ds.gated_pages_file()}): {', '.join(sorted(gated_slugs))}\n")
        header = f"{'taal':4}  {'id':>4}  {'slug':16}  {'gepub':5}  {'anon':>4}  categorie / titel"
        print(header)
        print("-" * len(header))
        for row in rows:
            print(
                f"{row['taal']:4}  {row['answer_id']:>4}  {row['slug']:16}  "
                f"{'ja' if row['gepubliceerd'] else 'nee':5}  {str(row['anoniem_http']):>4}  "
                f"{row['categorie']} / {row['titel']}"
            )
            print(f"{'':38}{row['publieke_url']}")
        if missing:
            print(f"\nGeen KB-artikel (nog niet gesynct): {', '.join(missing)}")

    if not apply_changes:
        print(
            f"\nDROOGLOOP — er is niets gewijzigd. Intrekken zou {len(rows)} artikel(en) "
            "archiveren (published -> archived): ze verdwijnen uit de publieke kennisbank, "
            "blijven bestaan voor agents, en zijn per artikel met één 'unarchive'-aanroep "
            "terug te zetten. De aanroepen staan hierboven per artikel.",
            file=sys.stderr,
        )
        return 0

    if os.getenv(CONFIRM_ENV, "").strip() != CONFIRM_VALUE:
        print(
            f"--apply geweigerd: zet {CONFIRM_ENV}={CONFIRM_VALUE} om {len(rows)} "
            "klantgerichte artikelen daadwerkelijk te archiveren. Dat is een besluit van Beau, "
            "geen agentbesluit.",
            file=sys.stderr,
        )
        return 2

    for row in rows:
        withdraw(client, row)
        print(f"[intrekken] {row['taal']} #{row['answer_id']} '{row['slug']}' gearchiveerd.", file=sys.stderr)
    print(f"{len(rows)} artikel(en) gearchiveerd. Terugdraaien: 'unarchive' per artikel.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ds.GatedPagesError as exc:
        print(f"[gated-report] FOUT (fail-closed): {exc}", file=sys.stderr)
        raise SystemExit(1) from None
