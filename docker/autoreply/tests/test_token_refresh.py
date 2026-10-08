"""A rotated Zammad token on disk must not leave the service stuck on 401.

On 30-09-2026 provisioning rewrote ``autoreply.token`` 23 s after the container
started; the client kept the old token in memory and every webhook failed with
401 "Can't find User for Token" until a restart on 08-10 (Zammad #59029).
"""
from __future__ import annotations

from typing import Any


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = "" if payload is None else "{}"

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    """Accepts only ``valid_token``; records which token each call carried."""

    def __init__(self, valid_token: str) -> None:
        self.valid_token = valid_token
        self.headers: dict[str, str] = {}
        self.seen_tokens: list[str] = []

    def request(self, **_kwargs: Any) -> _FakeResponse:
        token = self.headers["Authorization"].removeprefix("Token token=")
        self.seen_tokens.append(token)
        if token == self.valid_token:
            return _FakeResponse(200, [])
        return _FakeResponse(401)


def _client(app, *, boot_token: str, disk_token: str | None, valid_token: str):
    reader = None if disk_token is None else (lambda: disk_token)
    client = app.ZammadClient("http://zammad.test", boot_token, token_reader=reader)
    session = _FakeSession(valid_token)
    session.headers.update(client.session.headers)
    client.session = session
    return client, session


def test_rereads_rotated_token_and_retries_once(app):
    client, session = _client(app, boot_token="old", disk_token="new", valid_token="new")

    assert client.get_ticket_articles(30) == []
    assert session.seen_tokens == ["old", "new"]
    # The fresh token sticks for the next call: no second round trip.
    client.get_ticket_articles(30)
    assert session.seen_tokens == ["old", "new", "new"]


def test_unchanged_token_still_fails_without_a_retry(app):
    client, session = _client(app, boot_token="old", disk_token="old", valid_token="new")

    try:
        client.get_ticket_articles(30)
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:  # pragma: no cover - the assertion below is the point
        raise AssertionError("a 401 with an unchanged token must still raise")
    assert session.seen_tokens == ["old"]


def test_service_wires_the_token_file_as_reader(app):
    assert app.SERVICE.zammad.token_reader is not None
    assert app.SERVICE.zammad.token_reader() == app.SERVICE.zammad.token
