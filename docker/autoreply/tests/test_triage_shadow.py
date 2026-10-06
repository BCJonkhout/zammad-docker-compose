"""Triage shadow: a decision model runs next to the LLM triage, logs only.

Contract under test:
* off (default, or flag on without URL) = no network call, no log line;
* any failure (exception, timeout, non-200, bad shape) = one error line, never raised;
* on = one ``triage.shadow`` line with LLM choices, decision-model choices +
  probabilities and agreement per field, and NO ticket text in it;
* the webhook result of ``process_ticket`` is identical with the shadow on or off.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import logging
import threading
import time
from pathlib import Path

import pytest

import triage_shadow as ts

SUBJECT = "Factuur klopt niet Jansen BV"
MESSAGE = "Beste support, mijn naam is Piet Jansen en factuur 2026-0042 is dubbel afgeschreven."
DOC_TITLE = "Facturen downloaden"
DOC_PREVIEW = "Ga naar Instellingen en kies Facturen om een pdf te downloaden."
SECRET_STRINGS = ("Jansen", "Piet", "2026-0042", "afgeschreven", "Facturen downloaden", "Instellingen")


def _answers(disposition="escalate", category="billing", p_high=0.83):
    return {
        "model": "decision-2.0-kai",
        "answers": {
            "disposition": {
                "type": "choice",
                "choice": disposition,
                "probabilities": {"reply_with_docs": 0.05, "handoff": 0.15, "escalate": 0.80},
                "confidence": 0.7,
            },
            "category": {
                "type": "choice",
                "choice": category,
                "probabilities": {
                    "how_to": 0.02, "bug": 0.03, "billing": 0.9, "security": 0.0,
                    "outage": 0.0, "account_access": 0.0, "data_issue": 0.03, "general": 0.02,
                },
                "confidence": 0.85,
            },
            "high_priority": {"type": "noul", "noul": p_high},
        },
        "usage": {"input_tokens": 300, "output_tokens": 20},
    }


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _shadow(post, **kw):
    return ts.TriageShadow(url="http://decision-service:8100", enabled=True, post=post, **kw)


def _run_kwargs(**over):
    kwargs = dict(
        ticket_id=4711,
        article_id=9001,
        first_article=True,
        ticket_title=SUBJECT,
        customer_message=MESSAGE,
        docs=[{"title": DOC_TITLE, "preview": DOC_PREVIEW}],
        llm={"disposition": "escalate", "category": "billing", "priority": "normal"},
        llm_ok=True,
        final={"disposition": "escalate", "category": "billing", "priority": "high"},
    )
    kwargs.update(over)
    return kwargs


def _shadow_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("triage.shadow ")]


# --- off ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},  # defaults
        {"DECISION_SERVICE_URL": "http://decision-service:8100"},  # URL alone: flag defaults off
        {"DECISION_SERVICE_URL": "http://decision-service:8100", "TRIAGE_DECISION_SHADOW": "off"},
        {"TRIAGE_DECISION_SHADOW": "on"},  # flag alone: empty URL = off
        {"TRIAGE_DECISION_SHADOW": "on", "DECISION_SERVICE_URL": "   "},
    ],
)
def test_from_env_is_off_unless_flag_and_url(monkeypatch, env):
    for key in ("DECISION_SERVICE_URL", "TRIAGE_DECISION_SHADOW", "DECISION_SERVICE_MODEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert ts.TriageShadow.from_env().enabled is False


def test_from_env_on(monkeypatch):
    monkeypatch.setenv("DECISION_SERVICE_URL", "http://decision-service:8100/")
    monkeypatch.setenv("TRIAGE_DECISION_SHADOW", "on")
    shadow = ts.TriageShadow.from_env()
    assert shadow.enabled is True
    assert shadow.url == "http://decision-service:8100"
    assert shadow.timeout <= 10


def test_off_makes_no_call_and_no_log(caplog):
    calls = []
    shadow = ts.TriageShadow(url="", enabled=True, post=lambda *a, **k: calls.append(1))
    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        assert shadow.submit(**_run_kwargs()) is False
    assert calls == [] and _shadow_lines(caplog) == []


# --- failures are silent -------------------------------------------------------------


@pytest.mark.parametrize(
    "post, expected_error",
    [
        (lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused " + MESSAGE)), "ConnectionError"),
        (lambda *a, **k: _Resp(500, text="echo: " + MESSAGE), "http_500"),
        (lambda *a, **k: _Resp(200, payload=ValueError("bad json " + MESSAGE)), "ValueError"),
        (lambda *a, **k: _Resp(200, payload={"answers": {}}), "disposition_missing"),
    ],
)
def test_failure_is_one_contentfree_error_line(caplog, post, expected_error):
    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        record = _shadow(post).run(**_run_kwargs())
    assert record["status"] == "error"
    assert record["error"] == expected_error
    lines = _shadow_lines(caplog)
    assert len(lines) == 1
    for secret in SECRET_STRINGS:
        assert secret not in lines[0]


def test_submit_never_raises_and_runs_in_background(caplog):
    started = threading.Event()

    def slow_failing_post(*a, **k):
        started.set()
        time.sleep(0.2)
        raise RuntimeError("boom")

    shadow = _shadow(slow_failing_post)
    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        t0 = time.monotonic()
        assert shadow.submit(**_run_kwargs()) is True
        assert time.monotonic() - t0 < 0.1, "submit blocked on the decision call"
        assert started.wait(2)
        shadow._executor.shutdown(wait=True)
    lines = _shadow_lines(caplog)
    assert len(lines) == 1 and '"status": "error"' in lines[0]


def test_real_http_timeout_is_an_error_not_a_hang(caplog):
    """A real socket against a server that never answers within the timeout."""

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            time.sleep(2.5)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        shadow = ts.TriageShadow(url=f"http://127.0.0.1:{server.server_address[1]}", enabled=True, timeout=1.0)
        with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
            t0 = time.monotonic()
            record = shadow.run(**_run_kwargs())
        assert time.monotonic() - t0 < 2.3
        assert record["status"] == "error" and record["error"] == "ReadTimeout"
    finally:
        server.shutdown()


def test_backlog_is_skipped_not_queued_forever(caplog, monkeypatch):
    monkeypatch.setattr(ts, "MAX_PENDING", 0)
    shadow = _shadow(lambda *a, **k: _Resp(200, payload=_answers()))
    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        assert shadow.submit(**_run_kwargs()) is False
    lines = _shadow_lines(caplog)
    assert len(lines) == 1 and '"reason": "backlog"' in lines[0]


# --- on ------------------------------------------------------------------------------


def test_on_logs_choices_probabilities_and_agreement_without_content(caplog):
    sent = {}

    def post(url, *, json=None, headers=None, timeout=None):  # noqa: A002
        sent.update(url=url, json=json, timeout=timeout)
        return _Resp(200, payload=_answers())

    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        _shadow(post).run(**_run_kwargs())

    # request: Jev shape, three questions, English instructions, same options as the LLM
    assert sent["url"] == "http://decision-service:8100/v1/systemone"
    assert sent["timeout"] <= 10
    questions = sent["json"]["questions"]
    assert questions["disposition"]["type"] == "choice"
    assert set(questions["disposition"]["criteria"]) == {"reply_with_docs", "handoff", "escalate"}
    assert questions["category"]["type"] == "choice"
    assert set(questions["category"]["criteria"]) == {
        "how_to", "bug", "billing", "security", "outage", "account_access", "data_issue", "general"
    }
    assert questions["high_priority"]["type"] == "noul"
    assert sent["json"]["state"]["ticket_subject"] == SUBJECT  # content goes to the service ...

    lines = _shadow_lines(caplog)
    assert len(lines) == 1
    for secret in SECRET_STRINGS:  # ... but never into the log
        assert secret not in lines[0], secret
    record = json.loads(lines[0][len("triage.shadow "):])
    assert record["status"] == "ok"
    assert record["ticket_id"] == 4711 and record["article_id"] == 9001
    assert record["llm"] == {"disposition": "escalate", "category": "billing", "priority": "normal"}
    assert record["dm"]["disposition"]["choice"] == "escalate"
    assert record["dm"]["disposition"]["p"]["escalate"] == 0.8
    assert record["dm"]["category"]["p"]["billing"] == 0.9
    assert record["dm"]["priority"] == {"choice": "high", "p_high": 0.83}
    assert record["agree"] == {"disposition": True, "category": True, "priority": False}
    assert record["agree_final"] == {"disposition": True, "category": True, "priority": True}
    assert isinstance(record["latency_ms"], int)


def test_state_is_clipped():
    body = ts.build_request(model="m", ticket_title="x" * 5000, customer_message="y" * 50000, docs=[])
    assert len(body["state"]["ticket_subject"]) <= ts.MAX_SUBJECT_CHARS
    assert len(body["state"]["customer_message"]) <= ts.MAX_MESSAGE_CHARS


# --- wiring in app.py ----------------------------------------------------------------


class _FakeZammad:
    def __init__(self):
        self.writes = []

    def get_ticket_articles(self, ticket_id):
        return [
            {"id": 9001, "sender": "Customer", "subject": SUBJECT, "body": f"<p>{MESSAGE}</p>", "preferences": {}},
        ]

    def get_ticket_tags(self, ticket_id):
        return []

    def update_ticket(self, ticket_id, **fields):
        self.writes.append(("update", fields))
        return {}

    def add_tag(self, ticket_id, tag):
        self.writes.append(("tag", tag))

    def create_public_reply(self, ticket_id, body, *, marker):
        self.writes.append(("public", marker))
        return {"id": 1}

    def create_internal_note(self, ticket_id, body, *, marker):
        self.writes.append(("note", marker))
        return {"id": 2}


def _process(app, monkeypatch, shadow):
    zammad = _FakeZammad()
    monkeypatch.setattr(app.SERVICE, "zammad", zammad)
    monkeypatch.setattr(app.SERVICE, "triage_shadow", shadow)
    monkeypatch.setattr(app.SERVICE, "_retrieve", lambda **k: [])
    monkeypatch.setattr(app.SERVICE, "_notify_support_of_escalation", lambda **k: False)
    monkeypatch.setattr(
        app.SERVICE.litellm,
        "generate_decision",
        lambda **k: {
            "disposition": "handoff", "category": "billing", "priority": "normal",
            "customer_reply_html": "", "internal_note_html": "<p>x</p>", "used_sources": [],
        },
    )
    result = app.SERVICE.process_ticket({"ticket": {"id": 4711, "title": SUBJECT}, "article": {"id": 9001}})
    return result, zammad.writes


def test_process_ticket_result_identical_with_shadow_on_or_off(app, monkeypatch, caplog):
    off_result, off_writes = _process(app, monkeypatch, ts.TriageShadow(url="", enabled=False))

    def exploding_post(*a, **k):
        raise RuntimeError("decision-service down")

    on_shadow = _shadow(exploding_post)
    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        on_result, on_writes = _process(app, monkeypatch, on_shadow)
        on_shadow._executor.shutdown(wait=True)

    assert on_result == off_result
    assert on_writes == off_writes
    assert "llm_triage" not in on_result and "llm_ok" not in on_result
    lines = _shadow_lines(caplog)
    assert len(lines) == 1
    record = json.loads(lines[0][len("triage.shadow "):])
    # The LLM said billing/handoff/normal; the policy rule ("factuur") made the applied decision escalate/high.
    assert record["llm"] == {"disposition": "handoff", "category": "billing", "priority": "normal"}
    assert record["final"] == {"disposition": "escalate", "category": "billing", "priority": "high"}
    assert record["llm_ok"] is True and record["first_article"] is True


def test_llm_failure_is_flagged(app, monkeypatch):
    seen = {}

    class Capture(ts.TriageShadow):
        def submit(self, **kwargs):
            seen.update(kwargs)
            return True

    def failing(**k):
        raise RuntimeError("litellm down")

    monkeypatch.setattr(app.SERVICE.litellm, "generate_decision", failing)
    decision = app.SERVICE._decide(ticket_title="t", customer_message="m", language="nl", results=[])
    assert decision["llm_ok"] is False

    monkeypatch.setattr(app.SERVICE, "triage_shadow", Capture(url="http://x", enabled=True))
    app.SERVICE._submit_triage_shadow(
        ticket_id=1, article_id=2, articles=[], ticket_title="t", customer_message="m", results=[], decision=decision
    )
    assert seen["llm_ok"] is False


# --- report script -------------------------------------------------------------------


def _load_report(repo_root: Path):
    spec = importlib.util.spec_from_file_location("triage_shadow_report", repo_root / "bin" / "triage-shadow-report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_summarises_agreement_and_disagreement_probs(repo_root, caplog, capsys):
    shadow = _shadow(lambda *a, **k: _Resp(200, payload=_answers()))
    with caplog.at_level(logging.INFO, logger="prudai-autoreply"):
        shadow.run(**_run_kwargs())  # disposition+category agree, priority not
        shadow.run(**_run_kwargs(ticket_id=4712, llm={"disposition": "handoff", "category": "bug", "priority": "high"}))
        shadow.run(**_run_kwargs(ticket_id=4713, llm_ok=False))  # excluded vs llm
        _shadow(lambda *a, **k: _Resp(503)).run(**_run_kwargs(ticket_id=4714))
    lines = [f"2026-10-06 12:00:00,000 INFO {r.getMessage()}" for r in caplog.records] + ["noise line"]

    report = _load_report(repo_root)
    summary = report.summarise(report.parse(lines), vs="llm", first_only=False)
    assert summary["records_compared"] == 2
    assert summary["fields"]["disposition"]["agreed"] == 1
    assert summary["fields"]["category"]["agreement"] == 0.5
    assert summary["fields"]["priority"]["agreed"] == 1  # 4712 said high, dm high
    dis = summary["fields"]["disposition"]["disagreements"][0]
    assert dis["ticket_id"] == 4712 and "escalate=0.80" in dis["dm_probs"]
    assert summary["errors"] == {"http_503": 1}

    report.print_text(summary, max_examples=5)
    out = capsys.readouterr().out
    assert "disposition: 1/2 agree (50.0%)" in out


def test_compose_defaults_keep_shadow_off(repo_root):
    text = (repo_root / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert "TRIAGE_DECISION_SHADOW: ${TRIAGE_DECISION_SHADOW:-off}" in text
    assert "DECISION_SERVICE_URL: ${DECISION_SERVICE_URL:-}" in text
    dockerfile = (repo_root / "docker" / "autoreply" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY triage_shadow.py /app/triage_shadow.py" in dockerfile
