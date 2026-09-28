"""Home Assistant Supervisor API — add-on lifecycle, backups, host/core management.

Requires SUPERVISOR_TOKEN env var (auto-set when running as HA add-on).
config.yaml must have `hassio_api: true` and `hassio_role: manager`.
"""
from __future__ import annotations

import os
import re
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field

import self_protection
import service_guard
from tools._contract import destructive, read, write

mcp = FastMCP("supervisor")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_BASE_URL = "http://supervisor"

# --- Secret redaction (ADR-0004 "Powiazany dług" D-1) -----------------------
#
# `GET /addons/<slug>/info` (Supervisor `supervisor/api/apps.py::APIApps.info_data`)
# returns `options` unredacted whenever the caller is the app itself or holds
# the "manager"/"admin" hassio_role — nexus's own `config.yaml` declares
# `hassio_role: manager`, so every installed add-on's options (this nexus
# add-on's own API key, other add-ons' passwords/tokens) come back in full on
# every call, regardless of which add-on is being inspected:
#
#     expose_options = (
#         not isinstance(request_from, App)
#         or request_from is app
#         or request_from.hassio_role in (ROLE_MANAGER, ROLE_ADMIN)
#     )
#     ...
#     ATTR_OPTIONS: app.options if expose_options else {},
#     ATTR_SCHEMA: app.schema_ui,
#
# `app.schema_ui` (`supervisor/apps/options.py::UiOptions._single_ui_option`)
# is a `list[dict]`, one entry per top-level option key, each with a `"name"`
# and — for a `password(...)`-typed option — `"type": "string"` plus
# `"format": "password"`:
#
#     elif value.startswith(_PASSWORD):
#         ui_node["type"] = "string"
#         ui_node["format"] = "password"
#
# A nested option group serializes as `{"name", "type": "schema", "schema":
# [...]; "multiple": bool}` with its own list of child nodes, mirroring the
# nested `options` dict/list one level down. `schema_ui` is `None` when the
# add-on declares `schema: false` (`options.py::AppOptions.schema_ui` returns
# `None` for a bool raw schema) — in that case there is no type information
# at all and only the key-name heuristic below applies.
#
# Three independent redaction signals are combined, all recursively, since
# none alone is reliable: the schema can be absent/stale, a key can be named
# `mqtt_password` without the add-on ever declaring `password(...)` in its
# own `config.yaml`, and a field named e.g. `broker` can still hold a
# `scheme://user:pass@host` URL with the secret embedded in the value itself
# rather than in a dedicated field (W3 Security review, M2/M3 — the first
# pass covered only the schema and key-name signals).

_REDACTED = "**REDACTED**"

# Heuristic key-name tokens (case-insensitive, matched against
# underscore/camelCase-split tokens of the option key): a whole-word or
# suffix match on any of these flags the value as a likely secret even when
# the schema says nothing about it. "key" is handled separately below so it
# also catches a bare `key` field and a `..._key`/`apikey` suffix without
# also flagging unrelated words that merely contain "key" mid-token. "pass"/
# "pwd" (M2) are exact-token matches only (never substring), so
# `mqtt_pass`/`db_pwd` are flagged while `passenger`/`bypass_cache` — neither
# of which splits into a bare `pass` token — are not.
_SECRET_KEY_TOKENS = {"password", "passwd", "pass", "pwd", "secret", "token", "credential", "private"}

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# M3: a URL with inline credentials (`scheme://user:pass@host[...]`) redacts
# the whole string value, regardless of the option's key name or schema —
# neither signal above would catch e.g. `{"broker":
# "mqtt://iot:s3cr3t@core-mosquitto:1883"}`, where "broker" looks like plain
# connection config and the schema (if any) declares it a plain `url`/`str`,
# not `password`. Deliberately conservative: any `scheme://user:pass@` prefix
# redacts the entire value rather than trying to surgically cut out just the
# credentials, since a same-shape false positive (a URL that merely contains
# a literal "@" for an unrelated reason) is far cheaper than a missed secret.
_CREDENTIALS_IN_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@]+:[^\s/@]+@")


def _normalize_key(key: str) -> str:
    """Split `key` into lowercase, underscore-joined tokens for the heuristic
    below — handles both `snake_case` and `camelCase`/`PascalCase` option
    names (Supervisor add-on schemas use either convention)."""
    spaced = _CAMEL_BOUNDARY_RE.sub("_", str(key))
    return _NON_ALNUM_RE.sub("_", spaced.lower()).strip("_")


def _looks_like_secret_key(key: str) -> bool:
    """True when `key`'s name alone (regardless of any schema) suggests a
    secret value, per the token list/`key`-suffix rule above. Plural forms
    (`credentials`, `secrets`, `tokens`, `passwords`, `api_keys`, ...) are
    also matched via a trailing-`s` singularization, checked in addition to
    (never instead of) the exact-token match so that `pass`/`pwd` themselves
    stay exact-token matches and `passenger`/`bypass_cache` stay unflagged."""
    normalized = _normalize_key(key)
    if not normalized:
        return False
    for part in normalized.split("_"):
        singular = part.rstrip("s") or part
        if part in _SECRET_KEY_TOKENS or singular in _SECRET_KEY_TOKENS:
            return True
        if part in ("key", "keys") or part.endswith("key") or part.endswith("keys"):
            return True
    return False


