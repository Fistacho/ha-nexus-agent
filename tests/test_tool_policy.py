"""ADR-0003 P1 (`read_only`) + P2 (`disabled_namespaces`) enforcement.

Builds a throwaway `FastMCP` root that mounts every real `tools.<mod>.mcp`
singleton under its real namespace (`contract.generate_tool_surface.
module_namespace_map()` — the same parser T1's golden-file generator uses),
so this suite never touches the module-level `server.mcp` singleton that
`tests/test_audit_http_auth.py` and friends import and reuse for the whole
pytest session. Mounting an already-mounted child `FastMCP` object onto a
second parent does not mutate the child; `policy.apply_policy()` only ever
calls `.disable()`/`.add_middleware()` on the *parent* passed to it, so the
real `server.mcp` (and the T1/T2 golden-file tests that rely on its
unfiltered 323-tool catalogue) is unaffected by anything in this file.

`tools/discover.py` caches its index and known-namespace list in module
globals, shared by whichever `FastMCP` root last called `bind_root()`. The
`fresh_root` fixture below points `discover` at the throwaway root for the
duration of one test and restores the previous globals afterwards.
"""
from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from contract import generate_tool_surface as golden
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError

import policy
import server
from tools import discover as discover_mod

_NEXUS_ROOT = Path(__file__).resolve().parents[1]


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
    """A brand-new root with every tool module mounted, `discover` pointed
    at it, and `discover`'s module-global cache restored afterwards so this
    test can't leak into any test module that uses the real `server.mcp`."""
    root = FastMCP("test-nexus-policy")
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


def _read_only_tool_names(root: FastMCP) -> set[str]:
    tools = asyncio.run(root.list_tools())
    return {
        t.name
        for t in tools
        if t.annotations is not None and t.annotations.readOnlyHint is True
    }


# --- P1: read_only ------------------------------------------------------


def test_read_only_visible_set_equals_annotations_read_only_set(fresh_root):
    """`list_tools()` under `read_only` is *exactly* the annotation-derived
    set, computed independently here — not a hard-coded number reused from
    the implementation."""
    expected = _read_only_tool_names(fresh_root)

    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True))

    assert _list_tool_names(fresh_root) == expected


def test_read_only_matches_adr_fact_154_of_323(fresh_root):
    """Live-registry fact from ADR-0003 (154 `read()` / 323 total). A
    regression here means an annotation drifted from its preset without
    ADR-0002's own contract tests catching it."""
    total = len(asyncio.run(fresh_root.list_tools()))

    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True))

    assert total == 323
    assert len(_list_tool_names(fresh_root)) == 154


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("supervisor_delete_backup", {"slug": "abcd1234"}),
        ("automations_delete_automation", {"automation_id": "1"}),
        ("entities_turn_on", {"entity_id": "light.x"}),
    ],
)
def test_read_only_blocks_call_of_non_read_tool_before_io(fresh_root, tool_name, args):
    """A non-`read` tool is rejected by the *policy*, not by argument
    validation and not by reaching any I/O helper."""
    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(fresh_root.call_tool(tool_name, args))

    assert "ValidationError" not in type(excinfo.value).__name__


def test_read_only_call_still_blocked_with_middleware_bypassed(fresh_root):
    """Enforcement lives in `disable()`'s Visibility transform, not in the
    UX middleware: `call_tool(run_middleware=False)` — the same bypass path
    ADR-0003 calls out for middleware-only designs — must still refuse."""
    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True))

    with pytest.raises(NotFoundError):
        asyncio.run(
            fresh_root.call_tool(
                "supervisor_delete_backup", {"slug": "x"}, run_middleware=False
            )
        )


def test_read_only_still_allows_a_read_tool(fresh_root):
    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True))

    assert "entities_get_entity" in _list_tool_names(fresh_root)


def test_read_only_hides_every_prompt(fresh_root):
    prompts_before = asyncio.run(fresh_root.list_prompts())
    assert prompts_before  # sanity: prompts exist to hide

    policy.apply_policy(fresh_root, policy.ToolPolicy(read_only=True))

    assert asyncio.run(fresh_root.list_prompts()) == []


def test_default_policy_is_read_only_false_full_catalogue(fresh_root):
    before = _list_tool_names(fresh_root)

    policy.apply_policy(fresh_root, policy.ToolPolicy())

    assert _list_tool_names(fresh_root) == before
    assert len(before) == 323


# --- P2: disabled_namespaces ---------------------------------------------


def test_disabled_namespace_removed_from_list_tools(fresh_root):
    before = _list_tool_names(fresh_root)
    assert any(n.startswith("card_builder_") for n in before)

    policy.apply_policy(
        fresh_root, policy.ToolPolicy(disabled_namespaces=frozenset({"card_builder"}))
    )

    after = _list_tool_names(fresh_root)
    assert not any(n.startswith("card_builder_") for n in after)
    assert after == before - {n for n in before if n.startswith("card_builder_")}


def test_disabled_namespace_removed_from_discover_index(fresh_root):
    """`discover_tool_search`/`discover_get_tool_doc` read the same index
    this rebuilds — a namespace hidden by policy must disappear from both,
    not just from the raw `tools/list` the client also sees."""
    policy.apply_policy(
        fresh_root, policy.ToolPolicy(disabled_namespaces=frozenset({"card_builder"}))
    )

    discover_mod._INDEX = None  # force rebuild against the now-filtered root
    index = discover_mod._ensure_index()

    assert not any(entry["namespace"] == "card_builder" for entry in index)


def test_disabled_namespace_call_rejected(fresh_root):
    policy.apply_policy(
        fresh_root, policy.ToolPolicy(disabled_namespaces=frozenset({"supervisor"}))
    )

    with pytest.raises(ToolError):
        asyncio.run(fresh_root.call_tool("supervisor_delete_backup", {"slug": "x"}))


