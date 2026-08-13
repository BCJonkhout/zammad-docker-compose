#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


DOCS_USER_AGENT = "prudai-zammad-docs-sync/1.0"
DOCS_MARKER_PREFIX = "docs-slug-"
DOCS_MANAGED_TAG = "managed-by-docs-sync"
DOCS_LANG_TAG_PREFIX = "docs-lang-"
DOCS_METADATA_RE = re.compile(
    r"<!--\s*managed-by-docs-sync\s+lang:(?P<lang>[a-z]{2})\s+slug:(?P<slug>[^ ]+)\s+source:(?P<source>[^ ]+)\s*-->",
    re.IGNORECASE,
)
SOURCE_URL_RE = re.compile(
    r"<strong>\s*Source:\s*</strong>\s*<a [^>]*href=\"(?P<url>https?://[^\"]+)\"",
    re.IGNORECASE,
)
ANCHOR_OPEN_RE = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>", re.IGNORECASE)
SELF_LINK_RE = re.compile(r"<a href=\"([^\"]+)\">([^<]*)</a>", re.IGNORECASE)
CODE_LINK_RE = re.compile(r"<code>\s*<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>\s*</code>", re.IGNORECASE | re.DOTALL)
INLINE_RE = re.compile(
    r"(`[^`]+`)|(\[([^\]]+)\]\(([^)]+)\))|(\*\*([^*]+)\*\*)|(__(.+?)__)|(\*([^*]+)\*)|(_([^_]+)_)"
)


@dataclass(frozen=True)
class CategoryDef:
    path: tuple[str, ...]
    title: str
    parent_path: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class PageDef:
    language: str
    title: str
    slug: str
    markdown_path: str
    page_url: str
    category_path: tuple[str, ...]
    order: int


@dataclass
class CategoryState:
    id: int
    title: str
    parent_id: int | None
    translation_id: int | None


@dataclass
class AnswerState:
    id: int
    title: str
    category_id: int
    translation_id: int | None
    content_id: int | None
    body: str
    tags: list[str]
    published: bool
    slug: str | None
    language: str | None
    source_url: str | None
    managed: bool


