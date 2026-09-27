"""Tool discovery — search the catalogue instead of memorising all 248.

For AI clients with limited context (Sonnet/Opus session budgets), iterating
over every tool's docstring at idle time burns tokens. This module ships a
small index + keyword search so the assistant can ask "what can I do with
covers?" and get a ranked shortlist before invoking the right one.

It's an *additive* helper, not a replacement: every tool stays accessible
through its normal name. Mount this module last so its index sees every
sibling already registered.

ADR-0003 P3 (`tool_mode=search`) also lives here: `build_tool_search_transform()`
and the `NexusToolSearch` transform it returns replace `tools/list` with this
module's own three lookup tools (`tool_search`, `get_tool_doc`,
`list_namespaces`) plus one call-by-name proxy per ADR-0002 annotation class
(`discover_call_read_tool`/`_write_tool`/`_destructive_tool`). `policy.py` is
the only caller — it decides *whether* `tool_mode=search` is active and
passes the already-policy-filtered tool set; nothing here reads `NEXUS_*` env
vars or imports `policy` (that import would be circular: `policy.py` already
imports this module for its namespace parser).
"""
from __future__ import annotations

import ast
import asyncio
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Iterable, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.transforms.catalog import CatalogTransform
from fastmcp.server.transforms.visibility import is_enabled
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import Field

from tools._contract import destructive, read, write

mcp = FastMCP("discover")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

# Filled by `bind_root` from server.py once all modules are mounted.
_ROOT: FastMCP | None = None
_INDEX: list[dict[str, Any]] | None = None
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")

# server.py is the single source of truth for namespace prefixes (one
# `mcp.mount(..., namespace="...")` call per tool module). Reading its
# *source text* — instead of `import server` — avoids a circular import
# (server.py imports this module at load time) and avoids keeping a second,
# driftable copy of the namespace list in this file.
_SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"
_KNOWN_NAMESPACES: list[str] | None = None


def _known_namespaces(server_path: Path = _SERVER_PY) -> list[str]:
    """Parse `mcp.mount(..., namespace="...")` calls out of server.py's source.

    Longest-first so e.g. "card_builder" is tried before any shorter prefix
    that could otherwise also match (naive `name.split("_", 1)[0]` used to
    misfile every `card_builder_*` tool under namespace "card").
    """
    try:
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(server_path))
    except OSError:
        return []

    namespaces: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mount"):
            continue
        for kw in node.keywords:
            if kw.arg == "namespace" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                namespaces.append(kw.value.value)
    return sorted(set(namespaces), key=len, reverse=True)


def _namespace_prefixes() -> list[str]:
    global _KNOWN_NAMESPACES
    if _KNOWN_NAMESPACES is None:
        _KNOWN_NAMESPACES = _known_namespaces()
    return _KNOWN_NAMESPACES


def _namespace_for(name: str) -> str:
    """Resolve a fully-qualified tool name (e.g. 'card_builder_create_card') to its
    real mount namespace, matching the longest known prefix first."""
    for ns in _namespace_prefixes():
        if name == ns or name.startswith(ns + "_"):
            return ns
    # Fallback for anything mounted without a known namespace (shouldn't
    # normally happen — every tool goes through server.py's mount() calls).
    return name.split("_", 1)[0] if "_" in name else name


def bind_root(root: FastMCP) -> None:
    """Register the root FastMCP whose tools we should index. Call after mount()s."""
    global _ROOT, _INDEX, _KNOWN_NAMESPACES
    _ROOT = root
    _INDEX = None  # lazy rebuild on next search
    _KNOWN_NAMESPACES = None  # re-read server.py too, in case it changed


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def _summarise(description: str | None, max_len: int = 140) -> str:
    if not description:
        return ""
    first = description.strip().split("\n\n", 1)[0]
    first = first.replace("\n", " ").strip()
    if len(first) > max_len:
        first = first[: max_len - 1].rstrip() + "…"
    return first


async def _collect_tools() -> list[Any]:
    if _ROOT is None:
        return []
    return list(await _ROOT.list_tools())


def _build_index(tools: Iterable[Any]) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for t in tools:
        name = t.name
        desc = t.description or ""
        namespace = _namespace_for(name)
        tokens = _tokenize(name) + _tokenize(desc)
        index.append(
            {
                "name": name,
                "namespace": namespace,
                "summary": _summarise(desc),
                "_tokens": Counter(tokens),
                "_full_description": desc,
                "_input_schema": getattr(t, "parameters", None),
                "_annotations": getattr(t, "annotations", None),
            }
        )
    return index


