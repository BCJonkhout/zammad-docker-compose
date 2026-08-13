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
# Deletion floor: a run may prune a couple of genuinely removed pages, but a
# bulk deletion means the navigation was read incompletely -- fail instead.
DELETE_FLOOR_MIN = 2
DELETE_FLOOR_RATIO = 0.10
DELETE_OVERRIDE_ENV = "DOCS_SYNC_ALLOW_DELETE"
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
CODE_SPAN_RE = re.compile(r"<code>(.*?)</code>", re.IGNORECASE | re.DOTALL)
ANCHOR_TAG_RE = re.compile(r"</?a\b[^>]*>", re.IGNORECASE)
# Inline spans, matched left-to-right.  Order matters: "image" must precede
# "link", otherwise "![alt](src)" is matched from the "[" onwards and the "!"
# survives as literal text -- the loose exclamation marks that were visible on
# 22 knowledge-base pages.  Named groups keep the alternation readable.
INLINE_RE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<image>!\[(?P<img_alt>[^\]]*)\]\(\s*(?P<img_src>[^)\s]+)(?:\s+\"(?P<img_title>[^\"]*)\")?\s*\))"
    r"|(?P<link>\[(?P<link_label>[^\]]+)\]\((?P<link_href>[^)]+)\))"
    r"|(?P<strong_a>\*\*(?P<strong_a_text>[^*]+)\*\*)"
    r"|(?P<strong_b>__(?P<strong_b_text>.+?)__)"
    r"|(?P<em_a>\*(?P<em_a_text>[^*]+)\*)"
    r"|(?P<em_b>_(?P<em_b_text>[^_]+)_)"
)
# Block-level constructs.
LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])[ \t]+(?P<text>\S.*?)\s*$")
HR_RE = re.compile(r"^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(?P<info>.*)$")
# A table delimiter cell: "---", ":---", "---:" or ":---:".  Two dashes minimum
# so a lone "-" (a list bullet) can never be mistaken for one.
TABLE_DELIM_CELL_RE = re.compile(r"^:?-{2,}:?$")


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
    DOCS_KC_BOT_CLIENT_ID and DOCS_KC_BOT_CLIENT_SECRET are configured.

    The docs gate (marketing/docs/middleware.ts) accepts a realm-signed RS256
    bearer whose ``azp`` is in its DOCS_BOT_CLIENT_IDS allow-list; no audience or
    role is checked.  So the client here must be one of those client ids --
    ``prudai-docs-bot`` is the existing one.

    A failure to mint the token is never fatal: the 4 gated pages are then
    skipped with a warning, exactly as when no credentials are configured at
    all.  Letting it raise would take all 48 readable pages down with it.
    """
    client_id = os.getenv("DOCS_KC_BOT_CLIENT_ID", "").strip()
    client_secret = os.getenv("DOCS_KC_BOT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    issuer = os.getenv("DOCS_KC_ISSUER", "https://login.prudai.com/realms/prudai").strip().rstrip("/")
    try:
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
            raise RuntimeError("Keycloak returned no access_token.")
    except Exception as exc:  # noqa: BLE001 - degrade to "no bearer", never abort the sync
        print(
            f"[docs-sync] WAARSCHUWING: kon geen service-account token ophalen bij {issuer} "
            f"voor client '{client_id}' ({exc.__class__.__name__}: {exc}). "
            "De SSO-afgeschermde pagina's worden overgeslagen; de rest van de sync gaat door. "
            "Controleer DOCS_KC_BOT_CLIENT_ID/DOCS_KC_BOT_CLIENT_SECRET (OpenBao "
            "kv/prod/zammad/app) en of de client in realm 'prudai' service accounts aan heeft.",
            file=sys.stderr,
        )
        return None
    return token


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_body_for_compare(value: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", "", str(value or ""), flags=re.DOTALL)
    # Zammad's autolinker rewrites bare URLs inside <code> (scrubber/link.rb
    # skips only <a> and <pre>, not <code>), and it stops at the first "<", so a
    # URL containing a placeholder comes back as a *partial* anchor:
    #   <code>https://login.prudai.com/realms/</code>   ->
    #   <code><a href="...">https://login...realms/</a>&lt;realm&gt;/broker/...</code>
    # Matching only anchors that fill the whole <code> therefore missed these and
    # re-PATCHed the same article every night.  We never emit anchors inside
    # <code> ourselves, so dropping every anchor tag within a code span makes the
    # comparison symmetric regardless of what the autolinker did.
    normalized_code_links = CODE_SPAN_RE.sub(
        lambda match: f"<code>{ANCHOR_TAG_RE.sub('', match.group(1)).strip()}</code>",
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


def render_link(href: str, label: str) -> str:
    return (
        f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(label)}</a>"
    )


def render_image(base_url: str, src: str, alt: str) -> str:
    """Render a markdown image as a labelled link to the screenshot.

    NOT an ``<img>``, and deliberately so.  Zammad's knowledge-base sanitizer
    (``lib/html_sanitizer/scrubber/wipe.rb#remove_unsafe_src``) *deletes* any
    element whose ``src`` starts with ``http``, ``ftp`` or ``//`` -- the node is
    removed outright, so an ``<img src="https://docs.prudai.com/...">`` would
    silently disappear together with its alt text.  A relative ``src`` survives
    the sanitizer but resolves against support.prudai.com, which 404s (verified:
    /assets/screenshots/... returns 404 there and 200 on docs.prudai.com), so
    that only trades an invisible image for a broken-image icon.

    The only way to show the picture inline would be to inline it as a base64
    ``data:`` URI, which Zammad converts into a real ``cid:`` attachment.  That
    is ~6.6 MB of screenshots for this docs set, written by a nightly cron, and
    any instability in the body comparison would re-upload all of them every
    night -- so it is a deliberate product decision, not a rendering detail.

    A link keeps every bit of information (the alt text becomes the label, the
    screenshot stays one click away) and cannot break.
    """
    return render_link(resolve_docs_link(base_url, src.strip()), alt.strip())


def render_inline(base_url: str, text: str) -> str:
    output: list[str] = []
    last = 0
    for match in INLINE_RE.finditer(text):
        output.append(html.escape(text[last : match.start()]))
        if match.group("code"):
            output.append(f"<code>{html.escape(match.group('code')[1:-1])}</code>")
        elif match.group("image"):
            output.append(
                render_image(base_url, match.group("img_src") or "", match.group("img_alt") or "")
            )
        elif match.group("link"):
            output.append(
                render_link(
                    resolve_docs_link(base_url, match.group("link_href") or ""),
                    match.group("link_label") or "",
                )
            )
        elif match.group("strong_a") or match.group("strong_b"):
            strong_text = match.group("strong_a_text") or match.group("strong_b_text") or ""
            output.append(f"<strong>{html.escape(strong_text)}</strong>")
        else:
            em_text = match.group("em_a_text") or match.group("em_b_text") or ""
            output.append(f"<em>{html.escape(em_text)}</em>")
        last = match.end()
    output.append(html.escape(text[last:]))
    return "".join(output)


def split_table_row(line: str) -> list[str]:
    """Split one markdown table row into its cells, honouring escaped pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", stripped)]


def is_table_delimiter(line: str) -> bool:
    """True for the ``| --- | :---: |`` row that turns the line above into a table.

    A pipe is required, which is what keeps a bare ``---`` (a horizontal rule,
    and the frontmatter fence) from being mistaken for a one-column table.
    """
    if "|" not in line:
        return False
    cells = split_table_row(line)
    return bool(cells) and all(TABLE_DELIM_CELL_RE.match(cell) for cell in cells)


def column_alignments(delimiter_line: str) -> list[str | None]:
    """Read ``:---`` / ``---:`` / ``:---:`` markers into CSS text-align values."""
    alignments: list[str | None] = []
    for cell in split_table_row(delimiter_line):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            alignments.append("center")
        elif right:
            alignments.append("right")
        elif left:
            alignments.append("left")
        else:
            alignments.append(None)
    return alignments


def render_table(base_url: str, header_line: str, delimiter_line: str, body_lines: list[str]) -> str:
    """Render a GitHub-flavoured markdown table.

    ``class="zammad-table"`` is the one class Zammad's knowledge-base sanitizer
    keeps (its allowlist is ``js-signatureMarker``/``yahoo_quoted``/
    ``zammad-table``), and it is what gives the table its borders in the portal
    -- a bare <table> renders without any.  ``text-align`` on th/td is likewise
    inside the sanitizer's CSS allowlist.
    """
    alignments = column_alignments(delimiter_line)

    def cell(tag: str, value: str, index: int) -> str:
        align = alignments[index] if index < len(alignments) else None
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{render_inline(base_url, value)}</{tag}>"

    parts = ['<table class="zammad-table">', "<thead>", "<tr>"]
    parts += [cell("th", value, index) for index, value in enumerate(split_table_row(header_line))]
    parts += ["</tr>", "</thead>"]
    if body_lines:
        parts.append("<tbody>")
        for line in body_lines:
            parts.append("<tr>")
            parts += [cell("td", value, index) for index, value in enumerate(split_table_row(line))]
            parts.append("</tr>")
        parts.append("</tbody>")
    parts.append("</table>")
    return "".join(parts)


def render_list_block(base_url: str, items: list[tuple[int, str, str]]) -> str:
    """Render a run of list items, nesting deeper-indented ones inside their parent.

    ``items`` is (indent, "ul"|"ol", text).  A deeper indent opens a sub-list
    *inside* the still-open <li> above it; a shallower one closes back out.  The
    previous renderer ignored indentation entirely, so a sub-list closed its
    parent <ol> and the next top-level step restarted numbering at 1.
    """
    parts: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, tag, text in items:
        if not stack:
            parts.append(f"<{tag}>")
            stack.append((indent, tag))
        elif indent > stack[-1][0]:
            # Deeper: nest inside the <li> that is still open above.
            parts.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            parts.append("</li>")
            while len(stack) > 1 and indent < stack[-1][0]:
                parts.append(f"</{stack[-1][1]}>")
                stack.pop()
                parts.append("</li>")
            if stack[-1][1] != tag:
                parts.append(f"</{stack[-1][1]}>")
                stack.pop()
                parts.append(f"<{tag}>")
                stack.append((indent, tag))
        parts.append(f"<li>{render_inline(base_url, text)}")
    while stack:
        parts.append("</li>")
        parts.append(f"</{stack[-1][1]}>")
        stack.pop()
    return "".join(parts)


def starts_new_block(line: str) -> bool:
    """True if this line opens a block that a paragraph/quote must not swallow."""
    if not line.strip():
        return True
    return bool(
        HR_RE.match(line)
        or HEADING_RE.match(line)
        or BLOCKQUOTE_RE.match(line)
        or FENCE_RE.match(line)
        or LIST_ITEM_RE.match(line.rstrip())
    )


def markdown_to_html(base_url: str, markdown_text: str) -> str:
    """Convert docs markdown into the HTML subset Zammad's KB sanitizer keeps.

    Block constructs are consumed with lookahead (a table needs to see its
    delimiter row, a list its indentation), so this walks an index rather than
    streaming line by line.
    """
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index].rstrip()

        if not line.strip():
            index += 1
            continue

        fence = FENCE_RE.match(line)
        if fence:
            closing = fence.group(1)[0] * 3
            code_lines: list[str] = []
            index += 1
            while index < total and not lines[index].strip().startswith(closing):
                code_lines.append(lines[index].rstrip())
                index += 1
            index += 1  # step over the closing fence (or off the end)
            output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            continue

        if HR_RE.match(line):
            output.append("<hr>")
            index += 1
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{render_inline(base_url, heading.group(2))}</h{level}>")
            index += 1
            continue

        if BLOCKQUOTE_RE.match(line):
            quoted: list[str] = []
            while index < total:
                current = lines[index].rstrip()
                marker = BLOCKQUOTE_RE.match(current)
                if marker:
                    quoted.append(marker.group(1))
                    index += 1
                    continue
                # Lazy continuation: a plain text line keeps the quote going,
                # but a heading/list/table must not be swallowed by it.
                if quoted and current.strip() and not starts_new_block(current):
                    quoted.append(current.strip())
                    index += 1
                    continue
                break
            output.append(f"<blockquote>{markdown_to_html(base_url, chr(10).join(quoted))}</blockquote>")
            continue

        if "|" in line and index + 1 < total and is_table_delimiter(lines[index + 1]):
            header_line = line
            delimiter_line = lines[index + 1]
            index += 2
            body_lines: list[str] = []
            while index < total and lines[index].strip() and "|" in lines[index]:
                body_lines.append(lines[index])
                index += 1
            output.append(render_table(base_url, header_line, delimiter_line, body_lines))
            continue

        if LIST_ITEM_RE.match(line):
            items: list[tuple[int, str, str]] = []
            while index < total:
                current = lines[index].rstrip()
                item = LIST_ITEM_RE.match(current)
                if item:
                    items.append(
                        (
                            len(item.group("indent").expandtabs(4)),
                            "ol" if item.group("marker")[-1] in ".)" else "ul",
                            item.group("text"),
                        )
                    )
                    index += 1
                    continue
                # An indented plain line continues the item above it (the docs
                # use this for the italic metadata line under each source).
                if items and current.strip() and current[:1] in (" ", "\t"):
                    indent, tag, text = items[-1]
                    items[-1] = (indent, tag, f"{text} {current.strip()}")
                    index += 1
                    continue
                break
            output.append(render_list_block(base_url, items))
            continue

        paragraph: list[str] = []
        while index < total:
            current = lines[index].rstrip()
            if starts_new_block(current):
                break
            if "|" in current and index + 1 < total and is_table_delimiter(lines[index + 1]):
                break
            paragraph.append(current.strip())
            index += 1
        text = " ".join(part for part in paragraph if part)
        if text:
            output.append(f"<p>{render_inline(base_url, text)}</p>")

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
        if response.status_code in (401, 403):
            # SSO-gated "competitive edge" page (see gated-pages.json on the docs
            # site). Skip it instead of failing the whole run, and keep any
            # existing KB article -- an unreadable page must never cost us the
            # other 48, nor get itself pruned as "removed from the docs".
            if bearer:
                # Credentials ARE configured and the gate still says no: the
                # token is valid but its azp is not in the docs middleware's
                # DOCS_BOT_CLIENT_IDS allow-list, or the client lost access.
                # Loud, actionable -- but still not fatal.
                print(
                    f"[docs-sync] WAARSCHUWING: {language}: '{page.slug}' blijft afgeschermd "
                    f"ondanks een service-account token (HTTP {response.status_code} op {page_md_url}). "
                    "Het token is geldig maar wordt door de docs-gate geweigerd: zet de client-id "
                    "van DOCS_KC_BOT_CLIENT_ID in de env-var DOCS_BOT_CLIENT_IDS van het Vercel-"
                    "project 'prudai-docs' (comma-separated) en deploy die opnieuw. "
                    "Pagina overgeslagen; bestaand KB-artikel blijft staan.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[docs-sync] {language}: sla SSO-afgeschermde pagina '{page.slug}' over "
                    f"(HTTP {response.status_code} op {page_md_url}). "
                    "Stel DOCS_KC_BOT_CLIENT_ID + DOCS_KC_BOT_CLIENT_SECRET in "
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


def deletion_allowance(existing_count: int) -> int:
    """How many managed objects a single run may delete before it must stop.

    The page tree comes from scraped HTML, so a partially-parsed navigation
    looks exactly like "those pages were removed from the docs" and would prune
    live KB articles.  A run is allowed to clean up a couple of genuinely
    removed pages, but a bulk deletion is treated as a parsing accident.
    """
    return max(DELETE_FLOOR_MIN, int(existing_count * DELETE_FLOOR_RATIO))


def guard_deletions(kind: str, kb_id: int, doomed: list[str], existing_count: int) -> None:
    allowance = deletion_allowance(existing_count)
    if len(doomed) <= allowance:
        return
    if os.environ.get(DELETE_OVERRIDE_ENV) == "1":
        print(
            f"[docs-sync] LET OP: {len(doomed)} {kind} worden verwijderd in KB {kb_id} "
            f"(boven de drempel van {allowance}) omdat {DELETE_OVERRIDE_ENV}=1 is gezet.",
            file=sys.stderr,
        )
        return
    raise DocsIndexError(
        f"Afgebroken vóór verwijderen: de sync wilde {len(doomed)} van {existing_count} "
        f"{kind} verwijderen in KB {kb_id}, meer dan de drempel van {allowance}. "
        "Dat wijst op een onvolledig gelezen navigatie op de documentatiesite, niet op "
        f"echt verwijderde pagina's. Betrokken: {', '.join(doomed[:15])}"
        f"{' …' if len(doomed) > 15 else ''}. "
        "Klopt het dat deze pagina's echt weg zijn? Draai dan eenmalig met "
        f"{DELETE_OVERRIDE_ENV}=1. Er is niets verwijderd."
    )


def delete_stale_answers(
    client: ZammadClient,
    kb_id: int,
    language: str,
    desired_slugs: set[str],
    answers_by_id: dict[int, AnswerState],
) -> None:
    doomed: list[AnswerState] = []
    for answer in sorted(answers_by_id.values(), key=lambda item: item.id):
        if not answer.managed:
            continue
        if answer.language and answer.language != language:
            doomed.append(answer)
            continue
        if answer.slug and answer.slug in desired_slugs:
            continue
        doomed.append(answer)

    if not doomed:
        return

    managed_total = sum(1 for answer in answers_by_id.values() if answer.managed)
    guard_deletions(
        "artikelen",
        kb_id,
        [answer.slug or answer.title for answer in doomed],
        managed_total,
    )

    for answer in doomed:
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
    doomed = [category for category in ordered if build_path(category.id) not in desired_category_paths]
    if not doomed:
        return

    guard_deletions(
        "categorieën",
        kb_id,
        [category.title for category in doomed],
        len(categories_by_id),
    )

    for category in doomed:
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