def _schema_name_map(schema: Any) -> dict[str, dict]:
    """`{option name: its schema_ui node}` for one level of a Supervisor
    add-on's UI schema, or `{}` when `schema` isn't the expected `list[dict]`
    (missing, `None` from a `schema: false` add-on, or malformed)."""
    if not isinstance(schema, list):
        return {}
    return {
        node["name"]: node
        for node in schema
        if isinstance(node, dict) and isinstance(node.get("name"), str)
    }


def _classify_scalar(value: Any, node: dict, key_should_redact: bool) -> str | None:
    """Which of the three M2/M3 redaction signals (if any) applies to one
    scalar `options` leaf. Priority order when more than one could apply:
    the add-on's own schema (most authoritative — the add-on itself declared
    this a password field) beats the key-name heuristic, which beats the
    in-value URL-credentials scan (the least specific signal, and the only
    one that runs regardless of key name or schema at all)."""
    if node.get("format") == "password":
        return "schema_password"
    if key_should_redact:
        return "key_name_heuristic"
    if isinstance(value, str) and _CREDENTIALS_IN_URL_RE.search(value):
        return "credentials_in_url"
    return None


def _redact(
    value: Any,
    schema_by_name: dict[str, dict],
    path: str,
    key: str | None = None,
    node: dict | None = None,
) -> tuple[Any, list[dict[str, str]]]:
    """Recursively redact one add-on's `options` value (or a piece of it).

    `schema_by_name` is this level's `_schema_name_map(...)`; `key`/`node`
    are the option key and its schema node one level up — carried down
    unchanged through a `multiple` list so every scalar element gets the
    same classification as its parent key (a nested dict's own keys are
    always reclassified from its own `schema_by_name`, never inherited).
    Returns the redacted copy and a list of `{"path", "reason"}` dicts (path
    e.g. `"mqtt.password"`, `"tokens[0]"`; reason one of `_classify_scalar`'s
    three signals) for every leaf actually redacted — empty/`None` values are
    left as-is so a caller can still see a field is unset.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        redacted_fields: list[dict[str, str]] = []
        for child_key, val in value.items():
            item_path = f"{path}.{child_key}" if path else str(child_key)
            child_node = schema_by_name.get(child_key, {})
            nested_schema = (
                _schema_name_map(child_node.get("schema")) if child_node.get("type") == "schema" else {}
            )
            new_val, sub_fields = _redact(val, nested_schema, item_path, key=child_key, node=child_node)
            result[child_key] = new_val
            redacted_fields.extend(sub_fields)
        return result, redacted_fields
    if isinstance(value, list):
        result_list: list[Any] = []
        redacted_fields = []
        for idx, item in enumerate(value):
            item_path = f"{path}[{idx}]"
            new_item, sub_fields = _redact(item, schema_by_name, item_path, key=key, node=node)
            result_list.append(new_item)
            redacted_fields.extend(sub_fields)
        return result_list, redacted_fields
    key_should_redact = _looks_like_secret_key(key) if key is not None else False
    reason = _classify_scalar(value, node or {}, key_should_redact)
    if reason and value not in (None, ""):
        return _REDACTED, [{"path": path, "reason": reason}]
    return value, []


def _redact_addon_info_data(data: dict) -> tuple[dict, list[dict[str, str]]]:
    """Redact the `options` of one `GET /addons/<slug>/info` `data` object,
    using its own `schema` for the password-typed fields, the key-name
    heuristic, and an in-value URL-credentials scan — all applied
    recursively and independently of each other. Returns `data` unchanged
    (with no redacted fields) when `options` isn't a `dict` (missing/
    `None`)."""
    options = data.get("options")
    if not isinstance(options, dict):
        return data, []
    schema_by_name = _schema_name_map(data.get("schema"))
    redacted_options, redacted_fields = _redact(options, schema_by_name, "")
    new_data = dict(data)
    new_data["options"] = redacted_options
    return new_data, redacted_fields


def _contains_redaction_marker(value: Any) -> bool:
    """True when `value` (an options tree passed to `set_addon_options`)
    contains the redaction placeholder anywhere, dict/list nesting included —
    used to decide whether a read-modify-write round trip is needed at all."""
    if value == _REDACTED:
        return True
    if isinstance(value, dict):
        return any(_contains_redaction_marker(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_redaction_marker(v) for v in value)
    return False


class RedactionMergeError(ValueError):
    """Raised by `_merge_redaction_markers` (S1, W3 Security review) when a
    placeholder in a list can't be safely resolved back to a real stored
    value — currently only a list-length mismatch (see below)."""


def _merge_redaction_markers(new_value: Any, old_value: Any, path: str = "") -> Any:
    """Replace every redaction placeholder in `new_value` with the
    corresponding leaf from `old_value` (the add-on's currently stored
    options), preserving `new_value`'s own edits everywhere else.

    S1 (W3 Security review): a list is merged strictly *by index* — item 0
    against item 0, item 1 against item 1, and so on — since there is no
    other way to tell which stored item an echoed-back placeholder refers
    to. That only gives the right answer when the caller sent the list back
    in the same order and length it was read in, so a list containing a
    placeholder whose length doesn't match the currently stored list raises
    `RedactionMergeError` instead of silently guessing (padding missing
    positions with `None`, as a plain zip would) — the caller must re-read
    the current value and either keep every item in the same order, or
    replace the placeholder(s) with an explicit value instead of echoing
    them back positionally-mismatched.
    """
    if new_value == _REDACTED:
        return old_value
    if isinstance(new_value, dict):
        old_dict = old_value if isinstance(old_value, dict) else {}
        return {
            k: _merge_redaction_markers(v, old_dict.get(k), f"{path}.{k}" if path else str(k))
            for k, v in new_value.items()
        }
    if isinstance(new_value, list):
        old_list = old_value if isinstance(old_value, list) else []
        if _contains_redaction_marker(new_value) and len(new_value) != len(old_list):
            raise RedactionMergeError(
                f"option {path or '<root>'!r}: the submitted list has {len(new_value)} "
                f"item(s) and contains the redaction placeholder, but the currently stored "
                f"list has {len(old_list)} item(s) — can't tell which stored item each "
                "placeholder refers to. Read the current value again and resend every item "
                "in the same order, or replace the placeholder(s) with an explicit value."
            )
        return [
            _merge_redaction_markers(
                item, old_list[i] if i < len(old_list) else None, f"{path}[{i}]"
            )
            for i, item in enumerate(new_value)
        ]
    return new_value


# Own-add-on identity and the "is this slug nexus itself" check moved to
# `self_protection.py` (W3 Security review M1/M4): a per-call `GET
# /addons/self/info` here couldn't be shared with `tools/services.py`'s
# generic `hassio.*` dispatch (M1's finding — that path reached the exact
# same Supervisor operations without ever calling this module), and a
# per-call fetch also couldn't fail closed the way M4 requires (nothing to
# fail *to* if the fetch's own failure is what's being asked about). See
# `self_protection.is_own_addon` and its module docstring for the resulting
# three-state, fail-closed contract; `server.main()` feeds it nexus's own
# slug once at startup from `addon_network.resolve_listen_plan()`'s own
# fetch.


def _sanitize_error_detail(resp: dict) -> dict:
    """Strip Supervisor's raw `detail` text from an error response (S2, W3
    Security review) — used only on the branch of `set_addon_options` that
    already reconstituted a real secret into `options` via the
    redaction-marker merge, so a Supervisor validation error that echoes the
    submitted value back (a real, observed Supervisor behaviour on a
    rejected options payload) can't leak that secret through this tool's own
    error response. Leaves non-error responses, and error responses with no
    `detail` field, untouched."""
    if not isinstance(resp, dict) or "error" not in resp or "detail" not in resp:
        return resp
    sanitized = dict(resp)
    sanitized["detail"] = "redacted — this request involved substituting a stored secret back in"
    return sanitized


def _invalid_slug_error(slug: str) -> dict:
    return {
        "error": "invalid_slug",
        "message": (
            f"Invalid add-on/backup slug: {slug!r}. Slugs must not contain "
            "'/', '\\\\', '?', '#' or '..'."
        ),
    }


def _validate_slug(slug: str) -> dict | None:
    """ADR-0006 follow-up (Security review, 2026-09-28, "same class as
    F3"): validate `slug` via `service_guard.path_segment(slug,
    "identifier")` before it is spliced into an f-string Supervisor REST
    path (`_supervisor_request`/`_supervisor_get_text`) -- otherwise a
    value like `"../host/shutdown"` could resolve to a different
    Supervisor endpoint than the add-on/backup one named. Returns
    `{"error": "invalid_slug", "message": ...}` (this module's own error
    convention, not a raised exception) when invalid, `None` when `slug`
    may be used as-is. Every slug-taking tool below calls this first --
    before `self_protection`/`confirm` -- so a malicious slug is refused
    with zero Supervisor requests regardless of either.
    """
    try:
        service_guard.path_segment(slug, "identifier")
    except ValueError:
        return _invalid_slug_error(slug)
    return None


def _supervisor_request(method: str, path: str, json: dict | None = None) -> dict:
    """Internal: call Supervisor REST API with bearer token from env."""
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return {"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA add-on for Supervisor API"}
    try:
        with httpx.Client(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as c:
            if method.upper() == "GET":
                r = c.request(method, path)
            else:
                r = c.request(method, path, json=json or {})
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text}
    except Exception as e:
        return {"error": str(e)}


def _supervisor_get_text(path: str) -> str:
    """Internal: GET a text endpoint (e.g. logs) instead of JSON."""
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return ""
    with httpx.Client(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as c:
        r = c.get(path)
        r.raise_for_status()
        return r.text


# --- Add-on lifecycle ---

@mcp.tool(annotations=read("List installed add-ons"))
def list_addons() -> dict:
    """List every installed add-on with its slug, name, state and version.

    Calls Supervisor's `GET /addons` and projects each entry down to slug,
    name, state, version, `version_latest` and `update_available`.

    Use when: getting an overview of installed add-ons and which have
    updates pending.
    Not for: full add-on detail (options, ports, boot mode) — use
    `supervisor_get_addon`.
    Returns: `{"addons": [{slug, name, state, version, version_latest,
    update_available}, ...]}`.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` on a Supervisor API error.
    """
    resp = _supervisor_request("GET", "/addons")
    if "error" in resp:
        return resp
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    addons = data.get("addons", []) if isinstance(data, dict) else []
    return {
        "addons": [
            {
                "slug": a.get("slug"),
                "name": a.get("name"),
                "state": a.get("state"),
                "version": a.get("version"),
                "version_latest": a.get("version_latest"),
                "update_available": a.get("update_available", False),
            }
            for a in addons
        ]
    }


@mcp.tool(annotations=read("Get add-on details"))
def get_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Get full Supervisor info for one add-on, with secret option values redacted.

    Calls `GET /addons/<slug>/info`: every field Supervisor stores for that
    add-on (options schema, current options, ports, boot mode, version,
    state and more), except `options` values are replaced with a fixed
    placeholder wherever the schema marks a field a password, the key name
    looks like a secret (`password`/`pass`/`pwd`, `token`, `api_key`,
    `secret`, `credential`, `private`, `key` as a word/suffix), or the value
    is a URL with inline credentials (`scheme://user:pass@host`) — covers
    nexus's own API key and other add-ons' secrets/connection strings
    alike, recursively through nested groups and lists. An empty/unset
    value stays empty. Adds `redacted_fields`: `{"path", "reason"}` dicts.

    Use when: the full add-on record is needed, e.g. before editing its
    options with `supervisor_set_addon_options`, which accepts the same
    placeholder back for an unchanged secret field without overwriting it.
    Not for: a quick overview across all add-ons — use
    `supervisor_list_addons`.
    Returns: Supervisor's add-on info payload with secret-looking `options`
    values replaced by a placeholder, plus `redacted_fields`.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error":
    "SUPERVISOR_TOKEN not set — Nexus must run as HA add-on for Supervisor
    API"}` when the token env var is missing; `{"error": "HTTP <status>",
    "detail": ...}` if the slug does not exist.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    resp = _supervisor_request("GET", f"/addons/{slug}/info")
    if "error" in resp:
        return resp
    data = resp.get("data")
    if not isinstance(data, dict):
        return resp
    redacted_data, redacted_fields = _redact_addon_info_data(data)
    result = dict(resp)
    result["data"] = redacted_data
    result["redacted_fields"] = redacted_fields
    return result


@mcp.tool(annotations=write("Install an add-on", idempotent=True, open_world=True))
def install_addon(
    slug: Annotated[
        str,
        Field(
            description=(
                "Slug of an add-on already visible in a registered repository "
                "(e.g. from `hacs_list_hacs_repositories`-style store listings "
                "or Supervisor's own add-on store)."
            )
        ),
    ],
) -> dict:
    """Install an add-on from its slug.

    Calls `POST /addons/<slug>/install`. The add-on must already belong to a
    repository Supervisor knows about; this does not add repositories.
    Installing downloads the add-on's image, which reaches outside the HA
    instance and its host.

    Use when: adding a new add-on that is already visible in an installed
    repository.
    Not for: starting/stopping an already-installed add-on — use
    `supervisor_start_addon`/`supervisor_stop_addon`.
    Returns: Supervisor's install-job result.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown.
    Limits: downloads the add-on image over the network; can take a while
    on a slow connection.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    return _supervisor_request("POST", f"/addons/{slug}/install")


