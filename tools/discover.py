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

import asyncio
import math
import re
from collections import Counter
from typing import Any, Iterable

from fastmcp import FastMCP

mcp = FastMCP("discover")

# Filled by `bind_root` from server.py once all modules are mounted.
_ROOT: FastMCP | None = None
_INDEX: list[dict[str, Any]] | None = None
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def bind_root(root: FastMCP) -> None:
    """Register the root FastMCP whose tools we should index. Call after mount()s."""
    global _ROOT, _INDEX
    _ROOT = root
    _INDEX = None  # lazy rebuild on next search


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
        namespace = name.split("_", 1)[0] if "_" in name else name
        tokens = _tokenize(name) + _tokenize(desc)
        index.append(
            {
                "name": name,
                "namespace": namespace,
                "summary": _summarise(desc),
                "_tokens": Counter(tokens),
                "_full_description": desc,
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

@mcp.tool()
def tool_search(query: str, top_k: int = 10, namespace: str | None = None) -> list[dict]:
    """Fuzzy search the Nexus tool catalogue.

    Returns the top `top_k` tools matching `query`, scored by BM25-lite over
    name + description. Pass `namespace` (e.g. "card_builder") to restrict
    matches. Each hit: `{name, namespace, summary, score}`.

    Use this to discover the right tool name before calling it — instead of
    keeping the full ~250-tool surface in working memory.
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


@mcp.tool()
def list_namespaces() -> list[dict]:
    """Compact map of every Nexus tool namespace and its tool count."""
    index = _ensure_index()
    counts: Counter = Counter(e["namespace"] for e in index)
    return [{"namespace": ns, "count": c} for ns, c in counts.most_common()]


@mcp.tool()
def get_tool_doc(name: str) -> dict:
    """Full description (docstring) of a single tool — call after `tool_search` to read details."""
    index = _ensure_index()
    for e in index:
        if e["name"] == name:
            return {
                "name": e["name"],
                "namespace": e["namespace"],
                "description": e["_full_description"],
            }
    return {"error": "not_found", "name": name}


@mcp.tool()
def refresh_index() -> dict:
    """Force-rebuild the search index (run after dynamically adding tools at runtime)."""
    global _INDEX
    _INDEX = None
    idx = _ensure_index()
    return {"status": "rebuilt", "tools": len(idx)}
