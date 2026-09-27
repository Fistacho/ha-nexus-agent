"""ADR-0003 P3 (`tool_mode`) — the "search" tool-exposure mode.

Builds a throwaway `FastMCP` root that mounts every real `tools.<mod>.mcp`
singleton under its real namespace, exactly like `tests/test_tool_policy.py`
(see that module's docstring for why: `policy.apply_policy()` only ever
mutates the root passed to it, so this suite never touches the module-level
`server.mcp` singleton `tests/test_tool_contract.py` relies on for T1/T2's
unfiltered 323-tool catalogue).

`tool_mode=full` (the default) must leave `tools/list` byte-identical to
today's behaviour — those assertions live in `tests/test_tool_policy.py`
already (`test_default_policy_is_read_only_false_full_catalogue`) and are not
repeated here beyond one guard tying this suite to the same fact.

`tool_mode=search` replaces `tools/list` with exactly six tools:
`discover_tool_search`, `discover_get_tool_doc`, `discover_list_namespaces`
(pinned, unchanged from full mode) plus `discover_call_read_tool`,
`discover_call_write_tool`, `discover_call_destructive_tool` (new, one per
ADR-0002 annotation class). Every other tool stays directly callable by its
own name — `tool_mode` trades context budget for visibility, it is not a
security boundary (P1 `read_only` / P2 `disabled_namespaces` are).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from contract import generate_tool_surface as golden
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError

import ha_client as ha
import policy
from tools import discover as discover_mod

_NEXUS_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN_FILE = Path(__file__).resolve().parent / "contract" / "tool_surface_search.json"

_SEARCH_MODE_TOOL_NAMES = {
    "discover_tool_search",
    "discover_get_tool_doc",
    "discover_list_namespaces",
    "discover_call_read_tool",
    "discover_call_write_tool",
    "discover_call_destructive_tool",
}


def _mount_everything(root: FastMCP) -> dict[str, str]:
    """Mount every real `tools.<mod>.mcp` onto `root`, exactly like `server.py`."""
    server_py_text = (_NEXUS_ROOT / "server.py").read_text(encoding="utf-8")
    mapping = golden.module_namespace_map(server_py_text)
    for module_name, namespace in mapping.items():
        module = importlib.import_module(f"tools.{module_name}")
        root.mount(module.mcp, namespace=namespace)
    return mapping


@pytest.fixture
def fresh_root():
    """A brand-new root with every tool module mounted, `discover` pointed at
    it, and `discover`'s module-global cache restored afterwards — same
    isolation contract as `tests/test_tool_policy.py::fresh_root`."""
    root = FastMCP("test-nexus-tool-search")
    _mount_everything(root)

    original_root = discover_mod._ROOT
    original_index = discover_mod._INDEX
    original_namespaces = discover_mod._KNOWN_NAMESPACES
    discover_mod.bind_root(root)
    try:
        yield root
    finally:
        discover_mod._ROOT = original_root
        discover_mod._INDEX = original_index
        discover_mod._KNOWN_NAMESPACES = original_namespaces


def _list_tool_names(root: FastMCP) -> set[str]:
    return {t.name for t in asyncio.run(root.list_tools())}


def _tools_by_name(root: FastMCP) -> dict[str, object]:
    return {t.name: t for t in asyncio.run(root.list_tools())}


# --- from_env / PolicyConfigError -----------------------------------------


def test_default_tool_mode_is_full():
    assert policy.ToolPolicy().tool_mode == "full"


def test_from_env_defaults_tool_mode_to_full():
    result = policy.ToolPolicy.from_env({}, known_namespaces=frozenset())
    assert result.tool_mode == "full"


def test_from_env_parses_tool_mode_search():
    result = policy.ToolPolicy.from_env(
        {"NEXUS_TOOL_MODE": "search"}, known_namespaces=frozenset()
    )
    assert result.tool_mode == "search"


def test_from_env_rejects_invalid_tool_mode():
    with pytest.raises(policy.PolicyConfigError):
        policy.ToolPolicy.from_env(
            {"NEXUS_TOOL_MODE": "sometimes"}, known_namespaces=frozenset()
        )


def test_from_env_rejects_search_mode_with_discover_disabled():
    with pytest.raises(policy.PolicyConfigError):
        policy.ToolPolicy.from_env(
            {
                "NEXUS_TOOL_MODE": "search",
                "NEXUS_DISABLED_NAMESPACES": "discover",
            },
            known_namespaces=frozenset({"discover", "entities"}),
        )


def test_apply_policy_rejects_search_mode_with_discover_disabled_directly(fresh_root):
    """Same guard, exercised by constructing `ToolPolicy` directly (bypassing
    `from_env`) and calling `apply_policy()` — the invariant must hold no
    matter how the policy object was built."""
    bad_policy = policy.ToolPolicy(
        tool_mode="search", disabled_namespaces=frozenset({"discover"})
    )
    with pytest.raises(policy.PolicyConfigError):
        policy.apply_policy(fresh_root, bad_policy)


# --- full mode: unchanged -----------------------------------------------


def test_full_mode_tools_list_is_unaffected(fresh_root):
    before = _list_tool_names(fresh_root)

    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="full"))

    assert _list_tool_names(fresh_root) == before
    assert len(before) == 323


# --- search mode: catalogue shape -----------------------------------------


def test_search_mode_tools_list_is_exactly_six_tools(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    assert _list_tool_names(fresh_root) == _SEARCH_MODE_TOOL_NAMES


def test_search_mode_hides_refresh_index_but_it_stays_directly_callable(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    assert "discover_refresh_index" not in _list_tool_names(fresh_root)

    result = asyncio.run(fresh_root.call_tool("discover_refresh_index", {}))
    assert result.structured_content is not None


@pytest.mark.parametrize(
    "proxy_name,read_only,destructive,idempotent",
    [
        ("discover_call_read_tool", True, False, True),
        ("discover_call_write_tool", False, False, False),
        ("discover_call_destructive_tool", False, True, False),
    ],
)
def test_search_mode_proxy_annotations(fresh_root, proxy_name, read_only, destructive, idempotent):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    tools = _tools_by_name(fresh_root)
    annotations = tools[proxy_name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is read_only
    assert annotations.destructiveHint is destructive
    assert annotations.idempotentHint is idempotent
    assert annotations.title


def test_search_mode_read_only_hides_write_and_destructive_proxies(fresh_root):
    policy.apply_policy(
        fresh_root, policy.ToolPolicy(read_only=True, tool_mode="search")
    )

    names = _list_tool_names(fresh_root)
    assert names == {
        "discover_tool_search",
        "discover_get_tool_doc",
        "discover_list_namespaces",
        "discover_call_read_tool",
    }


def test_search_mode_absent_proxy_class_not_callable_directly(fresh_root):
    policy.apply_policy(
        fresh_root, policy.ToolPolicy(read_only=True, tool_mode="search")
    )

    with pytest.raises(NotFoundError):
        asyncio.run(
            fresh_root.call_tool("discover_call_write_tool", {"name": "entities_turn_on"})
        )


# --- search mode: proxy dispatch ------------------------------------------


def test_search_mode_read_proxy_dispatches_transparently(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    direct = asyncio.run(fresh_root.call_tool("discover_list_namespaces", {}))
    via_proxy = asyncio.run(
        fresh_root.call_tool(
            "discover_call_read_tool", {"name": "discover_list_namespaces"}
        )
    )

    assert via_proxy.structured_content == direct.structured_content
    assert via_proxy.content == direct.content


def test_search_mode_write_proxy_dispatches_transparently(fresh_root, monkeypatch):
    monkeypatch.setattr(
        ha, "call_service", lambda domain, service, data=None: [{"entity_id": "counter.x", "state": "0"}]
    )
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    direct = asyncio.run(
        fresh_root.call_tool("helpers_reset_counter", {"entity_id": "counter.x"})
    )
    via_proxy = asyncio.run(
        fresh_root.call_tool(
            "discover_call_write_tool",
            {"name": "helpers_reset_counter", "arguments": {"entity_id": "counter.x"}},
        )
    )

    assert via_proxy.structured_content == direct.structured_content


def test_search_mode_destructive_proxy_dispatches_transparently(fresh_root):
    """`automations_delete_scene` without `confirm` refuses before any I/O —
    zero mocking needed and still proves the proxy forwards `arguments`."""
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    direct = asyncio.run(
        fresh_root.call_tool("automations_delete_scene", {"scene_id": "movie_night"})
    )
    via_proxy = asyncio.run(
        fresh_root.call_tool(
            "discover_call_destructive_tool",
            {"name": "automations_delete_scene", "arguments": {"scene_id": "movie_night"}},
        )
    )

    assert via_proxy.structured_content == direct.structured_content
    assert via_proxy.structured_content["error"] == "confirmation_required"


def test_search_mode_proxy_rejects_wrong_class_before_any_io(fresh_root, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("proxy must not call the target tool on a class mismatch")

    monkeypatch.setattr(ha, "call_service", _boom)
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    result = asyncio.run(
        fresh_root.call_tool(
            "discover_call_read_tool",
            {"name": "entities_turn_on", "arguments": {"entity_id": "light.x"}},
        )
    )

    body = result.structured_content
    assert body["error"] == "wrong_proxy"
    assert body["use"] == "discover_call_write_tool"


def test_search_mode_proxy_returns_not_found_for_unknown_tool(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    result = asyncio.run(
        fresh_root.call_tool(
            "discover_call_read_tool", {"name": "entities_this_tool_does_not_exist"}
        )
    )

    assert result.structured_content == {
        "error": "not_found",
        "name": "entities_this_tool_does_not_exist",
    }


def test_search_mode_proxy_respects_disabled_namespaces(fresh_root):
    policy.apply_policy(
        fresh_root,
        policy.ToolPolicy(
            disabled_namespaces=frozenset({"entities"}), tool_mode="search"
        ),
    )

    result = asyncio.run(
        fresh_root.call_tool(
            "discover_call_read_tool", {"name": "entities_get_entity"}
        )
    )

    assert result.structured_content == {
        "error": "not_found",
        "name": "entities_get_entity",
    }


def test_search_mode_proxy_argument_validation_matches_target_schema(fresh_root):
    """A target tool's own required-parameter validation still applies —
    the proxy doesn't accept an incomplete `arguments` dict just because its
    own schema is `dict | None`."""
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    with pytest.raises(Exception):  # noqa: B017 - broad on purpose: pydantic ValidationError
        asyncio.run(
            fresh_root.call_tool(
                "discover_call_read_tool", {"name": "entities_get_entity", "arguments": {}}
            )
        )


def test_discover_tool_search_reports_access_class(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="full"))

    discover_mod._INDEX = None
    result = asyncio.run(
        fresh_root.call_tool(
            "discover_tool_search", {"query": "turn on light", "top_k": 5}
        )
    )
    hits = result.structured_content["result"]
    assert hits
    assert all("access" in hit for hit in hits)
    assert all(hit["access"] in ("read", "write", "destructive") for hit in hits)


def test_discover_get_tool_doc_reports_access_class(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="full"))

    discover_mod._INDEX = None
    result = asyncio.run(
        fresh_root.call_tool("discover_get_tool_doc", {"name": "entities_get_entity"})
    )
    assert result.structured_content["access"] == "read"


# --- golden file: search-mode surface --------------------------------------


def test_search_mode_matches_golden_tool_surface(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(tool_mode="search"))

    tools = {t.name: t for t in asyncio.run(fresh_root.list_tools())}
    current = {
        name: {"inputSchema": golden.strip_descriptions(tool.to_mcp_tool().inputSchema)}
        for name, tool in sorted(tools.items())
    }

    golden_data = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))

    assert set(current) == set(golden_data["tools"]), (
        "tool_mode=search surface changed vs the golden file "
        "tests/contract/tool_surface_search.json. If reviewed and intended, "
        "regenerate with: python tests/contract/generate_tool_surface.py --mode search"
    )
    for name in current:
        assert current[name] == golden_data["tools"][name], (
            f"Parameter schema changed for {name!r} vs the golden file "
            "tests/contract/tool_surface_search.json. If reviewed and intended, "
            "regenerate with: python tests/contract/generate_tool_surface.py --mode search"
        )
    assert golden_data["tool_count"] == 6 == len(current)