@mcp.tool(annotations=destructive("Uninstall an add-on", idempotent=True))
def uninstall_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to uninstall, e.g. from `supervisor_list_addons`."),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually uninstall the add-on and its data. "
                "False (default) performs no action."
            )
        ),
    ] = False,
) -> dict:
    """Uninstall an add-on and its persistent data after an explicit confirmation.

    Calls `POST /addons/<slug>/uninstall`. Refuses to run at all against
    nexus's own add-on (its real slug, or the alias Supervisor resolves to
    the caller itself — see `self_protection.is_own_addon`) — checked first,
    with no Supervisor request made either way, before even the
    confirmation gate: nexus removing itself is never the right tool for
    that regardless of `confirm`. Otherwise, without `confirm=True` nothing
    is removed; the call only returns an error asking for confirmation
    (ADR-0003 D1).

    Use when: permanently removing an add-on that is no longer needed.
    Not for: temporarily stopping it while keeping its data and config — use
    `supervisor_stop_addon`.
    Returns: Supervisor's uninstall-job result when confirmed.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "self_addon_uninstall_blocked", "message": ...}` when
    `slug` is nexus's own add-on; returns `{"error":
    "confirmation_required", "message": ..., "action": ...}` when `confirm`
    is false; `{"error": "HTTP <status>", "detail": ...}` on a Supervisor
    API error.
    Limits: WARNING: deletes the add-on's data with no separate backup step;
    requires `confirm=True`.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    if self_protection.is_own_addon(slug):
        return {
            "error": "self_addon_uninstall_blocked",
            "message": (
                "Refusing to uninstall nexus's own add-on through this tool. Uninstall it "
                "through the Home Assistant Supervisor UI instead if that is really intended."
            ),
        }
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                f"This will uninstall add-on '{slug}' and delete its persistent data. "
                "Call again with confirm=True to proceed."
            ),
            "action": f"uninstall_addon(slug={slug!r}, confirm=True)",
        }
    return _supervisor_request("POST", f"/addons/{slug}/uninstall")


@mcp.tool(annotations=write("Start an add-on", idempotent=True))
def start_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to start, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Start an installed, stopped add-on.

    Calls `POST /addons/<slug>/start`.

    Use when: bringing an installed add-on online after it was stopped.
    Not for: installing a new add-on — use `supervisor_install_addon`; for
    stopping it — use `supervisor_stop_addon`.
    Returns: Supervisor's start-job result.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown or
    already running.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    return _supervisor_request("POST", f"/addons/{slug}/start")


@mcp.tool(annotations=destructive("Stop an add-on", idempotent=True))
def stop_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to stop, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Stop a running add-on.

    Calls `POST /addons/<slug>/stop`. The add-on — e.g. an MQTT broker other
    integrations depend on — becomes unavailable until started again.
    Refuses to run at all against nexus's own add-on (by its real slug or
    the literal alias Supervisor resolves to the caller itself): nexus
    stopping itself would cut off the very connection carrying this call,
    with no guarantee the response ever comes back, and — unlike
    `supervisor_restart_addon` — leaves nexus down until someone starts it
    again some other way.

    Use when: taking one add-on offline without uninstalling it.
    Not for: removing it entirely — use `supervisor_uninstall_addon`; for
    bringing it back — use `supervisor_start_addon`; for a brief restart of
    nexus itself — use `supervisor_restart_addon` instead, which is allowed.
    Returns: Supervisor's stop-job result.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "self_addon_stop_blocked", "message": ...}` when
    `slug` is nexus's own add-on; `{"error": "SUPERVISOR_TOKEN not set —
    Nexus must run as HA add-on for Supervisor API"}` when the token env var
    is missing; `{"error": "HTTP <status>", "detail": ...}` if the slug is
    unknown.
    Limits: interrupts the add-on's availability, and anything depending on
    it, until it is started again.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    if self_protection.is_own_addon(slug):
        return {
            "error": "self_addon_stop_blocked",
            "message": (
                "Refusing to stop nexus's own add-on through this tool — nexus stopping "
                "itself would sever this very call with no guarantee the response arrives, "
                "and would need a separate action to start it again. Use "
                "supervisor_restart_addon if the goal is to recover from a stuck state, or "
                "stop it through the Home Assistant Supervisor UI instead."
            ),
        }
    return _supervisor_request("POST", f"/addons/{slug}/stop")


@mcp.tool(annotations=destructive("Restart an add-on", idempotent=True))
def restart_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to restart, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Restart an add-on (stop, then start).

    Calls `POST /addons/<slug>/restart`. The add-on — which could be this
    nexus add-on itself — is briefly unavailable during the restart.

    Use when: an add-on needs to pick up new options or recover from a
    stuck state.
    Not for: a one-way stop — use `supervisor_stop_addon`; for restarting
    Core instead — use `supervisor_restart_core`.
    Returns: Supervisor's restart-job result.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown.
    Limits: briefly interrupts the add-on's availability.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    return _supervisor_request("POST", f"/addons/{slug}/restart")


@mcp.tool(annotations=destructive("Update an add-on", idempotent=False, open_world=True))
def update_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to update, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Update an add-on to its latest available version.

    Calls `POST /addons/<slug>/update`, which downloads and installs the new
    version over the network; the previous version is only recoverable from
    a backup taken beforehand.

    Use when: `update_available` is true for the add-on and the new version
    should be applied.
    Not for: reverting an update — restore a backup via
    `supervisor_restore_backup` instead; there is no dedicated rollback
    call.
    Returns: Supervisor's update-job result.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` on a failed update.
    Limits: downloads the new version over the network; no automatic
    rollback on failure.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    return _supervisor_request("POST", f"/addons/{slug}/update")


@mcp.tool(annotations=read("Get add-on log lines"))
def get_addon_logs(
    slug: Annotated[
        str,
        Field(description="Add-on slug to fetch logs for, e.g. from `supervisor_list_addons`."),
    ],
    lines: Annotated[
        int,
        Field(
            description=(
                "Maximum number of most-recent log lines to return. 100 by "
                "default; 0 or negative returns the full fetched log text."
            )
        ),
    ] = 100,
) -> dict:
    """Get the most recent log lines from one add-on.

    Calls `GET /addons/<slug>/logs`, which returns plain text, and keeps
    only the last `lines` lines.

    Use when: debugging one add-on's runtime behaviour.
    Not for: the ESPHome add-on's per-device compile/upload logs — use
    `esphome_get_addon_logs` if that distinction matters to the caller.
    Returns: `{"logs": "<joined log lines>"}`.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "SUPERVISOR_TOKEN not set or empty response"}` when
    the token is missing; `{"error": "HTTP <status>", "detail": ...}` on a
    Supervisor API error.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    try:
        text = _supervisor_get_text(f"/addons/{slug}/logs")
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text}
    except Exception as e:
        return {"error": str(e)}
    if not text:
        return {"error": "SUPERVISOR_TOKEN not set or empty response"}
    log_lines = text.splitlines()
    if lines > 0:
        log_lines = log_lines[-lines:]
    return {"logs": "\n".join(log_lines)}


@mcp.tool(annotations=destructive("Set add-on options", idempotent=True))
def set_addon_options(
    slug: Annotated[
        str,
        Field(description="Add-on slug whose options to set, e.g. from `supervisor_list_addons`."),
    ],
    options: Annotated[
        dict,
        Field(
            description=(
                "Full options object to store, validated by Supervisor against "
                "the add-on's schema. Any field read via `supervisor_get_addon` "
                "and left out here is not preserved automatically — include the "
                "whole dict, not just changed keys."
            )
        ),
    ],
) -> dict:
    """Replace an add-on's stored options with the given object.

    Calls `POST /addons/<slug>/options` as `{"options": options}`. Read
    current values via `supervisor_get_addon`, modify them, and send the
    full dict back — overwrites previous options entirely, so omitted
    fields are lost. A value left as the placeholder for a secret field is
    swapped for the real stored value first, so echoing it back unchanged
    never overwrites the secret — a list containing it must keep every
    item's order/count as read, or the call is refused rather than guessed.
    Once a placeholder was resolved, a Supervisor error strips `detail`
    (it can otherwise echo the rejected payload, secret included). Takes
    effect after `supervisor_restart_addon`. Refuses to run against
    nexus's own add-on — could otherwise lift its own restrictions.

    Use when: changing an add-on's configuration programmatically.
    Not for: reading the current options first — use
    `supervisor_get_addon`; for changing nexus's own configuration — use the
    Home Assistant Supervisor UI instead.
    Returns: Supervisor's options-update result.
    Errors: `{"error": "invalid_slug"}` for a bad slug; `self_addon_options_blocked` for nexus's own add-on;
    `redaction_marker_list_length_mismatch` on a placeholder list-length
    mismatch; `SUPERVISOR_TOKEN not set` when the token is missing;
    `{"error": "HTTP <status>"}` on a schema-validation or placeholder
    read-back failure.
    Limits: does not apply until the add-on is restarted.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    if self_protection.is_own_addon(slug):
        return {
            "error": "self_addon_options_blocked",
            "message": (
                "Refusing to change nexus's own add-on options through this tool — doing so "
                "could lift this nexus add-on's own read_only/disabled_namespaces "
                "restrictions. Change nexus's own configuration through the Home Assistant "
                "Supervisor UI (Settings > Add-ons > Nexus > Configuration) instead."
            ),
        }
    if _contains_redaction_marker(options):
        current = _supervisor_request("GET", f"/addons/{slug}/info")
        if "error" in current:
            # Nothing has been reconstituted into `options` yet at this point
            # (the merge below hasn't run) — this GET's own error detail is
            # Supervisor's generic "no such add-on"/HTTP-failure text, not an
            # echo of any payload, so S2's sanitization isn't needed here.
            return current
        current_data = current.get("data")
        current_options = current_data.get("options") if isinstance(current_data, dict) else None
        try:
            options = _merge_redaction_markers(
                options, current_options if isinstance(current_options, dict) else {}
            )
        except RedactionMergeError as exc:
            return {"error": "redaction_marker_list_length_mismatch", "message": str(exc)}
        result = _supervisor_request("POST", f"/addons/{slug}/options", json={"options": options})
        return _sanitize_error_detail(result)
    return _supervisor_request("POST", f"/addons/{slug}/options", json={"options": options})


@mcp.tool(annotations=read("Get add-on resource stats"))
def get_addon_stats(
    slug: Annotated[
        str,
        Field(description="Add-on slug to fetch stats for, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Get an add-on's current runtime resource usage.

    Calls `GET /addons/<slug>/stats` for CPU, memory, network and disk I/O
    figures at the moment of the call.

    Use when: checking whether an add-on is consuming unexpected resources.
    Not for: historical usage over time — this returns only a snapshot.
    Returns: Supervisor's raw stats payload (CPU percent, memory
    usage/limit, network and block I/O counters).
    Errors: `{"error": "invalid_slug"}` for a bad slug; `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the add-on is not
    running.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    return _supervisor_request("GET", f"/addons/{slug}/stats")


