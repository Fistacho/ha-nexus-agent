"""Registry test for the shared `confirm` safety gate (ADR-0003 D1).

Seventeen destructive tools across seven modules refuse to run without
`confirm=True` and, when refused, all return the exact same shape:
`{"error": "confirmation_required", "message": ..., "action": ...}`, with
zero I/O performed before that check. This is the common convention
established in `tools/system.py` (`restart_ha`, `stop_ha`) and now applied to
every tool in the registry below — including the two that gained `confirm`
in 0.22.0 (`supervisor_delete_backup`, `files_delete_config_file`), the one
whose refusal shape was unified (`automations_delete_scene`, previously
`{"error": "set confirm=True to delete", "command": ...}`), and
`esphome_upload_device` (ADR-0004 D5, 0.23.0) — it flashes firmware with no
automatic rollback and, in add-on mode, never actually reached the dashboard
before ADR-0004's rewrite, so this is the first release where it does
anything at all.

This test does not enumerate every `delete_*`/`remove_*`/`stop_*` tool in the
add-on — ADR-0003 D1 deliberately leaves the rest (e.g. `areas_delete_area`,
`supervisor_stop_addon`) without `confirm` until a follow-up ADR. The
registry here is exactly the 17 tools that count as `confirm`-guarded after
this change (16 pre-existing + 1 new).
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

import ha_client as ha

# (module under tools/, local tool name, kwargs needed besides `confirm`)
_REGISTRY: list[tuple[str, str, dict[str, Any]]] = [
    ("automations", "delete_scene", {"scene_id": "movie_night"}),
    ("automations", "delete_automation", {"automation_id": "1733294456585"}),
    ("automations", "delete_script", {"script_id": "goodnight"}),
    ("automations", "remove_group", {"group_id": "living_room"}),
    ("dashboards", "remove_dashboard_resource", {"resource_id": 1}),
    ("git_ops", "git_rollback_file", {"relative_path": "automations.yaml"}),
    ("git_ops", "git_rollback_to_commit", {"sha": "abc1234"}),
    ("integrations", "remove_integration", {"entry_id": "01ABCDEFGH"}),
    ("supervisor", "uninstall_addon", {"slug": "core_mosquitto"}),
    ("supervisor", "restart_core", {}),
    ("supervisor", "restart_host", {}),
    ("supervisor", "restore_backup", {"slug": "abcd1234"}),
    ("supervisor", "delete_backup", {"slug": "abcd1234"}),
    ("system", "restart_ha", {}),
    ("system", "stop_ha", {}),
    ("files", "delete_config_file", {"relative_path": "packages/old.yaml"}),
    ("esphome", "upload_device", {"name": "kitchen_sensor"}),
]
_IDS = [f"{mod}.{name}" for mod, name, _ in _REGISTRY]


def _tool(module_name: str, local_name: str):
    """FastMCP FunctionTool for one `tools/<module>.py::<local_name>`."""
    module = importlib.import_module(f"tools.{module_name}")
    tools = {t.name: t for t in asyncio.run(module.mcp.list_tools())}
    return tools[local_name]


@pytest.fixture(autouse=True)
def _forbid_io(monkeypatch):
    """Fail loudly if any I/O helper runs before the `confirm=False` gate.

    Covers every transport the registry's tools use: `ha_client`'s HTTP
    client and WS call for automations/dashboards/integrations/system,
    Supervisor's own request helper, git_ops's repo accessor, `Path.unlink`
    for files.delete_config_file, and the ESPHome dashboard client
    singleton for esphome.upload_device.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError("I/O reached before the confirm=False gate")

    monkeypatch.setattr(ha, "_client", _boom)
    monkeypatch.setattr(ha, "_ws_call", _boom)

    supervisor = importlib.import_module("tools.supervisor")
    monkeypatch.setattr(supervisor, "_supervisor_request", _boom)

    git_ops = importlib.import_module("tools.git_ops")
    monkeypatch.setattr(git_ops, "_repo", _boom)

    monkeypatch.setattr(Path, "unlink", _boom)

    esphome = importlib.import_module("tools.esphome")
    monkeypatch.setattr(esphome, "_get_dashboard_client", _boom)
    monkeypatch.setattr(esphome, "_get_dashboard_locator", _boom)


@pytest.mark.parametrize("module_name, local_name, kwargs", _REGISTRY, ids=_IDS)
def test_confirm_false_returns_common_shape_without_io(module_name, local_name, kwargs):
    fn = _tool(module_name, local_name).fn
    result = fn(confirm=False, **kwargs)
    assert result == {
        "error": "confirmation_required",
        "message": result.get("message"),
        "action": result.get("action"),
    }
    assert isinstance(result["message"], str) and result["message"]
    assert isinstance(result["action"], str) and "confirm=True" in result["action"]


@pytest.mark.parametrize("module_name, local_name, kwargs", _REGISTRY, ids=_IDS)
def test_confirm_omitted_defaults_to_false(module_name, local_name, kwargs):
    fn = _tool(module_name, local_name).fn
    result = fn(**kwargs)
    assert result["error"] == "confirmation_required"


@pytest.mark.parametrize("module_name, local_name, kwargs", _REGISTRY, ids=_IDS)
def test_confirm_schema_is_optional_boolean_default_false(module_name, local_name, kwargs):
    tool = _tool(module_name, local_name)
    schema = tool.parameters or {}
    props = schema.get("properties", {})
    assert "confirm" in props, f"{module_name}.{local_name} has no `confirm` parameter"
    assert props["confirm"].get("type") == "boolean"
    assert props["confirm"].get("default") is False
    assert "confirm" not in (schema.get("required") or [])
