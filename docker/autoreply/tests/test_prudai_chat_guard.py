"""Platform chat tickets must never enter the legacy autoreply flow."""
from unittest.mock import Mock, call

import pytest


@pytest.fixture
def service(app, monkeypatch):
    zammad = Mock(spec=app.ZammadClient)
    zammad.get_ticket_tags.return_value = []
    zammad.get_ticket_articles.return_value = [
        {"id": 12, "sender": "Customer", "subject": "Vraag", "body": "<p>Hallo</p>"},
    ]
    zammad.create_internal_note.return_value = {"id": 22}
    monkeypatch.setattr(app.SERVICE, "zammad", zammad)
    monkeypatch.setattr(app.SERVICE, "_retrieve", Mock(return_value=[]))
    monkeypatch.setattr(app.SERVICE, "_submit_triage_shadow", Mock())
    monkeypatch.setattr(app.SERVICE, "_notify_support_of_escalation", Mock(return_value=False))
    monkeypatch.setattr(
        app.SERVICE.litellm,
        "generate_decision",
        Mock(return_value={
            "disposition": "handoff", "category": "general", "priority": "normal",
            "customer_reply_html": "", "internal_note_html": "<p>Support nodig</p>", "used_sources": [],
        }),
    )
    return app.SERVICE


@pytest.mark.parametrize("snapshot_tags", [None, [], ["ordinary"]])
def test_current_chat_tag_skips_before_articles_ai_and_writes(service, snapshot_tags):
    service.zammad.get_ticket_tags.return_value = ["ai-escalate", "prudai-chat"]
    # Missing article metadata and a stale snapshot must not defeat the guard.
    result = service.process_ticket({"ticket": {"id": 7, "tags": snapshot_tags}})

    assert result == {"status": "skipped", "reason": "prudai_chat", "ticket_id": 7}
    assert service.zammad.mock_calls == [call.get_ticket_tags(7)]
    service._retrieve.assert_not_called()
    service.litellm.generate_decision.assert_not_called()
    service._submit_triage_shadow.assert_not_called()
    service._notify_support_of_escalation.assert_not_called()


def test_snapshot_tag_does_not_block_ordinary_customer_ticket(service):
    result = service.process_ticket({
        "ticket": {"id": 7, "title": "Vraag", "tags": ["prudai-chat"]},
        "article": {"id": 12},
    })

    assert result["status"] == "ok"
    assert result["internal_article_id"] == 22
    assert service.zammad.mock_calls[:2] == [call.get_ticket_tags(7), call.get_ticket_articles(7)]
    service._retrieve.assert_called_once()
    service.litellm.generate_decision.assert_called_once()
    service.zammad.create_internal_note.assert_called_once()


def test_unavailable_current_tags_fail_before_processing(service):
    service.zammad.get_ticket_tags.side_effect = RuntimeError("tag lookup unavailable")

    with pytest.raises(RuntimeError, match="tag lookup unavailable"):
        service.process_ticket({"ticket": {"id": 7}, "article": {"id": 12}})

    assert service.zammad.mock_calls == [call.get_ticket_tags(7)]
    service._retrieve.assert_not_called()
    service.litellm.generate_decision.assert_not_called()