def test_disabled_namespaces_and_read_only_combine_as_union(fresh_root):
    ro_only = _read_only_tool_names(fresh_root)

    policy.apply_policy(
        fresh_root,
        policy.ToolPolicy(read_only=True, disabled_namespaces=frozenset({"entities"})),
    )

    visible = _list_tool_names(fresh_root)
    assert not any(n.startswith("entities_") for n in visible)
    assert visible == {n for n in ro_only if not n.startswith("entities_")}


# --- Strict option parsing -------------------------------------------------


def test_from_env_rejects_unknown_namespace():
    with pytest.raises(policy.PolicyConfigError):
        policy.ToolPolicy.from_env(
            {"NEXUS_DISABLED_NAMESPACES": "entities,not_a_real_namespace"},
            known_namespaces=frozenset({"entities", "services"}),
        )


def test_from_env_rejects_non_boolean_read_only():
    with pytest.raises(policy.PolicyConfigError):
        policy.ToolPolicy.from_env(
            {"NEXUS_READ_ONLY": "maybe"},
            known_namespaces=frozenset({"entities"}),
        )


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), ("", False)])
def test_from_env_parses_boolean_variants(raw, expected):
    result = policy.ToolPolicy.from_env(
        {"NEXUS_READ_ONLY": raw}, known_namespaces=frozenset()
    )
    assert result.read_only is expected


def test_from_env_strips_and_dedupes_namespace_list():
    result = policy.ToolPolicy.from_env(
        {"NEXUS_DISABLED_NAMESPACES": " entities , services ,entities"},
        known_namespaces=frozenset({"entities", "services"}),
    )
    assert result.disabled_namespaces == frozenset({"entities", "services"})


def test_default_policy_has_no_env_lookup_surprises():
    """Absent env vars behave like a fully-permissive default (0.21.0
    parity)."""
    result = policy.ToolPolicy.from_env({}, known_namespaces=frozenset())
    assert result == policy.ToolPolicy()


# --- Static guard: no session-scoped visibility override in this codebase --


_FORBIDDEN_CALLS = ("enable_components", "disable_components", "reset_visibility")
_FORBIDDEN_RE = re.compile("|".join(re.escape(name) for name in _FORBIDDEN_CALLS))


def test_no_session_scoped_visibility_override_in_source():
    """`ctx.enable_components()`/`disable_components()`/`reset_visibility()`
    are session-scoped and could silently re-open a namespace this module
    disabled process-wide. None of nexus's own code may call them."""
    offenders = []
    for path in _NEXUS_ROOT.rglob("*.py"):
        if "tests" in path.relative_to(_NEXUS_ROOT).parts:
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _FORBIDDEN_RE.search(text):
            offenders.append(str(path.relative_to(_NEXUS_ROOT)))

    assert offenders == []


# --- Security regression: fail-closed startup + no unprefixed-name bypass ---
#
# Both tests below exercise `server.py`'s module-level `mcp` singleton, not
# a throwaway `fresh_root`. Neither mutates it: `test_apply_tool_policy_or_exit_exits_on_bad_env`
# replaces `policy.apply_policy` with a spy that is asserted *never called*
# (the whole point of the test), and `test_unprefixed_tool_name_is_not_callable`
# only calls a name that was never registered, which raises before touching
# any tool's `.fn`. Safe to share the session-wide `server.mcp` with
# `tests/test_audit_http_auth.py` and friends.


@pytest.mark.parametrize(
    "env,expected_bad_option",
    [
        ({"NEXUS_READ_ONLY": "maybe"}, "read_only"),
        ({"NEXUS_DISABLED_NAMESPACES": "not_a_real_namespace"}, "disabled_namespaces"),
    ],
    ids=["bad-read-only-bool", "unknown-namespace"],
)
def test_apply_tool_policy_or_exit_exits_on_bad_env(monkeypatch, env, expected_bad_option):
    """A malformed add-on option (`NEXUS_READ_ONLY` not a boolean, or an
    unknown name in `NEXUS_DISABLED_NAMESPACES`) must abort startup with
    `SystemExit(1)` *before* `policy.apply_policy()` is ever called — a typo
    in a security-relevant option must never silently apply a narrower (or
    wider) policy than what was actually configured, and must never leave
    the process running with the fully-open default surface either.

    Regression this guards against: `server._apply_tool_policy_or_exit()`
    swallowing `PolicyConfigError` (or catching a different, broader
    exception type that also masks a real bug) and falling through to
    `apply_policy()` with a default/partial `ToolPolicy` instead of exiting.
    """
    for key in ("NEXUS_READ_ONLY", "NEXUS_DISABLED_NAMESPACES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    apply_spy = MagicMock(name="policy.apply_policy")
    monkeypatch.setattr(policy, "apply_policy", apply_spy)

    with pytest.raises(SystemExit) as excinfo:
        server._apply_tool_policy_or_exit()

    assert excinfo.value.code == 1
    apply_spy.assert_not_called()


def test_unprefixed_tool_name_is_not_callable():
    """`tools/supervisor.py` defines the function as `delete_backup`; it is
    only reachable through its mounted, namespace-prefixed name
    `supervisor_delete_backup` (see `tools/discover.py`'s own namespace
    parsing, which relies on this same prefixing).

    Regression this guards against: a bypass where the *unprefixed* function
    name — the same short name every other tool module also uses internally,
    e.g. some other namespace's own `delete_backup` — is still registered
    and callable on the mounted root, sidestepping any namespace- or
    annotation-based policy that only ever reasons about the prefixed name.
    """
    with pytest.raises(NotFoundError):
        asyncio.run(server.mcp.call_tool("delete_backup", {"slug": "irrelevant"}))
