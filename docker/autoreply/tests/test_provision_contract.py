"""Contract tests on bin/provision-zammad.sh (ZAM-1, ZAM-2).

The script is bash around an embedded Rails-runner script, so it cannot be
executed here (it writes to the live Zammad). These tests pin the security
relevant lines of the embedded Ruby instead, so a future edit that quietly
re-widens the service account or re-shares organizations turns red.
"""
from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def provision_script(repo_root) -> str:
    return (repo_root / "bin" / "provision-zammad.sh").read_text(encoding="utf-8")


def _block(script: str, start: str, end: str) -> str:
    begin = script.index(start)
    stop = script.index(end, begin)
    return script[begin:stop]


# --- ZAM-2: organizations.shared=false as the provisioning default ----------


def test_organization_shared_attribute_default_is_false(provision_script):
    block = _block(provision_script, "ZAM-2", "organizations_unshared = []")
    assert "ObjectManager::Attribute.get(object: 'Organization', name: 'shared')" in block
    assert "merge('default' => false)" in block


def test_existing_organizations_are_unshared_unless_allowlisted(provision_script):
    block = _block(provision_script, "organizations_unshared = []", "kb_nl = ensure_kb(")
    assert re.search(r"Organization\.where\(shared: true\)\.where\.not\(id: shared_organization_ids\)", block)
    assert "organization.update!(shared: false" in block


def test_shared_organization_allowlist_is_passed_into_the_runner(provision_script):
    assert '-e ZAMMAD_SHARED_ORGANIZATION_IDS="${ZAMMAD_SHARED_ORGANIZATION_IDS:-}"' in provision_script
    assert "ENV.fetch('ZAMMAD_SHARED_ORGANIZATION_IDS', '')" in provision_script
    assert "organizations_unshared: organizations_unshared" in provision_script


# --- embedded Ruby must at least parse -------------------------------------


def test_embedded_ruby_parses(repo_root):
    """``ruby -c`` on the heredoc, using the Zammad image's interpreter (read-only, no rails)."""
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    image = "ghcr.io/zammad/zammad:7.0.0-0042"
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode != 0:
        pytest.skip(f"{image} not present locally")

    script = (repo_root / "bin" / "provision-zammad.sh").read_text(encoding="utf-8")
    ruby = script.split("<<'RUBY'\n", 1)[1].split("\nRUBY\n", 1)[0]
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "ruby", image, "-c", "-"],
        input=ruby,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Syntax OK" in result.stdout


# --- ZAM-1: least-privilege autoreply service account ----------------------


@pytest.fixture(scope="module")
def autoreply_block(provision_script) -> str:
    return _block(provision_script, "autoreply_user = User.find_or_initialize_by", "staff_emails = ")


def test_autoreply_user_is_agent_only(autoreply_block):
    assert "autoreply_user.roles = [agent_role]" in autoreply_block
    assert "admin_role" not in autoreply_block, "the autoreply service user must not carry the Admin role"


def test_autoreply_token_is_scoped_to_what_app_py_calls(autoreply_block):
    assert "AUTOREPLY_TOKEN_PERMISSIONS = %w[ticket.agent knowledge_base.reader].freeze" in autoreply_block
    assert "permissions: AUTOREPLY_TOKEN_PERMISSIONS" in autoreply_block
    assert "autoreply_user.permissions.pluck" not in autoreply_block, "token scope must be explicit, not 'whatever the user has'"


def test_autoreply_admin_guard_and_rotation_are_wired(provision_script, autoreply_block):
    assert "if autoreply_user.permissions?('admin')" in autoreply_block
    assert "rotate:      autoreply_token_rotate" in autoreply_block
    assert '-e AUTOREPLY_TOKEN_ROTATE="${AUTOREPLY_TOKEN_ROTATE:-}"' in provision_script
    assert "if rotate && token\n    token.destroy!" in provision_script


def test_permission_list_matches_the_endpoints_app_py_uses(app):
    """Every Zammad path the client touches maps onto ticket.agent or knowledge_base.reader."""
    import inspect

    source = inspect.getsource(app.ZammadClient)
    paths = set(re.findall(r'"(/api/v1/[^"{?]+)', source))
    expected = {
        "/api/v1/ticket_articles/by_ticket/",
        "/api/v1/tags",
        "/api/v1/knowledge_bases/search",
        "/api/v1/tickets/",
        "/api/v1/ticket_articles",
        "/api/v1/tags/add",
    }
    assert paths == expected, f"app.py now calls other Zammad endpoints; re-check the token scope: {sorted(paths)}"
