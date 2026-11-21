"""server.json is what the MCP Registry (first publish at 1.0.0, ADR-0005)
and every client installing from it read. The policy env vars a standalone
user is told about in README/DOCS must be declared there too, as optional
inputs, so registry clients can offer them.

The schema itself is checked by `mcp-publisher validate` in
.github/workflows/mcp-registry.yml; this file only pins nexus's own
choices.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _env_vars() -> dict[str, dict]:
    data = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    (package,) = data["packages"]
    return {v["name"]: v for v in package["environmentVariables"]}


def test_required_connection_env_vars():
    env = _env_vars()
    assert env["HA_URL"]["isRequired"] is True
    assert env["HA_TOKEN"]["isRequired"] is True
    assert env["HA_TOKEN"]["isSecret"] is True


@pytest.mark.parametrize("name", ["NEXUS_READ_ONLY", "NEXUS_DISABLED_NAMESPACES"])
def test_policy_env_vars_are_declared_optional(name):
    var = _env_vars()[name]
    assert var["isRequired"] is False
    assert var["isSecret"] is False
    assert var["description"]


def test_read_only_is_a_boolean_defaulting_to_false():
    var = _env_vars()["NEXUS_READ_ONLY"]
    assert var["format"] == "boolean"
    assert var["default"] == "false"


def test_every_declared_env_var_is_read_by_run_sh_or_python():
    run_sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    sources = run_sh + (ROOT / "auth.py").read_text(encoding="utf-8") + (ROOT / "ha_client.py").read_text(encoding="utf-8")
    for name in _env_vars():
        assert name in sources, name
