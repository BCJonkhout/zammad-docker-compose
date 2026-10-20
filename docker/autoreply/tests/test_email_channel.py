"""E-mail channel support@ (docs/email-channel-support.md).

Three layers, all network-free:
* the autoreply never answers auto-replies, and marks its own escalation notice
  so the channel's postmaster filter can drop it (mail-loop protection);
* the signature helper keeps the house signature, the confidentiality footer and
  inlines the images, and fails loudly when the house-style file drifts;
* contract tests on bin/provision-email-channel.sh and bin/provision-zammad.sh for
  the lines that keep the rollout safe (plan is read-only, activate needs a go,
  AI skips e-mail in phase 1, no container restarts).

The behaviour against a real Zammad 7.0 is covered by
bin/email-channel-sandbox-scenarios.rb (throwaway instance, see the doc).
"""
from __future__ import annotations

import base64
import importlib.util
import re
import shutil
import subprocess
from email.message import EmailMessage

import pytest

SUBJECT = "Vraag"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


# --- autoreply: auto-replies ------------------------------------------------------------


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({"is-auto-response": True}, True),
        ({"is-auto-response": "true"}, True),
        ({"is-auto-response": False}, False),
        ({}, False),
        (None, False),
        ("not-a-dict", False),
    ],
)
def test_is_auto_response_article(app, preferences, expected):
    assert app.is_auto_response_article({"preferences": preferences}) is expected


class _FakeZammad:
    def __init__(self, preferences):
        self.preferences = preferences
        self.writes = []

    def get_ticket_articles(self, ticket_id):
        return [{"id": 9001, "sender": "Customer", "subject": SUBJECT, "body": "<p>Ik ben afwezig.</p>", "preferences": self.preferences}]

    def __getattr__(self, name):  # any write is a failure for the skip path
        def record(*args, **kwargs):
            self.writes.append(name)
            return {"id": 1}

        return record


def test_out_of_office_is_not_answered(app, monkeypatch):
    zammad = _FakeZammad({"is-auto-response": True})
    monkeypatch.setattr(app.SERVICE, "zammad", zammad)
    monkeypatch.setattr(app.SERVICE.litellm, "generate_decision", lambda **k: pytest.fail("LLM must not run"))
    result = app.SERVICE.process_ticket({"ticket": {"id": 4711, "title": SUBJECT}, "article": {"id": 9001}})
    assert result == {"status": "skipped", "reason": "auto_response_article", "ticket_id": 4711}
    assert zammad.writes == []


def test_escalation_notice_carries_loop_headers(app, monkeypatch):
    sent: list[EmailMessage] = []

    class _SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, **kwargs):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(app.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(app.SERVICE, "sendgrid_api_key", "test-key")
    assert app.SERVICE._notify_support_of_escalation(
        ticket={"number": "59999", "title": SUBJECT}, ticket_id=1, article_id=2, source_article={"from": "k@example.org", "body": "x"}
    )
    message = sent[0]
    assert message["X-Prudai-Mail-Kind"] == "ai-escalation"
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"


# --- signature helper ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def signature(repo_root):
    spec = importlib.util.spec_from_file_location("email_channel_signature", repo_root / "bin" / "email_channel_signature.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOUSE = (
    '<table><tr><td style="font-weight:700;">Het Prudai-team</td></tr>'
    '<tr><td><img src="https://cms.prudai.com/uploads/logo.png" width="132"></td></tr>'
    "<tr><td>Dit e-mailbericht en eventuele bijlagen zijn vertrouwelijk en uitsluitend bestemd voor de geadresseerde.</td></tr></table>"
)


def test_signature_renames_and_inlines(signature):
    fetched = []
    out = signature.render(HOUSE, "Prudai Support", fetch=lambda url: fetched.append(url) or PNG)
    assert ">Prudai Support</td>" in out and "Het Prudai-team" not in out
    assert f'src="data:image/png;base64,{base64.b64encode(PNG).decode()}"' in out
    assert "https://cms.prudai.com" not in out
    assert fetched == ["https://cms.prudai.com/uploads/logo.png"]
    assert "vertrouwelijk en uitsluitend bestemd voor" in out


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (HOUSE.replace("Het Prudai-team", "Iemand anders"), "exactly one name line"),
        (HOUSE.replace("Het Prudai-team</td>", "Het Prudai-team</td><td>Het Prudai-team</td>"), "exactly one name line"),
        (HOUSE.replace("vertrouwelijk en uitsluitend bestemd voor", "x"), "confidentiality footer"),
        (HOUSE.replace("https://cms.prudai.com/uploads/logo.png", "https://evil.example/logo.png"), "refusing to inline"),
    ],
)
def test_signature_fails_loudly(signature, source, message):
    with pytest.raises(signature.SignatureError, match=message):
        signature.render(source, fetch=lambda url: PNG)


