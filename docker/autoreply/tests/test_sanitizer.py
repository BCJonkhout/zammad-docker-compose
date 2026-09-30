"""ZAM-4: allowlist sanitizer for the LLM-shaped HTML that is posted to Zammad.

Every ``forbidden`` string below survived the old regex stripper (it only removed
<script>/<style>/<!DOCTYPE>/<html|head|body>), so each of these cases fails on
the pre-fix code.
"""
from __future__ import annotations

import re

import pytest


def _lower(value: str) -> str:
    return value.lower()


@pytest.mark.parametrize(
    ("payload", "forbidden", "kept_text"),
    [
        ('<img src="x" onerror="alert(1)">hallo', ["onerror", "<img"], "hallo"),
        ('<p onclick="alert(1)">klik</p>', ["onclick"], "klik"),
        ('<p ONMOUSEOVER="alert(1)">hover</p>', ["onmouseover"], "hover"),
        ('<a href="javascript:alert(1)">link</a>', ["javascript"], "link"),
        ('<a href="JaVaScRiPt:alert(1)">link</a>', ["javascript"], "link"),
        ('<a href="java\nscript:alert(1)">link</a>', ["script:"], "link"),
        ('<a href="&#106;avascript:alert(1)">link</a>', ["javascript", "&#106;"], "link"),
        ('<a href="data:text/html;base64,PHNjcmlwdD4=">link</a>', ["data:"], "link"),
        ('<a href="vbscript:msgbox(1)">link</a>', ["vbscript"], "link"),
        ('<a href="//evil.example/x">link</a>', ["evil.example"], "link"),
        ('<iframe src="https://evil.example"></iframe>tekst', ["iframe", "evil.example"], "tekst"),
        ('<object data="x.swf">obj</object>tekst', ["object", "x.swf", "obj"], "tekst"),
        ('<embed src="x.swf">tekst', ["embed", "x.swf"], "tekst"),
        ('<svg onload="alert(1)"><circle r="1"/></svg>tekst', ["svg", "onload", "circle"], "tekst"),
        ('<form action="https://evil.example"><input name="pw"></form>tekst', ["form", "input", "evil.example"], "tekst"),
        ('<script>alert(1)</script>tekst', ["script", "alert"], "tekst"),
        ('<SCRIPT SRC="https://evil.example/x.js"></SCRIPT>tekst', ["script", "evil.example"], "tekst"),
        ('<style>p{background:url(javascript:alert(1))}</style>tekst', ["style", "javascript"], "tekst"),
        ('<p style="background:url(javascript:alert(1))">tekst</p>', ["style", "javascript"], "tekst"),
        ('<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>tekst', ["math", "onerror", "<img"], ""),
        ('<!--<img src=x onerror=alert(1)>-->tekst', ["onerror", "<!--"], "tekst"),
        ('<a href="https://ok.example" srcdoc="<script>1</script>">link</a>', ["srcdoc", "script"], "link"),
        ('<div formaction="javascript:1">tekst</div>', ["formaction", "javascript"], "tekst"),
        ('<meta http-equiv="refresh" content="0;url=javascript:1">tekst', ["meta", "javascript"], "tekst"),
        ('<base href="javascript:1">tekst', ["base", "javascript"], "tekst"),
        ('<video><source onerror="alert(1)"></video>tekst', ["video", "source", "onerror"], "tekst"),
    ],
)
def test_dangerous_markup_is_removed(app, payload, forbidden, kept_text):
    cleaned = app.sanitize_html_fragment(payload)
    lowered = _lower(cleaned)
    for needle in forbidden:
        assert needle.lower() not in lowered, f"{needle!r} survived in {cleaned!r}"
    if kept_text:
        assert kept_text in cleaned


def test_safe_markup_is_preserved(app):
    payload = (
        '<p>Hallo <strong>daar</strong>, zie <a href="https://docs.prudai.com/x?a=1&amp;b=2" title="Docs">de docs</a>.</p>'
        "<ul><li>een</li><li>twee</li></ul><pre><code>x &lt; y</code></pre><hr><br>"
        '<table><tr><td colspan="2">cel</td></tr></table>'
    )
    cleaned = app.sanitize_html_fragment(payload)
    assert '<a href="https://docs.prudai.com/x?a=1&amp;b=2" title="Docs" target="_blank" rel="noopener noreferrer">de docs</a>' in cleaned
    assert "<strong>daar</strong>" in cleaned
    assert "<ul><li>een</li><li>twee</li></ul>" in cleaned
    assert "<pre><code>x &lt; y</code></pre>" in cleaned
    assert "<hr>" in cleaned and "<br>" in cleaned
    assert '<td colspan="2">cel</td>' in cleaned


def test_relative_and_mailto_links_survive(app):
    assert 'href="/help"' in app.sanitize_html_fragment('<a href="/help">help</a>')
    assert 'href="mailto:support@prudai.com"' in app.sanitize_html_fragment('<a href="mailto:support@prudai.com">mail</a>')


def test_unknown_tags_are_unwrapped_not_kept(app):
    cleaned = app.sanitize_html_fragment("<html><body><custom-x><p>tekst</p></custom-x></body></html>")
    assert cleaned == "<p>tekst</p>"


def test_plain_text_is_wrapped_and_escaped(app):
    assert app.sanitize_html_fragment("a < b & c") == "<p>a &lt; b &amp; c</p>"
    assert app.sanitize_html_fragment("   ") == ""
    assert app.sanitize_html_fragment("") == ""


def test_output_is_well_formed_even_for_broken_input(app):
    cleaned = app.sanitize_html_fragment("<p><strong>open<em>nest</p>rest")
    assert cleaned == "<p><strong>open<em>nest</em></strong></p>rest"
    # every opened allowed tag is closed exactly once
    for tag in ("p", "strong", "em"):
        assert len(re.findall(rf"<{tag}>", cleaned)) == len(re.findall(rf"</{tag}>", cleaned))


def test_text_after_a_dropped_container_is_kept(app):
    cleaned = app.sanitize_html_fragment("<p>voor</p><script>x</script><p>na</p><input><p>later</p>")
    assert cleaned == "<p>voor</p><p>na</p><p>later</p>"


def test_is_safe_href_scheme_matrix(app):
    assert app.is_safe_href("https://a.example/x")
    assert app.is_safe_href("http://a.example/x")
    assert app.is_safe_href("mailto:x@y.example")
    assert app.is_safe_href("/relative")
    assert app.is_safe_href("relative/path?x=1")
    assert not app.is_safe_href("javascript:1")
    assert not app.is_safe_href(" \t javascript:1")
    assert not app.is_safe_href("data:text/plain,1")
    assert not app.is_safe_href("//protocol-relative.example")
    assert not app.is_safe_href("")
    assert not app.is_safe_href(None)
