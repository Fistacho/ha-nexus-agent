"""Tools whose output schema is an object must return a dict, whatever HA answers.

Live failure (HA 2026.11.1, 2026-11-11): `areas_delete_area` deleted the area
but the MCP call failed with "structured_content must be a dict or None. Got
str: 'success'". HA's `config/area_registry/delete` answers the string
`"success"` (`homeassistant/components/config/area_registry.py`,
`websocket_delete_area`: `result_message(msg["id"], "success")`), and the
tool, declared `-> dict`, passed it through. FastMCP derives an object output
schema from `-> dict`, so any non-dict result (a string, a list, `None`) fails
the call after HA already did the work:

- a string or a list: FastMCP's `ToolResult` refuses it (`structured_content
  must be a dict or None`);
- `None`: the MCP client refuses it ("outputSchema defined but no structured
  output returned"), as already seen for the Marketplace in 0.28.1.

Same pattern, found by the static check below and confirmed against HA core
2026.11.1 sources:

- `blueprint/delete`, `assist_pipeline/pipeline/delete` and
  `assist_pipeline/pipeline/set_preferred` answer `send_result(msg["id"])`,
  i.e. `null` (`components/blueprint/websocket_api.py`,
  `helpers/collection.py` `ws_delete_item`,
  `components/assist_pipeline/pipeline.py`);
- `POST /api/services/<domain>/<service>` answers a JSON list of changed
  states (`ha_client.call_service -> list[dict]`), so `system_restart_ha`,
  `system_stop_ha`, `system_create_backup` and `themes_set_active_theme`
  failed the same way.

The runtime tests drive each tool through a real FastMCP client (in-memory)
with HA's real answer; the static test covers every tool, so a new
passthrough of a non-object answer fails here instead of on a live system.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import textwrap

import pytest
from contract import generate_tool_surface as golden
from fastmcp import Client

import ha_client as ha
from tools import areas, blueprints, system, themes, voice


def _call(module, name: str, args: dict):
    async def go():
        async with Client(module.mcp) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


@pytest.mark.parametrize(
    "module,name,args,ws_answer",
    [
        (areas, "delete_area", {"area_id": "garaz"}, "success"),
        (blueprints, "delete_blueprint", {"path": "author/x.yaml"}, None),
        (voice, "delete_pipeline", {"pipeline_id": "01abc"}, None),
        (voice, "set_preferred_pipeline", {"pipeline_id": "01abc"}, None),
    ],
)
def test_ws_commands_without_an_object_answer(monkeypatch, module, name, args, ws_answer):
    sent = []
    monkeypatch.setattr(ha, "_ws_call", lambda t, **kw: sent.append((t, kw)) or ws_answer)

    result = _call(module, name, args)

    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "module,name,args",
    [
        (system, "restart_ha", {"confirm": True}),
        (system, "stop_ha", {"confirm": True}),
        (themes, "set_active_theme", {"name": "dark"}),
    ],
)
def test_service_calls_answer_a_list(monkeypatch, module, name, args):
    monkeypatch.setattr(ha, "call_service", lambda *a, **kw: [])

    result = _call(module, name, args)

    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)


def test_create_backup_service_answer_is_wrapped(monkeypatch):
    monkeypatch.setattr(ha, "list_services", lambda: [{"domain": "backup", "services": {"create": {}}}])
    monkeypatch.setattr(ha, "call_service", lambda *a, **kw: [])

    result = _call(system, "create_backup", {})

    assert not result.is_error, result.content
    assert result.structured_content["service"] == "backup.create"


def test_delete_area_reports_what_was_deleted(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", lambda t, **kw: "success")

    result = _call(areas, "delete_area", {"area_id": "garaz"})

    assert result.structured_content == {"deleted": True, "area_id": "garaz"}


# --- static check over every mounted tool --------------------------------------------

# HA WebSocket commands whose result is a JSON object, so a tool declared
# `-> dict` may return them unchanged. Verified in HA core 2026.11.1 unless
# marked otherwise. A command missing here fails the test below: check what HA
# sends (`send_result(msg["id"], <object>)`) before adding it.
_OBJECT_RESULT_COMMANDS = {
    "trace/get",  # components/trace/websocket_api.py: the trace dict (inferred, not re-read)
    "blueprint/list",  # dict of path -> blueprint
    "blueprint/import",  # {"suggested_filename", "raw_data", "blueprint", ...}
    "blueprint/save",  # {"overrides_existing": bool}
    "blueprint/substitute",  # {"substituted_config": ...}
    "lovelace/config",  # the dashboard config
    "energy/get_prefs",  # manager.data
    "energy/info",
    "energy/validate",  # EnergyPreferencesValidation.as_dict()
    "config_entries/disable",  # {"require_restart": bool}
    "config_entries/update",
    "config/label_registry/create",
    "config/label_registry/update",
    "config/category_registry/create",
    "config/category_registry/update",
    "frontend/get_themes",
    "assist_pipeline/pipeline/list",  # {"pipelines", "preferred_pipeline"}
    "assist_pipeline/pipeline/get",
    "assist_pipeline/pipeline/create",  # collection ws_create_item: the item
    "assist_pipeline/pipeline/update",  # collection ws_update_item: the item
    "conversation/agent/list",  # {"agents": [...]} (inferred, not re-read)
    # card_builder is a HACS integration, not HA core: not verified here.
    "card_builder/cards/create",
    "card_builder/cards/update",
    "card_builder/style_presets/create",
    "card_builder/style_presets/update",
    "card_builder/css_custom_properties/create",
    "card_builder/media/list",
    "card_builder/media/upload",
}


def _is_plain_dict(annotation) -> bool:
    return annotation is dict or getattr(annotation, "__origin__", None) is dict


def _tool_functions():
    """`(namespace_tool, fn)` for every tool of every module `server.py` mounts with an unwrapped object schema."""
    for module_name, namespace in golden.module_namespace_map().items():
        module = importlib.import_module(f"tools.{module_name}")
        for tool in asyncio.run(module.mcp.list_tools()):
            schema = tool.output_schema
            if not schema or schema.get("type") != "object" or schema.get("x-fastmcp-wrap-result"):
                continue
            yield f"{namespace}_{tool.name}", tool.fn


def _bad_returns(fn) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "ha"):
            continue
        if func.attr in ("_ws_call", "_ws_call_timeout"):
            arg = node.value.args[0] if node.value.args else None
            command = arg.value if isinstance(arg, ast.Constant) else None
            if command not in _OBJECT_RESULT_COMMANDS:
                problems.append(f"returns ha.{func.attr}({command!r}) unchanged; HA's answer is not known to be an object")
            continue
        target = getattr(ha, func.attr, None)
        annotation = inspect.signature(target).return_annotation if callable(target) else None
        if isinstance(annotation, str):
            annotation = eval(annotation, vars(ha))  # noqa: S307 — ha_client's own annotations
        if not _is_plain_dict(annotation):
            problems.append(f"returns ha.{func.attr}(...) unchanged, annotated {annotation!r}, not dict")
    return problems


def test_dict_tools_do_not_pass_through_non_object_answers():
    failures = {}
    checked = 0
    for name, fn in _tool_functions():
        checked += 1
        problems = _bad_returns(fn)
        if problems:
            failures[name] = problems
    assert not failures, "\n".join(f"{n}: {p}" for n, ps in sorted(failures.items()) for p in ps)
