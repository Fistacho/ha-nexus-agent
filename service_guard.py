"""ADR-0006 "confirm w generycznym wywolaniu uslug": the closed list of
HA/Supervisor services guarded by `confirm` on every direct-dispatch path,
plus the shared name-validation and confirm-gate helpers `tools/services.py`,
`tools/websocket.py` and `ha_client.py` all import from here instead of
keeping their own, driftable copies.

Why this exists as its own module
----------------------------------
Panel Security (2026-09-27) found that a model handed `confirmation_required`
by one of the 17 dedicated tools registered in `tests/test_confirm_policy.py`
(`system_restart_ha`, `supervisor_restore_backup`, `automations_remove_group`,
...) could simply call the generic `services_call_service`/`ws_call_service`
dispatchers instead and skip the prompt entirely -- those two tools only ever
ran nexus's own D-2 self-protection guard (`self_protection.py`), never a
`confirm` check of their own. A single module every guarded call site imports
is the only way to keep "which services are guarded" and "which tool is the
preferred, already-confirm-gated alternative" from drifting apart between
`tools/services.py` and `tools/websocket.py`.

D3: service-name identity (fixes F2-F4)
-----------------------------------------
Home Assistant's own REST API folds `domain`/`service` to lower case before
dispatch, but the URL segment nexus's `ha_client.call_service` builds keeps
whatever case (and characters) the caller passed -- so `"HASSIO"`/
`"ADDON_STOP"` reaches the exact same handler as `"hassio"`/`"addon_stop"`
would (F2), and httpx's own URL-joining semantics mean a `service` value
containing `?` starts a query string, `#` a fragment (dropped before the
request is even sent), and `..` segments get normalised away entirely (F3) --
so `domain="light", service="../hassio/addon_stop"` actually reaches
`/api/services/hassio/addon_stop`. `validate_service_name` closes both: only
ASCII letters, digits and underscores are accepted for either name, matched
with `re.fullmatch` (never `re.match`/`re.search`, which would let a suffix
like the `?x`/`../...` cases above slip through a prefix match). Every caller
below applies this *before* any other check, and matches case-insensitively
against the guarded/self-protection lists (`.lower()`) rather than requiring
the caller to have already normalised the case itself -- `self_protection.
blocked_hassio_service_call` does the same (see that module for the F4 fix).
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

_SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9_]+", re.ASCII)


def validate_service_name(name: str) -> bool:
    """True iff `name` is a bare HA domain/service identifier.

    ASCII letters, digits and underscores only, matched with
    `re.fullmatch` -- no dots, slashes, `?`, `#` or anything else httpx's own
    URL-joining (`?`/`#`/`..` segments) or HA's own case-folding could be
    used to reach a different domain/service than the one that was
    validated (ADR-0006 D3, closes F2-F4). Applies identically to `domain`
    and to `service`.
    """
    return isinstance(name, str) and bool(_SERVICE_NAME_RE.fullmatch(name))


# --- Path-segment injection (Security review follow-up, 2026-09-28,
# "same class as F3") -------------------------------------------------------
#
# `validate_service_name`/D3 above only ever covered `services_call_service`/
# `ws_call_service`'s own `domain`/`service` arguments. Every OTHER f-string
# URL path `ha_client.py` (and `tools/supervisor.py`'s own Supervisor client)
# builds from a caller-supplied value has the identical structural bug: httpx
# resolves `..` segments and treats a `?`/`#` in the composed path string as
# the start of a query string/fragment *before* the request is ever sent
# (same mechanism as F3), so an unvalidated `entity_id`, automation/script
# config id, or Supervisor add-on/backup slug can redirect the request to a
# different endpoint than the one named. `path_segment()` below is the one
# helper every such call site routes through instead of duplicating this
# logic per f-string.

# HA Core's own `homeassistant/core.py` (verified against
# home-assistant/core@dev, read 2026-09-28):
#
#   _OBJECT_ID = r"(?!_)[\da-z_]+(?<!_)"
#   _DOMAIN = r"(?!.+__)" + _OBJECT_ID
#   VALID_ENTITY_ID = re.compile(r"^" + _DOMAIN + r"\." + _OBJECT_ID + r"$")
#
# Reproduced verbatim (not just "close enough") so `valid_entity_id` accepts
# exactly the same strings HA's own `/api/states/<entity_id>` would.
_ENTITY_OBJECT_ID = r"(?!_)[\da-z_]+(?<!_)"
_ENTITY_DOMAIN = r"(?!.+__)" + _ENTITY_OBJECT_ID
_VALID_ENTITY_ID_RE = re.compile(r"^" + _ENTITY_DOMAIN + r"\." + _ENTITY_OBJECT_ID + r"$")


def valid_entity_id(entity_id: str) -> bool:
    """True iff `entity_id` matches HA Core's own `valid_entity_id` pattern:
    `<domain>.<object_id>`, each a lower-case `[\\da-z_]+` slug with no
    leading/trailing underscore, `domain` additionally forbidding a `__`
    anywhere. Rejects `/`, `?`, `#`, `..`, uppercase, and anything else HA's
    own endpoint would never have accepted either -- unlike
    `validate_service_name`, this does not lower-case first: a real
    `entity_id` is always already lower-case, so accepting a mixed-case one
    here would validate a string HA's `/api/states/<entity_id>` does not
    actually recognise as the same entity.
    """
    return isinstance(entity_id, str) and _VALID_ENTITY_ID_RE.match(entity_id) is not None


_PATH_STRUCTURAL_CHARS = ("/", "\\", "?", "#")


def _looks_path_unsafe(value: str) -> bool:
    """True iff `value` contains a `/`, `\\`, `?`, `#` or a literal `..` --
    every character/sequence that could turn one intended path segment into
    more than one, start a query string/fragment, or walk a parent
    directory once the surrounding f-string path is parsed."""
    if ".." in value:
        return True
    return any(ch in value for ch in _PATH_STRUCTURAL_CHARS)


def path_segment(value: str, kind: str) -> str:
    """Validate (and for `kind="identifier"`, encode) `value` before it is
    spliced into an f-string URL path segment. Raises `ValueError` --
    before any URL is built or request made -- when `value` is unsafe for
    `kind`.

    - `kind="entity_id"`: validated against `valid_entity_id` and returned
      unchanged (the literal `.` separator is structurally meaningful to
      the endpoint and the pattern already forbids every unsafe character).
    - `kind="identifier"`: a generic caller-supplied path component whose
      real-world alphabet isn't a single fixed, verifiable upstream pattern
      (automation/script config ids, event types, Supervisor add-on/backup
      slugs, a self-computed ISO timestamp). Raises outright -- rather than
      merely encoding around it -- when `value`, or `value` decoded once
      via `urllib.parse.unquote` (closing a percent-encoded disguise of the
      same payload, e.g. `"%2e%2e%2f...", before either check), contains a
      `/`, `\\`, `?`, `#` or a literal `..`: none of those are legitimate in
      a real id/event-type, and failing loud (zero I/O) matches the
      fail-before-I/O convention the rest of ADR-0006 already established,
      rather than relying solely on encoding. Whatever remains is
      percent-encoded via `urllib.parse.quote(value, safe="")` as a second,
      independent layer -- so any other special character (space, unicode)
      still can't be resolved as more than the single path segment it was
      meant to be, even if some future caller of this function forgets to
      check for the specific payloads above.
    """
    if kind == "entity_id":
        if not valid_entity_id(value):
            raise ValueError(f"invalid entity_id: {value!r}")
        return value
    if kind == "identifier":
        if not isinstance(value, str) or not value:
            raise ValueError(f"invalid path segment: {value!r}")
        if _looks_path_unsafe(value) or _looks_path_unsafe(urllib.parse.unquote(value)):
            raise ValueError(f"invalid path segment: {value!r}")
        return urllib.parse.quote(value, safe="")
    raise ValueError(f"unknown path segment kind: {kind!r}")


@dataclass(frozen=True)
class GuardedService:
    """One `domain.service` pair from the closed `GUARDED_SERVICES` list.

    Always constructed (or compared) via lower-cased `domain`/`service` --
    `is_guarded`/`confirm_gate` below do that normalisation, so equality
    here is effectively case-insensitive for every caller that goes through
    them instead of constructing/comparing `GuardedService` directly.
    """

    domain: str
    service: str


# ADR-0006 D2: exactly these eight. `update.install` is guarded whole,
# without inspecting its target entity (an ESPHome/HACS/Core/OS/Supervisor
# update all reach this same service) -- MCP tool annotations carry no
# argument-dependent classification (option (d) in the ADR was rejected for
# exactly this reason). Deliberately NOT here: `hassio.backup_*`,
# `backup.create*`, `hassio.addon_*` against any add-on other than nexus's
# own (that is `self_protection.py`'s job, a separate guard), and
# `recorder.purge*`. Changing this set requires a new ADR or a
# DECISIONS-LOG entry plus a Security review (ADR-0006 D2).
GUARDED_SERVICES: frozenset[GuardedService] = frozenset(
    {
        GuardedService("homeassistant", "restart"),
        GuardedService("homeassistant", "stop"),
        GuardedService("hassio", "host_reboot"),
        GuardedService("hassio", "host_shutdown"),
        GuardedService("hassio", "restore_full"),
        GuardedService("hassio", "restore_partial"),
        GuardedService("update", "install"),
        GuardedService("group", "remove"),
    }
)


def is_guarded(domain: str, service: str) -> bool:
    """True iff `domain.service` (compared case-insensitively) is one of
    `GUARDED_SERVICES`. Callers are expected to have already run
    `validate_service_name` on both -- this makes no attempt to reject a
    malformed name itself."""
    return GuardedService(domain.lower(), service.lower()) in GUARDED_SERVICES


# ADR-0006 D2: every MCP tool with its own server-side `confirm` gate
# (the 17-tool registry in `tests/test_confirm_policy.py`, plus
# `services_call_service` itself once it gains `confirm`) maps to either the
# tuple of `GuardedService` pairs reaching the same HA/Supervisor effect --
# the dedicated tool `services_call_service`'s refusal message points a
# caller at instead -- or a short justification string when no guarded
# service produces that effect at all (a Supervisor-only REST operation, a
# config-entry/CRUD registry write, or nexus's own git working copy). The
# completeness test in `tests/test_service_guard.py` asserts this dict's
# keys are exactly the set of tools with a `confirm` parameter in
# `server.mcp.list_tools()`, and that every guarded-pair value is a subset
# of `GUARDED_SERVICES` -- so a new `confirm`-guarded tool can't be added
# without also deciding its entry here.
CONFIRM_TOOL_EQUIVALENTS: dict[str, tuple[GuardedService, ...] | str] = {
    "system_restart_ha": (GuardedService("homeassistant", "restart"),),
    "system_stop_ha": (GuardedService("homeassistant", "stop"),),
    "supervisor_restart_core": (GuardedService("homeassistant", "restart"),),
    "supervisor_restart_host": (GuardedService("hassio", "host_reboot"),),
    "supervisor_restore_backup": (GuardedService("hassio", "restore_full"),),
    "automations_remove_group": (GuardedService("group", "remove"),),
    "esphome_upload_device": (GuardedService("update", "install"),),
    "services_call_service": tuple(
        sorted(GUARDED_SERVICES, key=lambda g: (g.domain, g.service))
    ),
    "supervisor_delete_backup": (
        "No guarded HA service performs a bare backup delete -- Supervisor's "
        "own REST API (DELETE /backups/<slug>) is the only path."
    ),
    "supervisor_uninstall_addon": (
        "No guarded HA service uninstalls an add-on -- Supervisor's own REST "
        "API (POST /addons/<slug>/uninstall) is the only path."
    ),
    "integrations_remove_integration": (
        "No guarded HA service removes a config entry -- the config-entry "
        "registry WebSocket API is the only path."
    ),
    "automations_delete_scene": (
        "No guarded HA service deletes a scene config -- the scene "
        "config-CRUD REST API is the only path."
    ),
    "automations_delete_automation": (
        "No guarded HA service deletes an automation config -- the "
        "automation config-CRUD REST API is the only path."
    ),
    "automations_delete_script": (
        "No guarded HA service deletes a script config -- the script "
        "config-CRUD REST API is the only path."
    ),
    "dashboards_remove_dashboard_resource": (
        "No guarded HA service removes a dashboard resource -- the Lovelace "
        "resource-CRUD WebSocket API is the only path."
    ),
    "git_git_rollback_file": (
        "No guarded HA service rolls back a file in /config -- this uses "
        "nexus's own git working copy, not a HA service."
    ),
    "git_git_rollback_to_commit": (
        "No guarded HA service rolls back /config to a commit -- this uses "
        "nexus's own git working copy, not a HA service."
    ),
    "files_delete_config_file": (
        "No guarded HA service deletes a file in /config -- the files REST "
        "API is the only path."
    ),
}


def dedicated_tool_for(domain: str, service: str) -> str | None:
    """The dedicated MCP tool name to prefer for `domain.service` (compared
    case-insensitively), if one of `CONFIRM_TOOL_EQUIVALENTS`'s guarded-pair
    entries names it. `None` when no dedicated tool covers this exact pair
    -- including every lookup against `services_call_service`'s own entry,
    deliberately skipped here since it is the generic dispatcher this
    function's result is meant to steer callers *away from*, not toward.
    """
    target = GuardedService(domain.lower(), service.lower())
    for tool_name, equivalents in CONFIRM_TOOL_EQUIVALENTS.items():
        if tool_name == "services_call_service":
            continue
        if isinstance(equivalents, tuple) and target in equivalents:
            return tool_name
    return None


def confirm_gate(
    domain: str, service: str, data: dict | None, *, confirm: bool
) -> dict | None:
    """ADR-0006 D4: the `services_call_service` confirm gate.

    Returns the ADR-0003 D1 refusal shape (`{"error":
    "confirmation_required", "message": ..., "action": ...}`) when
    `domain.service` (compared case-insensitively) is one of
    `GUARDED_SERVICES` and `confirm` is not `True`; `None` when the call may
    proceed -- either because `confirm=True` was given for a guarded
    service, or because the service isn't guarded at all, in which case
    `confirm` is ignored regardless of its value. Callers must run
    `validate_service_name` and `self_protection.blocked_hassio_service_call`
    first -- this makes no attempt to re-check either.
    """
    if not is_guarded(domain, service):
        return None
    if confirm:
        return None
    dedicated = dedicated_tool_for(domain, service)
    prefer = (
        f" Prefer the dedicated tool {dedicated}(confirm=True) if it fits this case."
        if dedicated
        else ""
    )
    return {
        "error": "confirmation_required",
        "message": (
            f"'{domain}.{service}' is a guarded Home Assistant service (ADR-0006) -- "
            f"confirm is a human-in-the-loop checkpoint, not a security boundary.{prefer} "
            "Call again with confirm=True to proceed via call_service."
        ),
        "action": f"call_service(domain={domain!r}, service={service!r}, data={data!r}, confirm=True)",
    }