def _ensure_index() -> list[dict[str, Any]]:
    global _INDEX
    if _INDEX is None:
        tools = asyncio.run(_collect_tools()) if not _running_loop() else _list_sync()
        _INDEX = _build_index(tools)
    return _INDEX


def _running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _list_sync() -> list[Any]:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, _collect_tools()).result()


# --- BM25-lite scoring ---------------------------------------------------

_K1 = 1.2
_B = 0.75


def _score(query_tokens: list[str], entry: dict[str, Any], avgdl: float, df: Counter, n_docs: int) -> float:
    tokens: Counter = entry["_tokens"]
    dl = sum(tokens.values()) or 1
    score = 0.0
    for q in query_tokens:
        if q not in tokens:
            continue
        tf = tokens[q]
        idf = math.log(1 + (n_docs - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
        norm = tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * dl / avgdl))
        score += idf * norm
    # Small boost when query word appears in the tool name itself
    name_tokens = set(_tokenize(entry["name"]))
    score += 0.5 * sum(1 for q in query_tokens if q in name_tokens)
    return score


def _tool_class(annotations: ToolAnnotations | None) -> Literal["read", "write", "destructive"]:
    """Classify a tool by its MCP annotations (ADR-0003 P: "Klasyfikacja
    pochodzi wyłącznie z adnotacji"): `read` iff `readOnlyHint is True`,
    `destructive` iff `destructiveHint is True`, `write` otherwise.

    Fail-closed the same way `policy._is_read_only_tool()` is: a tool with no
    annotations at all classifies as `destructive`, matching the MCP spec's
    own default (`destructiveHint` defaults to `True` when unset) rather than
    a silent, more-permissive guess.
    """
    if annotations is None:
        return "destructive"
    if annotations.readOnlyHint is True:
        return "read"
    if annotations.destructiveHint is True:
        return "destructive"
    return "write"


# --- Public tools --------------------------------------------------------

@mcp.tool(annotations=read("Search the tool catalogue"))
def tool_search(
    query: Annotated[
        str,
        Field(description="Free-text search query, e.g. 'turn on light' or 'backup'."),
    ],
    top_k: Annotated[
        int,
        Field(description="Maximum number of ranked hits to return, e.g. 10."),
    ] = 10,
    namespace: Annotated[
        str | None,
        Field(
            description=(
                "Restrict results to one namespace, e.g. 'card_builder'. "
                "Omit to search every namespace."
            )
        ),
    ] = None,
) -> list[dict]:
    """Fuzzy search the Nexus tool catalogue by name and description.

    Scores every mounted tool's name + description against `query` with a
    BM25-lite ranking (term frequency/inverse document frequency plus a
    small boost when a query word appears in the tool name itself) and
    returns the top `top_k` matches.

    Use when: finding the right tool name before calling it, instead of
    keeping the whole tool surface in working memory.
    Not for: a single tool's full documentation once you have its name —
    use `discover_get_tool_doc`.
    Returns: list of `{"name", "namespace", "summary", "score", "access"}`
    dicts, highest score first; empty list if nothing scores above zero.
    `access` is `"read"`, `"write"` or `"destructive"` (ADR-0002/0003
    annotation class).
    """
    index = _ensure_index()
    if namespace:
        index = [e for e in index if e["namespace"] == namespace]
    if not index:
        return []

    q_tokens = _tokenize(query)
    if not q_tokens:
        return []

    df: Counter = Counter()
    for e in index:
        for tok in e["_tokens"]:
            df[tok] += 1
    avgdl = sum(sum(e["_tokens"].values()) for e in index) / max(len(index), 1)

    scored = [
        (
            _score(q_tokens, e, avgdl, df, len(index)),
            e,
        )
        for e in index
    ]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {
            "name": e["name"],
            "namespace": e["namespace"],
            "summary": e["summary"],
            "score": round(s, 3),
            "access": _tool_class(e["_annotations"]),
        }
        for s, e in scored[:top_k]
    ]


