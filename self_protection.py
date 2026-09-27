"""Single source of truth for nexus's own add-on slug and the D-2
self-protection guard (ADR-0004 "Powiązany dług" D-2; hardened after the W3
Security review's NO-GO, see the review's M1/M4).

Why this exists as its own module
----------------------------------
D-2's threat model — a model or a hijacked MCP client using an add-on-slug-
targeted action to lift nexus's own `read_only`/`disabled_namespaces`
restrictions — has more than one door into Supervisor:

- `tools/supervisor.py`'s own `set_addon_options`/`uninstall_addon`/
  `stop_addon`, calling Supervisor's REST API directly;
- `services_call_service`/`ws_call_service` (`tools/services.py`,
  `tools/websocket.py`) dispatching the generic `hassio.*` domain HA Core
  registers for exactly this purpose (`homeassistant/components/hassio/
  services.py`, verified against `home-assistant/core` @ `main`, read
  2026-09-27) — `hassio.addon_stop`/`app_stop` and `hassio.addon_stdin`/
  `app_stdin` take a plain add-on/app slug in `data` and reach the *same*
  Supervisor operations the tools above already block, without ever calling
  `tools/supervisor.py` at all.

A single module every one of those call sites imports is the only way to
keep "nexus's own slug" and "which operations are blocked against it" from
drifting out of sync between them — the first W3 pass (guarding only
`tools/supervisor.py`) missed the `hassio.*` service path entirely, which is
exactly the bypass this module closes.

Scope: direct invocation only (accepted residual risk, ADR-0004)
------------------------------------------------------------------
This guard only covers *direct* invocation of the blocked operations — a
model or MCP client calling `supervisor_*`, `services_call_service` or
`ws_call_service` itself. It does **not** cover HA Core's own engine
executing `hassio.addon_stop`/`app_stop`/`addon_stdin`/`app_stdin`
indirectly on nexus's behalf — a script or automation written via
`automations_set_*_config` and run through `run_script`/`trigger_automation`,
a YAML file written through `files_*` and reloaded, a blueprint, or a Jinja
template. Home Assistant Supervisor / ADR-0004's "Zaakceptowane ryzyko
szczątkowe (0.24.0)" section records this gap as an accepted residual risk
(owner sign-off 2026-09-27), not an oversight: the actual security boundary
is the add-on's own options (`read_only`, `disabled_namespaces`), which none
of those indirect paths can change — `supervisor_set_addon_options` against
nexus's own slug is blocked fail-closed by this same module regardless of
which caller (direct or indirect) reaches it. Stopping nexus via an indirect
path is nexus disabling itself, not a privilege escalation.

Fail-closed contract (M4) — single entry point assumed
--------------------------------------------------------
This whole contract assumes `server.main()` is the *only* process entry
point that constructs a running nexus server — every state below is defined
in terms of "has `server.main()` called `set_own_slug(...)` yet", so a
second, ad-hoc way of starting the server (bypassing `server.main()`) would
leave this module's initialization state (and therefore D-2's guarantees)
undefined. `server.main()` calls `set_own_slug(...)` exactly once at
startup, only when running as a Supervisor add-on (`SUPERVISOR_TOKEN`
present) — reusing `addon_network.resolve_listen_plan()`'s own fail-closed,
retried `GET /addons/self/info` (`ListenPlan.own_slug`) rather than a second
fetch. That call is what `is_own_addon()` below keys off of, in three
states:

1. **Never initialized** (`set_own_slug()` never called — standalone /
   `NEXUS_HTTP=1` without `SUPERVISOR_TOKEN`, stdio mode, or a test that
   hasn't set it up): the guard is inert. There is no "own add-on" for a
   process that isn't one, so `is_own_addon()` returns `False` for every
   slug and nothing is blocked by this module.
2. **Initialized with a real slug** (the expected add-on-mode case —
   `resolve_listen_plan()` already aborts nexus's own startup via
   `AddonNetworkUnavailable` if this fetch fails outright, so reaching a
   running server in add-on mode means this is the common case):
   `is_own_addon(slug)` is `True` for that slug or the literal `"self"`
   alias Supervisor itself resolves to the calling add-on
   (`supervisor/api/apps.py::APIApps.get_app_for_request`: `if app_slug ==
   "self": app = request.get(REQUEST_FROM)` — resolved independent of the
   slug comparison, so both must be checked), `False` for anything else.
3. **Initialized with `None`** (add-on mode confirmed but the slug still
   isn't known — should not happen in practice given (2), but a defensive
   floor rather than an assumption): `is_own_addon(slug)` is `True` for
   *every* slug. An unknown identity can't be proven non-self, so every
   D-2-covered operation is refused until the real slug becomes known
   rather than silently waved through for whichever slug happens to be
   asked about first.
"""
from __future__ import annotations

_initialized = False
_own_slug: str | None = None


def set_own_slug(slug: str | None) -> None:
    """Record nexus's own add-on slug at startup (`server.main()`, add-on
    mode only) — `slug` may be `None` if add-on mode is confirmed but the
    slug itself could not be resolved (state 3 above). Never call this in
    standalone/stdio mode: doing so would turn the inert default (state 1)
    into state 3, refusing every D-2-covered operation for no reason."""
    global _initialized, _own_slug
    _initialized = True
    _own_slug = slug


