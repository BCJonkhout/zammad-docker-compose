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
import sys
import unittest

spec = importlib.util.spec_from_file_location("docs_sync", "/root/zammad/bin/docs-sync.py")
ds = importlib.util.module_from_spec(spec)
sys.modules["docs_sync"] = ds
spec.loader.exec_module(ds)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
