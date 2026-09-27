"""Static, per-process tool-exposure policy (ADR-0003 P1 + P2).

Two add-on options gate which MCP tools and prompts a client can see or
call, enforced through FastMCP's own Visibility mechanism
(`FastMCP.disable()`) so a hidden tool is also *refused* at `tools/call`,
not merely omitted from `tools/list`:

- **`read_only`** (`NEXUS_READ_ONLY`): hides and blocks every tool whose MCP
  annotations do not declare `readOnlyHint=True` — i.e. everything except
  ADR-0002's `read()` preset — and every MCP prompt. Prompts carry no
  annotations at all, so there is no way to prove one only ever reads;
  fail-closed hides all three of them.
- **`disabled_namespaces`** (`NEXUS_DISABLED_NAMESPACES`, comma-separated):
  hides and blocks every tool/prompt whose mount namespace (the prefix
  parsed from `server.py`'s `mcp.mount(..., namespace="...")` calls) is in
  the list.

Both are evaluated **once**, by `apply_policy()`, called from `server.main()`
after every `tools/<ns>.py` module has been mounted onto the root server
(and after `discover.bind_root()` has run, so the snapshot this module reads
sees every namespace, `discover` included). `import server` on its own
(as done by `tests/contract/generate_tool_surface.py` and most of this test
suite) never calls `apply_policy()` — the tool surface it relies on for
T1/T2 stays the full, unfiltered 323-tool catalogue.

Classification is fail-closed by construction: a tool with no annotations,
or with `readOnlyHint` set to anything other than the literal `True`, is
treated as **not** read-only. There is no separate "unknown" bucket that
policy could accidentally let through.

Policy is static for the process lifetime — there is no per-session or
per-API-key variant (`stateless_http=True` rules out the former; the latter
needs the OAuth work tracked as a follow-up). Changing an option requires an
add-on restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

# Reuses discover.py's own namespace parser instead of keeping a second,
# driftable copy of "which mount() calls in server.py define which
# namespace". discover.py already justifies reading server.py's source
# directly (to avoid a circular import); this borrows that same parsing,
# not a second implementation of it.
from tools.discover import _known_namespaces, _namespace_for

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


class PolicyConfigError(RuntimeError):
    """An add-on option cannot be parsed into a valid `ToolPolicy`.

    `server.main()` catches this, logs a readable message, and exits before
    ever binding a port — a typo in a security-relevant option must never
    silently leave a wider surface enabled than the operator asked for.
    """


@dataclass(frozen=True)
class ToolPolicy:
    """Value object: the tool-exposure policy for this process's lifetime."""

    read_only: bool = False
    disabled_namespaces: frozenset[str] = field(default_factory=frozenset)
    # Reserved for ADR-0003 P3 (0.23.0, tool-search mode). Fixed at "full"
    # until that partition wires a `NEXUS_TOOL_MODE` option through
    # `config.yaml`/`run.sh` — `from_env()` never reads an env var for it.
    tool_mode: Literal["full", "search"] = "full"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        known_namespaces: frozenset[str] | None = None,
    ) -> ToolPolicy:
        """Parse `NEXUS_READ_ONLY` / `NEXUS_DISABLED_NAMESPACES` strictly.

        `known_namespaces` defaults to the live set parsed out of
        `server.py`'s `mcp.mount(..., namespace="...")` calls (see module
        docstring). Pass it explicitly in tests that mount a different set
        of namespaces onto a throwaway root.
        """
        env = os.environ if env is None else env
        known = (
            frozenset(_known_namespaces())
            if known_namespaces is None
            else known_namespaces
        )

        read_only = _parse_bool(env.get("NEXUS_READ_ONLY", "false"), option="read_only")
        disabled = _parse_namespaces(env.get("NEXUS_DISABLED_NAMESPACES", ""), known=known)

        return cls(read_only=read_only, disabled_namespaces=disabled)


def _parse_bool(raw: str, *, option: str) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise PolicyConfigError(
        f"Add-on option {option!r} must be a boolean (true/false), got {raw!r}. "
        f"Fix it in the add-on's Configuration tab."
    )