def reset_for_tests() -> None:
    """Test-only: return to the uninitialized (inert, state 1) default."""
    global _initialized, _own_slug
    _initialized = False
    _own_slug = None


def get_own_slug() -> str | None:
    """The cached slug, or `None` in states 1 and 3 above."""
    return _own_slug


def is_initialized() -> bool:
    """Whether `set_own_slug()` has been called (states 2/3) — exposed
    mainly for tests asserting on the fail-closed boundary itself."""
    return _initialized


def is_own_addon(slug: str) -> bool:
    """True when `slug` refers to nexus's own add-on, per the three-state
    fail-closed contract in this module's docstring."""
    if not _initialized:
        return False
    if slug == "self":
        return True
    if _own_slug is None:
        return True
    return slug == _own_slug


# --- `hassio.*` service guard (M1) ------------------------------------------
#
# Exactly the fifteen `hassio.*` services HA Core registers as of
# `home-assistant/core` @ `main` (`homeassistant/components/hassio/
# services.py`/`services.yaml`, read 2026-09-27):
#
#   addon_start, addon_stop, addon_restart, addon_stdin  (legacy add-on names)
#   app_start,   app_stop,   app_restart,   app_stdin    (current app names)
#   host_reboot, host_shutdown
#   backup_full, backup_partial, restore_full, restore_partial, mount_reload
#
# Only the first eight take a single add-on/app slug in `data` (`ATTR_ADDON`
# = "addon", `ATTR_APP` = "app"; schema is `probatio.Required(ATTR_ADDON):
# VALID_ADDON_SLUG`, a plain string — never a list — but this module handles
# a list defensively anyway, see `_iter_slugs`). `host_*` take no slug at
# all (`SCHEMA_NO_DATA`) and `backup_*`/`restore_*`/`mount_reload` identify a
# *backup* or *device*, never nexus's own add-on identity — none of those
# seven can target "nexus's own add-on" in the first place, so they are
# deliberately outside this guard's scope (confirmed by reading the actual
# `probatio.Schema(...)` for each in `homeassistant/components/hassio/
# services.py`).
#
# Of the eight slug-taking services, blocked here (mirrors
# `tools/supervisor.py`'s own `stop_addon` block):
#
# - `addon_stop`/`app_stop` — identical effect to `supervisor_stop_addon`,
#   blocked for the same reason (cuts off the very connection carrying the
#   call, with no guarantee of a reply, and needs a separate action to
#   start it again).
# - `addon_stdin`/`app_stdin` — writes an arbitrary byte string straight to
#   the add-on container's stdin. No `tools/supervisor.py` tool exposes this
#   at all today, and there is no legitimate reason for an MCP caller to
#   feed nexus's own process arbitrary stdin bytes; blocked as a
#   conservative default (unpredictable effect on the running process,
#   zero legitimate use case) rather than an established, needed workflow.
#
# Deliberately NOT blocked:
#
# - `addon_start`/`app_start` — starting an already-running process is not
#   a privilege-escalation vector, mirrors `supervisor_start_addon` staying
#   unblocked.
# - `addon_restart`/`app_restart` — mirrors `supervisor_restart_addon`
#   staying unblocked (ADR-0004 recommendation): restarting nexus with
#   unchanged options is not privilege escalation and is nexus's own
#   legitimate self-recovery action. Blocking `set_addon_options` already
#   removes the only path from a restart to actually lifting
#   `read_only`/`disabled_namespaces` (there is nothing changed for the
#   restart to "pick up").

_SELF_HASSIO_SERVICES: dict[str, str] = {
    "addon_stop": "addon",
    "app_stop": "app",
    "addon_stdin": "addon",
    "app_stdin": "app",
}


def _iter_slugs(value: object) -> list[str]:
    """Every string slug `value` could plausibly hold — a bare string (the
    only shape HA Core's own schema actually allows for these services
    today) or a list/tuple/set of strings (handled defensively in case a
    caller's `data` doesn't match that schema, or a future HA release widens
    it) — never raises on an unexpected shape."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if isinstance(item, str)]
    return []


def blocked_hassio_service_call(domain: str, service: str, data: dict | None) -> dict | None:
    """Guard for the generic `hassio.*` service-dispatch tools
    (`services_call_service`, `ws_call_service`). Returns a `{"error": ...}`
    dict when `domain`/`service`/`data` would reach nexus's own add-on
    through one of the blocked services above (`is_own_addon` on every
    slug found in `data[<addon-or-app-key>]`, string or list), else `None`
    (call is not blocked by this guard and may proceed)."""
    if domain != "hassio":
        return None
    data_key = _SELF_HASSIO_SERVICES.get(service)
    if data_key is None:
        return None
    slugs = _iter_slugs((data or {}).get(data_key))
    if not any(is_own_addon(slug) for slug in slugs):
        return None
    return {
        "error": "self_addon_hassio_service_blocked",
        "message": (
            f"Refusing to call hassio.{service} against nexus's own add-on. Use the Home "
            "Assistant Supervisor UI instead, or hassio.addon_restart/app_restart "
            "(supervisor_restart_addon) if the goal is to recover from a stuck state."
        ),
    }
