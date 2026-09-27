"""TDD contract tests for `discover_get_tool_doc`'s additive keys (ADR-0002 batch F).

Batch F only touches `tools/discover.py`. Per ADR-0002 ("Partia F dodatkowo")
`discover_get_tool_doc` must gain two new response keys — `input_schema` and
`annotations` — without changing any existing key (`name`/`namespace`/
`description`) or the `{"error": "not_found", ...}` shape. Without this,
moving parameter descriptions out of docstrings and into
`Annotated[..., Field(description=...)]` (ADR-0002 D1) would make
`discover_get_tool_doc` silently lose all parameter documentation, since it
previously only ever returned the bare `description` string.
"""
from __future__ import annotations

import pytest
from mcp.types import ToolAnnotations

from tools import discover as discover_mod


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


class _FakeTool:
    """Stand-in for a `fastmcp` `FunctionTool`: `.parameters` is the JSON-schema
    dict (same shape as `mcp.types.Tool.inputSchema`), `.annotations` is a
    `ToolAnnotations` or `None` (mirrors an un-migrated tool)."""

    def __init__(self, name: str, description: str = "", parameters: dict | None = None,
                 annotations: ToolAnnotations | None = None):
        self.name = name
        self.description = description
        self.parameters = parameters if parameters is not None else {"type": "object", "properties": {}}
        self.annotations = annotations


class _FakeRoot:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return self._tools


@pytest.fixture(autouse=True)
def _reset_discover_state():
    """Every test starts from a clean discover.py module-level cache."""
    discover_mod._ROOT = None
    discover_mod._INDEX = None
    discover_mod._KNOWN_NAMESPACES = None
    yield
    discover_mod._ROOT = None
    discover_mod._INDEX = None
    discover_mod._KNOWN_NAMESPACES = None


def test_get_tool_doc_adds_input_schema_without_dropping_existing_keys():
    schema = {
        "type": "object",
        "properties": {"entity_id": {"type": "string", "description": "Full entity ID."}},
        "required": ["entity_id"],
    }
    tools = [_FakeTool("entities_get_entity", "Get one entity's state.", parameters=schema)]
    discover_mod.bind_root(_FakeRoot(tools))

    doc = _unwrap(discover_mod.get_tool_doc)("entities_get_entity")

    assert doc["name"] == "entities_get_entity"
    assert doc["namespace"] == "entities"
    assert doc["description"] == "Get one entity's state."
    assert doc["input_schema"] == schema


def test_get_tool_doc_adds_annotations_as_a_plain_json_serialisable_dict():
    ann = ToolAnnotations(
        title="Get entity state",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    tools = [_FakeTool("entities_get_entity", "Get one entity's state.", annotations=ann)]
    discover_mod.bind_root(_FakeRoot(tools))

    doc = _unwrap(discover_mod.get_tool_doc)("entities_get_entity")

    assert doc["annotations"] == {
        "title": "Get entity state",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_get_tool_doc_annotations_is_none_for_a_not_yet_migrated_tool():
    """A tool whose module has no TOOL_CONTRACT marker yet has no annotations
    (FastMCP leaves `.annotations` as `None` when `@mcp.tool()` sets none) —
    `discover_get_tool_doc` must surface that as `None`, not crash or fabricate
    a value."""
    tools = [_FakeTool("legacy_ns_old_tool", "Not yet migrated.", annotations=None)]
    discover_mod.bind_root(_FakeRoot(tools))

    doc = _unwrap(discover_mod.get_tool_doc)("legacy_ns_old_tool")

    assert doc["annotations"] is None


def test_get_tool_doc_not_found_shape_is_unchanged():
    discover_mod.bind_root(_FakeRoot([]))

    doc = _unwrap(discover_mod.get_tool_doc)("does_not_exist")

    assert doc == {"error": "not_found", "name": "does_not_exist"}
