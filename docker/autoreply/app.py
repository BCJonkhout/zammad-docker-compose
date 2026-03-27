#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape, unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("prudai-autoreply")

AUTOREPLY_MARKER_PREFIX = "prudai-autoreply:docs-bm25:v2"
DEFAULT_PORT = int(os.getenv("PORT", "8081"))
REQUEST_TIMEOUT = 60
SEARCH_LIMIT_PER_KB = 4
MAX_CONTEXT_RESULTS = 4
MAX_BODY_CHARS = 1800

DISPOSITION_REPLY = "reply_with_docs"
DISPOSITION_HANDOFF = "handoff"
DISPOSITION_ESCALATE = "escalate"

CATEGORY_HOW_TO = "how_to"
CATEGORY_BUG = "bug"
CATEGORY_BILLING = "billing"
CATEGORY_SECURITY = "security"
CATEGORY_OUTAGE = "outage"
CATEGORY_ACCOUNT_ACCESS = "account_access"
CATEGORY_DATA_ISSUE = "data_issue"
CATEGORY_GENERAL = "general"

ALLOWED_DISPOSITIONS = {
    DISPOSITION_REPLY,
    DISPOSITION_HANDOFF,
    DISPOSITION_ESCALATE,
}
ALLOWED_CATEGORIES = {
    CATEGORY_HOW_TO,
    CATEGORY_BUG,
    CATEGORY_BILLING,
    CATEGORY_SECURITY,
    CATEGORY_OUTAGE,
    CATEGORY_ACCOUNT_ACCESS,
    CATEGORY_DATA_ISSUE,
    CATEGORY_GENERAL,
}
ALLOWED_PRIORITIES = {"normal", "high"}
PRIORITY_IDS = {"high": 3}

POLICY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        CATEGORY_SECURITY,
        (
            "security",
            "beveilig",
            "breach",
            "hacked",
            "hack",
            "compromis",
            "compromise",
            "phishing",
            "unauthorized",
            "onbevoegd",
            "datalek",
            "lek",
        ),
    ),
    (
        CATEGORY_OUTAGE,
        (
            "outage",
            "offline",
            "down",
            "incident",
            "storing",
            "not available",
            "unavailable",
            "niet beschikbaar",
            "kan niet bereiken",
            "cannot reach",
        ),
    ),
    (
        CATEGORY_ACCOUNT_ACCESS,
        (
            "login",
            "log in",
            "sign in",
            "inloggen",
            "locked out",
            "geen toegang",
            "can't access",
            "cannot access",
            "2fa",
            "two-factor",
            "two factor",
            "wachtwoord",
            "password reset",
        ),
    ),
    (
        CATEGORY_BILLING,
        (
            "billing",
            "invoice",
            "factuur",
            "payment",
            "betaling",
            "charge",
            "subscription",
            "abonnement",
        ),
    ),
    (
        CATEGORY_DATA_ISSUE,
        (
            "data loss",
            "verwijderd",
            "deleted",
            "kwijt",
            "corrupt",
            "privacy",
            "gdpr",
            "avg",
            "personal data",
        ),
    ),
]

BUG_HINTS = (
    "bug",
    "error",
    "issue",
    "probleem",
    "werkt niet",
    "not working",
    "fails",
    "failure",
    "crash",
    "kapot",
)

SEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "de",
    "dit",
    "een",
    "en",
    "exactly",
    "help",
    "het",
    "hoe",
    "how",
    "i",
    "ik",
    "in",
    "is",
    "it",
    "je",
    "kan",
    "kun",
    "me",
    "my",
    "of",
    "op",
    "or",
    "our",
    "please",
    "precies",
    "prudai",
    "support",
    "terug",
    "the",
    "this",
    "to",
    "waar",
    "we",
    "werkt",
    "what",
    "where",
    "zie",
}


