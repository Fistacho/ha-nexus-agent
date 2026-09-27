"""Preset MCP tool annotations (ADR-0002 D2).

Single source of the tool-annotation taxonomy so every `@mcp.tool(...)`
decorator states all four `mcp.types.ToolAnnotations` hints explicitly,
instead of relying on the protocol defaults that apply whenever a hint is
left `None` (`readOnlyHint=False`, `destructiveHint=True`,
`idempotentHint=False`, `openWorldHint=True` — i.e. "assume destructive and
open-world" for anything undeclared).

This module defines no `mcp = FastMCP(...)` and registers no tools, so it is
never mounted in `server.py` and never picked up by `discover` as a
namespace, despite living under `tools/` with the rest of the namespaces.

Pick a preset by what the tool's code actually does today (ADR-0002 rule
table R1-R6), not by its name or HTTP method:

- `read()` — R1: no change to HA state, host, `/config`, devices or
  add-ons (list/get/search/find/render/validate/check/ping, passive
  WebSocket listen). Always `readOnly=True, destructive=False,
  idempotent=True`.
- `write()` — R2/R5a/R5b/R6: changes state or config but is additive,
  reversible, or the caller supplies everything that could otherwise be
  lost (runtime entity state, enable/disable, addititve create, running
  caller-owned content). `readOnly=False, destructive=False`.
- `destructive()` — R2/R3/R4: a generic pass-through whose effect depends
  on the argument, an availability interruption/restore, or loss/overwrite
  of data the caller did not supply (including version updates).
  `readOnly=False, destructive=True`.

`open_world` is orthogonal to the R1-R6 pick (see ADR-0002 D2): pass
`open_world=True` only when the tool can reach outside the HA instance and
its host — an internet fetch, an outbound notification, or a generic
service/event dispatch (R2). HA's own REST/WebSocket API, `/config`, the
Supervisor, ESPHome add-on and LAN devices are a closed world
(`open_world=False`, the default).
"""
from __future__ import annotations

from mcp.types import ToolAnnotations

_MAX_TITLE_LEN = 60


def _checked_title(title: str) -> str:
    if not title:
        raise ValueError("title is required and must be non-empty (ADR-0002 D2)")
    if len(title) > _MAX_TITLE_LEN:
        raise ValueError(f"title {title!r} is {len(title)} chars, must be <={_MAX_TITLE_LEN}")
    return title


def read(title: str, *, open_world: bool = False) -> ToolAnnotations:
    """R1 preset: no side effects on HA state, host, `/config`, devices or add-ons.

    Use for `list_`/`get_`/`search_`/`find_`/`render_`/`validate_`/`check_`/
    `ping_`-style tools and passive WebSocket listeners. `readOnly=True`
    forces `destructive=False` and `idempotent=True` regardless of
    arguments (ADR-0002 T3b), so this preset never needs an `idempotent`
    argument.
    """
    return ToolAnnotations(
        title=_checked_title(title),
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=open_world,
    )


def write(title: str, *, idempotent: bool, open_world: bool = False) -> ToolAnnotations:
    """R2/R5a/R5b/R6 preset: modifies something but is additive, reversible or lossless.

    `idempotent` must reflect the worst case across the tool's own
    parameters (e.g. a toggle-style action is `idempotent=False` even if a
    set-style action on the same tool would be `True`). Never `readOnly` or
    `destructive`.
    """
    return ToolAnnotations(
        title=_checked_title(title),
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def destructive(title: str, *, idempotent: bool, open_world: bool = False) -> ToolAnnotations:
    """R2/R3/R4 preset: generic dispatch, availability interruption, or data loss.

    Covers a generic pass-through call whose effect depends on the argument
    (R2), interrupting or restoring HA/host/add-on/device availability
    (R3), and deleting or overwriting data/config the caller did not
    supply, including version updates (R4). `idempotent` follows the R4
    split: `True` for delete or full-replace-with-the-same-argument,
    `False` for version updates and partial modification.
    """
    return ToolAnnotations(
        title=_checked_title(title),
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )
