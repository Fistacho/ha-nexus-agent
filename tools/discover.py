"""Tool discovery — search the catalogue instead of memorising all 248.

For AI clients with limited context (Sonnet/Opus session budgets), iterating
over every tool's docstring at idle time burns tokens. This module ships a
small index + keyword search so the assistant can ask "what can I do with
covers?" and get a ranked shortlist before invoking the right one.

It's an *additive* helper, not a replacement: every tool stays accessible
through its normal name. Mount this module last so its index sees every
sibling already registered.
"""
from __future__ import annotations

import ast
import asyncio
import math
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Iterable

from fastmcp import FastMCP
from pydantic import Field

from tools._contract import read

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
    Returns: list of `{"name", "namespace", "summary", "score"}` dicts,
    highest score first; empty list if nothing scores above zero.
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
    "annotations"}`. `input_schema` is the tool's JSON-schema parameter
    dict; `annotations` is the four MCP hints plus `title` as a plain dict,
    or `None` for a tool whose module has not yet declared them.
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