def getenv(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def maybe_docs_bearer() -> str | None:
    """Optional Keycloak service-account token so the crawl can read docs pages
    that sit behind Keycloak SSO. Returns None (unchanged behaviour) unless both
    DOCS_KC_BOT_CLIENT_ID and DOCS_KC_BOT_CLIENT_SECRET are configured."""
    client_id = os.getenv("DOCS_KC_BOT_CLIENT_ID", "").strip()
    client_secret = os.getenv("DOCS_KC_BOT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    issuer = os.getenv("DOCS_KC_ISSUER", "https://login.prudai.com/realms/prudai").strip().rstrip("/")
    response = requests.post(
        f"{issuer}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    token = str(response.json().get("access_token") or "")
    if not token:
        raise RuntimeError("Keycloak returned no access_token for the docs service account.")
    return token


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_body_for_compare(value: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", "", str(value or ""), flags=re.DOTALL)
    normalized_code_links = CODE_LINK_RE.sub(
        lambda match: f"<code>{match.group(2).strip()}</code>",
        without_comments,
    )
    normalized_anchors = ANCHOR_OPEN_RE.sub(
        lambda match: f'<a href="{html.escape(html.unescape(match.group(1)), quote=True)}">',
        normalized_code_links,
    )
    # Zammad auto-links bare URLs in the stored body, so text that we send as
    # plain "https://..." comes back as <a href="https://...">https://...</a>.
    # Collapse such self-labelled anchors on both sides, otherwise every run
    # sees a difference and re-PATCHes an unchanged article forever.
    normalized_self_links = SELF_LINK_RE.sub(
        lambda match: match.group(2) if html.unescape(match.group(1)) == html.unescape(match.group(2).strip()) else match.group(0),
        normalized_anchors,
    )
    normalized_tag_spacing = re.sub(r">\s+<", "><", normalized_self_links)
    # Zammad stores the body with HTML entities resolved (&quot; -> "), so
    # compare on unescaped text.  Comparison-only: the body that is written is
    # always the freshly rendered one.
    return normalize_whitespace(html.unescape(normalized_tag_spacing))


def to_markdown_path(language: str, route_path: str) -> str:
    """Map a docs route onto its raw-markdown endpoint.

    Since the Docsify -> Astro Starlight migration (2026-05-24) the docs site
    serves raw markdown from ``src/pages/[...slug].md.ts``: every page is
    ``/<slug>.md``, the Dutch home page is ``/index.md`` and the English home
    page is ``/en.md``.  Starlight routes carry a trailing slash
    (``/getting-started/``), which has to be dropped first.
    """
    clean_route = str(route_path or "").strip().split("#", 1)[0].split("?", 1)[0]
    if clean_route != "/":
        clean_route = clean_route.rstrip("/")
    if language == "en":
        if clean_route in {"", "/", "/en"}:
            return "/en.md"
        normalized = clean_route if clean_route.startswith("/en/") else f"/en/{clean_route.lstrip('/')}"
        return normalized if normalized.endswith(".md") else f"{normalized}.md"
    if clean_route in {"", "/"}:
        return "/index.md"
    if clean_route.startswith("/en/"):
        raise RuntimeError(f"Dutch route must not include /en/: {clean_route}")
    return clean_route if clean_route.endswith(".md") else f"{clean_route}.md"


def to_slug(language: str, route_path: str) -> str:
    clean_route = str(route_path or "").strip().split("#", 1)[0].split("?", 1)[0]
    if language == "en":
        if clean_route in {"/en", "/en/"}:
            return "README"
        if not clean_route.startswith("/en/"):
            raise RuntimeError(f"English route must start with /en/: {clean_route}")
        tail = clean_route[len("/en/") :].rstrip("/")
        return (tail or "README").removesuffix(".md")
    if not clean_route.startswith("/"):
        raise RuntimeError(f"Invalid docs route: {clean_route}")
    if clean_route.startswith("/en/"):
        raise RuntimeError(f"Dutch route must not include /en/: {clean_route}")
    tail = clean_route[1:].rstrip("/")
    return (tail or "README").removesuffix(".md")


def to_page_url(base_url: str, language: str, slug: str) -> str:
    clean_base = base_url.rstrip("/")
    if language == "en":
        return f"{clean_base}/en/" if slug == "README" else f"{clean_base}/en/{slug}"
    return f"{clean_base}/" if slug == "README" else f"{clean_base}/{slug}"


def resolve_docs_link(base_url: str, href: str) -> str:
    if href.startswith("/"):
        return f"{base_url.rstrip('/')}{href}"
    return urljoin(f"{base_url.rstrip('/')}/", href)


def render_inline(base_url: str, text: str) -> str:
    output: list[str] = []
    last = 0
    for match in INLINE_RE.finditer(text):
        output.append(html.escape(text[last : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            output.append(f"<code>{html.escape(token[1:-1])}</code>")
        elif match.group(2):
            label = match.group(3) or ""
            href = resolve_docs_link(base_url, match.group(4) or "")
            output.append(
                f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a>'
            )
        elif match.group(5) or match.group(7):
            strong_text = match.group(6) or match.group(8) or ""
            output.append(f"<strong>{html.escape(strong_text)}</strong>")
        else:
            em_text = match.group(10) or match.group(12) or ""
            output.append(f"<em>{html.escape(em_text)}</em>")
        last = match.end()
    output.append(html.escape(text[last:]))
    return "".join(output)


def markdown_to_html(base_url: str, markdown_text: str) -> str:
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    in_code_block = False
    current_list: str | None = None

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        text = " ".join(part.strip() for part in paragraph if part.strip())
        if text:
            output.append(f"<p>{render_inline(base_url, text)}</p>")
        paragraph = []

    def close_list() -> None:
        nonlocal current_list
        if current_list:
            output.append(f"</{current_list}>")
            current_list = None

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            close_list()
            if in_code_block:
                output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            close_list()
            continue

        heading_match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading_match:
            flush_paragraph()
            close_list()
            level = len(heading_match.group(1))
            output.append(f"<h{level}>{render_inline(base_url, heading_match.group(2))}</h{level}>")
            continue

        list_match = re.match(r"^\s*([-*]|\d+\.)\s+(.+?)\s*$", line)
        if list_match:
            flush_paragraph()
            target_list = "ol" if list_match.group(1).endswith(".") else "ul"
            if current_list != target_list:
                close_list()
                output.append(f"<{target_list}>")
                current_list = target_list
            output.append(f"<li>{render_inline(base_url, list_match.group(2))}</li>")
            continue

        if current_list:
            close_list()
        paragraph.append(line)

    flush_paragraph()
    close_list()
    if in_code_block:
        output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")

    return "\n".join(output)


def strip_frontmatter(markdown_text: str) -> str:
    """Drop the leading YAML frontmatter block emitted by the Starlight .md route.

    ``src/pages/[...slug].md.ts`` prefixes every page with its serialized
    frontmatter (title/description/template/...).  Without stripping it the
    block would end up as literal text at the top of every KB article.
    """
    text = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.lstrip("﻿").startswith("---"):
        return markdown_text
    stripped = text.lstrip("﻿")
    lines = stripped.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            remainder = lines[index + 1 :]
            while remainder and not remainder[0].strip():
                remainder.pop(0)
            return "\n".join(remainder)
    return markdown_text


def strip_duplicate_leading_heading(title: str, markdown_text: str) -> str:
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first_content_index: int | None = None

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        first_content_index = index
        break

    if first_content_index is None:
        return markdown_text

    heading_match = re.match(r"^\s*#\s+(.+?)\s*$", lines[first_content_index])
    if not heading_match:
        return markdown_text

    heading_title = normalize_whitespace(re.sub(r"[*_`]+", "", heading_match.group(1)))
    page_title = normalize_whitespace(title)
    if heading_title.casefold() != page_title.casefold():
        return markdown_text

    stripped_lines = lines[first_content_index + 1 :]
    while stripped_lines and not stripped_lines[0].strip():
        stripped_lines.pop(0)

    return "\n".join(stripped_lines)


class DocsIndexError(RuntimeError):
    """The docs site no longer exposes a readable page index at the expected URL."""


class StarlightSidebarParser(HTMLParser):
    """Extract the navigation tree from a rendered Astro Starlight page.

    Starlight renders the whole sidebar into every page as
    ``<ul class="top-level">`` containing one ``<li><details>`` per group; the
    group label sits in ``<span class="group-label">`` and each page is an
    ``<a href="/route/">``.  Groups may nest, so ``<details>`` drives a stack.
    Only structural hooks are used (``top-level``, ``group-label``,
    ``details``); the hashed ``astro-*`` classes change on every build and are
    deliberately ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, tuple[str, ...], str, str]] = []
        self._stack: list[str] = []
        self._active = False
        self._ul_depth = 0
        self._label_depth = 0
        self._label_buf: list[str] = []
        self._link_href: str | None = None
        self._link_buf: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for key, value in attrs:
            if key == "class" and value:
                return set(value.split())
        return set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "ul":
            if "top-level" in classes and not self._active:
                self._active = True
                self._ul_depth = 1
            elif self._active:
                self._ul_depth += 1
            return
        if not self._active:
            return
        if tag == "span" and "group-label" in classes and not self._label_depth:
            self._label_depth = 1
            self._label_buf = []
            return
        if self._label_depth:
            if tag == "span":
                self._label_depth += 1
            return
        if tag == "a" and self._link_href is None:
            self._link_href = dict(attrs).get("href")
            self._link_buf = []

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        if self._label_depth:
            if tag != "span":
                return
            self._label_depth -= 1
            if not self._label_depth:
                label = normalize_whitespace("".join(self._label_buf))
                if label:
                    self._stack.append(label)
                    self.entries.append(("group", tuple(self._stack), "", label))
            return
        if tag == "a" and self._link_href is not None:
            title = normalize_whitespace("".join(self._link_buf))
            self.entries.append(("link", tuple(self._stack), self._link_href, title))
            self._link_href = None
            return
        if tag == "details":
            if self._stack:
                self._stack.pop()
            return
        if tag == "ul":
            self._ul_depth -= 1
            if self._ul_depth <= 0:
                self._active = False

    def handle_data(self, data: str) -> None:
        if not self._active:
            return
        if self._label_depth:
            self._label_buf.append(data)
        elif self._link_href is not None:
            self._link_buf.append(data)


def parse_sidebar_nav(index_url: str, page_html: str) -> list[tuple[str, tuple[str, ...], str, str]]:
    parser = StarlightSidebarParser()
    parser.feed(page_html)
    entries = parser.entries
    if not any(kind == "link" for kind, *_ in entries):
        raise DocsIndexError(
            f"De documentatiesite heeft geen leesbare index meer op {index_url} — "
            "de pagina laadde wel (HTTP 200), maar bevat geen herkenbare navigatieboom "
            "(<ul class=\"top-level\"> met paginalinks). Structuur gewijzigd? "
            "Controleer de sidebar van de docs-generator (Astro Starlight) en werk "
            "parse_sidebar_nav() in docs-sync.py bij."
        )
    return entries


def build_sidebar(
    language: str,
    base_url: str,
    nav_entries: list[tuple[str, tuple[str, ...], str, str]],
) -> tuple[dict[tuple[str, ...], CategoryDef], list[PageDef]]:
    categories: dict[tuple[str, ...], CategoryDef] = {}
    pages: list[PageDef] = []
    category_order: dict[tuple[str, ...], int] = defaultdict(int)
    page_order: dict[tuple[str, ...], int] = defaultdict(int)
    seen_slugs: set[str] = set()

    for kind, path, href, title in nav_entries:
        if kind == "group":
            if path in categories:
                continue
            parent_path = path[:-1]
            categories[path] = CategoryDef(
                path=path,
                title=title,
                parent_path=parent_path,
                order=category_order[parent_path],
            )
            category_order[parent_path] += 1
            continue

        route_path = normalize_whitespace(href)
        if not route_path.startswith("/"):
            continue
        slug = to_slug(language, route_path)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        pages.append(
            PageDef(
                language=language,
                title=title,
                slug=slug,
                markdown_path=to_markdown_path(language, route_path),
                page_url=to_page_url(base_url, language, slug),
                category_path=path,
                order=page_order[path],
            )
        )
        page_order[path] += 1

    if not pages:
        raise DocsIndexError(f"No documentation pages discovered for language {language}.")

    return categories, pages


def build_answer_body(base_url: str, page: PageDef, markdown_text: str) -> str:
    html_body = markdown_to_html(base_url, strip_duplicate_leading_heading(page.title, markdown_text))
    source_line = (
        '<hr><p><strong>Source:</strong> '
        f'<a href="{html.escape(page.page_url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(page.page_url)}</a>'
        "</p>"
    )
    return "\n".join([html_body, source_line])


def infer_page_identity(source_url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(source_url)
    path = (parsed.path or "/").rstrip("/") or "/"
    if path in {"/en", "/en/"}:
        return "en", "README"
    if path.startswith("/en/"):
        return "en", (path[len("/en/") :].strip("/") or "README").removesuffix(".md")
    if path.startswith("/"):
        return "nl", (path.strip("/") or "README").removesuffix(".md")
    return None, None


def extract_managed_metadata(tags: list[str], body: str) -> tuple[bool, str | None, str | None, str | None]:
    managed = DOCS_MANAGED_TAG in tags
    slug = None
    language = None
    for tag in tags:
        if tag.startswith(DOCS_MARKER_PREFIX):
            managed = True
            slug = tag[len(DOCS_MARKER_PREFIX) :]
        elif tag.startswith(DOCS_LANG_TAG_PREFIX):
            managed = True
            language = tag[len(DOCS_LANG_TAG_PREFIX) :]
    match = DOCS_METADATA_RE.search(body or "")
    if match:
        return True, match.group("slug"), match.group("lang"), match.group("source")
    source_match = SOURCE_URL_RE.search(body or "")
    if source_match:
        source_url = html.unescape(source_match.group("url"))
        inferred_language, inferred_slug = infer_page_identity(source_url)
        return True, inferred_slug or slug, inferred_language or language, source_url
    return managed, slug, language, None


class ZammadClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token token={token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": DOCS_USER_AGENT,
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self.session.request(
            method=method,
            url=f"{self.base_url}{path}",
            json=json_body,
            params=params,
            timeout=60,
        )
        if response.status_code not in expected:
            raise RuntimeError(
                f"{method} {path} failed with status {response.status_code}: {response.text[:800]}"
            )
        if not response.text.strip():
            return None
        return response.json()


def asset_table(assets: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        value = assets.get(name)
        if isinstance(value, dict):
            return value
    return {}


def choose_translation(
    translations: dict[str, dict[str, Any]],
    foreign_key: str,
    object_id: int,
    kb_locale_id: int,
) -> dict[str, Any] | None:
    candidates = [value for value in translations.values() if value.get(foreign_key) == object_id]
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.get("kb_locale_id") == kb_locale_id:
            return candidate
    return candidates[0]


def get_kb_snapshot(client: ZammadClient, kb_id: int) -> dict[str, Any]:
    payload = client.request("POST", "/api/v1/knowledge_bases/init", expected=(200,), json_body={})
    assets = payload.get("assets", payload) if isinstance(payload, dict) else {}
    if not assets:
        raise RuntimeError(f"Knowledge base {kb_id} returned no assets.")

    answer_translations = asset_table(
        assets,
        "KnowledgeBaseAnswerTranslation",
        "KnowledgeBase::Answer::Translation",
    )
    content_assets = asset_table(
        assets,
        "KnowledgeBaseAnswerTranslationContent",
        "KnowledgeBase::Answer::Translation::Content",
    )
    content_ids = sorted(
        {
            int(translation["content_id"])
            for translation in answer_translations.values()
            if translation.get("content_id") is not None
        }
    )
    missing_content_ids = [content_id for content_id in content_ids if str(content_id) not in content_assets]
    if missing_content_ids:
        content_payload = client.request(
            "POST",
            "/api/v1/knowledge_bases/init",
            expected=(200,),
            json_body={"answer_translation_content_ids": missing_content_ids},
        )
        extra_assets = content_payload.get("assets", content_payload) if isinstance(content_payload, dict) else {}
        for key, value in extra_assets.items():
            if not isinstance(value, dict):
                continue
            assets.setdefault(key, {})
            assets[key].update(value)

    return assets


def get_kb_locale_id(assets: dict[str, Any], kb_id: int) -> int:
    locales = list(
        asset_table(
            assets,
            "KnowledgeBaseLocale",
            "KnowledgeBase::Locale",
        ).values()
    )
    matches = [locale for locale in locales if locale.get("knowledge_base_id") == kb_id]
    if not matches:
        raise RuntimeError(f"Unable to find locale for knowledge base {kb_id}.")
    primary = next((locale for locale in matches if locale.get("primary")), None)
    chosen = primary or matches[0]
    return int(chosen["id"])


def build_category_state(
    assets: dict[str, Any],
    kb_locale_id: int,
    kb_id: int | None = None,
) -> tuple[dict[int, CategoryState], dict[tuple[int | None, str], CategoryState]]:
    categories = asset_table(
        assets,
        "KnowledgeBaseCategory",
        "KnowledgeBase::Category",
    )
    translations = asset_table(
        assets,
        "KnowledgeBaseCategoryTranslation",
        "KnowledgeBase::Category::Translation",
    )
    by_id: dict[int, CategoryState] = {}
    by_key: dict[tuple[int | None, str], CategoryState] = {}

    for category in categories.values():
        if kb_id is not None and int(category.get("knowledge_base_id") or 0) != kb_id:
            continue
        category_id = int(category["id"])
        translation = choose_translation(translations, "category_id", category_id, kb_locale_id)
        if translation is None:
            continue
        title = normalize_whitespace((translation or {}).get("title", ""))
        if not title:
            continue
        state = CategoryState(
            id=category_id,
            title=title,
            parent_id=category.get("parent_id"),
            translation_id=(translation or {}).get("id"),
        )
        by_id[category_id] = state
        by_key[(state.parent_id, title)] = state

    return by_id, by_key


def build_answer_state(
    assets: dict[str, Any],
    kb_locale_id: int,
    allowed_category_ids: set[int] | None = None,
) -> tuple[dict[int, AnswerState], dict[str, AnswerState]]:
    answers = asset_table(
        assets,
        "KnowledgeBaseAnswer",
        "KnowledgeBase::Answer",
    )
    translations = asset_table(
        assets,
        "KnowledgeBaseAnswerTranslation",
        "KnowledgeBase::Answer::Translation",
    )
    contents = asset_table(
        assets,
        "KnowledgeBaseAnswerTranslationContent",
        "KnowledgeBase::Answer::Translation::Content",
    )
    by_id: dict[int, AnswerState] = {}
    by_slug: dict[str, AnswerState] = {}

    for answer in answers.values():
        category_id = int(answer["category_id"])
        if allowed_category_ids is not None and category_id not in allowed_category_ids:
            continue
        answer_id = int(answer["id"])
        translation = choose_translation(translations, "answer_id", answer_id, kb_locale_id)
        if translation is None:
            continue
        content_id = (translation or {}).get("content_id")
        content = {}
        if content_id is not None:
            content = contents.get(str(content_id)) or contents.get(content_id) or {}
        tags = list(answer.get("tags") or [])
        managed, slug, language, source_url = extract_managed_metadata(tags, str(content.get("body") or ""))
        state = AnswerState(
            id=answer_id,
            title=normalize_whitespace((translation or {}).get("title", "")),
            category_id=category_id,
            translation_id=(translation or {}).get("id"),
            content_id=(translation or {}).get("content_id"),
            body=str(content.get("body") or ""),
            tags=tags,
            published=bool(answer.get("published_at")),
            slug=slug,
            language=language,
            source_url=source_url,
            managed=managed,
        )
        by_id[answer_id] = state
        if slug:
            by_slug[slug] = state

    return by_id, by_slug


def api_payload_for_category(title: str, kb_locale_id: int, parent_id: int | None, translation_id: int | None) -> dict[str, Any]:
    translation: dict[str, Any] = {
        "title": title,
        "kb_locale_id": kb_locale_id,
        "content_attributes": {
            "body": "",
        },
    }
    if translation_id:
        translation["id"] = translation_id
    return {
        "category_icon": "f02d",
        "parent_id": "" if parent_id is None else str(parent_id),
        "translations_attributes": [translation],
    }


def api_payload_for_answer(
    *,
    title: str,
    body: str,
    category_id: int,
    kb_locale_id: int,
    tags: list[str],
    translation_id: int | None,
    content_id: int | None,
    include_tags: bool = True,
) -> dict[str, Any]:
    translation: dict[str, Any] = {
        "title": title,
        "kb_locale_id": kb_locale_id,
        "content_attributes": {
            "body": body,
        },
    }
    if translation_id:
        translation["id"] = translation_id
    if content_id:
        translation["content_attributes"]["id"] = content_id

    payload: dict[str, Any] = {
        "category_id": category_id,
        "translations_attributes": [translation],
    }
    if include_tags:
        payload["tags"] = tags
    return payload


def index_answers_for_language(
    answers_by_id: dict[int, AnswerState],
    language: str,
) -> tuple[dict[str, AnswerState], dict[tuple[str, int], AnswerState]]:
    by_slug: dict[str, AnswerState] = {}
    by_title_category: dict[tuple[str, int], AnswerState] = {}

    for answer in sorted(answers_by_id.values(), key=lambda item: item.id):
        if not answer.managed:
            continue
        if answer.language and answer.language != language:
            continue
        if answer.slug and answer.slug not in by_slug:
            by_slug[answer.slug] = answer
        if answer.title:
            key = (answer.title, answer.category_id)
            if key not in by_title_category:
                by_title_category[key] = answer

    return by_slug, by_title_category


def update_or_create_answer(
    client: ZammadClient,
    kb_id: int,
    kb_locale_id: int,
    page: PageDef,
    category_id: int,
    body: str,
    existing: AnswerState | None,
) -> None:
    tags = [DOCS_MANAGED_TAG, f"{DOCS_LANG_TAG_PREFIX}{page.language}", f"{DOCS_MARKER_PREFIX}{page.slug}"]
    payload = api_payload_for_answer(
        title=page.title,
        body=body,
        category_id=category_id,
        kb_locale_id=kb_locale_id,
        tags=tags,
        translation_id=existing.translation_id if existing else None,
        content_id=existing.content_id if existing else None,
    )

    if existing:
        needs_update = (
            existing.title != page.title
            or existing.category_id != category_id
            or normalize_body_for_compare(existing.body) != normalize_body_for_compare(body)
        )
        if not needs_update:
            return
        try:
            client.request(
                "PATCH",
                f"/api/v1/knowledge_bases/{kb_id}/answers/{existing.id}",
                expected=(200,),
                json_body=payload,
            )
        except RuntimeError as exc:
            if "tags" not in str(exc):
                raise
            payload = api_payload_for_answer(
                title=page.title,
                body=body,
                category_id=category_id,
                kb_locale_id=kb_locale_id,
                tags=tags,
                translation_id=existing.translation_id,
                content_id=existing.content_id,
                include_tags=False,
            )
            client.request(
                "PATCH",
                f"/api/v1/knowledge_bases/{kb_id}/answers/{existing.id}",
                expected=(200,),
                json_body=payload,
            )
        if not existing.published:
            client.request(
                "POST",
                f"/api/v1/knowledge_bases/{kb_id}/answers/{existing.id}/publish",
                expected=(200,),
            )
        return

    created = None
    try:
        created = client.request(
            "POST",
            f"/api/v1/knowledge_bases/{kb_id}/answers",
            expected=(201,),
            json_body=payload,
        )
    except RuntimeError as exc:
        if "tags" not in str(exc):
            raise
        payload = api_payload_for_answer(
            title=page.title,
            body=body,
            category_id=category_id,
            kb_locale_id=kb_locale_id,
            tags=tags,
            translation_id=None,
            content_id=None,
            include_tags=False,
        )
        created = client.request(
            "POST",
            f"/api/v1/knowledge_bases/{kb_id}/answers",
            expected=(201,),
            json_body=payload,
        )
    if created and created.get("id"):
        client.request(
            "POST",
            f"/api/v1/knowledge_bases/{kb_id}/answers/{created['id']}/publish",
            expected=(200,),
        )


def fetch_docs_tree(
    base_url: str, language: str
) -> tuple[dict[tuple[str, ...], CategoryDef], list[PageDef], dict[str, str], set[str]]:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "text/plain, text/markdown;q=0.9, */*;q=0.8",
            "User-Agent": DOCS_USER_AGENT,
        }
    )
    bearer = maybe_docs_bearer()
    if bearer:
        session.headers["Authorization"] = f"Bearer {bearer}"
    index_path = "/en/" if language == "en" else "/"
    index_url = f"{base_url.rstrip('/')}{index_path}"
    try:
        index_response = session.get(index_url, timeout=30)
    except requests.RequestException as exc:
        raise DocsIndexError(
            f"De documentatiesite is niet bereikbaar op {index_url} ({exc.__class__.__name__}: {exc}). "
            "Zonder index kan de paginaboom niet worden bepaald."
        ) from exc
    if index_response.status_code != 200:
        raise DocsIndexError(
            f"De documentatiesite heeft geen leesbare index meer op {index_url} "
            f"(HTTP {index_response.status_code}) — structuur gewijzigd? "
            "De paginaboom wordt gelezen uit de gerenderde Starlight-navigatie op deze pagina; "
            "controleer of de URL nog bestaat en werk fetch_docs_tree() in docs-sync.py bij."
        )

    nav_entries = parse_sidebar_nav(index_url, index_response.text)
    categories, pages = build_sidebar(language, base_url, nav_entries)

    markdown_by_slug: dict[str, str] = {}
    kept_pages: list[PageDef] = []
    skipped_slugs: set[str] = set()
    for page in pages:
        page_md_url = f"{base_url.rstrip('/')}{page.markdown_path}"
        response = session.get(page_md_url, timeout=30)
        if response.status_code in (401, 403) and not bearer:
            # SSO-gated "competitive edge" page (see gated-pages.json on the docs
            # site). Without DOCS_KC_* credentials it cannot be read; skip it
            # instead of failing the whole run, and keep any existing KB article.
            print(
                f"[docs-sync] {language}: sla SSO-afgeschermde pagina '{page.slug}' over "
                f"(HTTP {response.status_code} op {page_md_url}). "
                "Stel DOCS_KC_ISSUER/DOCS_KC_BOT_CLIENT_ID + ZAMMAD_DOCS_KC_SECRET_FILE in "
                "om deze pagina wel te synchroniseren.",
                file=sys.stderr,
            )
            skipped_slugs.add(page.slug)
            continue
        if response.status_code != 200:
            raise DocsIndexError(
                f"Documentatiepagina '{page.slug}' ({language}) is niet leesbaar op {page_md_url} "
                f"(HTTP {response.status_code}) — structuur gewijzigd? "
                "De ruwe markdown komt van de Starlight-route src/pages/[...slug].md.ts; "
                "controleer to_markdown_path() in docs-sync.py."
            )
        markdown_by_slug[page.slug] = strip_frontmatter(response.text)
        kept_pages.append(page)

    if not markdown_by_slug:
        raise DocsIndexError(
            f"Geen enkele documentatiepagina kon worden gelezen voor taal {language} via {index_url}."
        )

    return categories, kept_pages, markdown_by_slug, skipped_slugs


def ensure_categories(
    client: ZammadClient,
    kb_id: int,
    kb_locale_id: int,
    category_defs: dict[tuple[str, ...], CategoryDef],
) -> dict[tuple[str, ...], int]:
    path_to_id: dict[tuple[str, ...], int] = {}

    for path, category_def in sorted(category_defs.items(), key=lambda item: (len(item[0]), item[1].order, item[0])):
        assets = get_kb_snapshot(client, kb_id)
        _, categories_by_key = build_category_state(assets, kb_locale_id, kb_id)
        parent_id = path_to_id.get(category_def.parent_path)
        existing = categories_by_key.get((parent_id, category_def.title))

        if existing:
            needs_update = existing.parent_id != parent_id or existing.title != category_def.title
            if needs_update:
                client.request(
                    "PATCH",
                    f"/api/v1/knowledge_bases/{kb_id}/categories/{existing.id}",
                    expected=(200,),
                    json_body=api_payload_for_category(
                        category_def.title,
                        kb_locale_id,
                        parent_id,
                        existing.translation_id,
                    ),
                )
                assets = get_kb_snapshot(client, kb_id)
                _, categories_by_key = build_category_state(assets, kb_locale_id, kb_id)
                existing = categories_by_key[(parent_id, category_def.title)]
            path_to_id[path] = existing.id
            continue

        created = client.request(
            "POST",
            f"/api/v1/knowledge_bases/{kb_id}/categories",
            expected=(201,),
            json_body=api_payload_for_category(category_def.title, kb_locale_id, parent_id, None),
        )
        if not created or not created.get("id"):
            assets = get_kb_snapshot(client, kb_id)
            _, categories_by_key = build_category_state(assets, kb_locale_id, kb_id)
            existing = categories_by_key[(parent_id, category_def.title)]
            path_to_id[path] = existing.id
        else:
            path_to_id[path] = int(created["id"])

    return path_to_id


def delete_stale_answers(
    client: ZammadClient,
    kb_id: int,
    language: str,
    desired_slugs: set[str],
    answers_by_id: dict[int, AnswerState],
) -> None:
    for answer in sorted(answers_by_id.values(), key=lambda item: item.id):
        if not answer.managed:
            continue
        if answer.language and answer.language != language:
            client.request(
                "DELETE",
                f"/api/v1/knowledge_bases/{kb_id}/answers/{answer.id}",
                expected=(200,),
            )
            continue
        if answer.slug and answer.slug in desired_slugs:
            continue
        client.request(
            "DELETE",
            f"/api/v1/knowledge_bases/{kb_id}/answers/{answer.id}",
            expected=(200,),
        )


def delete_stale_categories(
    client: ZammadClient,
    kb_id: int,
    desired_category_paths: set[tuple[str, ...]],
    kb_locale_id: int,
) -> None:
    assets = get_kb_snapshot(client, kb_id)
    categories_by_id, _ = build_category_state(assets, kb_locale_id, kb_id)
    path_by_id: dict[int, tuple[str, ...]] = {}

    def build_path(category_id: int) -> tuple[str, ...]:
        if category_id in path_by_id:
            return path_by_id[category_id]
        category = categories_by_id[category_id]
        if category.parent_id is None:
            path = (category.title,)
        else:
            path = build_path(category.parent_id) + (category.title,)
        path_by_id[category_id] = path
        return path

    ordered = sorted(categories_by_id.values(), key=lambda item: (-len(build_path(item.id)), item.id))
    for category in ordered:
        if build_path(category.id) in desired_category_paths:
            continue
        client.request(
            "DELETE",
            f"/api/v1/knowledge_bases/{kb_id}/categories/{category.id}",
            expected=(200,),
        )


def reorder_categories(
    client: ZammadClient,
    kb_id: int,
    kb_locale_id: int,
    desired_categories: dict[tuple[str, ...], CategoryDef],
) -> None:
    assets = get_kb_snapshot(client, kb_id)
    categories_by_id, _ = build_category_state(assets, kb_locale_id, kb_id)
    title_path_to_id: dict[tuple[str, ...], int] = {}

    def build_path(category_id: int) -> tuple[str, ...]:
        category = categories_by_id[category_id]
        if category.parent_id is None:
            return (category.title,)
        return build_path(category.parent_id) + (category.title,)

    for category_id in categories_by_id:
        title_path_to_id[build_path(category_id)] = category_id

    children_by_parent: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for path, category_def in sorted(desired_categories.items(), key=lambda item: (item[1].parent_path, item[1].order)):
        category_id = title_path_to_id.get(path)
        if category_id:
            children_by_parent[category_def.parent_path].append(category_id)

    for parent_path, ordered_ids in children_by_parent.items():
        if not ordered_ids:
            continue
        if parent_path:
            parent_id = title_path_to_id.get(parent_path)
            if not parent_id:
                continue
            current_scope = [state.id for state in categories_by_id.values() if state.parent_id == parent_id]
            if sorted(current_scope) != sorted(ordered_ids):
                continue
            if current_scope == ordered_ids:
                continue
            client.request(
                "PATCH",
                f"/api/v1/knowledge_bases/{kb_id}/categories/{parent_id}/reorder_categories",
                expected=(200,),
                json_body={"ordered_ids": ordered_ids},
            )
        else:
            current_scope = [state.id for state in categories_by_id.values() if state.parent_id is None]
            if sorted(current_scope) != sorted(ordered_ids):
                continue
            if current_scope == ordered_ids:
                continue
            client.request(
                "PATCH",
                f"/api/v1/knowledge_bases/{kb_id}/categories/reorder_root_categories",
                expected=(200,),
                json_body={"ordered_ids": ordered_ids},
            )


def reorder_answers(
    client: ZammadClient,
    kb_id: int,
    pages: list[PageDef],
    path_to_category_id: dict[tuple[str, ...], int],
    answers_by_slug: dict[str, AnswerState],
) -> None:
    ordered_by_category: dict[int, list[int]] = defaultdict(list)
    for page in sorted(pages, key=lambda item: (item.category_path, item.order)):
        answer = answers_by_slug.get(page.slug)
        category_id = path_to_category_id.get(page.category_path)
        if answer and category_id:
            ordered_by_category[category_id].append(answer.id)

    for category_id, ordered_ids in ordered_by_category.items():
        if not ordered_ids:
            continue
        current_assets = get_kb_snapshot(client, kb_id)
        current_kb_locale_id = get_kb_locale_id(current_assets, kb_id)
        current_categories_by_id, _ = build_category_state(current_assets, current_kb_locale_id, kb_id)
        answers_by_id, _ = build_answer_state(
            current_assets,
            current_kb_locale_id,
            allowed_category_ids=set(current_categories_by_id),
        )
        current_scope = [answer.id for answer in answers_by_id.values() if answer.category_id == category_id]
        if sorted(current_scope) != sorted(ordered_ids):
            continue
        if current_scope == ordered_ids:
            continue
        client.request(
            "PATCH",
            f"/api/v1/knowledge_bases/{kb_id}/categories/{category_id}/reorder_answers",
            expected=(200,),
            json_body={"ordered_ids": ordered_ids},
        )


def sync_language(client: ZammadClient, kb_id: int, language: str, docs_base_url: str) -> None:
    category_defs, pages, markdown_by_slug, skipped_slugs = fetch_docs_tree(docs_base_url, language)
    assets = get_kb_snapshot(client, kb_id)
    kb_locale_id = get_kb_locale_id(assets, kb_id)

    path_to_category_id = ensure_categories(client, kb_id, kb_locale_id, category_defs)

    assets = get_kb_snapshot(client, kb_id)
    categories_by_id, _ = build_category_state(assets, kb_locale_id, kb_id)
    answers_by_id, _ = build_answer_state(assets, kb_locale_id, allowed_category_ids=set(categories_by_id))
    answers_by_slug, answers_by_title_category = index_answers_for_language(answers_by_id, language)
    # Pages that could not be read (SSO-gated) stay in the "keep" set so an
    # already-published article is never deleted just because it is unreadable.
    desired_slugs = {page.slug for page in pages} | skipped_slugs

    for page in sorted(pages, key=lambda item: (item.category_path, item.order)):
        category_id = path_to_category_id.get(page.category_path)
        if category_id is None:
            raise RuntimeError(f"No category id resolved for page {page.slug}")
        body = build_answer_body(docs_base_url, page, markdown_by_slug[page.slug])
        existing = answers_by_slug.get(page.slug) or answers_by_title_category.get((page.title, category_id))
        update_or_create_answer(client, kb_id, kb_locale_id, page, category_id, body, existing)
        assets = get_kb_snapshot(client, kb_id)
        categories_by_id, _ = build_category_state(assets, kb_locale_id, kb_id)
        answers_by_id, _ = build_answer_state(assets, kb_locale_id, allowed_category_ids=set(categories_by_id))
        answers_by_slug, answers_by_title_category = index_answers_for_language(answers_by_id, language)

    assets = get_kb_snapshot(client, kb_id)
    categories_by_id, _ = build_category_state(assets, kb_locale_id, kb_id)
    allowed_category_ids = set(categories_by_id)
    answers_by_id, answers_by_slug = build_answer_state(assets, kb_locale_id, allowed_category_ids=allowed_category_ids)
    delete_stale_answers(client, kb_id, language, desired_slugs, answers_by_id)
    delete_stale_categories(client, kb_id, set(category_defs.keys()), kb_locale_id)
    reorder_categories(client, kb_id, kb_locale_id, category_defs)

    assets = get_kb_snapshot(client, kb_id)
    categories_by_id, _ = build_category_state(assets, kb_locale_id, kb_id)
    answers_by_id, _ = build_answer_state(assets, kb_locale_id, allowed_category_ids=set(categories_by_id))
    answers_by_slug, _ = index_answers_for_language(answers_by_id, language)
    reorder_answers(client, kb_id, pages, path_to_category_id, answers_by_slug)


def main() -> int:
    base_url = getenv("ZAMMAD_BASE_URL")
    docs_base_url = getenv("ZAMMAD_DOCS_BASE_URL")
    token = getenv("ZAMMAD_DOCS_SYNC_TOKEN")
    kb_nl_id = int(getenv("ZAMMAD_DOCS_KB_NL_ID"))
    kb_en_id = int(getenv("ZAMMAD_DOCS_KB_EN_ID"))

    client = ZammadClient(base_url, token)

    sync_language(client, kb_nl_id, "nl", docs_base_url)
    sync_language(client, kb_en_id, "en", docs_base_url)

    print(
        json.dumps(
            {
                "status": "ok",
                "kb_nl_id": kb_nl_id,
                "kb_en_id": kb_en_id,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DocsIndexError as exc:
        # Structural change on the docs site: report it in plain language and
        # fail (exit 1) so the systemd unit goes red -- but without the bare
        # stacktrace that kept this failure unreadable for weeks.
        print(f"[docs-sync] FOUT: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
