"""ZAM-5: Elasticsearch authentication is gated behind ELASTICSEARCH_SECURITY_ENABLED.

Runs ``docker compose config`` (read-only rendering, own project name, no
containers touched) against the base compose file with and without the gate.
Skipped when Docker is not on the PATH. On the pre-fix compose file the value
is hard-wired to 'false', so the "enabled" case fails there.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest


def _render(repo_root, env: dict[str, str]) -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    result = subprocess.run(
        ["docker", "compose", "-p", "ci-zammad-pentest", "-f", "docker-compose.yml", "config"],
        cwd=repo_root,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _es_block(rendered: str) -> str:
    match = re.search(r"\n  zammad-elasticsearch:\n(.*?)(?=\n  \S)", rendered, flags=re.DOTALL)
    assert match, rendered
    return match.group(1)


def _env_value(block: str, key: str) -> str:
    match = re.search(rf"^\s+{re.escape(key)}: (.*)$", block, flags=re.MULTILINE)
    assert match, f"{key} missing in:\n{block}"
    return match.group(1).strip().strip("'\"")


def test_default_keeps_security_off(repo_root):
    block = _es_block(_render(repo_root, {"ELASTICSEARCH_SECURITY_ENABLED": "", "ELASTICSEARCH_PASS": ""}))
    assert _env_value(block, "xpack.security.enabled") == "false"


def test_gate_enables_security_with_bootstrap_password(repo_root):
    block = _es_block(
        _render(repo_root, {"ELASTICSEARCH_SECURITY_ENABLED": "true", "ELASTICSEARCH_PASS": "dummy-test-password"})
    )
    assert _env_value(block, "xpack.security.enabled") == "true"
    assert _env_value(block, "ELASTIC_PASSWORD") == "dummy-test-password"
    # plain HTTP inside the compose network: Zammad keeps ELASTICSEARCH_SCHEMA=http
    assert _env_value(block, "xpack.security.http.ssl.enabled") == "false"
    assert _env_value(block, "xpack.security.transport.ssl.enabled") == "false"


def test_no_scenario_maps_elasticsearch_to_the_host_by_default(repo_root):
    """The base file must not publish 9200; only the opt-in scenario file may."""
    block = _es_block(_render(repo_root, {}))
    assert "ports:" not in block
    scenario = (repo_root / "scenarios" / "add-hostport-to-elasticsearch.yml").read_text(encoding="utf-8")
    assert "9200" in scenario  # the only place a host port for ES may live
