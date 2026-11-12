"""Node-RED writes are opt-in (ADR-0007, security review of 0.29.0, finding H1).

A deployed flow runs arbitrary code inside the Node-RED add-on, which holds a
Supervisor token with the `manager` role (it could change nexus's own options,
e.g. lift `read_only`) and Node-RED's `credential_secret`. So the five
`nodered_*` write tools are hidden and refused unless the add-on option
`nodered_write_enabled` is true — the same Visibility mechanism `read_only`
uses (`policy.apply_policy`, enforced at `tools/call` too). The read tools
stay available.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
import yaml
from contract import generate_tool_surface as golden
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError

import policy
from tools import discover as discover_mod

_ROOT = Path(__file__).resolve().parents[1]
_WRITE_TOOLS = {
    "nodered_create_flow",
    "nodered_update_flow",
    "nodered_delete_flow",
    "nodered_set_flow_enabled",
    "nodered_restore_backup",
}


@pytest.fixture
def fresh_root():
    root = FastMCP("test-nodered-write-policy")
    mapping = golden.module_namespace_map((_ROOT / "server.py").read_text(encoding="utf-8"))
    for module_name, namespace in mapping.items():
        root.mount(importlib.import_module(f"tools.{module_name}").mcp, namespace=namespace)
    saved = (discover_mod._ROOT, discover_mod._INDEX, discover_mod._KNOWN_NAMESPACES)
    discover_mod.bind_root(root)
    try:
        yield root
    finally:
        discover_mod._ROOT, discover_mod._INDEX, discover_mod._KNOWN_NAMESPACES = saved


def _names(root: FastMCP) -> set[str]:
    return {t.name for t in asyncio.run(root.list_tools())}


def test_write_tools_are_exactly_the_non_read_nodered_tools(fresh_root):
    tools = asyncio.run(fresh_root.list_tools())
    non_read = {t.name for t in tools if t.name.startswith("nodered_") and not t.annotations.readOnlyHint}
    assert non_read == _WRITE_TOOLS


def test_default_policy_hides_nodered_writes(fresh_root):
    before = _names(fresh_root)

    application = policy.apply_policy(fresh_root, policy.ToolPolicy())

    assert _names(fresh_root) == before - _WRITE_TOOLS
    assert "nodered_list_flows" in _names(fresh_root)
    assert application.tools_disabled == len(_WRITE_TOOLS)


def test_hidden_write_tool_is_refused_with_the_reason(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy())

    with pytest.raises((ToolError, NotFoundError)) as excinfo:
        asyncio.run(fresh_root.call_tool("nodered_delete_flow", {"flow_id": "x"}))
    assert "nodered_write_enabled" in str(excinfo.value)


def test_opt_in_shows_nodered_writes(fresh_root):
    before = _names(fresh_root)

    policy.apply_policy(fresh_root, policy.ToolPolicy(nodered_write_enabled=True))

    assert _names(fresh_root) == before


def test_read_only_wins_over_opt_in(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True, nodered_write_enabled=True))

    assert not (_names(fresh_root) & _WRITE_TOOLS)


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), ("", False)])
def test_from_env_parses_the_option(raw, expected):
    result = policy.ToolPolicy.from_env({"NEXUS_NODERED_WRITE_ENABLED": raw}, known_namespaces=frozenset())
    assert result.nodered_write_enabled is expected


def test_from_env_defaults_to_disabled_and_rejects_garbage():
    assert policy.ToolPolicy.from_env({}, known_namespaces=frozenset()).nodered_write_enabled is False
    with pytest.raises(policy.PolicyConfigError):
        policy.ToolPolicy.from_env({"NEXUS_NODERED_WRITE_ENABLED": "sure"}, known_namespaces=frozenset())


def test_addon_option_default_false_and_passed_by_run_sh():
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert config["options"]["nodered_write_enabled"] is False
    assert config["schema"]["nodered_write_enabled"] == "bool"
    run_sh = (_ROOT / "run.sh").read_text(encoding="utf-8")
    assert "NEXUS_NODERED_WRITE_ENABLED" in run_sh
    translations = yaml.safe_load((_ROOT / "translations" / "en.yaml").read_text(encoding="utf-8"))
    assert "nodered_write_enabled" in translations["configuration"]
