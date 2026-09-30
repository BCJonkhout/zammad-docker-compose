"""Pytest wiring for the autoreply service.

``app.py`` builds ``SERVICE = AutoreplyService()`` at import time and that
constructor reads two secret files plus a handful of environment variables
(fail-closed by design, see ZAM-7).  The tests never talk to Zammad, LiteLLM
or SendGrid, so we point every setting at throw-away values *before* the
module is imported.  Nothing here touches the live stack.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

AUTOREPLY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AUTOREPLY_DIR.parents[1]

if str(AUTOREPLY_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOREPLY_DIR))

_SECRET_DIR = Path(tempfile.mkdtemp(prefix="prudai-autoreply-test-"))
(_SECRET_DIR / "autoreply.token").write_text("test-zammad-api-token\n", encoding="utf-8")
(_SECRET_DIR / "autoreply-webhook.token").write_text("test-webhook-token\n", encoding="utf-8")

_TEST_ENV = {
    "ZAMMAD_PUBLIC_BASE_URL": "https://support.example.test",
    "ZAMMAD_INTERNAL_BASE_URL": "http://zammad-nginx.test:8080",
    "ZAMMAD_AUTOREPLY_TOKEN_FILE": str(_SECRET_DIR / "autoreply.token"),
    "ZAMMAD_AUTOREPLY_WEBHOOK_TOKEN_FILE": str(_SECRET_DIR / "autoreply-webhook.token"),
    "LITELLM_BASE_URL": "http://litellm.test:4000/v1",
    "LITELLM_MASTER_KEY": "test-litellm-key",
    "LITELLM_MODEL": "test-model",
    "SENDGRID_API_KEY_FILE": str(_SECRET_DIR / "missing-sendgrid.key"),
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

WEBHOOK_TOKEN = "test-webhook-token"


@pytest.fixture(scope="session")
def app():
    """The imported ``app`` module (imported once, after the env is prepared)."""
    return importlib.import_module("app")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
