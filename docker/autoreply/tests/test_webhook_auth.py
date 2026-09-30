"""ZAM-7: the webhook bearer compare must be constant-time (hmac.compare_digest)."""
from __future__ import annotations

import hmac

import pytest

from conftest import WEBHOOK_TOKEN


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"Bearer {WEBHOOK_TOKEN}", True),
        (f"  Bearer {WEBHOOK_TOKEN}  ", True),
        (f"Bearer {WEBHOOK_TOKEN}x", False),
        (f"Bearer {WEBHOOK_TOKEN[:-1]}", False),
        (f"bearer {WEBHOOK_TOKEN}", False),
        (WEBHOOK_TOKEN, False),
        ("", False),
        (None, False),
        ("Bearer ééé", False),
    ],
)
def test_is_authorized_semantics(app, header, expected):
    assert app.SERVICE.is_authorized(header) is expected


def test_is_authorized_uses_constant_time_compare(app, monkeypatch):
    """Fails on the old ``==`` compare: the spy is never reached."""
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)

    assert app.SERVICE.is_authorized(f"Bearer {WEBHOOK_TOKEN}") is True
    assert app.SERVICE.is_authorized("Bearer wrong") is False
    assert len(calls) == 2, "is_authorized must route every compare through hmac.compare_digest"
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)