@mcp.tool(annotations=read("List tool namespaces"))
def list_namespaces() -> list[dict]:
    """List every mounted Nexus tool namespace with its tool count.

    Groups the current tool index by namespace (the mount prefix parsed
    from `server.py`, e.g. "card_builder", "entities") and counts entries.

    Use when: getting an overview of which domains exist before narrowing a
    `discover_tool_search` call with its `namespace` parameter.
    Not for: the tools within one namespace — use `discover_tool_search`
    with that `namespace`.
    Returns: list of `{"namespace", "count"}`, most populated first.
    """
    index = _ensure_index()
    counts: Counter = Counter(e["namespace"] for e in index)
    return [{"namespace": ns, "count": c} for ns, c in counts.most_common()]


@mcp.tool(annotations=read("Get full documentation for one tool"))
def get_tool_doc(
    name: Annotated[
        str,
        Field(description="Full tool name including namespace, e.g. 'entities_get_entity', from `discover_tool_search`."),
    ],
) -> dict:
    """Get one tool's full description, parameter schema and MCP annotations.

    Looks the tool up by exact name in the current index and returns its
    full (un-truncated) docstring alongside the same `inputSchema` and
    `annotations` an MCP client sees from `tools/list`.

    Use when: reading a specific tool's full contract after finding its
    name with `discover_tool_search`.
    Not for: searching by keyword — use `discover_tool_search`.
    Returns: `{"name", "namespace", "description", "input_schema",
    "annotations", "access"}`. `input_schema` is the tool's JSON-schema
    parameter dict; `annotations` is the four MCP hints plus `title` as a
    plain dict, or `None` for a tool whose module has not yet declared them.
    `access` is `"read"`, `"write"` or `"destructive"` (ADR-0002/0003
    annotation class), computed fail-closed even when `annotations` is
    `None`.
    Errors: `{"error": "not_found", "name": name}` when no tool with that
    exact name is in the index.
    """
    index = _ensure_index()
    for e in index:
        if e["name"] == name:
            annotations = e["_annotations"]
            return {
                "name": e["name"],
                "namespace": e["namespace"],
                "description": e["_full_description"],
                "input_schema": e["_input_schema"],
                "annotations": annotations.model_dump() if annotations is not None else None,
                "access": _tool_class(annotations),
            }
    return {"error": "not_found", "name": name}


@mcp.tool(annotations=read("Rebuild the tool search index"))
def refresh_index() -> dict:
    """Force-rebuild the in-memory tool search index.

    Drops the cached index and rebuilds it from `_ROOT.list_tools()`. Only
    nexus's own in-process cache is affected — nothing in HA changes.

    Use when: tools were mounted or changed at runtime after the index was
    first built (normally only relevant during development, since
    `server.py` mounts every namespace once at startup).
    Not for: routine searches — `discover_tool_search`/
    `discover_list_namespaces`/`discover_get_tool_doc` already rebuild the
    index lazily on first use.
    Returns: `{"status": "rebuilt", "tools": <count>}`.
    """
    global _INDEX
    _INDEX = None
    idx = _ensure_index()
    return {"status": "rebuilt", "tools": len(idx)}


# --- ADR-0003 P3: tool_mode=search -----------------------------------------

# Full, namespace-prefixed names of the three lookup tools above, kept
# visible (pinned) in `tools/list` under `tool_mode=search` — everything
# else in this module (`refresh_index`) and in every other namespace is
# hidden from the list but stays directly callable (see `NexusToolSearch`
# docstring below).
_PINNED_SEARCH_MODE_TOOLS = frozenset(
    {"discover_tool_search", "discover_get_tool_doc", "discover_list_namespaces"}
)

# One call-by-name proxy per ADR-0002 annotation class, keyed by `_tool_class()`'s
# own return values so a lookup never drifts out of sync with the classifier.
_PROXY_TOOL_NAMES: dict[str, str] = {
    "read": "discover_call_read_tool",
    "write": "discover_call_write_tool",
    "destructive": "discover_call_destructive_tool",
}

_PROXY_TITLES: dict[str, str] = {
    "read": "Call a read-only tool by name",
    "write": "Call a write tool by name",
    "destructive": "Call a destructive tool by name",
}