def test_signature_rejects_non_png(signature):
    with pytest.raises(signature.SignatureError, match="not a PNG"):
        signature.render(HOUSE, fetch=lambda url: b"<html>")


def test_house_signature_file_still_matches(signature):
    """The real huisstijl file, when present on this host (it is not in CI)."""
    from pathlib import Path

    path = Path("/root/prudai-huisstijl/signature/out/support.html")
    if not path.exists():
        pytest.skip("huisstijl repo not on this host")
    out = signature.render(path.read_text(encoding="utf-8"), fetch=lambda url: PNG)
    assert ">Prudai Support</td>" in out
    assert "<img" in out and 'src="https://' not in out


# --- provisioning contracts -----------------------------------------------------------


@pytest.fixture(scope="module")
def email_script(repo_root) -> str:
    return (repo_root / "bin" / "provision-email-channel.sh").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def provision_script(repo_root) -> str:
    return (repo_root / "bin" / "provision-zammad.sh").read_text(encoding="utf-8")


def _ruby(script: str) -> str:
    return script.split("<<'RUBY'\n", 1)[1].split("\nRUBY\n", 1)[0]


def test_plan_is_a_rolled_back_transaction(email_script):
    ruby = _ruby(email_script)
    assert "ActiveRecord::Base.transaction do" in ruby
    assert "raise ActiveRecord::Rollback if MODE == 'plan'" in ruby
    assert "MODE != 'plan'" in ruby  # no remote Graph call while planning


def test_activate_requires_go_marker_and_safe_inbound(email_script):
    ruby = _ruby(email_script)
    block = ruby[ruby.index("when 'activate'\n      bad = ") : ruby.index("channel.active = true")]
    assert "bad << 'go_marker' if GO_MARKER.empty?" in block
    assert "keep_on_server" in block and "folder_id" in block and "shared_mailbox" in block
    assert 'raise "activate refused' in block
    assert "inbound['keep_on_server'] = true" in ruby


def test_loop_filters_drop_own_mail(email_script):
    ruby = _ruby(email_script)
    assert "'from' => { 'operator' => 'contains', 'value' => ADDRESS }" in ruby
    assert "'x-prudai-mail-kind' => { 'operator' => 'contains', 'value' => 'ai-escalation' }" in ruby
    assert "'x-zammad-ignore' => { 'value' => 'true' }" in ruby


def test_customer_notice_skips_email_articles_and_stays_internal(email_script):
    ruby = _ruby(email_script)
    block = ruby[ruby.index("name:      CUSTOMER_TRIGGER") : ruby.index("name:      STAFF_TRIGGER")]
    assert "'article.type_id'   => { 'operator' => 'is', 'value' => [TYPE_NOTE, TYPE_WEB] }" in block
    assert "'article.sender_id' => { 'operator' => 'is', 'value' => [SENDER_AGENT] }" in block
    assert "'internal'            => true" in block
    assert "active:    channel_live," in block


def test_staff_follow_up_trigger_is_phase_one_only(email_script):
    ruby = _ruby(email_script)
    assert "active:    channel_live && !AI_ON_EMAIL," in ruby


def test_email_script_restarts_nothing(email_script):
    assert not re.search(r"docker compose (up|restart|stop|down)|docker (restart|stop|rm)\b", email_script)


def test_ai_skips_email_by_default_in_both_scripts(email_script, provision_script):
    assert "ai_article_type_ids = ai_on_email ? [1, 5, 11] : [5, 11]" in provision_script
    assert provision_script.count("  article_type_ids: ai_article_type_ids,\n") == 2
    assert '-e ZAMMAD_AI_ON_EMAIL="${ZAMMAD_AI_ON_EMAIL:-0}"' in provision_script
    assert "(AI_ON_EMAIL ? [TYPE_EMAIL, TYPE_PHONE, TYPE_WEB] : [TYPE_PHONE, TYPE_WEB])" in email_script
    assert '-e ZAMMAD_AI_ON_EMAIL="${ZAMMAD_AI_ON_EMAIL:-0}"' in email_script


def test_provision_recreates_autoreply_without_dependencies(provision_script):
    recreate = [line for line in provision_script.splitlines() if "--force-recreate" in line and "zammad-autoreply" in line]
    assert recreate and all("--no-deps" in line for line in recreate)


def test_email_ruby_parses(email_script):
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    # The upstream image, or the Prudai image built on top of it (same Ruby).
    candidates = ["ghcr.io/zammad/zammad:7.0.0-0042", "prudai/zammad-support:7.0.0-0042"]
    present = [i for i in candidates if subprocess.run(["docker", "image", "inspect", i], capture_output=True, check=False).returncode == 0]
    if not present:
        pytest.skip("no Zammad 7.0.0-0042 image present locally")
    image = present[0]
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "ruby", image, "-c", "-"],
        input=_ruby(email_script),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