# --- Supervisor self / Core / Host ---

@mcp.tool(annotations=read("Get Supervisor info"))
def get_supervisor_info() -> dict:
    """Get info about the Supervisor component itself.

    Calls `GET /supervisor/info` for its version, update channel and health
    flag.

    Use when: checking the Supervisor's own version/channel/health, as
    opposed to Core or the host.
    Not for: Home Assistant Core details — use `supervisor_get_core_info`;
    for the host OS — use `supervisor_get_host_info`.
    Returns: Supervisor's raw info payload (version, channel, `healthy`,
    and related fields).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/supervisor/info")


@mcp.tool(annotations=read("Get HA Core info"))
def get_core_info() -> dict:
    """Get info about the Home Assistant Core container.

    Calls `GET /core/info` for its version, CPU architecture and machine
    type.

    Use when: checking Core's version/arch, as opposed to the Supervisor or
    host.
    Not for: HA's own location/units/component config — use
    `history_get_ha_config`.
    Returns: Supervisor's raw Core info payload (version, arch, machine).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/core/info")


@mcp.tool(annotations=read("Get host OS info"))
def get_host_info() -> dict:
    """Get info about the host operating system.

    Calls `GET /host/info` for Home Assistant OS version, hostname, kernel and related
    host-level fields.

    Use when: checking the underlying OS/hardware, as opposed to Core or the
    Supervisor.
    Not for: Supervisor's own version — use
    `supervisor_get_supervisor_info`.
    Returns: Supervisor's raw host info payload (Home Assistant OS version, hostname,
    kernel, and related fields).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/host/info")


@mcp.tool(annotations=destructive("Restart HA Core", idempotent=True))
def restart_core(
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually restart Core. False (default) performs "
                "no action."
            )
        ),
    ] = False,
) -> dict:
    """Restart Home Assistant Core through the Supervisor after an explicit confirmation.

    Calls `POST /core/restart`. WARNING: causes downtime for HA Core (and
    everything depending on it) until it comes back up. Without
    `confirm=True` nothing is restarted.

    Use when: Core needs restarting on a Supervisor install specifically.
    Not for: the equivalent call via HA's own service — use
    `system_restart_ha`; for rebooting the whole host — use
    `supervisor_restart_host`.
    Returns: Supervisor's restart-job result when confirmed.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "SUPERVISOR_TOKEN
    not set — Nexus must run as HA add-on for Supervisor API"}` when the
    token env var is missing.
    Limits: requires `confirm=True`; interrupts Core until it restarts.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                "This will restart Home Assistant Core via Supervisor (brief downtime). "
                "Call again with confirm=True to proceed."
            ),
            "action": "restart_core(confirm=True)",
        }
    return _supervisor_request("POST", "/core/restart")


