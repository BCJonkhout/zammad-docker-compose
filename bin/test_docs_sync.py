#!/usr/bin/env python3
"""Tests for docs-sync.py — run with: /usr/bin/python3 bin/test_docs_sync.py

No network and no Zammad access: the docs-site navigation is pinned as a
fixture below.  These cover the parts that silently broke when docs.prudai.com
migrated from Docsify to Astro Starlight (2026-05-24) and went unnoticed for
81 nights, plus the guards that keep a half-read navigation from pruning the
knowledge base.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest

# Overridable so the break-the-test probe can point the same suite at a mutated
# copy of the script (see the "niet-vacuum" note at the bottom of this file).
MODULE_PATH = os.environ.get("DOCS_SYNC_PATH", "/root/zammad/bin/docs-sync.py")
spec = importlib.util.spec_from_file_location("docs_sync", MODULE_PATH)
ds = importlib.util.module_from_spec(spec)
sys.modules["docs_sync"] = ds
spec.loader.exec_module(ds)

DOCS = "https://docs.prudai.com"


# Trimmed but structurally faithful copy of the rendered Starlight sidebar
# (hashed astro-* classes kept, since the parser must ignore them).
SIDEBAR_HTML = """
<nav class="sidebar" aria-label="Hoofdnavigatie"><div class="sidebar-content">
<ul class="top-level astro-3ii7xxms">
  <li><details open class="astro-3ii7xxms"><summary>
      <span class="group-label astro-3ii7xxms"><span class="large">Intro</span></span>
      <svg class="caret"><path d="m14"/></svg></summary>
    <ul class="astro-3ii7xxms">
      <li><a href="/" aria-current="page"><span>Prudai | Documentatie</span></a></li>
      <li><a href="/getting-started/"><span>Snelstart</span></a></li>
    </ul></details></li>
  <li><details open><summary>
      <span class="group-label"><span class="large">Basis</span></span></summary>
    <ul>
      <li><a href="/authentication/"><span>Inloggen &amp; productkeuze</span></a></li>
      <li><a href="/knowledge/"><span>Kennis (bronnen &amp; tools)</span></a></li>
    </ul></details></li>