def getenv(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not str(value).strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return str(value).strip()


def getenv_int(name: str, default: int | None = None) -> int:
    if default is None:
        raw_value = getenv(name)
    else:
        raw_value = os.getenv(name, str(default))
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:  # noqa: PERF203
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def read_secret(path_env: str) -> str:
    path = getenv(path_env)
    with open(path, "r", encoding="utf-8") as file:
        value = file.read().strip()
    if not value:
        raise RuntimeError(f"Secret file was empty: {path}")
    return value


def read_secret_if_exists(path: str | None) -> str:
    if not path:
        return ""

    normalized = str(path).strip()
    if not normalized or not os.path.exists(normalized):
        return ""

    with open(normalized, "r", encoding="utf-8") as file:
        return file.read().strip()


class HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


def html_to_text(value: str) -> str:
    parser = HTMLStripper()
    parser.feed(value or "")
    return normalize_whitespace(unescape(parser.get_text()))


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clip(value: str, limit: int) -> str:
    value = normalize_whitespace(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def strip_code_fences(value: str) -> str:
    stripped = (value or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_json_response(content: str) -> dict[str, Any]:
    raw = strip_code_fences(content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def detect_language(text: str) -> str:
    lowered = f" {text.lower()} "
    dutch_markers = [" hoe ", " wat ", " waar ", " niet ", " een ", " het ", " de ", " ik ", " werkt "]
    english_markers = [" how ", " what ", " where ", " not ", " the ", " and ", " i ", " works "]
    dutch_hits = sum(marker in lowered for marker in dutch_markers)
    english_hits = sum(marker in lowered for marker in english_markers)
    return "nl" if dutch_hits >= english_hits else "en"


def normalize_search_text(value: str) -> str:
    return normalize_whitespace(re.sub(r"[^0-9A-Za-zÀ-ÿ]+", " ", value or " "))


def build_search_queries(*values: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = normalize_whitespace(candidate)
        if len(normalized) < 2:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    combined = " ".join(value for value in values if value)
    title = values[0] if values else ""
    message = values[1] if len(values) > 1 else ""

    add(combined)
    add(title)
    add(message)
    add(normalize_search_text(combined))
    add(normalize_search_text(title))
    add(normalize_search_text(message))

    keyword_tokens = [
        token.lower()
        for token in normalize_search_text(combined).split()
        if len(token) >= 3 and token.lower() not in SEARCH_STOPWORDS
    ]
    if keyword_tokens:
        unique_keywords: list[str] = []
        for token in keyword_tokens:
            if token not in unique_keywords:
                unique_keywords.append(token)

        add(" ".join(unique_keywords[:6]))
        add(" ".join(unique_keywords[:4]))
        add(" ".join(unique_keywords[:3]))
        add(" ".join(unique_keywords[:2]))
        if len(unique_keywords) >= 2:
            add(f"{unique_keywords[0]} {unique_keywords[-1]}")
        for token in unique_keywords[:4]:
            add(token)

    return candidates


def sanitize_html_fragment(value: str) -> str:
    fragment = (value or "").strip()
    if not fragment:
        return ""
    fragment = re.sub(r"<\s*(script|style)\b.*?>.*?<\s*/\s*\1\s*>", "", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<!DOCTYPE.*?>", "", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<\s*/?\s*(html|head|body)\b[^>]*>", "", fragment, flags=re.IGNORECASE)
    fragment = fragment.strip()
    if "<" not in fragment and ">" not in fragment:
        fragment = f"<p>{escape(fragment)}</p>"
    return fragment


def sanitize_tag(value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    if not normalized:
        return None
    return normalized[:64]


@dataclass
class SearchResult:
    translation_id: int
    kb_id: int
    locale: str
    title: str
    subtitle: str
    public_url: str
    api_url: str
    preview: str
    body_html: str = ""


class ZammadClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token token={token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "prudai-zammad-autoreply/1.0",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        response = self.session.request(
            method=method,
            url=f"{self.base_url}{path}",
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code not in expected:
            raise RuntimeError(f"{method} {path} failed with status {response.status_code}: {response.text[:800]}")
        if not response.text.strip():
            return None
        return response.json()

    def get_ticket_articles(self, ticket_id: int) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/api/v1/ticket_articles/by_ticket/{ticket_id}?expand=true")
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected ticket articles payload for ticket {ticket_id}: {type(payload)!r}")
        return payload

    def get_ticket_tags(self, ticket_id: int) -> list[str]:
        payload = self.request("GET", f"/api/v1/tags?object=Ticket&o_id={ticket_id}")
        tags = payload.get("tags") or []
        return [str(tag).strip() for tag in tags if str(tag).strip()]

    def search_kb(self, kb_id: int, locale: str, query: str, *, flavor: str) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "knowledge_base_id": kb_id,
            "locale": locale,
            "query": query,
            "flavor": flavor,
            "limit": SEARCH_LIMIT_PER_KB,
        }
        if flavor == "agent":
            payload.update(
                {
                    "index": "KnowledgeBase::Answer::Translation",
                    "url_type": "agent",
                    "highlight_enabled": False,
                    "include_subtitle": True,
                }
            )

        response = self.request("POST", "/api/v1/knowledge_bases/search", json_body=payload)
        details = response.get("details") or []
        results: list[SearchResult] = []
        for detail in details:
            raw_url = str(detail.get("url") or "").strip()
            translation_id = int(detail.get("id") or 0)
            public_url = raw_url if flavor == "public" and raw_url and not raw_url.startswith("/api/") else ""
            api_url = raw_url if raw_url.startswith("/api/") else ""
            results.append(
                SearchResult(
                    translation_id=translation_id,
                    kb_id=kb_id,
                    locale=locale,
                    title=str(detail.get("title") or "").strip(),
                    subtitle=str(detail.get("subtitle") or "").strip(),
                    public_url=public_url,
                    api_url=api_url,
                    preview=normalize_whitespace(str(detail.get("body") or "")),
                )
            )
        return results

    def fetch_answer_body(self, result: SearchResult) -> str:
        if not result.api_url:
            return ""

        payload = self.request("GET", result.api_url)
        assets = payload.get("assets") or {}
        translations = assets.get("KnowledgeBaseAnswerTranslation") or assets.get("KnowledgeBase::Answer::Translation") or {}
        contents = assets.get("KnowledgeBaseAnswerTranslationContent") or assets.get("KnowledgeBase::Answer::Translation::Content") or {}

        translation = translations.get(str(result.translation_id)) or translations.get(result.translation_id)
        if translation is None:
            for candidate in translations.values():
                if str(candidate.get("title") or "").strip() == result.title:
                    translation = candidate
                    break
        if translation is None and translations:
            translation = next(iter(translations.values()))
        if translation is None:
            return ""

        content_id = translation.get("content_id")
        content = contents.get(str(content_id)) or contents.get(content_id) or {}
        body = str(content.get("body") or "")
        result.body_html = body
        return body

    def update_ticket(self, ticket_id: int, **fields: Any) -> dict[str, Any]:
        json_body = {key: value for key, value in fields.items() if value is not None}
        if not json_body:
            return {}
        return self.request("PUT", f"/api/v1/tickets/{ticket_id}", json_body=json_body)

    def create_public_reply(self, ticket_id: int, body_html: str, *, marker: str) -> dict[str, Any]:
        return self._create_article(
            ticket_id=ticket_id,
            body_html=body_html,
            internal=False,
            article_type="web",
            preferences={"send-auto-response": False, "prudai_marker": marker},
        )

    def create_internal_note(self, ticket_id: int, body_html: str, *, marker: str) -> dict[str, Any]:
        return self._create_article(
            ticket_id=ticket_id,
            body_html=body_html,
            internal=True,
            article_type="note",
            preferences={"prudai_marker": marker},
        )

    def _create_article(
        self,
        *,
        ticket_id: int,
        body_html: str,
        internal: bool,
        article_type: str,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/ticket_articles",
            expected=(201,),
            json_body={
                "ticket_id": ticket_id,
                "body": body_html,
                "content_type": "text/html",
                "sender": "Agent",
                "type": article_type,
                "internal": internal,
                "preferences": preferences or {},
            },
        )

    def add_tag(self, ticket_id: int, tag: str) -> Any:
        return self.request(
            "POST",
            "/api/v1/tags/add",
            expected=(200, 201),
            json_body={
                "object": "Ticket",
                "o_id": ticket_id,
                "item": tag,
            },
        )


class LiteLLMClient:
    def __init__(self, base_url: str, token: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model = model
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "prudai-zammad-autoreply/1.0",
            }
        )

    def generate_decision(self, ticket_title: str, customer_message: str, results: list[SearchResult]) -> dict[str, Any]:
        docs_blocks: list[str] = []
        for index, result in enumerate(results, start=1):
            body_text = clip(html_to_text(result.body_html or result.preview), MAX_BODY_CHARS)
            docs_blocks.append(
                "\n".join(
                    [
                        f"[{index}] Title: {result.title}",
                        f"[{index}] Section: {unescape(result.subtitle)}",
                        f"[{index}] URL: {result.public_url}",
                        f"[{index}] Content: {body_text}",
                    ]
                )
            )

        system_prompt = (
            "You are PrudAI Support's automatic first-response assistant and ticket triage worker.\n"
            "Use only the supplied PrudAI documentation passages for any customer-facing factual claim.\n"
            "Decide whether to answer immediately, hand the ticket to a human, or escalate it.\n"
            "Choose disposition=reply_with_docs only when the retrieved PrudAI docs clearly answer the customer's request.\n"
            "Choose disposition=handoff when the docs are not enough but the ticket is not urgent.\n"
            "Choose disposition=escalate when the request sounds urgent, risky, outage-related, security-related, billing-related, data-related, or access-related.\n"
            "Return strict JSON with keys: disposition, category, priority, customer_reply_html, internal_note_html, used_sources.\n"
            "disposition must be one of: reply_with_docs, handoff, escalate.\n"
            "category must be one of: how_to, bug, billing, security, outage, account_access, data_issue, general.\n"
            "priority must be one of: normal, high.\n"
            "customer_reply_html must be an HTML fragment only. Leave it empty unless disposition is reply_with_docs.\n"
            "internal_note_html must be an HTML fragment only and should briefly explain the reasoning for the support agent.\n"
            "used_sources must be an array of integer source indices from the provided docs.\n"
            "Reply in the same language as the customer."
        )

        user_prompt = "\n\n".join(
            [
                f"Ticket title: {ticket_title}",
                f"Customer message: {customer_message}",
                "Retrieved PrudAI docs:",
                "\n\n".join(docs_blocks) if docs_blocks else "[none]",
            ]
        )

        response = self.session.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(f"LiteLLM chat completion failed with status {response.status_code}: {response.text[:800]}")
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return parse_json_response(content)


class AutoreplyService:
    def __init__(self) -> None:
        self.public_base_url = getenv("ZAMMAD_PUBLIC_BASE_URL").rstrip("/")
        self.kb_nl_id = getenv_int("ZAMMAD_DOCS_KB_NL_ID", 1)
        self.kb_en_id = getenv_int("ZAMMAD_DOCS_KB_EN_ID", 2)
        self.zammad = ZammadClient(
            base_url=getenv("ZAMMAD_INTERNAL_BASE_URL"),
            token=read_secret("ZAMMAD_AUTOREPLY_TOKEN_FILE"),
        )
        self.litellm = LiteLLMClient(
            base_url=getenv("LITELLM_BASE_URL"),
            token=getenv("LITELLM_MASTER_KEY"),
            model=getenv("LITELLM_MODEL", "gemini-support"),
        )
        self.webhook_token = read_secret("ZAMMAD_AUTOREPLY_WEBHOOK_TOKEN_FILE")
        self.sendgrid_api_key = (
            read_secret_if_exists(os.getenv("SENDGRID_API_KEY_FILE", "/run/prudai-secrets/sendgrid-api.key"))
            or str(os.getenv("SENDGRID_API_KEY") or "").strip()
        )
        self.support_escalation_recipients = [
            item.strip()
            for item in str(os.getenv("SUPPORT_ESCALATION_EMAIL_TO", "support@prudai.com")).split(",")
            if item.strip()
        ]
        self.support_escalation_from = str(os.getenv("SUPPORT_ESCALATION_EMAIL_FROM") or "support@prudai.com").strip()
        self.support_escalation_from_name = (
            str(os.getenv("SUPPORT_ESCALATION_EMAIL_FROM_NAME") or "PrudAI Support").strip()
        )

    def is_authorized(self, header_value: str | None) -> bool:
        expected = f"Bearer {self.webhook_token}"
        return (header_value or "").strip() == expected

    def process_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        ticket = payload.get("ticket") or {}
        article = payload.get("article") or {}
        ticket_id = int(ticket["id"])
        article_id = int(article["id"])
        article_marker = self._article_marker(ticket_id=ticket_id, article_id=article_id)
        escalation_email_tag = self._escalation_email_tag(article_id)

        articles = self.zammad.get_ticket_articles(ticket_id)
        if self._already_processed(articles, article_marker):
            escalation_email_sent = self._ensure_escalation_email_for_existing_marker(
                ticket=ticket,
                ticket_id=ticket_id,
                article_id=article_id,
                marker=article_marker,
                articles=articles,
                escalation_email_tag=escalation_email_tag,
            )
            return {
                "status": "ok" if escalation_email_sent else "skipped",
                "reason": "already_processed",
                "ticket_id": ticket_id,
                "article_id": article_id,
                "escalation_email_sent": escalation_email_sent,
            }

        source_article = next((item for item in articles if int(item.get("id", 0)) == article_id), None)
        if source_article is None:
            raise RuntimeError(f"Unable to find source article {article_id} for ticket {ticket_id}")

        sender_value = source_article.get("sender")
        if isinstance(sender_value, dict):
            sender_name = str(sender_value.get("name") or "")
        else:
            sender_name = str(sender_value or "")
        if sender_name.lower() != "customer":
            return {"status": "skipped", "reason": "non_customer_article", "ticket_id": ticket_id}

        ticket_title = str(ticket.get("title") or source_article.get("subject") or "").strip()
        customer_message = html_to_text(str(source_article.get("body") or ""))
        language = detect_language(f"{ticket_title}\n{customer_message}")
        results = self._retrieve(ticket_title=ticket_title, customer_message=customer_message)

        decision = self._decide(ticket_title=ticket_title, customer_message=customer_message, language=language, results=results)

        if decision["priority"] == "high":
            self.zammad.update_ticket(ticket_id, priority_id=PRIORITY_IDS["high"])

        applied_tags = self._apply_tags(
            ticket_id=ticket_id,
            tags=self._build_tags(
                language=language,
                disposition=decision["disposition"],
                category=decision["category"],
                policy_signals=decision["policy_signals"],
                used_sources=decision["used_sources"],
            ),
        )

        public_article_id = None
        if decision["customer_reply_html"]:
            public_reply = self.zammad.create_public_reply(
                ticket_id,
                self._build_customer_reply_html(decision, results),
                marker=article_marker,
            )
            public_article_id = public_reply.get("id")

        internal_note = self.zammad.create_internal_note(
            ticket_id,
            self._build_internal_note_html(decision, results),
            marker=article_marker,
        )

        escalation_email_sent = False
        if decision["disposition"] == DISPOSITION_ESCALATE:
            escalation_email_sent = self._notify_support_of_escalation(
                ticket=ticket,
                ticket_id=ticket_id,
                article_id=article_id,
                source_article=source_article,
            )
            if escalation_email_sent:
                self.zammad.add_tag(ticket_id, escalation_email_tag)

        return {
            "status": "ok",
            "ticket_id": ticket_id,
            "public_article_id": public_article_id,
            "internal_article_id": internal_note.get("id"),
            "disposition": decision["disposition"],
            "category": decision["category"],
            "priority": decision["priority"],
            "used_sources": decision["used_sources"],
            "applied_tags": applied_tags,
            "escalation_email_sent": escalation_email_sent,
        }

    def _article_marker(self, *, ticket_id: int, article_id: int) -> str:
        return f"{AUTOREPLY_MARKER_PREFIX}:ticket:{ticket_id}:article:{article_id}"

    def _escalation_email_tag(self, article_id: int) -> str:
        return f"ai-escalation-email-{article_id}"

    def _already_processed(self, articles: list[dict[str, Any]], marker: str) -> bool:
        for article in articles:
            preferences = article.get("preferences") or {}
            if isinstance(preferences, dict) and str(preferences.get("prudai_marker") or "").strip() == marker:
                return True
            body = str(article.get("body") or "")
            if marker in body:
                return True
        return False

    def _ensure_escalation_email_for_existing_marker(
        self,
        *,
        ticket: dict[str, Any],
        ticket_id: int,
        article_id: int,
        marker: str,
        articles: list[dict[str, Any]],
        escalation_email_tag: str,
    ) -> bool:
        tags = set(self.zammad.get_ticket_tags(ticket_id))
        if escalation_email_tag in tags:
            return False

        source_article = next((item for item in articles if int(item.get("id") or 0) == article_id), None)
        if source_article is None:
            return False

        marker_articles = []
        for item in articles:
            preferences = item.get("preferences") or {}
            if isinstance(preferences, dict) and str(preferences.get("prudai_marker") or "").strip() == marker:
                marker_articles.append(item)

        if not any("Disposition: escalate" in str(item.get("body") or "") for item in marker_articles):
            return False

        sent = self._notify_support_of_escalation(
            ticket=ticket,
            ticket_id=ticket_id,
            article_id=article_id,
            source_article=source_article,
        )
        if sent:
            self.zammad.add_tag(ticket_id, escalation_email_tag)
        return sent

    def _decide(
        self,
        *,
        ticket_title: str,
        customer_message: str,
        language: str,
        results: list[SearchResult],
    ) -> dict[str, Any]:
        try:
            raw_decision = self.litellm.generate_decision(
                ticket_title=ticket_title,
                customer_message=customer_message,
                results=results,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("LiteLLM decision generation failed: %s", exc)
            raw_decision = {
                "disposition": DISPOSITION_HANDOFF,
                "category": CATEGORY_GENERAL,
                "priority": "normal",
                "customer_reply_html": "",
                "internal_note_html": (
                    "<p>Gemini did not return a usable decision, so the ticket was handed off to a human agent.</p>"
                ),
                "used_sources": [],
            }

        return self._normalize_decision(
            decision=raw_decision,
            ticket_title=ticket_title,
            customer_message=customer_message,
            language=language,
            results=results,
        )

    def _normalize_decision(
        self,
        *,
        decision: dict[str, Any],
        ticket_title: str,
        customer_message: str,
        language: str,
        results: list[SearchResult],
    ) -> dict[str, Any]:
        combined_text = f"{ticket_title}\n{customer_message}"
        policy_signals = self._detect_policy_signals(combined_text)

        disposition = str(decision.get("disposition") or DISPOSITION_HANDOFF).strip().lower()
        if disposition not in ALLOWED_DISPOSITIONS:
            disposition = DISPOSITION_HANDOFF

        category = str(decision.get("category") or CATEGORY_GENERAL).strip().lower()
        if category not in ALLOWED_CATEGORIES:
            category = self._fallback_category(combined_text, results)

        priority = str(decision.get("priority") or "normal").strip().lower()
        if priority not in ALLOWED_PRIORITIES:
            priority = "high" if disposition == DISPOSITION_ESCALATE else "normal"

        customer_reply_html = sanitize_html_fragment(str(decision.get("customer_reply_html") or ""))
        internal_note_html = sanitize_html_fragment(str(decision.get("internal_note_html") or ""))
        used_sources = self._normalize_used_sources(decision.get("used_sources"), results)

        if policy_signals:
            disposition = DISPOSITION_ESCALATE
            priority = "high"
            if category in {CATEGORY_GENERAL, CATEGORY_HOW_TO}:
                category = policy_signals[0]

        if disposition == DISPOSITION_REPLY and (not results or not used_sources or not customer_reply_html):
            disposition = DISPOSITION_HANDOFF if not policy_signals else DISPOSITION_ESCALATE
            customer_reply_html = ""

        if disposition == DISPOSITION_ESCALATE and not customer_reply_html:
            customer_reply_html = self._default_escalation_reply_html(language=language)

        if disposition not in {DISPOSITION_REPLY, DISPOSITION_ESCALATE}:
            customer_reply_html = ""

        if disposition == DISPOSITION_ESCALATE:
            priority = "high"

        if not internal_note_html:
            internal_note_html = self._default_internal_note_html(
                disposition=disposition,
                category=category,
                language=language,
            )

        return {
            "disposition": disposition,
            "category": category,
            "priority": priority,
            "customer_reply_html": customer_reply_html,
            "internal_note_html": internal_note_html,
            "used_sources": used_sources,
            "policy_signals": policy_signals,
        }

    def _fallback_category(self, text: str, results: list[SearchResult]) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in BUG_HINTS):
            return CATEGORY_BUG
        if results:
            return CATEGORY_HOW_TO
        return CATEGORY_GENERAL

    def _default_internal_note_html(self, *, disposition: str, category: str, language: str) -> str:
        if language == "nl":
            if disposition == DISPOSITION_REPLY:
                return "<p>Er is automatisch een eerste antwoord opgesteld op basis van PrudAI-documentatie.</p>"
            if disposition == DISPOSITION_ESCALATE:
                return "<p>Het ticket is gemarkeerd voor snelle menselijke opvolging vanwege het onderwerp of risico.</p>"
            return "<p>Er is geen veilig documentatieantwoord gevonden; een menselijk supportantwoord is nodig.</p>"

        if disposition == DISPOSITION_REPLY:
            return "<p>An automatic first response was generated from PrudAI documentation.</p>"
        if disposition == DISPOSITION_ESCALATE:
            return "<p>The ticket was marked for fast human follow-up because of its risk or urgency.</p>"
        return "<p>No safe documentation-based answer was found, so the ticket was handed to a human agent.</p>"

    def _default_escalation_reply_html(self, *, language: str) -> str:
        if language == "nl":
            return (
                "<p>Dank voor uw bericht. Ik zet dit direct door naar een medewerker van PrudAI Support, "
                "zodat u hier persoonlijke hulp bij krijgt.</p>"
            )

        return (
            "<p>Thanks for your message. I am escalating this ticket to a PrudAI Support employee now so "
            "you can get personal follow-up.</p>"
        )

    def _notify_support_of_escalation(
        self,
        *,
        ticket: dict[str, Any],
        ticket_id: int,
        article_id: int,
        source_article: dict[str, Any],
    ) -> bool:
        if not self.support_escalation_recipients:
            LOGGER.warning("Skipping escalation email for ticket %s because no recipients are configured.", ticket_id)
            return False
        if not self.sendgrid_api_key:
            raise RuntimeError("Escalation email requested but SENDGRID_API_KEY is not configured.")

        ticket_number = str(ticket.get("number") or ticket_id)
        ticket_title = str(ticket.get("title") or source_article.get("subject") or f"Ticket {ticket_number}").strip()
        customer_label = normalize_whitespace(str(source_article.get("from") or "")) or "Customer"
        article_excerpt = clip(html_to_text(str(source_article.get("body") or "")), 600)
        ticket_link = f"{self.public_base_url}/#ticket/zoom/{ticket_id}"

        subject = f"PrudAI AI escalation ({ticket_title})"
        text_body = "\n".join(
            [
                "PrudAI AI escalated a support ticket for human follow-up.",
                "",
                f"Ticket: #{ticket_number}",
                f"Title: {ticket_title}",
                f"Customer: {customer_label}",
                f"Source article: {article_id}",
                f"Link: {ticket_link}",
                "",
                "Latest customer message:",
                article_excerpt or "[empty]",
            ]
        )
        html_body = "\n".join(
            [
                "<div>PrudAI AI escalated a support ticket for human follow-up.</div>",
                f"<div><strong>Ticket:</strong> #{escape(ticket_number)}</div>",
                f"<div><strong>Title:</strong> {escape(ticket_title)}</div>",
                f"<div><strong>Customer:</strong> {escape(customer_label)}</div>",
                f"<div><strong>Source article:</strong> {article_id}</div>",
                f'<div><strong>Link:</strong> <a href="{escape(ticket_link, quote=True)}">{escape(ticket_link)}</a></div>',
                "<br>",
                "<div><strong>Latest customer message:</strong></div>",
                f"<div>{escape(article_excerpt or '[empty]')}</div>",
            ]
        )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.support_escalation_from_name} <{self.support_escalation_from}>"
        message["To"] = ", ".join(self.support_escalation_recipients)
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.sendgrid.net", 587, timeout=REQUEST_TIMEOUT) as smtp:
            smtp.starttls(context=context)
            smtp.login("apikey", self.sendgrid_api_key)
            smtp.send_message(message)

        LOGGER.info(
            "Sent escalation email for ticket %s article %s to %s.",
            ticket_id,
            article_id,
            ", ".join(self.support_escalation_recipients),
        )
        return True

    def _normalize_used_sources(self, raw_sources: Any, results: list[SearchResult]) -> list[int]:
        normalized: list[int] = []
        for item in raw_sources or []:
            try:
                index = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(results) and index not in normalized:
                normalized.append(index)
        return normalized

    def _detect_policy_signals(self, text: str) -> list[str]:
        lowered = text.lower()
        signals: list[str] = []
        for category, markers in POLICY_RULES:
            if any(marker in lowered for marker in markers):
                signals.append(category)
        return signals

    def _apply_tags(self, *, ticket_id: int, tags: list[str]) -> list[str]:
        applied: list[str] = []
        for tag in tags:
            try:
                self.zammad.add_tag(ticket_id, tag)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to add tag %s to ticket %s: %s", tag, ticket_id, exc)
                continue
            applied.append(tag)
        return applied

    def _build_tags(
        self,
        *,
        language: str,
        disposition: str,
        category: str,
        policy_signals: list[str],
        used_sources: list[int],
    ) -> list[str]:
        tag_values = {
            "ai-first-response",
            f"ai-{disposition.replace('_', '-')}",
            f"topic-{category.replace('_', '-')}",
            f"lang-{language}",
        }
        if disposition != DISPOSITION_REPLY:
            tag_values.add("needs-human")
        if used_sources:
            tag_values.add("docs-grounded")
        for signal in policy_signals:
            tag_values.add(f"escalation-{signal.replace('_', '-')}")
        normalized = [sanitize_tag(value) for value in sorted(tag_values)]
        return [value for value in normalized if value]

    def _retrieve(self, *, ticket_title: str, customer_message: str) -> list[SearchResult]:
        query_text = clip(f"{ticket_title}\n{customer_message}", 1200)
        preferred_language = detect_language(query_text)
        search_queries = build_search_queries(ticket_title, customer_message)
        kb_order = [("nl", self.kb_nl_id, "nl-nl"), ("en", self.kb_en_id, "en-us")]
        if preferred_language == "en":
            kb_order.reverse()

        combined: list[SearchResult] = []
        seen_keys: set[tuple[int, int]] = set()

        for _, kb_id, locale in kb_order:
            for candidate_query in search_queries:
                public_results = {
                    result.translation_id: result
                    for result in self.zammad.search_kb(kb_id, locale, candidate_query, flavor="public")
                }
                agent_results = {
                    result.translation_id: result
                    for result in self.zammad.search_kb(kb_id, locale, candidate_query, flavor="agent")
                }

                ordered_translation_ids: list[int] = []
                for translation_id in public_results:
                    ordered_translation_ids.append(translation_id)
                for translation_id in agent_results:
                    if translation_id not in ordered_translation_ids:
                        ordered_translation_ids.append(translation_id)

                for translation_id in ordered_translation_ids:
                    public_result = public_results.get(translation_id)
                    agent_result = agent_results.get(translation_id)
                    seed = public_result or agent_result
                    if seed is None:
                        continue
                    key = (kb_id, translation_id)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    combined.append(
                        SearchResult(
                            translation_id=translation_id,
                            kb_id=kb_id,
                            locale=locale,
                            title=(public_result or agent_result).title,
                            subtitle=(public_result or agent_result).subtitle,
                            public_url=self._absolute_public_url(public_result.public_url if public_result else ""),
                            api_url=agent_result.api_url if agent_result else "",
                            preview=public_result.preview if public_result and public_result.preview else (agent_result.preview if agent_result else ""),
                        )
                    )

                if len(combined) >= MAX_CONTEXT_RESULTS:
                    break

            if len(combined) >= MAX_CONTEXT_RESULTS:
                break

        selected = combined[:MAX_CONTEXT_RESULTS]
        for result in selected:
            try:
                self.zammad.fetch_answer_body(result)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to fetch full KB answer for %s: %s", result.title, exc)
            if not result.public_url and result.api_url:
                result.public_url = self._fallback_public_url()
        return selected

    def _absolute_public_url(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("/"):
            return f"{self.public_base_url}{value}"
        return f"{self.public_base_url}/{value.lstrip('/')}"

    def _fallback_public_url(self) -> str:
        return f"{self.public_base_url}/help"

    def _build_customer_reply_html(
        self,
        decision: dict[str, Any],
        results: list[SearchResult],
    ) -> str:
        parts = [decision["customer_reply_html"]]
        if decision["disposition"] == DISPOSITION_REPLY and decision["used_sources"]:
            parts.append("<hr><p><strong>Relevant PrudAI docs:</strong></p><ul>")
            for index in decision["used_sources"]:
                result = results[index - 1]
                url = escape(result.public_url or self._fallback_public_url(), quote=True)
                title = escape(result.title)
                parts.append(f'<li><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></li>')
            parts.append("</ul>")
        return "\n".join(parts)

    def _build_internal_note_html(
        self,
        decision: dict[str, Any],
        results: list[SearchResult],
    ) -> str:
        disposition_label = escape(decision["disposition"].replace("_", " "))
        category_label = escape(decision["category"].replace("_", " "))
        priority_label = escape(decision["priority"])

        parts = [
            "<p><strong>PrudAI AI support agent</strong></p>",
            "<ul>",
            f"<li>Disposition: {disposition_label}</li>",
            f"<li>Category: {category_label}</li>",
            f"<li>Priority: {priority_label}</li>",
            "</ul>",
            decision["internal_note_html"],
        ]

        if decision["policy_signals"]:
            parts.append("<p><strong>Policy escalation signals:</strong></p><ul>")
            for signal in decision["policy_signals"]:
                parts.append(f"<li>{escape(signal.replace('_', ' '))}</li>")
            parts.append("</ul>")

        if decision["used_sources"]:
            parts.append("<p><strong>Retrieved PrudAI docs used:</strong></p><ul>")
            for index in decision["used_sources"]:
                result = results[index - 1]
                url = escape(result.public_url or self._fallback_public_url(), quote=True)
                title = escape(result.title)
                parts.append(f'<li><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></li>')
            parts.append("</ul>")

        return "\n".join(parts)


SERVICE = AutoreplyService()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "PrudAIAutoreply/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.warning("Webhook client disconnected before response body could be written.")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/webhooks/zammad/new-ticket":
            self._send_json(404, {"error": "not_found"})
            return

        if not SERVICE.is_authorized(self.headers.get("Authorization")):
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length) or b"{}")
            result = SERVICE.process_ticket(payload)
            self._send_json(200, result)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Autoreply processing failed: %s", exc)
            self._send_json(500, {"status": "error", "error": str(exc)})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", DEFAULT_PORT), RequestHandler)
    LOGGER.info("Starting PrudAI Zammad autoreply service on port %s", DEFAULT_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
