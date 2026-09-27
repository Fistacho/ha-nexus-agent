"""Golden-file generator for the MCP tool surface contract (ADR-0002, T1).

`tool_surface.json` is the API-surface snapshot that `tests/test_tool_contract.py`
(test T1) diffs the live server against on every run. It captures, per tool
name, the JSON-schema shape of its parameters: names, `type`/`anyOf`/`items`,
`default`, and top-level `required`. It deliberately does NOT capture
`description` (recursively stripped) because prose is expected to change
throughout the ADR-0002 migration (batches A-F) without that being an API
break — only the batch-0 golden file has to survive the whole migration
unchanged.

    ============================================================
    REGENERATE ONLY AFTER A DELIBERATE, REVIEWED API CHANGE.
    ============================================================

Tool names and parameter names/types/defaults/required-ness are the nexus
public API (see workspace CLAUDE.md: "Nazwy narzędzi MCP to publiczne API dla
modeli — stabilne; zmiana nazwy = breaking change"). If `test_t1_...` fails,
the default assumption is a bug (accidental rename, dropped/added parameter,
changed default), not a stale golden file. Only regenerate when the diff was
intended and reviewed, then call that out explicitly in the commit/PR — never
regenerate silently as a side effect of an unrelated change.

Usage (works from any cwd; paths are resolved relative to this file):

    python tests/contract/generate_tool_surface.py

Requires HA_URL / HA_TOKEN in the environment (or `.env`) purely so
`import server` succeeds — no live HTTP call is made to build the tool list.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]  # nexus/
_GOLDEN_FILE = Path(__file__).resolve().parent / "tool_surface.json"

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Mirrors tests/conftest.py: only needed so `import server` (and whatever it
# imports at module level) doesn't blow up outside a real HA/add-on env. No
# network call happens while building the tool list.
os.environ.setdefault("NEXUS_API_KEY", "test-api-key")
os.environ.setdefault("HA_TOKEN", "test-ha-token")
os.environ.setdefault("HA_URL", "http://ha.test:8123")

_IMPORT_RE = re.compile(r'^from tools\.(\w+) import mcp as (\w+)\s*$', re.MULTILINE)
_MOUNT_RE = re.compile(r'^mcp\.mount\((\w+),\s*namespace="(\w+)"\)\s*$', re.MULTILINE)


def strip_descriptions(node: Any) -> Any:
    """Recursively drop every `description` key from a JSON-schema fragment.

    Descriptions are prose, not API surface — dropping them lets T1 stay
    green while ADR-0002 batches rewrite every docstring/Field(description=).
    """
    if isinstance(node, dict):
        return {k: strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [strip_descriptions(v) for v in node]
    return node


def module_namespace_map(server_py_text: str | None = None) -> dict[str, str]:
    """Map `tools/<module>.py` -> mounted namespace, read from server.py.

    Parses `from tools.<module> import mcp as <var>` and
    `mcp.mount(<var>, namespace="<ns>")` lines rather than hardcoding the
    mapping, so it can't silently drift from server.py (e.g. `git_ops.py` ->
    namespace "git", `websocket.py` -> namespace "ws").
    """
    if server_py_text is None:
        server_py_text = (_ROOT / "server.py").read_text(encoding="utf-8")
    var_to_module = {var: module for module, var in _IMPORT_RE.findall(server_py_text)}
    var_to_namespace = {var: ns for var, ns in _MOUNT_RE.findall(server_py_text)}
    return {
        var_to_module[var]: ns
        for var, ns in var_to_namespace.items()
        if var in var_to_module
    }


def mount_call_count(server_py_text: str | None = None) -> int:
    """Literal count of `mcp.mount(` calls in server.py (T2's namespace count)."""
    if server_py_text is None:
        server_py_text = (_ROOT / "server.py").read_text(encoding="utf-8")
    return len(re.findall(r"^mcp\.mount\(", server_py_text, re.MULTILINE))


async def _collect_server_tools_async() -> dict[str, Any]:
    import server  # local import: needs sys.path/env set up above first

    tools = await server.mcp.list_tools()
    return {t.name: t.to_mcp_tool() for t in tools}


def collect_server_tools() -> dict[str, Any]:
    """Return {full tool name: mcp.types.Tool}, exactly what an MCP client sees."""
    return asyncio.run(_collect_server_tools_async())


def build_golden() -> dict[str, Any]:
    tools = collect_server_tools()
    namespaces = set(module_namespace_map().values())
    return {
        "tool_count": len(tools),
        "namespace_count": len(namespaces),
        "tools": {
            name: {"inputSchema": strip_descriptions(tool.inputSchema)}
            for name, tool in sorted(tools.items())
        },
    }


def main() -> None:
    golden = build_golden()
    _GOLDEN_FILE.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Wrote {_GOLDEN_FILE} "
        f"({golden['tool_count']} tools, {golden['namespace_count']} namespaces)."
    )


if __name__ == "__main__":
    main()
