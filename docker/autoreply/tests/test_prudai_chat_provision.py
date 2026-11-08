"""Offline executable tests of the Rails extension and provisioning contract.

Ruby stubs isolate the two Zammad mail entry points; no Rails process, database,
Docker container or mail provider is started. Selectors are tested as data with
the operator semantics used by Zammad 7.0 (including nested AND/OR).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


RUBY_STUBS = """
require 'json'
module Transaction
  class Notification
    def send_to_single_recipient_email(*args)
      'agent mail'
    end
  end
end
class Ticket
  module PerformChanges
    module Action
      class NotificationEmail
        attr_accessor :record
        def execute(*args)
          'automatic mail'
        end
      end
    end
  end
end
module Rails
  def self.application; self; end
  def self.config; self; end
  def self.to_prepare(&block); block.call; end
end
"""


def ruby(repo_root, code):
    extension = Path(os.environ.get("PRUDAI_CHAT_RUBY_PATH", repo_root / "docker/zammad-assets/prudai_chat.rb"))
    result = subprocess.run(
        ["ruby", "-", str(extension)],
        input=RUBY_STUBS + "\nload ARGV.fetch(0)\n" + code,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def selector(repo_root):
    return ruby(repo_root, """
puts JSON.generate(PrudaiChat.reply_condition(
  agent_sender_id: 1, system_sender_id: 3, email_type_id: 1,
  excluded_user_ids: [1, 90, 91, 92]))
""")


def matches(condition, event):
    if "conditions" not in condition and "name" not in condition:
        return all(matches(rule | {"name": name}, event) for name, rule in condition.items())
    if "conditions" in condition:
        results = [matches(child, event) for child in condition["conditions"]]
        return all(results) if condition["operator"] == "AND" else any(results)
    actual = event.get(condition["name"])
    operator = condition["operator"]
    expected = condition["value"]
    if operator in {"is", "is not"}:
        result = actual in expected if isinstance(expected, list) else actual == expected
        return result if operator == "is" else not result
    if operator == "contains all":
        return all(tag in actual for tag in expected.split(","))
    if operator == "contains one not":
        return not any(tag in actual for tag in expected.split(","))
    if operator in {"matches regex", "does not match regex"}:
        if actual is None:
            return False  # PostgreSQL regex comparison with NULL is not true.
        result = re.search(expected, actual or "") is not None
        return result if operator == "matches regex" else not result
    raise AssertionError(f"Unhandled Zammad operator: {operator}")


BASE_EVENT = {
    "ticket.tags": ["prudai-chat", "other"],
    "article.action": "create",
    "article.internal": False,
    "article.sender_id": 1,
    "article.created_by_id": 45,
    "article.type_id": 5,
    "article.from": "staff@prudai.com",
}


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, True),
    ({"article.from": None}, True),  # public agent note
    ({"article.sender_id": 2, "article.type_id": 1}, True),
    ({"article.sender_id": 2, "article.type_id": 1, "article.created_by_id": 1}, True),
    ({"article.sender_id": 2, "article.type_id": 1, "article.from": '"Staff" <STAFF@PRUDAI.COM>'}, True),
    ({"ticket.tags": []}, False),
    ({"ticket.tags": ["prudai-chat-other"]}, False),
    ({"article.action": None}, False),
    ({"article.internal": True}, False),
    ({"article.sender_id": 3, "article.type_id": 1}, False),
    ({"article.sender_id": 2, "article.type_id": 1, "article.from": "customer@example.test"}, False),
    ({"article.sender_id": 2, "article.type_id": 5}, False),
    ({"article.created_by_id": 90, "article.from": None}, False),
    ({"article.created_by_id": 91, "article.from": None}, False),
    ({"article.created_by_id": 92, "article.from": None}, False),
    ({"article.created_by_id": 1, "article.from": None}, False),
    ({"article.from": "support@prudai.com", "article.type_id": 1}, False),
    ({"article.from": "no-reply@prudai.com", "article.type_id": 1}, False),
    ({"article.sender_id": 2, "article.type_id": 1, "article.from": '"Support" <SUPPORT@prudai.com>'}, False),
    ({"article.sender_id": 2, "article.type_id": 1, "article.from": "staff@prudai.com.attacker.test"}, False),
    ({"article.sender_id": 2, "article.type_id": 1, "article.from": "staff@evilprudai.com"}, False),
])
def test_only_public_staff_replies_match(selector, overrides, expected):
    assert matches(selector, BASE_EVENT | overrides) is expected


def test_null_sender_uses_zammad_string_selector_syntax(selector):
    null_condition = selector["conditions"][4]["conditions"][0]
    assert null_condition == {"name": "article.from", "operator": "is", "value": None}


@pytest.mark.parametrize("expert", [False, True])
def test_email_exclusion_preserves_conditions_and_is_idempotent(repo_root, expert):
    condition = {"ticket.group_id": {"operator": "is", "value": 7}}
    if expert:
        condition = {"operator": "OR", "conditions": [
            {"name": "ticket.group_id", "operator": "is", "value": 7},
            {"name": "ticket.group_id", "operator": "is", "value": 8},
        ]}
    data = ruby(repo_root, """
original = JSON.parse(%s)
once = PrudaiChat.exclude(original)
puts JSON.generate([once, PrudaiChat.exclude(once), original])
""" % json.dumps(json.dumps(condition)))
    once, twice, original = data
    assert once == twice
    assert original == condition
    for group in [7, 8, 9]:
        event = {"ticket.group_id": group, "ticket.tags": []}
        assert matches(once, event) == matches(condition, event)
        assert not matches(once, event | {"ticket.tags": ["prudai-chat"]})


def test_email_boundaries_cover_all_agents_and_automatic_recipients(repo_root):
    result = ruby(repo_root, """
ticket_class = Struct.new(:tag_list)
notification = Transaction::Notification.new
automatic = Ticket::PerformChanges::Action::NotificationEmail.new
output = []
[[], ['other'], ['prudai-chat']].each do |tags|
  ticket = ticket_class.new(tags)
  automatic.record = ticket
  output << [notification.send(:send_to_single_recipient_email, :any_agent, ticket, nil, {}), automatic.execute]
end
# Rails to_prepare may run again; reloading must not stack wrappers.
load ARGV.fetch(0)
output << Transaction::Notification.ancestors.count(PrudaiChat::AgentEmail)
puts JSON.generate(output)
""")
    assert result == [["agent mail", "automatic mail"], ["agent mail", "automatic mail"], [None, None], 1]


def test_provisioning_secrets_permissions_and_runtime_contract(repo_root):
    script = (repo_root / "bin/provision-zammad.sh").read_text()
    assert 'secrets/prudai-chat-webhook.token' in script
    assert 'ensure_secret_file "${prudai_chat_webhook_token_path}"' in script
    assert 'PRUDAI_CHAT_WEBHOOK_URL:-https://api.prudai.com/support/zammad/webhook' in script
    assert "chat_webhook.bearer_token = ENV.fetch('PRUDAI_CHAT_WEBHOOK_BEARER_TOKEN')" in script
    assert "chat_webhook.ssl_verify = true" in script
    assert "chat_webhook.customized_payload = false" in script
    block = script.split("platform_user = User.find_or_initialize_by", 1)[1].split("staff_emails =", 1)[0]
    assert "platform_user.roles = [agent_role]" in block
    assert "platform_user.group_names_access_map = { support_group.name => %w[read create change] }" in block
    assert "permissions: ['ticket.agent']" in block
    assert "if platform_user.permissions?('admin')" in block
    assert "platform_user.group_ids_access(%w[read create change overview]).sort == [support_group.id]" in block
    assert "!platform_user.group_access?(support_group, %w[full overview])" in block
    assert "rotate: ENV.fetch('PRUDAI_PLATFORM_TOKEN_ROTATE', '') == '1'" in block
    assert 'platform_token_path = root / "secrets" / "prudai-platform.token"' in script
    assert 'sanitized.pop("platform_token", None)' in script
    assert 'os.chmod(platform_token_path, 0o600)' in script
    assert 'root = Path(os.environ["ROOT_DIR"])' in script
    assert "Trigger.all.each do |trigger|" in script
    assert "perform.key?('notification.email')" in script
    assert "PrudaiChat.exclude((trigger.condition || {}).deep_stringify_keys)" in script
    assert "PrudaiChat.reply_condition(" in script
    assert "Build and roll out the Prudai-chat Zammad image first." in script
    assert 'PRUDAI_PROVISION_RESTART_AUTOREPLY:-1' in script
    assert 'up -d --build --force-recreate --no-deps zammad-autoreply' in script
    dockerfile = (repo_root / "docker/zammad-assets/Dockerfile").read_text()
    assert 'prudai_chat.rb /opt/zammad/config/initializers/prudai_chat.rb' in dockerfile


def test_local_syntax_without_running_provisioning(repo_root):
    script = (repo_root / "bin/provision-zammad.sh").read_text()
    ruby_source = script.split("<<'RUBY'\n", 1)[1].split("\nRUBY\n", 1)[0]
    for command, source in [(["bash", "-n"], script), (["ruby", "-c"], ruby_source)]:
        result = subprocess.run(command, input=source, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
