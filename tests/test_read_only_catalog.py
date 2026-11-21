"""`read_only` (ADR-0003 P1) against the FULL registered tool catalogue.

`tests/test_tool_policy.py` proves the mechanism: under `read_only` the
visible set equals whatever carries `readOnlyHint=True`. That check is
tautological with respect to the annotations themselves -- a new tool that
writes but is (wrongly) annotated `read()` would pass it, and nothing else
in the suite would notice: `tests/contract/tool_surface.json` (T1) records
input schemas only, not annotations.

This module closes that gap with two independent checks on top of the
annotation-driven mechanism (no second enforcement path -- enforcement stays
in `policy.apply_policy()`):

1. A reviewed allowlist, `tests/contract/read_only_tools.json`. The set of
   tools left visible under `read_only` must equal it exactly. Adding a
   `read()` tool (or flipping an annotation to `read()`) therefore fails
   here until someone adds the name to the file on purpose -- the point at
   which ADR-0003 ("every change of an annotation to `read` goes through
   Panel Security review") becomes a visible diff instead of a silent one.
2. A name-based tripwire: no tool visible under `read_only` may carry a
   mutating verb in its local name, independent of what its annotation
   says.

Plus explicit checks that the generic pass-through tools (arbitrary service
calls, event firing, raw state writes) and the 0.29.0 additions are hidden
and refused at `tools/call`.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from pathlib import Path

import pytest
from contract import generate_tool_surface as golden
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.tools import Tool

import policy
import server
from tools import _contract
from tools import discover as discover_mod
from tools.discover import _namespace_for

_NEXUS_ROOT = Path(__file__).resolve().parents[1]
_ALLOWLIST_PATH = Path(__file__).resolve().parent / "contract" / "read_only_tools.json"


def _mount_everything(root: FastMCP) -> None:
    server_py_text = (_NEXUS_ROOT / "server.py").read_text(encoding="utf-8")
    for module_name, namespace in golden.module_namespace_map(server_py_text).items():
        module = importlib.import_module(f"tools.{module_name}")
        root.mount(module.mcp, namespace=namespace)


@pytest.fixture
def fresh_root():
    """Same isolation as tests/test_tool_policy.py: a throwaway root with
    every real tool module mounted, so `server.mcp` itself is never filtered."""
    root = FastMCP("test-nexus-read-only-catalog")
    _mount_everything(root)
    original = (discover_mod._ROOT, discover_mod._INDEX, discover_mod._KNOWN_NAMESPACES)
    discover_mod.bind_root(root)
    try:
        yield root
    finally:
        discover_mod._ROOT, discover_mod._INDEX, discover_mod._KNOWN_NAMESPACES = original


def _names(root: FastMCP) -> set[str]:
    return {t.name for t in asyncio.run(root.list_tools())}


def _reviewed_allowlist() -> set[str]:
    data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return set(data["tools"])


def _visible_under_read_only(root: FastMCP) -> set[str]:
    policy.apply_policy(root, policy.ToolPolicy(read_only=True))
    return _names(root)


# Verbs that, at the start of a tool's local name (namespace prefix and the
# `git_`/`lvgl_` subgroup prefixes stripped), mean the tool changes
# something. A read-only tool must never be named like this; if one ever
# legitimately needs to be, add it to `_VERB_EXCEPTIONS` with a reason.
_MUTATING_VERBS = (
    "abort", "activate", "add", "append", "apply", "assign", "bulk", "call",
    "cancel", "checkout", "clean", "commit", "compile", "control", "create",
    "delete", "disable", "dismiss", "enable", "execute", "fire", "flash",
    "increment", "init", "install", "patch", "press", "purge", "reload",
    "remove", "rename", "reset", "restart", "restore", "rollback", "run",
    "safe_write", "save", "select", "send", "set", "start", "stop", "submit",
    "toggle", "trigger", "turn", "uninstall", "update", "upload", "write",
)
_MUTATING_RE = re.compile(rf"^(?:{'|'.join(_MUTATING_VERBS)})(?:_|$)")
_VERB_EXCEPTIONS: dict[str, str] = {}


def _local_name(full_name: str) -> str:
    ns = _namespace_for(full_name)
    local = full_name[len(ns) + 1:] if ns else full_name
    for prefix in (f"{ns}_", "git_", "lvgl_"):
        if local.startswith(prefix) and len(local) > len(prefix):
            local = local[len(prefix):]
    return local


def test_fresh_root_catalogue_is_the_registered_server_catalogue(fresh_root):
    """The root this module filters mounts exactly what `server.py` registers."""
    assert _names(fresh_root) == {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_read_only_visible_set_equals_reviewed_allowlist(fresh_root):
    visible = _visible_under_read_only(fresh_root)
    reviewed = _reviewed_allowlist()

    new_unreviewed = sorted(visible - reviewed)
    no_longer_read = sorted(reviewed - visible)
    assert not new_unreviewed and not no_longer_read, (
        "The set of tools exposed in read_only mode changed.\n"
        f"  Now visible but not in {_ALLOWLIST_PATH.name} (annotated read() -- "
        f"confirm each one cannot change anything, then add it): {new_unreviewed}\n"
        f"  In {_ALLOWLIST_PATH.name} but no longer visible (remove them): {no_longer_read}\n"
        "ADR-0003: every change of an annotation to `read` needs Panel Security review."
    )


def test_allowlist_has_no_duplicates_and_is_sorted():
    names = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))["tools"]
    assert names == sorted(set(names))


def test_no_tool_visible_in_read_only_has_a_mutating_name(fresh_root):
    offenders = sorted(
        name
        for name in _visible_under_read_only(fresh_root)
        if _MUTATING_RE.match(_local_name(name)) and name not in _VERB_EXCEPTIONS
    )
    assert offenders == []


def test_every_visible_tool_is_annotated_read_and_every_hidden_one_is_not(fresh_root):
    catalogue = {t.name: t for t in asyncio.run(fresh_root.list_tools())}
    visible = _visible_under_read_only(fresh_root)

    for name, tool in catalogue.items():
        is_read = tool.annotations is not None and tool.annotations.readOnlyHint is True
        assert (name in visible) == is_read, name


# Generic pass-throughs whose effect depends on the arguments, plus the
# 0.29.0 additions: none of them may survive read_only, whatever arguments
# a client sends.
_MUST_BE_HIDDEN = [
    ("services_call_service", {"domain": "light", "service": "turn_on"}),
    ("ws_call_service", {"domain": "light", "service": "turn_on"}),
    ("services_fire_event", {"event_type": "x"}),
    ("services_send_notification", {"message": "x"}),
    ("services_reload_config", {}),
    ("entities_bulk_set_state", {"updates": []}),
    ("entities_bulk_control", {"entity_ids": ["light.x"], "action": "turn_on"}),
    ("areas_control_area", {"area_id": "x", "action": "turn_on"}),
    ("entities_remove_entity", {"entity_id": "light.x"}),
    ("files_write_config_file", {"relative_path": "x.yaml", "content": ""}),
    ("git_git_rollback_to_commit", {"sha": "abc"}),
    ("esphome_upload_device", {"name": "x"}),
    ("esphome_compile_device", {"name": "x"}),
    ("supervisor_set_addon_options", {"slug": "x", "options": {}}),
    ("supervisor_restart_core", {}),
    ("system_restart_ha", {}),
    ("hacs_install_hacs_repository", {"repository": "x"}),
    ("automations_trigger_automation", {"entity_id": "automation.x"}),
    ("automations_run_script", {"entity_id": "script.x"}),
    ("nodered_list_flows", None),  # read: must stay visible (control case)
    ("neonplan_save_plan", {"plan": "x", "building": {}}),
    ("neonplan_patch_plan", {"plan": "x", "operations": []}),
    ("neonplan_restore_restore_point", {"plan": "x", "restore_point_id": "x"}),
]


def _nodered_write_tool_names(root: FastMCP) -> list[str]:
    return sorted(
        t.name
        for t in asyncio.run(root.list_tools())
        if t.name.startswith("nodered_") and t.annotations.readOnlyHint is not True
    )


def test_named_write_tools_exist_in_catalogue(fresh_root):
    """Guards the list above against silently testing nothing after a rename."""
    names = _names(fresh_root)
    missing = [n for n, _ in _MUST_BE_HIDDEN if n not in names]
    assert missing == []
    assert len(_nodered_write_tool_names(fresh_root)) == 5


@pytest.mark.parametrize("tool_name,args", [(n, a) for n, a in _MUST_BE_HIDDEN if a is not None])
def test_generic_and_new_write_tools_hidden_and_refused(fresh_root, tool_name, args):
    policy.apply_policy(
        fresh_root, policy.ToolPolicy(read_only=True, nodered_write_enabled=True)
    )

    assert tool_name not in _names(fresh_root)
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(fresh_root.call_tool(tool_name, args))
    assert "disabled by the add-on policy (read_only" in str(excinfo.value)
    # Enforcement is the Visibility transform, not the UX middleware.
    with pytest.raises(NotFoundError):
        asyncio.run(fresh_root.call_tool(tool_name, args, run_middleware=False))


def test_nodered_writes_hidden_even_when_opted_in(fresh_root):
    writes = _nodered_write_tool_names(fresh_root)
    policy.apply_policy(
        fresh_root, policy.ToolPolicy(read_only=True, nodered_write_enabled=True)
    )
    visible = _names(fresh_root)
    assert not set(writes) & visible
    assert "nodered_list_flows" in visible


def test_allowlist_check_catches_a_misannotated_writer(fresh_root):
    """Meta-test: a write tool wrongly annotated `read()` is caught by the
    allowlist, even though the annotation-driven mechanism lets it through."""

    def create_fake_thing() -> dict:
        return {}

    fresh_root.add_tool(
        Tool.from_function(
            create_fake_thing,
            name="entities_create_fake_thing",
            annotations=_contract.read("Fake writer annotated read"),
        )
    )
    visible = _visible_under_read_only(fresh_root)

    assert "entities_create_fake_thing" in visible  # the mechanism alone leaks it
    assert visible - _reviewed_allowlist() == {"entities_create_fake_thing"}
    assert _MUTATING_RE.match(_local_name("entities_create_fake_thing"))