_PROXY_DOCSTRINGS: dict[str, str] = {
    "read": """Call any read-only Nexus tool by its full name, with arguments validated against its own schema.

Resolves `name` through the live catalogue (any namespace, e.g. "entities_get_entity") and forwards `arguments` to it exactly as a direct call would, after validating them with that tool's own input schema — the target function itself never runs on a validation failure. Only reachable when the add-on's tool_mode option is "search", which replaces tools/list with discover's own lookup tools plus one call proxy per annotation class; the target tool's own name stays wired to the same handler and keeps working if called directly, and stays subject to the add-on's read_only/disabled_namespaces policy exactly as before.

Use when: tool_mode is "search", discover_tool_search found a tool whose annotations classify it read-only, and you now want to call it by name.
Not for: a tool classified write or destructive — this proxy refuses those; use discover_call_write_tool or discover_call_destructive_tool instead.
Returns: the target tool's own result, unchanged.
Errors: {"error": "not_found", "name": name} when no tool with that exact name exists or is currently enabled by the add-on's read_only/disabled_namespaces policy; {"error": "wrong_proxy", "message", "use"} when name resolves to a tool outside this proxy's class.""",
    "write": """Call any write-class Nexus tool by its full name, with arguments validated against its own schema.

Resolves `name` through the live catalogue and forwards `arguments` to it exactly as a direct call would, after validating them with that tool's own input schema. A write-class tool changes Home Assistant or add-on state but is additive, reversible, or supplies its own data (ADR-0002 R2/R5a/R5b/R6) — never a destructive one. Only reachable when tool_mode is "search"; the target tool's own name keeps working if called directly, and stays subject to the add-on's read_only/disabled_namespaces policy exactly as before.

Use when: tool_mode is "search", discover_tool_search found a tool whose annotations classify it write, and you now want to call it by name.
Not for: a read-only or destructive tool — this proxy refuses those; use discover_call_read_tool or discover_call_destructive_tool instead.
Returns: the target tool's own result, unchanged.
Errors: {"error": "not_found", "name": name} when no tool with that exact name exists or is currently enabled by the add-on's read_only/disabled_namespaces policy; {"error": "wrong_proxy", "message", "use"} when name resolves to a tool outside this proxy's class.""",
    "destructive": """Call any destructive Nexus tool by its full name, with arguments validated against its own schema.

Resolves `name` through the live catalogue and forwards `arguments` to it exactly as a direct call would, after validating them with that tool's own input schema. A destructive-class tool can interrupt availability, or lose/overwrite data the caller did not supply (ADR-0002 R2/R3/R4). Its own server-side safeguards (e.g. confirm=True) still apply unchanged — this proxy adds none of its own, it only forwards arguments. Only reachable when tool_mode is "search"; the target tool's own name keeps working if called directly, and stays subject to the add-on's read_only/disabled_namespaces policy exactly as before.

Use when: tool_mode is "search", discover_tool_search found a tool whose annotations classify it destructive, and you now want to call it by name.
Not for: a read-only or write tool — this proxy refuses those; use discover_call_read_tool or discover_call_write_tool instead.
Returns: the target tool's own result, unchanged.
Errors: {"error": "not_found", "name": name} when no tool with that exact name exists or is currently enabled by the add-on's read_only/disabled_namespaces policy; {"error": "wrong_proxy", "message", "use"} when name resolves to a tool outside this proxy's class.""",
}


@dataclass(frozen=True)
class _ProxySpec:
    """One class's worth of `NexusToolSearch` proxy: its full tool name,
    which `_tool_class()` bucket it serves, and the `openWorldHint` to
    advertise (OR'd across every currently-visible member of that class)."""

    name: str
    cls: Literal["read", "write", "destructive"]
    open_world: bool


def _proxy_annotations(cls: Literal["read", "write", "destructive"], *, open_world: bool) -> ToolAnnotations:
    title = _PROXY_TITLES[cls]
    if cls == "read":
        return read(title, open_world=open_world)
    if cls == "write":
        return write(title, idempotent=False, open_world=open_world)
    return destructive(title, idempotent=False, open_world=open_world)


def build_tool_search_transform(visible_tools: Sequence[Tool]) -> "NexusToolSearch":
    """Build the ADR-0003 P3 transform from the process's already
    policy-filtered tool catalogue.

    `visible_tools` must already exclude anything P1 (`read_only`) or P2
    (`disabled_namespaces`) disabled — `policy.apply_policy()` is the only
    caller, and passes it the same snapshot it just used to compute its own
    `disabled_tools`, minus that set (see `apply_policy()`).

    Which proxy classes exist and each one's `openWorldHint` are computed
    **once, here** — matching ADR-0003's "wyliczony przy starcie" — because
    `policy.py`'s own module docstring guarantees the policy, and therefore
    this visible set, is static for the life of the process; there is no
    later point where a class could gain or lose members.
    """
    by_class: dict[str, list[Tool]] = {"read": [], "write": [], "destructive": []}
    for t in visible_tools:
        by_class[_tool_class(t.annotations)].append(t)

    specs: list[_ProxySpec] = []
    for cls in ("read", "write", "destructive"):
        members = by_class[cls]
        if not members:
            # ADR-0003: "Proxy klasy bez żadnego widocznego narzędzia... nie
            # jest wystawiane" — e.g. write/destructive under read_only.
            continue
        open_world = any(bool(t.annotations and t.annotations.openWorldHint) for t in members)
        specs.append(_ProxySpec(name=_PROXY_TOOL_NAMES[cls], cls=cls, open_world=open_world))
    return NexusToolSearch(specs)