def _parse_namespaces(raw: str, *, known: frozenset[str]) -> frozenset[str]:
    names = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = sorted(names - known)
    if unknown:
        raise PolicyConfigError(
            f"Add-on option 'disabled_namespaces' names unknown namespace(s) "
            f"{unknown}. Known namespaces: {sorted(known)}. Fix it in the "
            f"add-on's Configuration tab."
        )
    return names


def _is_read_only_tool(tool: object) -> bool:
    """Fail-closed: only a literal `readOnlyHint=True` counts as read-only."""
    annotations = getattr(tool, "annotations", None)
    return annotations is not None and getattr(annotations, "readOnlyHint", None) is True


async def _snapshot(server: FastMCP) -> tuple[list, list]:
    """The full, pre-policy tool/prompt catalogue `server` currently mounts."""
    tools = list(await server.list_tools())
    prompts = list(await server.list_prompts())
    return tools, prompts


def _run_snapshot(server: FastMCP) -> tuple[list, list]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_snapshot(server))
    # `apply_policy()` is only ever called from `server.main()`, before any
    # event loop exists. This mirrors `tools/discover.py`'s own fallback for
    # the (untested-in-practice) case of an already-running loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, _snapshot(server)).result()


class PolicyDeniedMiddleware(Middleware):
    """UX only: a named reason instead of FastMCP's generic "Unknown tool".

    All enforcement lives in the `FastMCP.disable()` Visibility transform
    `apply_policy()` applies below; this middleware only makes the client's
    error message explain *why*. Removing it makes the message worse, never
    the access wider — `tests/test_tool_policy.py` asserts exactly that by
    calling a disabled tool with this middleware stripped back out.
    """

    def __init__(self, disabled_tools: frozenset[str], policy: ToolPolicy) -> None:
        self._disabled_tools = disabled_tools
        self._policy = policy

    def _reason(self) -> str:
        reasons = []
        if self._policy.read_only:
            reasons.append("read_only")
        if self._policy.disabled_namespaces:
            reasons.append("disabled_namespaces")
        return " and ".join(reasons) or "policy"

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", None)
        if name in self._disabled_tools:
            raise ToolError(f"Tool '{name}' is disabled by the add-on policy ({self._reason()})")
        return await call_next(context)


@dataclass(frozen=True)
class PolicyApplication:
    """Summary `apply_policy()` returns, for the startup log and Setup UI."""

    policy: ToolPolicy
    tools_total: int
    tools_disabled: int
    prompts_total: int
    prompts_disabled: int


def apply_policy(server: FastMCP, policy: ToolPolicy) -> PolicyApplication:
    """Enforce `policy` on `server`, in place. Call exactly once, from `main()`.

    Must run after every `mcp.mount()` in `server.py` (and after
    `discover.bind_root()`) so the snapshot taken here includes every
    namespace — `import server` alone never calls this function.
    """
    tools, prompts = _run_snapshot(server)

    disabled_tools: set[str] = set()
    disabled_prompts: set[str] = set()

    if policy.read_only:
        disabled_tools |= {t.name for t in tools if not _is_read_only_tool(t)}
        # Prompts have no MCP annotations at all -- fail-closed, hide all of
        # them rather than guess which ones only ever read.
        disabled_prompts |= {p.name for p in prompts}

    if policy.disabled_namespaces:
        disabled_tools |= {
            t.name for t in tools if _namespace_for(t.name) in policy.disabled_namespaces
        }
        disabled_prompts |= {
            p.name for p in prompts if _namespace_for(p.name) in policy.disabled_namespaces
        }

    if disabled_tools:
        server.disable(names=disabled_tools, components={"tool"})
        server.add_middleware(PolicyDeniedMiddleware(frozenset(disabled_tools), policy))
    if disabled_prompts:
        server.disable(names=disabled_prompts, components={"prompt"})

    return PolicyApplication(
        policy=policy,
        tools_total=len(tools),
        tools_disabled=len(disabled_tools),
        prompts_total=len(prompts),
        prompts_disabled=len(disabled_prompts),
    )
