#!/usr/bin/env python3
"""Render the Zammad signature for support@ from the house-style signature.

Source of truth is the huisstijl repo (/root/prudai-huisstijl/signature/out/support.html),
the same HTML Outlook uses for the support@ mailbox, including the confidentiality footer.
Two changes:
- the name line: the mailbox signature says "Het Prudai-team", the Zammad channel signs
  as "Prudai Support" (besluit Beau 19-10-2026);
- the images (logo, keurmerken) are inlined as data: URIs. Zammad's HtmlSanitizer deletes
  every <img> whose src starts with http (Scrubber::Wipe#remove_unsafe_src), so a remote
  logo would silently vanish; base64 PNGs survive and Zammad sends them as inline parts.

Fails loudly instead of guessing: if the house-style file stops containing exactly one
name line or the confidentiality footer, the signature in Zammad would silently drift, so
the provisioning run has to stop and someone has to look.

Usage: email_channel_signature.py <support.html> [display-name]   (prints the HTML)
"""
from __future__ import annotations

import base64
import html
import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable

HOUSE_NAME = "Het Prudai-team"
FOOTER_MARKER = "vertrouwelijk en uitsluitend bestemd voor"
IMAGE_HOST = "https://cms.prudai.com/uploads/"
MAX_IMAGE_BYTES = 200_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IMG_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)


class SignatureError(ValueError):
    pass


def fetch_url(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - host allowlisted below
        return response.read(MAX_IMAGE_BYTES + 1)


def inline_images(source_html: str, fetch: Callable[[str], bytes] = fetch_url) -> str:
    def replace(match: re.Match[str]) -> str:
        url = html.unescape(match.group(2))
        if url.startswith("data:"):
            return match.group(0)
        if not url.startswith(IMAGE_HOST) or not url.lower().endswith(".png"):
            raise SignatureError(f"refusing to inline image outside {IMAGE_HOST} or not .png: {url}")
        data = fetch(url)
        if len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG_MAGIC):
            raise SignatureError(f"image is not a PNG under {MAX_IMAGE_BYTES} bytes: {url}")
        return f"{match.group(1)}data:image/png;base64,{base64.b64encode(data).decode('ascii')}{match.group(3)}"

    return IMG_SRC.sub(replace, source_html)


def render(source_html: str, display_name: str = "Prudai Support", fetch: Callable[[str], bytes] = fetch_url) -> str:
    name_cell = f">{HOUSE_NAME}</td>"
    count = source_html.count(name_cell)
    if count != 1:
        raise SignatureError(f"expected exactly one name line {HOUSE_NAME!r} in the house signature, found {count}")
    if FOOTER_MARKER not in source_html:
        raise SignatureError("house signature has no confidentiality footer")
    if not display_name.strip():
        raise SignatureError("empty display name")
    rendered = source_html.replace(name_cell, f">{html.escape(display_name.strip())}</td>")
    return inline_images(rendered, fetch).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        source = Path(argv[1]).read_text(encoding="utf-8")
        sys.stdout.write(render(source, argv[2] if len(argv) == 3 else "Prudai Support"))
    except (OSError, SignatureError) as exc:
        print(f"email_channel_signature: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