class NexusToolSearch(CatalogTransform):
    """ADR-0003 P3: replace `tools/list` with this module's 3 lookup tools
    plus one call-by-name proxy per ADR-0002 annotation class.

    Deliberately not FastMCP's own `BaseSearchTransform`/`BM25SearchTransform`:
    those synthesize a single, annotation-less `call_tool` proxy which, per
    `mcp.types.ToolAnnotations`' own defaults, a client honouring MCP hints
    would see as destructive and open-world regardless of what it actually
    calls — throwing away exactly the read/write/destructive distinction
    ADR-0002 built. Splitting into three proxies, one per class, keeps a
    client's consent scoped to the risk of what it is actually about to run.

    `tool_mode` is a context-budget feature, not a security boundary: any
    tool this transform hides from `tools/list` stays directly callable by
    its own name (`get_tool()` below delegates every non-synthetic name to
    `call_next`, i.e. whatever `policy.apply_policy()`'s own
    `server.disable()` calls already decided). P1 (`read_only`) and P2
    (`disabled_namespaces`) remain the only enforcement layer — this
    transform is added to the server strictly *after* both, so the
    `visible_tools` `build_tool_search_transform()` computed proxy classes
    from already reflects every P1/P2 disable, and this transform's own
    proxy dispatch re-resolves each call through `ctx.fastmcp.get_tool()` /
    `call_tool()`, which apply the exact same Visibility filtering a direct
    `tools/call` would.
    """

    def __init__(self, proxy_specs: Sequence[_ProxySpec]) -> None:
        super().__init__()
        self._specs: dict[str, _ProxySpec] = {s.name: s for s in proxy_specs}

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        # `tools` here has already passed through every transform registered
        # before this one (Visibility included) but Visibility only *marks*
        # disabled tools rather than removing them (see
        # `fastmcp.server.transforms.visibility.Visibility`), so `is_enabled()`
        # is still needed to actually drop them from what `tools/list` shows.
        pinned = [t for t in tools if is_enabled(t) and t.name in _PINNED_SEARCH_MODE_TOOLS]
        proxies = [self._build_proxy_tool(spec) for spec in self._specs.values()]
        return [*pinned, *proxies]

    async def get_tool(self, name, call_next, *, version=None):
        spec = self._specs.get(name)
        if spec is not None:
            return self._build_proxy_tool(spec)
        return await call_next(name, version=version)

    def _build_proxy_tool(self, spec: _ProxySpec) -> Tool:
        expected_cls = spec.cls

        async def call_proxy(
            name: Annotated[
                str,
                Field(
                    description=(
                        "Full tool name including namespace, e.g. 'entities_get_entity', "
                        "from discover_tool_search."
                    )
                ),
            ],
            arguments: Annotated[
                dict[str, Any] | None,
                Field(
                    description=(
                        "Arguments for the target tool, matching its own input schema "
                        "exactly. Omit for a tool with no required parameters."
                    )
                ),
            ] = None,
            ctx: Context = None,  # type: ignore[assignment]
        ) -> Any:
            target = await ctx.fastmcp.get_tool(name)
            if target is None:
                return {"error": "not_found", "name": name}
            actual_cls = _tool_class(target.annotations)
            if actual_cls != expected_cls:
                return {
                    "error": "wrong_proxy",
                    "message": f"'{name}' is a {actual_cls} tool, not {expected_cls}.",
                    "use": _PROXY_TOOL_NAMES[actual_cls],
                }
            return await ctx.fastmcp.call_tool(name, arguments or {})

        call_proxy.__doc__ = _PROXY_DOCSTRINGS[expected_cls]
        return Tool.from_function(
            fn=call_proxy,
            name=spec.name,
            annotations=_proxy_annotations(expected_cls, open_world=spec.open_world),
        )