</ul></div></nav>
"""

MARKDOWN_WITH_FRONTMATTER = (
    '---\ntitle: "Snelstart (Prudai Platform)"\n'
    'description: "De kortste route."\nsidebar: {"hidden":false}\n---\n\n'
    "Dit is de kortste route.\n\n## 0) Kies bewust je product\n"
)


class MarkdownPathTests(unittest.TestCase):
    """Starlight routes carry a trailing slash; the home pages are special."""

    def test_dutch_routes(self):
        self.assertEqual(ds.to_markdown_path("nl", "/"), "/index.md")
        self.assertEqual(ds.to_markdown_path("nl", "/getting-started/"), "/getting-started.md")
        self.assertEqual(ds.to_markdown_path("nl", "/getting-started"), "/getting-started.md")

    def test_english_routes(self):
        self.assertEqual(ds.to_markdown_path("en", "/en/"), "/en.md")
        self.assertEqual(ds.to_markdown_path("en", "/en/getting-started/"), "/en/getting-started.md")

    def test_no_docsify_readme_paths_remain(self):
        for language, route in (("nl", "/"), ("en", "/en/")):
            self.assertNotIn("README", ds.to_markdown_path(language, route))

    def test_slug_and_page_url_identity_is_stable(self):
        """Article identity is matched via the stored Source: URL — keep it stable."""
        base = "https://docs.prudai.com"
        for language, route, slug, url in (
            ("nl", "/", "README", "https://docs.prudai.com/"),
            ("nl", "/authentication/", "authentication", "https://docs.prudai.com/authentication"),
            ("en", "/en/", "README", "https://docs.prudai.com/en/"),
            ("en", "/en/authentication/", "authentication", "https://docs.prudai.com/en/authentication"),
        ):
            self.assertEqual(ds.to_slug(language, route), slug)
            self.assertEqual(ds.to_page_url(base, language, slug), url)
            self.assertEqual(ds.infer_page_identity(url), (language, slug))


class FrontmatterTests(unittest.TestCase):
    def test_frontmatter_is_removed(self):
        out = ds.strip_frontmatter(MARKDOWN_WITH_FRONTMATTER)
        self.assertTrue(out.startswith("Dit is de kortste route."))
        for leaked in ("---", "title:", "sidebar:"):
            self.assertNotIn(leaked, out.split("\n")[0])

    def test_body_without_frontmatter_is_untouched(self):
        body = "# Titel\n\nTekst met --- streepje.\n"
        self.assertEqual(ds.strip_frontmatter(body), body)

    def test_horizontal_rule_body_is_not_eaten(self):
        self.assertIn("Slot", ds.strip_frontmatter("---\ntitle: \"x\"\n---\n\nStart\n\n---\n\nSlot\n"))


class SidebarParserTests(unittest.TestCase):
    def test_tree_titles_order_and_categories(self):
        entries = ds.parse_sidebar_nav("https://docs.prudai.com/", SIDEBAR_HTML)
        categories, pages = ds.build_sidebar("nl", "https://docs.prudai.com", entries)
        self.assertEqual([c.title for c in categories.values()], ["Intro", "Basis"])
        self.assertEqual([p.title for p in pages],
                         ["Prudai | Documentatie", "Snelstart",
                          "Inloggen & productkeuze", "Kennis (bronnen & tools)"])
        self.assertEqual([p.slug for p in pages],
                         ["README", "getting-started", "authentication", "knowledge"])
        self.assertEqual([p.category_path for p in pages],
                         [("Intro",), ("Intro",), ("Basis",), ("Basis",)])
        self.assertEqual([p.order for p in pages], [0, 1, 0, 1])

    def test_entities_in_labels_are_decoded(self):
        entries = ds.parse_sidebar_nav("https://docs.prudai.com/", SIDEBAR_HTML)
        titles = [title for kind, _, _, title in entries if kind == "link"]
        self.assertIn("Inloggen & productkeuze", titles)
        self.assertNotIn("Inloggen &amp; productkeuze", titles)

    def test_page_without_navigation_raises_readable_error(self):
        """A 200 that is no longer a Starlight page must fail loudly, not silently."""
        with self.assertRaises(ds.DocsIndexError) as caught:
            ds.parse_sidebar_nav("https://docs.prudai.com/", "<html><body><h1>Docs</h1></body></html>")
        message = str(caught.exception)
        self.assertIn("geen leesbare index", message)
        self.assertIn("https://docs.prudai.com/", message)

    def test_renamed_top_level_class_fails_safe(self):
        with self.assertRaises(ds.DocsIndexError):
            ds.parse_sidebar_nav("https://docs.prudai.com/",
                                 SIDEBAR_HTML.replace("top-level", "sl-nav-root"))


class DeletionGuardTests(unittest.TestCase):
    """A half-read navigation looks like 'those pages were deleted' — refuse it."""

    def test_small_cleanup_is_allowed(self):
        ds.guard_deletions("artikelen", 1, ["oude-pagina"], 26)  # must not raise

    def test_bulk_deletion_is_refused_with_readable_error(self):
        doomed = [f"pagina-{n}" for n in range(12)]
        with self.assertRaises(ds.DocsIndexError) as caught:
            ds.guard_deletions("artikelen", 1, doomed, 26)
        message = str(caught.exception)
        self.assertIn("Er is niets verwijderd", message)
        self.assertIn("DOCS_SYNC_ALLOW_DELETE", message)

    def test_override_allows_bulk_deletion(self):
        import os
        os.environ[ds.DELETE_OVERRIDE_ENV] = "1"
        try:
            ds.guard_deletions("artikelen", 1, [f"p{n}" for n in range(12)], 26)
        finally:
            del os.environ[ds.DELETE_OVERRIDE_ENV]

    def test_allowance_scales_with_kb_size(self):
        self.assertEqual(ds.deletion_allowance(0), 2)
        self.assertEqual(ds.deletion_allowance(26), 2)
        self.assertEqual(ds.deletion_allowance(200), 20)


class BodyCompareTests(unittest.TestCase):
    """Zammad rewrites stored HTML; unchanged articles must not be re-written."""

    def test_autolinked_bare_url_compares_equal(self):
        stored = '<p>zie <a href="https://app.prudai.com" rel="nofollow">https://app.prudai.com</a></p>'
        generated = "<p>zie https://app.prudai.com</p>"
        self.assertEqual(ds.normalize_body_for_compare(stored),
                         ds.normalize_body_for_compare(generated))

    def test_entity_escaping_compares_equal(self):
        self.assertEqual(ds.normalize_body_for_compare('<p>Taken (voorheen "kanban")</p>'),
                         ds.normalize_body_for_compare("<p>Taken (voorheen &quot;kanban&quot;)</p>"))

    def test_real_content_change_is_still_detected(self):
        self.assertNotEqual(ds.normalize_body_for_compare("<p>Ja, dit kan.</p>"),
                            ds.normalize_body_for_compare("<p>Nee, dit kan niet.</p>"))

    def test_changed_link_label_is_still_detected(self):
        self.assertNotEqual(
            ds.normalize_body_for_compare('<p><a href="https://a.example">Handleiding</a></p>'),
            ds.normalize_body_for_compare('<p><a href="https://a.example">Snelstart</a></p>'))

    def test_autolink_filling_a_whole_code_span_compares_equal(self):
        stored = ('<p><code><a href="https://app.prudai.com" rel="nofollow noreferrer noopener" '
                  'target="_blank">https://app.prudai.com</a></code></p>')
        self.assertEqual(ds.normalize_body_for_compare(stored),
                         ds.normalize_body_for_compare("<p><code>https://app.prudai.com</code></p>"))

    def test_partial_autolink_inside_code_compares_equal(self):
        """Zammad's autolinker stops at the first '<', so a URL with a placeholder
        comes back as an anchor covering only *part* of the code span -- and the
        text between two such spans used to be swallowed whole by a non-greedy
        <code>...</a></code> match, which re-PATCHed admin-sso-sharepoint on every
        single run.  Two code spans in one body is what reproduces it."""
        stored = (
            '<p><code><a href="https://login.prudai.com/realms/" rel="nofollow">'
            "https://login.prudai.com/realms/</a>&lt;realm&gt;/broker</code>"
            " Vervang <code>&lt;realm&gt;</code> door je realm. Open"
            ' <code><a href="https://app.prudai.com" rel="nofollow" target="_blank">'
            "https://app.prudai.com</a></code> daarna.</p>"
        )
        generated = (
            "<p><code>https://login.prudai.com/realms/&lt;realm&gt;/broker</code>"
            " Vervang <code>&lt;realm&gt;</code> door je realm. Open"
            " <code>https://app.prudai.com</code> daarna.</p>"
        )
        self.assertEqual(ds.normalize_body_for_compare(stored),
                         ds.normalize_body_for_compare(generated))
        # and the prose between the two code spans must survive intact
        self.assertIn("door je realm", ds.normalize_body_for_compare(stored))

    def test_autolink_that_rewrites_the_url_inside_code_compares_equal(self):
        """The collapse-self-links rule only fires when href == link text.  Zammad's
        autolinker also *rewrites* hrefs (it prefixes a scheme, strips a trailing
        '.', forces rel/target), and then href != text and the article would be
        re-PATCHed forever.  Inside <code> we never emit anchors at all, so every
        anchor tag in a code span is dropped before comparing."""
        stored = '<p><code><a href="http://www.example.com" rel="nofollow">www.example.com</a></code></p>'
        self.assertEqual(ds.normalize_body_for_compare(stored),
                         ds.normalize_body_for_compare("<p><code>www.example.com</code></p>"))

    def test_table_alignment_survives_zammads_style_rewrite(self):
        """Zammad's clear_style re-serialises text-align:left as 'text-align:left;'
        (verified live against zammad 7.0.0's HtmlSanitizer).  Without normalising
        style attributes that article differs every night and is re-written
        forever -- the exact churn class this run already fixed once."""
        generated = ds.markdown_to_html(DOCS, "| A | B |\n| :--- | ---: |\n| 1 | 2 |\n")
        sanitized = generated.replace("text-align:left;", "text-align:left ;").replace(
            "text-align:right;", "text-align: right"
        )
        self.assertEqual(ds.normalize_body_for_compare(sanitized),
                         ds.normalize_body_for_compare(generated))

    def test_a_different_alignment_is_still_detected(self):
        left = ds.markdown_to_html(DOCS, "| A |\n| :--- |\n| 1 |\n")
        right = ds.markdown_to_html(DOCS, "| A |\n| ---: |\n| 1 |\n")
        self.assertNotEqual(ds.normalize_body_for_compare(left),
                            ds.normalize_body_for_compare(right))

    def test_changed_code_content_is_still_detected(self):
        """Stripping anchors inside <code> must not blind the comparison."""
        self.assertNotEqual(
            ds.normalize_body_for_compare("<p><code>https://app.prudai.com</code></p>"),
            ds.normalize_body_for_compare("<p><code>https://leo.prudai.com</code></p>"))


class TableTests(unittest.TestCase):
    """20 of 48 live pages rendered their tables as a row of vertical bars."""

    TABLE_MD = (
        "| Keuze | Wanneer |\n"
        "| --- | --- |\n"
        "| **LEO** | brede juridische bronnen |\n"
        "| **VERA** | omgevingsrecht |\n"
    )

    def test_table_becomes_a_real_table(self):
        out = ds.markdown_to_html(DOCS, self.TABLE_MD)
        self.assertIn("<table", out)
        self.assertIn("<thead><tr><th>Keuze</th><th>Wanneer</th></tr></thead>", out)
        self.assertIn("<td><strong>LEO</strong></td><td>brede juridische bronnen</td>", out)
        self.assertEqual(out.count("<tr>"), 3)

    def test_no_pipe_soup_survives(self):
        """The regression itself: no rendered text node may still contain a pipe."""
        out = ds.markdown_to_html(DOCS, self.TABLE_MD)
        self.assertNotRegex(out, r"<(p|li)>[^<]*\|")
        self.assertNotIn("| --- |", out)

    def test_alignment_row_is_consumed_and_applied(self):
        out = ds.markdown_to_html(DOCS, "| A | B | C |\n| :--- | :---: | ---: |\n| 1 | 2 | 3 |\n")
        # Trailing semicolon matches how Zammad re-serialises the declaration.
        self.assertIn('<th style="text-align:left;">A</th>', out)
        self.assertIn('<th style="text-align:center;">B</th>', out)
        self.assertIn('<th style="text-align:right;">C</th>', out)
        # The delimiter row must never surface as a data row.
        self.assertNotIn("---", out)

    def test_short_alignment_markers_are_recognised(self):
        out = ds.markdown_to_html(DOCS, "| A | B |\n|:-:|-:|\n| 1 | 2 |\n")
        self.assertIn('<th style="text-align:center;">A</th>', out)
        self.assertIn('<th style="text-align:right;">B</th>', out)
        self.assertNotRegex(out, r"<(p|li)>[^<]*\|")

    def test_a_row_of_single_dashes_is_not_a_delimiter(self):
        """'| - | - |' is a data row, not an alignment row."""
        self.assertFalse(ds.is_table_delimiter("| - | - |"))

    def test_table_carries_the_only_class_zammad_keeps(self):
        """Zammad's KB sanitizer allows exactly js-signatureMarker/yahoo_quoted/
        zammad-table; without it the table renders borderless in the portal."""
        self.assertIn('<table class="zammad-table">', ds.markdown_to_html(DOCS, self.TABLE_MD))

    def test_bare_dashes_are_a_rule_not_a_table(self):
        out = ds.markdown_to_html(DOCS, "Tekst\n\n---\n\nMeer tekst\n")
        self.assertIn("<hr>", out)
        self.assertNotIn("<table", out)

    def test_escaped_pipe_stays_inside_its_cell(self):
        out = ds.markdown_to_html(DOCS, "| A | B |\n| --- | --- |\n| x \\| y | z |\n")
        self.assertIn("<td>x | y</td><td>z</td>", out)


class ImageTests(unittest.TestCase):
    """22 of 48 live pages showed a loose '!' where a screenshot belongs."""

    IMAGE_MD = "![De afgeronde onboarding-tour](/assets/screenshots/leo/tour_completed.png)\n"

    def test_no_stray_exclamation_mark(self):
        out = ds.markdown_to_html(DOCS, self.IMAGE_MD)
        self.assertNotIn("!", out)
        self.assertNotIn("![", out)

    def test_relative_path_becomes_an_absolute_docs_url(self):
        out = ds.markdown_to_html(DOCS, self.IMAGE_MD)
        self.assertIn('href="https://docs.prudai.com/assets/screenshots/leo/tour_completed.png"', out)
        self.assertNotIn('href="/assets/', out)

    def test_alt_text_is_kept_as_the_label(self):
        self.assertIn(">De afgeronde onboarding-tour</a>", ds.markdown_to_html(DOCS, self.IMAGE_MD))

    def test_never_emits_a_hotlinked_img_tag(self):
        """Zammad's sanitizer DELETES any element whose src starts with http/ftp/
        '//' (scrubber/wipe.rb#remove_unsafe_src), so an <img src="https://..">
        would vanish along with its alt text.  Emitting one is the bug."""
        out = ds.markdown_to_html(DOCS, self.IMAGE_MD)
        self.assertNotRegex(out, r'<img[^>]+src="(https?:|//|ftp)')

    def test_image_inside_a_sentence_and_a_list_item(self):
        out = ds.markdown_to_html(DOCS, "- zie ![alt tekst](/a/b.png) hierboven\n")
        self.assertIn("<li>zie <a href=", out)
        self.assertIn(">alt tekst</a> hierboven</li>", out)
        self.assertNotIn("!", out)

    def test_plain_link_is_not_mistaken_for_an_image(self):
        out = ds.markdown_to_html(DOCS, "[Snelstart](/getting-started)\n")
        self.assertIn(">Snelstart</a>", out)
        self.assertNotIn("<img", out)


class BlockquoteTests(unittest.TestCase):
    """12 of 48 live pages leaked the '>' marker as literal text."""

    def test_quote_becomes_a_blockquote(self):
        out = ds.markdown_to_html(DOCS, "> **Binnenkort**: Excel en Outlook.\n")
        self.assertIn("<blockquote>", out)
        self.assertIn("<strong>Binnenkort</strong>", out)

    def test_marker_does_not_leak_as_text(self):
        out = ds.markdown_to_html(DOCS, "> Let op: dit geldt alleen binnen LEO.\n")
        self.assertNotRegex(out, r"<(p|li)>\s*&gt;")
        self.assertNotIn("&gt; Let op", out)

    def test_multiline_quote_is_one_block(self):
        out = ds.markdown_to_html(DOCS, "> regel een\n> regel twee\n")
        self.assertEqual(out.count("<blockquote>"), 1)
        self.assertIn("regel een regel twee", out)

    def test_heading_after_quote_is_not_swallowed(self):
        out = ds.markdown_to_html(DOCS, "> citaat\n## Kop\n")
        self.assertIn("<h2>Kop</h2>", out)
        self.assertNotIn("<h2>", out.split("</blockquote>")[0])

    def test_list_inside_quote_is_rendered(self):
        out = ds.markdown_to_html(DOCS, "> - een\n> - twee\n")
        self.assertIn("<blockquote><ul><li>een</li><li>twee</li></ul></blockquote>", out)


class NestedListTests(unittest.TestCase):
    """The old renderer ignored indentation, so a sub-list closed its parent
    <ol> and the next top-level step restarted numbering at 1."""

    NESTED_MD = (
        "1. Open de webapp-URL.\n"
        "2. Kies je inlogmethode:\n"
        "   - Microsoft / Entra ID;\n"
        "   - e-mail + wachtwoord.\n"
        "3. Rond het SSO-proces af.\n"
    )

    def test_sub_list_nests_inside_its_parent_item(self):
        out = ds.markdown_to_html(DOCS, self.NESTED_MD)
        self.assertIn("<li>Kies je inlogmethode:<ul>", out)
        self.assertIn("</ul></li>", out)

    def test_ordered_numbering_is_not_restarted(self):
        """One <ol> for the three steps -- two would renumber step 3 as '1'."""
        out = ds.markdown_to_html(DOCS, self.NESTED_MD)
        self.assertEqual(out.count("<ol>"), 1)
        self.assertEqual(out.count("</ol>"), 1)

    def test_tags_are_balanced(self):
        out = ds.markdown_to_html(DOCS, self.NESTED_MD)
        for tag in ("ol", "ul", "li"):
            self.assertEqual(out.count(f"<{tag}>"), out.count(f"</{tag}>"), tag)

    def test_lazy_continuation_stays_in_the_same_item(self):
        """The docs put an italic metadata line under each source entry."""
        out = ds.markdown_to_html(DOCS, "- **Wetten.nl** — wettenbank\n  *wetgeving · NL*\n- **EUR-Lex**\n")
        self.assertEqual(out.count("<ul>"), 1)
        self.assertIn("<em>wetgeving · NL</em></li>", out)
        self.assertNotIn("<p>", out)

    def test_separate_top_level_lists_still_switch_type(self):
        out = ds.markdown_to_html(DOCS, "- een\n- twee\n\n1. drie\n")
        self.assertIn("<ul><li>een</li><li>twee</li></ul>", out)
        self.assertIn("<ol><li>drie</li></ol>", out)


class CodeBlockTests(unittest.TestCase):
    def test_fenced_block_is_preserved_verbatim(self):
        out = ds.markdown_to_html(DOCS, "```bash\ncurl -s https://api.example.com\n```\n")
        self.assertIn("<pre><code>curl -s https://api.example.com</code></pre>", out)

    def test_markdown_inside_a_code_block_is_not_rendered(self):
        out = ds.markdown_to_html(DOCS, "```\n| a | b |\n| --- | --- |\n- **niet vet**\n```\n")
        self.assertNotIn("<table", out)
        self.assertNotIn("<strong>", out)
        self.assertNotIn("<li>", out)
        self.assertIn("| a | b |", out)

    def test_html_in_a_code_block_is_escaped(self):
        out = ds.markdown_to_html(DOCS, "```\n<script>alert(1)</script>\n```\n")
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_inline_code_is_escaped_and_wrapped(self):
        self.assertIn("<code>&lt;realm&gt;</code>", ds.markdown_to_html(DOCS, "Vervang `<realm>` hier.\n"))


class SanitizerContractTests(unittest.TestCase):
    """Everything emitted must survive Zammad's KB allowlist unchanged."""

    SAMPLE = (
        "# Kop\n\nTekst met **vet**, *cursief* en `code`.\n\n"
        "> Een citaat\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "- een\n  - genest\n\n![alt](/a/b.png)\n\n---\n\n```\ncode\n```\n"
    )
    # Tags Zammad keeps for KB answers; <span> is deliberately absent because
    # RemoveLineBreaks unwraps every span in KB content.
    ALLOWED = {
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "strong", "em", "code", "pre",
        "blockquote", "table", "thead", "tbody", "tr", "th", "td", "ul", "ol", "li", "hr", "br",
    }

    def test_only_allowlisted_tags_are_emitted(self):
        out = ds.markdown_to_html(DOCS, self.SAMPLE)
        emitted = set(re.findall(r"</?([a-z0-9]+)", out))
        self.assertEqual(emitted - self.ALLOWED, set(), f"niet-toegestane tags: {emitted - self.ALLOWED}")

    def test_no_span_or_div_or_id_or_foreign_class(self):
        out = ds.markdown_to_html(DOCS, self.SAMPLE)
        for forbidden in ("<span", "<div", " id=", "<img"):
            self.assertNotIn(forbidden, out)
        for klass in re.findall(r'class="([^"]+)"', out):
            self.assertEqual(klass, "zammad-table")


class _Response:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers if headers is not None else {"Content-Type": "text/markdown"}


class _StubSession:
    """Stands in for requests.Session inside fetch_docs_tree."""

    def __init__(self, router):
        self.headers = {}
        self._router = router
        self.requested = []

    def get(self, url, timeout=None, allow_redirects=True):
        self.requested.append(url)
        return self._router(url)


class _RecordingClient:
    """Stands in for ZammadClient; records calls instead of making them."""

    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        return {}

    def deletes(self):
        return [path for method, path in self.calls if method == "DELETE"]


def _answer(answer_id, slug, language="nl", managed=True):
    return ds.AnswerState(
        id=answer_id, title=f"Titel {slug}", category_id=1, translation_id=answer_id,
        content_id=answer_id, body="", tags=[], published=True, slug=slug,
        language=language, source_url=None, managed=managed,
    )


class BearerSkipTests(unittest.TestCase):
    """A configured-but-rejected bearer must skip the page, not kill the run.

    These assert behaviour.  An earlier version grepped the source for the line
    that implements the rule, which passed happily when the rule was removed.
    """

    def setUp(self):
        for key in ("DOCS_KC_BOT_CLIENT_ID", "DOCS_KC_BOT_CLIENT_SECRET", "DOCS_KC_ISSUER"):
            os.environ.pop(key, None)
        self._session = ds.requests.Session
        self._post = ds.requests.post

    def tearDown(self):
        ds.requests.Session = self._session
        ds.requests.post = self._post
        for key in ("DOCS_KC_BOT_CLIENT_ID", "DOCS_KC_BOT_CLIENT_SECRET", "DOCS_KC_ISSUER"):
            os.environ.pop(key, None)

    def test_no_credentials_means_no_bearer(self):
        self.assertIsNone(ds.maybe_docs_bearer())

    def test_unreachable_keycloak_degrades_to_none(self):
        """WITH credentials configured: minting must fail soft, not abort the run."""
        os.environ["DOCS_KC_BOT_CLIENT_ID"] = "prudai-docs-bot"
        os.environ["DOCS_KC_BOT_CLIENT_SECRET"] = "geheim"

        def explode(*args, **kwargs):
            raise ds.requests.RequestException("keycloak onbereikbaar")

        ds.requests.post = explode
        self.assertIsNone(ds.maybe_docs_bearer())

    def test_token_response_without_access_token_degrades_to_none(self):
        os.environ["DOCS_KC_BOT_CLIENT_ID"] = "prudai-docs-bot"
        os.environ["DOCS_KC_BOT_CLIENT_SECRET"] = "geheim"

        class _TokenResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"error": "invalid_client"}

        ds.requests.post = lambda *a, **k: _TokenResponse()
        self.assertIsNone(ds.maybe_docs_bearer())

    def _gated_tree(self):
        def router(url):
            if url.endswith("/"):
                return _Response(200, SIDEBAR_HTML, {"Content-Type": "text/html"})
            if url.endswith("/knowledge.md"):
                return _Response(401, "", {"Content-Type": "text/html"})
            return _Response(200, "# Kop\n\nTekst.\n")

        session = _StubSession(router)
        ds.requests.Session = lambda: session
        return ds.fetch_docs_tree("https://docs.prudai.com", "nl")

    def test_a_401_page_is_skipped_and_the_run_continues(self):
        categories, pages, markdown, skipped = self._gated_tree()
        self.assertEqual(skipped, {"knowledge"})
        self.assertNotIn("knowledge", {page.slug for page in pages})
        self.assertNotIn("knowledge", markdown)
        # the other three pages still came through
        self.assertEqual({page.slug for page in pages},
                         {"README", "getting-started", "authentication"})

    def test_a_skipped_page_is_never_pruned(self):
        """The invariant that matters: unreadable != removed from the docs.

        Deliberately does NOT put the skipped slug into desired_slugs -- if the
        test builds the keep-set itself it passes even when the production code
        stops building it, which is exactly how this test was vacuous before.
        Here the skipped-set is the only thing standing between the article and
        a DELETE.
        """
        _, pages, _, skipped = self._gated_tree()
        readable_only = {page.slug for page in pages}
        self.assertNotIn("knowledge", readable_only)

        client = _RecordingClient()
        ds.delete_stale_answers(client, 1, "nl", readable_only, {7: _answer(7, "knowledge")}, skipped)
        self.assertEqual(client.deletes(), [], "een onleesbare pagina mag niet verwijderd worden")

    def test_a_genuinely_removed_page_is_still_pruned(self):
        """Counterpart, so the test above cannot pass by never deleting anything."""
        client = _RecordingClient()
        ds.delete_stale_answers(client, 1, "nl", {"README"}, {7: _answer(7, "weg-uit-de-docs")}, set())
        self.assertEqual(client.deletes(), ["/api/v1/knowledge_bases/1/answers/7"])

    def test_a_redirected_page_is_skipped_not_followed(self):
        def router(url):
            if url.endswith("/"):
                return _Response(200, SIDEBAR_HTML, {"Content-Type": "text/html"})
            if url.endswith("/knowledge.md"):
                return _Response(302, "", {"Location": "https://login.prudai.com/"})
            return _Response(200, "# Kop\n\nTekst.\n")

        ds.requests.Session = lambda: _StubSession(router)
        _, pages, _, skipped = ds.fetch_docs_tree("https://docs.prudai.com", "nl")
        self.assertEqual(skipped, {"knowledge"})
        self.assertNotIn("knowledge", {page.slug for page in pages})

    def test_an_html_login_page_served_as_200_is_refused(self):
        """A gate that answers 200 with HTML must not be published as an article."""
        def router(url):
            if url.endswith("/"):
                return _Response(200, SIDEBAR_HTML, {"Content-Type": "text/html"})
            if url.endswith("/knowledge.md"):
                return _Response(200, "<html><body>Log in</body></html>", {"Content-Type": "text/html"})
            return _Response(200, "# Kop\n\nTekst.\n")

        ds.requests.Session = lambda: _StubSession(router)
        with self.assertRaises(ds.DocsIndexError) as caught:
            ds.fetch_docs_tree("https://docs.prudai.com", "nl")
        self.assertIn("in plaats van markdown", str(caught.exception))


class ParserInvariantTests(unittest.TestCase):
    def test_every_block_branch_consumes_a_line(self):
        """A branch that consumes nothing would hang the nightly sync forever."""
        sample = (
            "# Kop\n\ntekst\n\n> citaat\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
            "- een\n  - genest\n\n```\ncode\n```\n\n---\n\n![alt](/a.png)\n"
        )
        self.assertTrue(ds.markdown_to_html(DOCS, sample))  # must return, not hang

    def test_list_continuation_does_not_swallow_a_heading(self):
        out = ds.markdown_to_html(DOCS, "- item\n  ## Kop\n")
        self.assertIn("<h2>Kop</h2>", out)
        self.assertNotIn("## Kop", out)

    def test_list_continuation_does_not_swallow_a_code_fence(self):
        out = ds.markdown_to_html(DOCS, "- item\n  ```\n  code\n  ```\n")
        self.assertIn("<pre><code>", out)

    def test_list_continuation_does_not_swallow_a_table(self):
        out = ds.markdown_to_html(DOCS, "- item\n  | A | B |\n  | --- | --- |\n  | 1 | 2 |\n")
        self.assertIn("<table", out)
        self.assertNotRegex(out, r"<li>[^<]*\|")


if __name__ == "__main__":
    unittest.main(verbosity=2)