@mcp.tool(annotations=destructive("Reboot the host", idempotent=True))
def restart_host(
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually reboot the host. False (default) performs "
                "no action."
            )
        ),
    ] = False,
) -> dict:
    """Reboot the whole host machine after an explicit confirmation.

    Calls `POST /host/reboot`. WARNING: Home Assistant, every add-on
    (including this nexus add-on) and the host OS go offline for minutes.
    Without `confirm=True` nothing is rebooted.

    Use when: a host-level change (e.g. a Home Assistant OS update) requires a full reboot.
    Not for: restarting only Core — use `supervisor_restart_core`; for
    restarting one add-on — use `supervisor_restart_addon`.
    Returns: Supervisor's reboot-job result when confirmed.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "SUPERVISOR_TOKEN
    not set — Nexus must run as HA add-on for Supervisor API"}` when the
    token env var is missing.
    Limits: requires `confirm=True`; takes the whole host, including this
    add-on, offline for minutes — the call itself may not receive a reply.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                "This will reboot the whole host machine, taking Home Assistant, every "
                "add-on and the host OS offline for minutes. Call again with confirm=True "
                "to proceed."
            ),
            "action": "restart_host(confirm=True)",
        }
    return _supervisor_request("POST", "/host/reboot")


# --- Backups ---

@mcp.tool(annotations=read("List Supervisor backups"))
def list_backups() -> dict:
    """List every backup Supervisor manages.

    Calls `GET /backups` for the full backup catalogue Supervisor tracks.

    Use when: finding a backup's slug before restoring or deleting it, or
    checking whether a scheduled backup succeeded.
    Not for: HA Core's own `backup.create`/`backup.create_automatic` backups
    when they were not made through Supervisor — those may not appear here
    under the same view.
    Returns: Supervisor's raw backups payload.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/backups")


@mcp.tool(annotations=write("Create a Supervisor backup", idempotent=False))
def create_backup(
    name: Annotated[
        str,
        Field(description="Human-readable name to store on the new backup."),
    ],
    addons: Annotated[
        list[str] | None,
        Field(
            description=(
                "Add-on slugs to include for a partial backup. Omit together "
                "with `folders` for a full backup of everything."
            )
        ),
    ] = None,
    folders: Annotated[
        list[str] | None,
        Field(
            description=(
                "Folder names to include for a partial backup, e.g. 'share', "
                "'ssl', 'media', 'addons/local'. Omit together with `addons` "
                "for a full backup of everything."
            )
        ),
    ] = None,
    password: Annotated[
        str | None,
        Field(
            description=(
                "Password to encrypt the backup with. Omit for an unencrypted "
                "backup."
            )
        ),
    ] = None,
) -> dict:
    """Create a new Supervisor backup, full or partial.

    Calls `POST /backups/new/full` when neither `addons` nor `folders` is
    given, otherwise `POST /backups/new/partial` with those slugs/folders.
    Unlike `system_create_backup`, this never deletes older backups — it
    only adds a new one.

    Use when: a named, partial and/or password-protected backup is needed on
    a Supervisor install.
    Not for: a quick backup using the instance's own automatic-backup
    settings (which may prune old ones) — use `system_create_backup`.
    Returns: Supervisor's backup-job result, including the new backup's
    slug on success.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` on a timeout or failure —
    check `supervisor_list_backups` before retrying.
    Limits: a full backup can take minutes; on a timeout error, check
    `supervisor_list_backups` before retrying rather than assuming failure.
    """
    payload: dict = {"name": name}
    if password:
        payload["password"] = password
    if addons is not None or folders is not None:
        if addons is not None:
            payload["addons"] = addons
        if folders is not None:
            payload["folders"] = folders
        return _supervisor_request("POST", "/backups/new/partial", json=payload)
    return _supervisor_request("POST", "/backups/new/full", json=payload)


@mcp.tool(annotations=destructive("Restore a full backup", idempotent=True))
def restore_backup(
    slug: Annotated[
        str,
        Field(description="Backup slug to restore, from `supervisor_list_backups`."),
    ],
    password: Annotated[
        str | None,
        Field(description="Password for an encrypted backup. Omit for an unencrypted one."),
    ] = None,
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually restore the backup. False (default) "
                "performs no action."
            )
        ),
    ] = False,
) -> dict:
    """Restore a full backup after an explicit confirmation.

    Calls `POST /backups/<slug>/restore/full`, which replaces the HA
    configuration, every add-on with its data, and the backed-up folders
    with the backup's contents; anything changed since the backup was taken
    is lost. Core and this nexus add-on restart as part of the restore, so
    the call may drop the connection before it can reply. Without
    `confirm=True` nothing is restored.

    Use when: reverting to a known-good state after a failed update or
    misconfiguration.
    Not for: restoring only some add-ons/folders — Supervisor's partial
    restore is not exposed by this tool.
    Returns: Supervisor's restore-job result when confirmed; the reply may
    never arrive because the add-on restarts mid-call.
    Errors: `{"error": "invalid_slug"}` for a bad slug; returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "HTTP <status>",
    "detail": ...}` if the slug or password is wrong.
    Limits: WARNING: discards every change made since the backup; requires
    `confirm=True`.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                f"This will restore backup '{slug}', discarding every change made since it "
                "was taken. Call again with confirm=True to proceed."
            ),
            "action": f"restore_backup(slug={slug!r}, confirm=True)",
        }
    payload: dict = {}
    if password:
        payload["password"] = password
    return _supervisor_request("POST", f"/backups/{slug}/restore/full", json=payload)


@mcp.tool(annotations=destructive("Delete a Supervisor backup", idempotent=True))
def delete_backup(
    slug: Annotated[
        str,
        Field(description="Backup slug to delete, from `supervisor_list_backups`."),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually delete the backup. False (default) "
                "performs no action and instead returns a confirmation "
                "prompt; the backup is not looked up or touched in that "
                "case."
            )
        ),
    ] = False,
) -> dict:
    """Permanently delete one backup after an explicit confirmation.

    Calls `DELETE /backups/<slug>`. Without `confirm=True` nothing is
    deleted; the call only returns a confirmation prompt. There is no undo
    once the backup file is gone — a backup is itself the undo mechanism
    for every other change in this add-on.

    Use when: cleaning up a backup that is confirmed to be no longer
    needed.
    Not for: any situation where the backup might still be needed — there
    is no recovery after this call, even with `confirm=True`.
    Returns: Supervisor's delete result when confirmed, otherwise a
    confirmation prompt.
    Errors: `{"error": "invalid_slug"}` for a bad slug; returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "SUPERVISOR_TOKEN
    not set — Nexus must run as HA add-on for Supervisor API"}` when the
    token env var is missing; `{"error": "HTTP <status>", "detail": ...}`
    if the slug does not exist.
    Limits: requires `confirm=True`; deletion is immediate and permanent
    with no undo once confirmed.
    """
    invalid = _validate_slug(slug)
    if invalid is not None:
        return invalid
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will permanently delete backup '{slug}'. Call again with confirm=True.",
            "action": f"delete_backup(slug={slug!r}, confirm=True)",
        }
    return _supervisor_request("DELETE", f"/backups/{slug}")
