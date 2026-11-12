"""Node-RED add-on — read flows, find HA entity usage, change one flow with an automatic backup.

Transport (verified against source, not assumed):

- The community Node-RED add-on (`hassio-addons/app-node-red`, installed as
  `<repo hash>_nodered`, e.g. `a0d7b954_nodered`) declares
  `host_network: true`. Its `/etc/node-red/config.js` (tag v22.2.2) loads the
  user's `settings.js` and then forces `uiHost = "127.0.0.1"`,
  `uiPort = 46836` and `adminAuth = null` ("Disable authentication, let HA
  handle that"), so Node-RED's Admin API listens unauthenticated on the
  host loopback only. The add-on's own nginx proxies to that same
  `127.0.0.1:46836` (`rootfs/etc/nginx/includes/upstream.conf`).
- nexus also runs with `host_network: true` (ADR-0004, `config.yaml`), so it
  shares the host's loopback and reaches `http://127.0.0.1:46836` directly —
  no HA user password, no ingress session, no extra Supervisor role.
- The other two doors are worse: ingress (`templates/ingress.gtpl`) only
  allows `172.30.32.2` (Supervisor), and an add-on cannot mint an ingress
  session; the optional direct port (`80/tcp`, default 1880,
  `templates/direct.gtpl`) sits behind nginx `auth_request` to Supervisor
  `/auth`, i.e. it would need an HA user's password stored in nexus's
  options. The flows files in `/addon_configs/<slug>/` are not mapped into
  nexus (`map: config:rw` only) and editing them would need an add-on
  restart to take effect.

The Supervisor API is used only to find the add-on (`GET /addons`) and check
it is started and host-networked (`GET /addons/<slug>/info`); the
`SUPERVISOR_TOKEN` goes into the `Authorization` header and is never logged,
returned, or sent to Node-RED.

Closed set of Node-RED endpoints (Node-RED 5.0.9, `@node-red/editor-api`
`lib/admin/index.js`), no generic passthrough: GET `/flows` and POST `/flows`
(both with `Node-RED-API-Version: v2`), GET `/flows/state`, GET `/nodes`
(`Accept: application/json`), GET `/settings`, GET `/diagnostics`. Every
write is a read-modify-write of the whole flow configuration through
POST `/flows` with the `rev` just read and `Node-RED-Deployment-Type: flows`
(only changed flows restart). Node-RED itself answers 409 `version_mismatch`
when someone deployed in between (`@node-red/runtime` `lib/api/flows.js`
`setFlows`), so no concurrent edit is overwritten silently. Flow ids are
never spliced into a URL path.

Credentials: GET `/flows` never contains node credentials — they live in
`flows_cred.json` and `credentials.clean()` / `extract()` strip them
(`@node-red/runtime` `lib/nodes/credentials.js`). Nodes posted back without
a `credentials` property keep the stored ones (`clean()` only drops the
credentials of node ids that disappeared). Tool output is additionally run
through the Supervisor tools' secret redaction (key names such as
`password`/`token`/`apikey`, `scheme://user:pass@` URLs), and a placeholder
echoed back by the caller is restored from the current value before deploy.

Backups: before every write the full flow list (as returned by GET `/flows`,
so without credentials) is written to nexus's private `/data/nodered_backups/`
(`NEXUS_DATA_PATH` overrides `/data`); the newest 30 are kept. Not under
`/config`, so git versioning and the `files_*` tools never see them. A
failed backup aborts the write before anything reaches Node-RED.

Node types are checked against the installed palette (GET `/nodes`) before
deploy: a flow with an unknown type stops every flow in Node-RED
(`flows/index.js` `start()` waits for "missing types"), so such a write is
refused instead.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Callable

import httpx
from fastmcp import FastMCP
from pydantic import Field

import service_guard
from tools._contract import destructive, read
from tools.supervisor import (
    _CREDENTIALS_IN_URL_RE,
    _REDACTED,
    RedactionMergeError,
    _contains_redaction_marker,
    _looks_like_secret_key,
    _merge_redaction_markers,
)

mcp = FastMCP("nodered")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_SUPERVISOR_URL = "http://supervisor"
# Fixed by the add-on itself (`config.js`: uiHost 127.0.0.1, uiPort 46836), see module docstring.
_NODERED_URL = "http://127.0.0.1:46836"
_NODERED_SLUG = "nodered"

_TIMEOUT = 15.0
# A deploy stops and restarts the changed flows inside the request.
_DEPLOY_TIMEOUT = 60.0

_MAX_BACKUPS = 30
_BACKUP_ID_RE = re.compile(r"flows-\d{8}T\d{12}Z(-\d{1,3})?")  # always used with fullmatch

_MAX_NODES = 2000
# JSON size of the `nodes` argument (Node-RED's own apiMaxLength default is 5 MB for the whole deploy)
_MAX_NODES_BYTES = 4 * 1024 * 1024
# `find_entity_usage` evaluates selector regexes from the flows with Python's engine, which has no timeout
_MAX_PATTERN_LEN = 200
# a quantified group that itself contains a quantifier, e.g. `(a+)+` — catastrophic backtracking
_NESTED_QUANTIFIER_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
_MAX_ID_LEN = 64
_MAX_TYPE_LEN = 128
_MAX_LABEL_LEN = 256
_MAX_INFO_LEN = 20000
_MAX_REV_LEN = 128
_MAX_FIELDS_PER_HIT = 10
_SEARCH_LIMIT_MAX = 500

# Not node types in Node-RED's registry; `parseConfig` skips them in its
# missing-type check (`@node-red/runtime` `lib/flows/util.js`).
_STRUCTURAL_TYPES = {"tab", "subflow", "group"}
_SUBFLOW_INSTANCE_RE = re.compile(r"subflow:(.+)")  # always used with fullmatch

_SETTINGS_KEYS = (
    "version",
    "httpNodeRoot",
    "context",
    "flowEncryptionType",
    "flowFilePretty",
    "externalModules",
)


class _NodeRedError(Exception):
    """Internal: carries one `{"error": code, ...}` result up to the tool."""

    def __init__(self, code: str, message: str | None = None, **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        result: dict[str, Any] = {"error": self.code}
        if self.message:
            result["message"] = self.message
        result.update(self.extra)
        return result


# --- Supervisor: find the add-on -------------------------------------------


def _supervisor_get(path: str) -> dict:
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        raise _NodeRedError(
            "SUPERVISOR_TOKEN not set — Nexus must run as HA add-on for Supervisor API"
        )
    try:
        with httpx.Client(
            base_url=_SUPERVISOR_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        ) as c:
            r = c.get(path)
    except httpx.HTTPError as e:
        raise _NodeRedError("supervisor_unreachable", type(e).__name__) from None
    if r.status_code >= 400:
        raise _NodeRedError(f"HTTP {r.status_code}", detail=r.text[:300])
    try:
        body = r.json()
    except ValueError:
        raise _NodeRedError("supervisor_protocol_error", "non-JSON Supervisor response") from None
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _is_nodered_slug(slug: str) -> bool:
    return slug == _NODERED_SLUG or slug.endswith("_" + _NODERED_SLUG)


def _locate_addon() -> dict:
    """Return `{"slug", "version"}` of a started, host-networked Node-RED add-on, or raise."""
    addons = _supervisor_get("/addons").get("addons") or []
    matches = [
        a for a in addons
        if isinstance(a, dict) and isinstance(a.get("slug"), str) and _is_nodered_slug(a["slug"])
    ]
    if not matches:
        raise _NodeRedError(
            "nodered_not_found",
            "No Node-RED add-on (slug '*_nodered') is installed.",
        )
    matches.sort(key=lambda a: (a.get("state") != "started", a["slug"]))
    slug = matches[0]["slug"]
    try:
        service_guard.path_segment(slug, "identifier")
    except ValueError:
        raise _NodeRedError("nodered_not_found", f"Unusable add-on slug {slug!r}.") from None
    try:
        info = _supervisor_get(f"/addons/{slug}/info")
    except _NodeRedError as e:
        if e.code in ("HTTP 400", "HTTP 404"):
            raise _NodeRedError("nodered_not_found", f"Add-on {slug} is not installed.", slug=slug) from None
        raise
    if info.get("state") != "started":
        raise _NodeRedError(
            "nodered_not_running",
            f"Add-on {slug} is not started.",
            slug=slug,
            state=info.get("state"),
        )
    if info.get("host_network") is not True:
        raise _NodeRedError(
            "nodered_unsupported_addon",
            f"Add-on {slug} does not use host networking, so its Admin API is not on 127.0.0.1:46836.",
            slug=slug,
        )
    return {"slug": slug, "version": info.get("version")}


# --- Node-RED Admin API: closed set of calls -------------------------------


def _error_detail(r: httpx.Response) -> Any:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    if isinstance(body, dict):
        return {k: str(body[k])[:300] for k in ("code", "message", "error") if k in body}
    return r.text[:300]


def _request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = _TIMEOUT,
) -> Any:
    hdrs = {"Accept": "application/json", "Node-RED-API-Version": "v2"}
    if headers:
        hdrs.update(headers)
    try:
        with httpx.Client(base_url=_NODERED_URL, timeout=timeout, follow_redirects=False) as c:
            r = c.request(method, path, json=json_body, headers=hdrs)
    except httpx.TimeoutException:
        raise _NodeRedError("timeout", f"Node-RED did not answer {path} within {timeout:.0f} s.") from None
    except httpx.HTTPError as e:
        raise _NodeRedError(
            "nodered_unreachable",
            f"Cannot reach the Node-RED Admin API at {_NODERED_URL} ({type(e).__name__}).",
        ) from None
    if r.status_code in (401, 403):
        raise _NodeRedError(
            "nodered_auth_required",
            "Node-RED refused the request; its Admin API has authentication enabled, "
            "which this tool cannot use.",
        )
    if r.status_code == 409:
        raise _NodeRedError(
            "rev_conflict",
            "Node-RED's flows changed since they were read (someone deployed); nothing was written.",
            detail=_error_detail(r),
        )
    if r.status_code == 404:
        raise _NodeRedError(
            "HTTP 404",
            f"Node-RED has no {path} endpoint (is httpAdminRoot changed in settings.js?).",
            detail=_error_detail(r),
        )
    if r.status_code >= 300:
        raise _NodeRedError(f"HTTP {r.status_code}", detail=_error_detail(r))
    if not r.content.strip():
        return None
    try:
        return r.json()
    except ValueError:
        raise _NodeRedError("nodered_protocol_error", f"Non-JSON response from {path}.") from None


def _get_flows() -> tuple[str, list[dict]]:
    data = _request("GET", "/flows")
    if not isinstance(data, dict) or not isinstance(data.get("flows"), list):
        raise _NodeRedError(
            "nodered_protocol_error",
            "GET /flows did not return {rev, flows}; Node-RED may still be starting.",
        )
    rev = data.get("rev")
    flows = [n for n in data["flows"] if isinstance(n, dict)]
    return (rev if isinstance(rev, str) else ""), flows


def _post_flows(flows: list[dict], rev: str) -> str | None:
    result = _request(
        "POST",
        "/flows",
        json_body={"flows": flows, "rev": rev},
        headers={"Node-RED-Deployment-Type": "flows"},
        timeout=_DEPLOY_TIMEOUT,
    )
    new_rev = result.get("rev") if isinstance(result, dict) else None
    return new_rev if isinstance(new_rev, str) else None


def _installed_types() -> set[str]:
    data = _request("GET", "/nodes")
    if not isinstance(data, list):
        raise _NodeRedError("nodered_protocol_error", "GET /nodes is not a JSON list.")
    types: set[str] = set()
    for node_set in data:
        if isinstance(node_set, dict) and node_set.get("enabled") is not False:
            types.update(t for t in node_set.get("types") or [] if isinstance(t, str))
    return types


# --- backups ----------------------------------------------------------------


def _backup_dir() -> Path:
    return Path(os.getenv("NEXUS_DATA_PATH", "/data")) / "nodered_backups"


def _save_backup(rev: str, flows: list[dict], reason: str, slug: str) -> str:
    """Write the current flows to a new backup file and prune old ones; raise `backup_failed`."""
    directory = _backup_dir()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    payload = {
        "format": 1,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "addon_slug": slug,
        "rev": rev,
        "reason": reason,
        "flows": flows,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for n in range(100):
            backup_id = f"flows-{stamp}" if n == 0 else f"flows-{stamp}-{n}"
            path = directory / f"{backup_id}.json"
            try:
                with path.open("x", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("no free backup file name")
    except OSError as e:
        raise _NodeRedError(
            "backup_failed",
            f"Could not write the flows backup to {directory} ({type(e).__name__}); nothing was deployed.",
        ) from None
    _prune_backups(directory)
    return backup_id


def _backup_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [p for p in directory.glob("flows-*.json") if _BACKUP_ID_RE.fullmatch(p.stem)]
    return sorted(files, key=lambda p: p.stem, reverse=True)


def _prune_backups(directory: Path) -> None:
    for old in _backup_files(directory)[_MAX_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass


def _load_backup(backup_id: str) -> dict:
    if not isinstance(backup_id, str) or not _BACKUP_ID_RE.fullmatch(backup_id):
        raise _NodeRedError(
            "invalid_backup_id",
            "Expected an id like 'flows-20261111T120000000000Z' from nodered_list_backups.",
        )
    path = _backup_dir() / f"{backup_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _NodeRedError("backup_not_found", f"No backup {backup_id}.") from None
    except (OSError, ValueError):
        raise _NodeRedError("backup_unreadable", f"Backup {backup_id} cannot be read as JSON.") from None
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, list) or not all(
        isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("type"), str)
        for n in flows
    ):
        raise _NodeRedError("backup_unreadable", f"Backup {backup_id} has no valid flow list.")
    return data


# --- flow model helpers -----------------------------------------------------


def _tabs(flows: list[dict]) -> list[dict]:
    return [n for n in flows if n.get("type") == "tab"]


def _containers(flows: list[dict]) -> dict[str, dict]:
    """`{id: tab-or-subflow node}` for every flow (tab) and subflow definition."""
    return {n["id"]: n for n in flows if n.get("type") in ("tab", "subflow") and isinstance(n.get("id"), str)}


def _find_tab(flows: list[dict], flow_id: str) -> dict:
    for n in flows:
        if n.get("type") == "tab" and n.get("id") == flow_id:
            return n
    raise _NodeRedError(
        "flow_not_found",
        f"No flow (tab) with id {flow_id!r}; use nodered_list_flows for ids.",
        flow_id=flow_id,
    )


def _container_label(container: dict | None) -> str | None:
    if container is None:
        return None
    return container.get("label") if container.get("type") == "tab" else container.get("name")


def _node_location(node: dict, containers: dict[str, dict]) -> dict:
    container = containers.get(node.get("z")) if isinstance(node.get("z"), str) else None
    in_subflow = container is not None and container.get("type") == "subflow"
    return {
        "flow_id": node.get("z") if container is not None else None,
        "flow_label": _container_label(container),
        "in_subflow": in_subflow,
        "disabled": node.get("d") is True or (container is not None and container.get("disabled") is True),
    }


def _node_summary(node: dict, containers: dict[str, dict]) -> dict:
    summary = {"id": node.get("id"), "type": node.get("type"), "name": node.get("name") or None}
    summary.update(_node_location(node, containers))
    return summary


# Header names whose value is a secret whatever the key-name heuristic says.
_SECRET_NAMES = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
# Keys that look like secrets to the heuristic ("...key...") but name a header, not a secret value.
_NOT_SECRET_KEYS = {"keyType", "keyValue", "valueType", "keyname"}
_AUTH_VALUE_RE = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE)
# `(name key, value keys)` pairs: tab/subflow/group env, http request headers, change rules, inject props.
_NAME_VALUE_PAIRS = (("name", ("value",)), ("p", ("to", "v")))


def _secret_key(key: Any) -> bool:
    return isinstance(key, str) and key not in _NOT_SECRET_KEYS and _looks_like_secret_key(key)


def _secret_name(name: Any) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    last = name.strip().split(".")[-1]
    return last.lower() in _SECRET_NAMES or _looks_like_secret_key(last)


def _header_name(item: dict) -> Any:
    key_type = item.get("keyType")
    return item.get("keyValue") if key_type in (None, "", "other") else key_type


def _has_secret(value: Any, key: Any = None) -> bool:
    if isinstance(value, dict):
        return any(
            (k == "credentials" and v not in (None, "", {}))
            or (_secret_key(k) and isinstance(v, (str, int, float)) and v not in ("", None))
            or _has_secret(v, k)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_secret(v, key) for v in value)
    return isinstance(value, str) and _secret_text(value)


def _secret_text(text: str) -> bool:
    """A URL with `user:pass@`, an `Authorization`-style value, or a JSON string holding a secret."""
    if _CREDENTIALS_IN_URL_RE.search(text) or _AUTH_VALUE_RE.search(text):
        return True
    stripped = text.strip()
    if stripped[:1] in ("{", "[") and len(stripped) <= 200_000:
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return False
        return _has_secret(parsed)
    return False


def _redacted(value: Any) -> tuple[Any, list[dict[str, str]]]:
    """Copy of `value` with node credentials and secret-looking values replaced by the placeholder.

    A value is replaced whole (never cut inside), so a placeholder sent back
    unchanged can be restored from the stored value (`_merge_redaction_markers`).
    """
    fields: list[dict[str, str]] = []

    def hide(path: str, reason: str) -> str:
        fields.append({"path": path, "reason": reason})
        return _REDACTED

    def walk(v: Any, path: str, key: Any = None) -> Any:
        if isinstance(v, dict):
            secret_values: set[str] = set()
            if _secret_name(_header_name(v)) and ("keyType" in v or "keyValue" in v):
                secret_values.add("valueValue")
            for name_key, value_keys in _NAME_VALUE_PAIRS:
                if _secret_name(v.get(name_key)):
                    secret_values.update(value_keys)
            out = {}
            for k, child in v.items():
                child_path = f"{path}.{k}" if path else str(k)
                if k == "credentials" and child not in (None, "", {}):
                    out[k] = hide(child_path, "node_credentials")
                elif k in secret_values and child not in (None, ""):
                    out[k] = hide(child_path, "secret_name")
                elif _secret_key(k) and not isinstance(child, (dict, list)) and child not in (None, ""):
                    out[k] = hide(child_path, "key_name_heuristic")
                else:
                    out[k] = walk(child, child_path, k)
            return out
        if isinstance(v, list):
            return [walk(item, f"{path}[{i}]", key) for i, item in enumerate(v)]
        if isinstance(v, str) and _secret_text(v):
            return hide(path, "secret_value")
        return v

    return walk(value, ""), fields


# --- write pipeline ---------------------------------------------------------


def _check_expected_rev(expected_rev: str | None, rev: str) -> None:
    if expected_rev is not None and expected_rev != rev:
        raise _NodeRedError(
            "rev_conflict",
            "The flows changed since expected_rev was read; nothing was written. Read them again.",
            expected_rev=expected_rev,
            current_rev=rev,
        )


def _validate_expected_rev(expected_rev: Any) -> str | None:
    if expected_rev is None:
        return None
    if not isinstance(expected_rev, str) or not expected_rev.strip() or len(expected_rev) > _MAX_REV_LEN:
        raise _NodeRedError("invalid_expected_rev", "expected_rev must be the `rev` string returned by a nodered_* tool.")
    return expected_rev.strip()


def _deploy(slug: str, rev: str, before: list[dict], after: list[dict], reason: str) -> tuple[str, str | None]:
    backup_id = _save_backup(rev, before, reason, slug)
    new_rev = _post_flows(after, rev)
    return backup_id, new_rev


def _clean_text(value: Any, field: str, max_len: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise _NodeRedError(f"invalid_{field}", f"{field} must be a string.")
    text = value.strip() if field == "label" else value
    if (not text and not allow_empty) or len(text) > max_len:
        raise _NodeRedError(f"invalid_{field}", f"{field} must be {0 if allow_empty else 1}-{max_len} characters.")
    return text


# Fields that say where a node sends data; with a stored secret they must not change silently.
_DESTINATION_FIELDS = ("host", "hostname", "url", "broker", "port", "server", "proxy")


def _stored_credentials(node: dict) -> bool:
    """Whether Node-RED holds credentials for `node` (`GET /credentials/<type>/<id>`); unknown counts as yes.

    Node-RED registers credential definitions under the type with whitespace
    turned into dashes (`runtime/lib/nodes/credentials.js` `register`) and
    looks them up by the exact type, so `http request` must be asked as
    `http-request` — asked with the space it answers `{}` even with a token stored.
    """
    try:
        dashed = re.sub(r"\s+", "-", str(node.get("type")))
        node_type = service_guard.path_segment(dashed, "identifier")
        node_id = service_guard.path_segment(str(node.get("id")), "identifier")
        safe = _request("GET", f"/credentials/{node_type}/{node_id}")
    except (ValueError, _NodeRedError):
        return True
    if not isinstance(safe, dict):
        return True
    return any(v not in (None, "", False) for v in safe.values())


def _explicit_credentials(node: dict) -> bool:
    creds = node.get("credentials")
    return isinstance(creds, dict) and bool(creds) and not _contains_redaction_marker(creds)


def _redirect_problem(new: dict, old: dict, had_placeholder: bool, stored: Callable[[dict], bool]) -> dict | None:
    """Refuse moving a kept secret to another node type or destination (`secret_redirect_blocked`).

    `stored(old)` is asked at most once and only when type or destination changes.
    """
    type_changed = new.get("type") != old.get("type")
    changed = [f for f in _DESTINATION_FIELDS if new.get(f) != old.get(f)]
    if not type_changed and (not changed or _explicit_credentials(new)):
        return None
    keeps_secret = had_placeholder or stored(old)
    if type_changed and keeps_secret:
        return {"id": new.get("id"), "reason": "type changed on a node with hidden values or stored credentials"}
    if not changed or _explicit_credentials(new):
        return None
    if keeps_secret:
        return {
            "id": new.get("id"),
            "fields": changed,
            "reason": "destination changed while a stored secret would be kept; send the secret explicitly in `credentials`",
        }
    return None


def _prepare_nodes(
    nodes: Any,
    flow_id: str,
    flows: list[dict],
    installed_types: Callable[[], set[str]],
    stored_credentials: Callable[[dict], bool] = _stored_credentials,
) -> list[dict]:
    """Validate the caller's node list for one flow and return deploy-ready copies (z = flow_id).

    Raises `invalid_nodes`, `id_conflict`, `secret_redirect_blocked` or
    `unknown_node_types` before any write.
    """
    if not isinstance(nodes, list) or len(nodes) > _MAX_NODES:
        raise _NodeRedError("invalid_nodes", f"nodes must be a list of at most {_MAX_NODES} node objects.")
    try:
        size = len(json.dumps(nodes))
    except (TypeError, ValueError):
        raise _NodeRedError("invalid_nodes", "nodes must be JSON data.") from None
    if size > _MAX_NODES_BYTES:
        raise _NodeRedError("invalid_nodes", f"nodes is {size} bytes as JSON; at most {_MAX_NODES_BYTES}.")
    redirects: list[dict] = []
    current_by_id = {n.get("id"): n for n in flows if isinstance(n.get("id"), str)}
    seen: set[str] = set()
    conflicts: list[str] = []
    problems: list[str] = []
    prepared: list[dict] = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            problems.append(f"nodes[{i}] is not an object")
            continue
        node_id, node_type = node.get("id"), node.get("type")
        if not isinstance(node_id, str) or not node_id or len(node_id) > _MAX_ID_LEN or any(
            ord(c) < 32 for c in node_id
        ):
            problems.append(f"nodes[{i}].id must be a 1-{_MAX_ID_LEN} character string")
            continue
        if not isinstance(node_type, str) or not node_type or len(node_type) > _MAX_TYPE_LEN:
            problems.append(f"nodes[{i}].type must be a 1-{_MAX_TYPE_LEN} character string")
            continue
        if node_type in ("tab", "subflow"):
            problems.append(f"nodes[{i}] is a {node_type}; only nodes inside the flow are accepted")
            continue
        if node_id in seen or node_id == flow_id:
            problems.append(f"nodes[{i}].id {node_id!r} is used twice")
            continue
        seen.add(node_id)
        existing = current_by_id.get(node_id)
        if existing is not None and existing.get("z") != flow_id:
            conflicts.append(node_id)
            continue
        clean = dict(node)
        had_placeholder = _contains_redaction_marker(clean)
        if clean.get("credentials") == _REDACTED:
            del clean["credentials"]  # Node-RED keeps the stored credentials
        if _contains_redaction_marker(clean):
            if existing is None:
                problems.append(f"nodes[{i}] ({node_id}) is new but contains the redaction placeholder")
                continue
            try:
                clean = _merge_redaction_markers(clean, existing)
            except RedactionMergeError as e:
                problems.append(f"nodes[{i}] ({node_id}): {e}")
                continue
        if existing is not None:
            problem = _redirect_problem(clean, existing, had_placeholder, stored_credentials)
            if problem:
                redirects.append(problem)
                continue
        clean["z"] = flow_id
        prepared.append(clean)
    if problems:
        raise _NodeRedError("invalid_nodes", "Nothing was written.", problems=problems[:20])
    if conflicts:
        raise _NodeRedError(
            "id_conflict",
            "These node ids already exist in another flow or subflow; give the new nodes fresh ids.",
            ids=conflicts[:20],
        )
    if redirects:
        raise _NodeRedError(
            "secret_redirect_blocked",
            "A node with a stored or hidden secret would get another type or destination; nothing was written. "
            "Send the secret explicitly in the node's `credentials`, or keep type and destination.",
            nodes=redirects[:20],
        )
    _check_types(prepared, flows, installed_types)
    return prepared


def _check_types(prepared: list[dict], flows: list[dict], installed_types: Callable[[], set[str]]) -> None:
    """Raise `unknown_node_types` for a type Node-RED does not have (it would stop every flow)."""
    subflow_ids = {n["id"] for n in flows if n.get("type") == "subflow" and isinstance(n.get("id"), str)}
    to_check = {n["type"] for n in prepared if n["type"] not in _STRUCTURAL_TYPES}
    unknown: list[str] = []
    registry: set[str] | None = None
    for node_type in sorted(to_check):
        match = _SUBFLOW_INSTANCE_RE.fullmatch(node_type)
        if match:
            if match.group(1) not in subflow_ids:
                unknown.append(node_type)
            continue
        if registry is None:
            registry = installed_types()
        if node_type not in registry:
            unknown.append(node_type)
    if unknown:
        raise _NodeRedError(
            "unknown_node_types",
            "Node-RED has no such node types installed (or no such subflow); deploying them would stop "
            "every flow, so nothing was written.",
            types=unknown,
        )


def _replace_members(flows: list[dict], flow_id: str, tab: dict, members: list[dict]) -> list[dict]:
    """Flat flow list with `flow_id`'s tab replaced by `tab` and its nodes by `members`."""
    result: list[dict] = []
    for n in flows:
        if n.get("type") == "tab" and n.get("id") == flow_id:
            result.append(tab)
            result.extend(members)
        elif n.get("z") == flow_id:
            continue
        else:
            result.append(n)
    return result


# --- read tools -------------------------------------------------------------


@mcp.tool(annotations=read("List Node-RED flows"))
def list_flows() -> dict:
    """List the Node-RED add-on's flows (editor tabs) and subflows with node counts and the current deploy rev.

    Finds the Node-RED add-on through the Supervisor, then reads
    `GET /flows` (Node-RED Admin API v2) on `http://127.0.0.1:46836`.
    Changes nothing.

    Use when: getting flow ids before `nodered_get_flow`, `nodered_update_flow`,
    `nodered_set_flow_enabled` or `nodered_delete_flow`, or an overview.
    Not for: nodes inside a flow — use `nodered_get_flow`; HA automations —
    use `automations_list_automations`.
    Returns: `{"addon_slug", "rev", "flows": [{id, label, disabled,
    node_count}], "subflows": [{id, name, node_count, instance_count}],
    "global_config_nodes", "total_nodes"}` in editor tab order. Pass `rev`
    as `expected_rev` to a write tool to refuse it if someone deploys first.
    Errors: `nodered_not_found`, `nodered_not_running`,
    `nodered_unsupported_addon`, `nodered_unreachable`,
    `nodered_auth_required`, `timeout`, `HTTP <status>`, or a missing
    `SUPERVISOR_TOKEN`.
    """
    try:
        addon = _locate_addon()
        rev, flows = _get_flows()
    except _NodeRedError as e:
        return e.as_dict()
    counts: dict[str, int] = {}
    instances: dict[str, int] = {}
    global_configs = 0
    for n in flows:
        z = n.get("z")
        if isinstance(z, str) and z:
            counts[z] = counts.get(z, 0) + 1
        elif n.get("type") not in ("tab", "subflow"):
            global_configs += 1
        match = _SUBFLOW_INSTANCE_RE.fullmatch(n.get("type") or "")
        if match:
            instances[match.group(1)] = instances.get(match.group(1), 0) + 1
    return {
        "addon_slug": addon["slug"],
        "rev": rev,
        "flows": [
            {
                "id": t.get("id"),
                "label": t.get("label"),
                "disabled": t.get("disabled") is True,
                "node_count": counts.get(t.get("id"), 0),
            }
            for t in _tabs(flows)
        ],
        "subflows": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "node_count": counts.get(s.get("id"), 0),
                "instance_count": instances.get(s.get("id"), 0),
            }
            for s in flows
            if s.get("type") == "subflow"
        ],
        "global_config_nodes": global_configs,
        "total_nodes": len(flows),
    }


@mcp.tool(annotations=read("Get one Node-RED flow"))
def get_flow(
    flow_id: Annotated[
        str,
        Field(description="Flow (tab) id from `nodered_list_flows`, e.g. `a1b2c3d4e5f60718`."),
    ],
) -> dict:
    """Read one Node-RED flow (editor tab) with all its nodes, secrets replaced by a placeholder.

    Reads `GET /flows` and returns the tab plus every node whose `z` is
    this tab (regular nodes, config nodes scoped to the tab, groups,
    junctions). Node `credentials` are never in Node-RED's answer; fields
    that look secret (`password`, `token`, `apikey`, URLs with
    `user:pass@`) come back as the redaction placeholder and are listed in
    `redacted_fields`. Changes nothing.

    Use when: inspecting a flow, or reading it before `nodered_update_flow`
    (placeholders sent back unchanged keep their stored value).
    Not for: the list of flows — use `nodered_list_flows`; finding nodes
    across flows — use `nodered_search_nodes` or `nodered_find_entity_usage`.
    Returns: `{"addon_slug", "rev", "flow": {id, label, disabled, info,
    env}, "nodes": [...], "node_count", "redacted_fields"}`.
    Errors: `flow_not_found`, plus the add-on lookup errors of
    `nodered_list_flows`.
    Limits: secrets typed into function code or templates are not detected.
    """
    try:
        addon = _locate_addon()
        rev, flows = _get_flows()
        tab = _find_tab(flows, flow_id)
    except _NodeRedError as e:
        return e.as_dict()
    members = [n for n in flows if n.get("z") == flow_id]
    flow_info = {
        "id": tab.get("id"),
        "label": tab.get("label"),
        "disabled": tab.get("disabled") is True,
        "info": tab.get("info") or "",
        "env": tab.get("env") or [],
    }
    (flow_out, nodes_out), fields = _redacted([flow_info, members])
    return {
        "addon_slug": addon["slug"],
        "rev": rev,
        "flow": flow_out,
        "nodes": nodes_out,
        "node_count": len(members),
        "redacted_fields": fields,
    }


@mcp.tool(annotations=read("Search Node-RED nodes"))
def search_nodes(
    type: Annotated[
        str | None,
        Field(description="Case-insensitive part of the node type, e.g. `api-call-service`, `mqtt in`, `function`."),
    ] = None,
    name: Annotated[
        str | None,
        Field(description="Case-insensitive part of the node's name as shown in the editor."),
    ] = None,
    flow_id: Annotated[
        str | None,
        Field(description="Only nodes in this flow (tab) or subflow id; omit to search everywhere."),
    ] = None,
    include_config: Annotated[
        bool,
        Field(description="True adds each node's full configuration (secrets replaced by the placeholder)."),
    ] = False,
    limit: Annotated[
        int,
        Field(ge=1, le=_SEARCH_LIMIT_MAX, description="Maximum number of nodes returned (1-500, default 100)."),
    ] = 100,
) -> dict:
    """Find Node-RED nodes by type and/or name across all flows and subflows.

    Reads `GET /flows` and matches every node except tab and subflow
    definitions: `type` and `name` are case-insensitive substrings, both
    must match when both are given. Each hit carries its flow (or subflow)
    id and label and whether it is disabled (node or flow). Changes nothing.

    Use when: locating nodes of one kind, e.g. every `api-call-service` or
    `server-state-changed` node, or a node by its name.
    Not for: which nodes use one HA entity — use
    `nodered_find_entity_usage`; a whole flow — use `nodered_get_flow`.
    Returns: `{"addon_slug", "rev", "matches": [{id, type, name, flow_id,
    flow_label, in_subflow, disabled, config?}], "count", "truncated"}`;
    `config` only with `include_config`, plus `redacted_fields` then.
    Errors: `no_filter` when neither `type` nor `name` is given (no I/O),
    plus the add-on lookup errors of `nodered_list_flows`.
    """
    type_q = (type or "").strip().lower()
    name_q = (name or "").strip().lower()
    if not type_q and not name_q:
        return {"error": "no_filter", "message": "Pass `type` and/or `name`."}
    try:
        addon = _locate_addon()
        rev, flows = _get_flows()
    except _NodeRedError as e:
        return e.as_dict()
    containers = _containers(flows)
    hits: list[dict] = []
    total = 0
    for n in flows:
        if n.get("type") in ("tab", "subflow"):
            continue
        if flow_id and n.get("z") != flow_id:
            continue
        if type_q and type_q not in str(n.get("type") or "").lower():
            continue
        if name_q and name_q not in str(n.get("name") or "").lower():
            continue
        total += 1
        if len(hits) < limit:
            hits.append(n)
    matches = [_node_summary(n, containers) for n in hits]
    result: dict[str, Any] = {"addon_slug": addon["slug"], "rev": rev}
    if include_config:
        configs, fields = _redacted(hits)
        for summary, config in zip(matches, configs, strict=True):
            summary["config"] = config
        result["redacted_fields"] = fields
    result.update({"matches": matches, "count": total, "truncated": total > len(hits)})
    return result


def _entity_text_re(entity_id: str) -> re.Pattern[str]:
    # `binary_sensor.x` must not match `sensor.x`; `states.sensor.x.state` must.
    return re.compile(r"(?<![\w])" + re.escape(entity_id) + r"(?![\w])")


def _pattern_hits(kind: str, patterns: Any, entity_id: str, skipped: list[str] | None = None) -> bool:
    """Whether a `substring`/`regex` selector matches; a regex that is too long or has nested
    quantifiers (catastrophic backtracking, Python's `re` has no timeout) is skipped and counted."""
    items = patterns if isinstance(patterns, list) else [patterns]
    for p in items:
        if not isinstance(p, str) or not p:
            continue
        if kind == "substring" and p in entity_id:
            return True
        if kind == "regex":
            if len(p) > _MAX_PATTERN_LEN or _NESTED_QUANTIFIER_RE.search(p):
                if skipped is not None:
                    skipped.append(p[:60])
                continue
            try:
                if re.search(p, entity_id):
                    return True
            except re.error:
                continue
    return False


def _scan_node(
    node: dict, entity_id: str, text_re: re.Pattern[str], skipped: list[str] | None = None
) -> list[tuple[str, str]]:
    """`[(field path, "exact"|"text"|"substring"|"regex")]` where `node` references `entity_id`."""
    hits: list[tuple[str, str]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                child = f"{path}.{k}" if path else str(k)
                # node-red-contrib-home-assistant-websocket `entities: {entity, substring, regex}`
                if k in ("substring", "regex") and isinstance(v, list):
                    if _pattern_hits(k, v, entity_id, skipped):
                        hits.append((child, k))
                    continue
                walk(v, child)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        elif isinstance(value, str):
            if value == entity_id:
                hits.append((path, "exact"))
            elif entity_id in value and text_re.search(value):
                hits.append((path, "text"))

    walk(node, "")
    # Legacy single-field selector: `entityId` + `entityIdType`/`entityIdFilterType`.
    for type_key in ("entityIdType", "entityIdFilterType"):
        kind = node.get(type_key)
        if kind in ("substring", "regex") and _pattern_hits(kind, node.get("entityId"), entity_id, skipped):
            hits.append(("entityId", kind))
    return hits


@mcp.tool(annotations=read("Find HA entity usage in Node-RED"))
def find_entity_usage(
    entity_id: Annotated[
        str,
        Field(description="Home Assistant entity id, e.g. `light.kitchen` or `binary_sensor.front_door`."),
    ],
) -> dict:
    """List the Node-RED nodes and flows that reference one Home Assistant entity.

    Reads `GET /flows` and scans every node's configuration: a field equal
    to the entity id (`exact`, e.g. `entities.entity`, `target.entityId`),
    the id inside a template, JSON or function code as a whole word
    (`text`), and Home Assistant node selectors that would match it by
    `substring` or `regex` (also the older `entityId` + `entityIdType`
    form). Changes nothing.

    Use when: checking what breaks before renaming or removing an entity, or
    explaining what controls a device, together with HA automations.
    Not for: HA automations and scripts — use `search_find_related` or
    `automations_validate_automation_references`; nodes by type — use
    `nodered_search_nodes`.
    Returns: `{"addon_slug", "rev", "entity_id", "nodes": [{id, type, name,
    flow_id, flow_label, in_subflow, disabled, match: [...], fields: [...]}],
    "flows": [{flow_id, flow_label, node_count}], "count", "skipped_patterns"}`.
    Errors: `invalid_entity_id` (no I/O), plus the add-on lookup errors of
    `nodered_list_flows`.
    Limits: a regex is evaluated with Python's engine; one over 200
    characters or with nested quantifiers is skipped (counted in
    `skipped_patterns`); entity ids built at runtime are not found.
    """
    eid = (entity_id or "").strip()
    if not service_guard.valid_entity_id(eid):
        return {"error": "invalid_entity_id", "message": f"Not an entity id: {entity_id!r} (expected domain.object_id)."}
    try:
        addon = _locate_addon()
        rev, flows = _get_flows()
    except _NodeRedError as e:
        return e.as_dict()
    containers = _containers(flows)
    text_re = _entity_text_re(eid)
    nodes_out: list[dict] = []
    per_flow: dict[str, dict] = {}
    skipped: list[str] = []
    for n in flows:
        if n.get("type") in ("tab", "subflow"):
            continue
        hits = _scan_node(n, eid, text_re, skipped)
        if not hits:
            continue
        summary = _node_summary(n, containers)
        summary["match"] = sorted({kind for _, kind in hits})
        summary["fields"] = sorted({path for path, _ in hits})[:_MAX_FIELDS_PER_HIT]
        nodes_out.append(summary)
        key = summary["flow_id"] or ""
        entry = per_flow.setdefault(
            key, {"flow_id": summary["flow_id"], "flow_label": summary["flow_label"], "node_count": 0}
        )
        entry["node_count"] += 1
    return {
        "addon_slug": addon["slug"],
        "rev": rev,
        "entity_id": eid,
        "nodes": nodes_out,
        "flows": list(per_flow.values()),
        "count": len(nodes_out),
        "skipped_patterns": len(skipped),
    }


def _optional(fetch: Callable[[], Any]) -> Any:
    """One `get_info` section; a Node-RED HTTP error stays local to it."""
    try:
        return fetch()
    except _NodeRedError as e:
        if e.code.startswith("HTTP "):
            return e.as_dict()
        raise


def _project_modules(node_sets: Any) -> Any:
    if not isinstance(node_sets, list):
        return node_sets
    modules: dict[str, dict] = {}
    for s in node_sets:
        if not isinstance(s, dict) or not isinstance(s.get("module"), str):
            continue
        m = modules.setdefault(
            s["module"],
            {"module": s["module"], "version": s.get("version"), "types": 0, "enabled": True, "user_installed": False},
        )
        m["types"] += len(s.get("types") or [])
        m["enabled"] = m["enabled"] and s.get("enabled") is not False
        m["user_installed"] = m["user_installed"] or s.get("user") is True
    return sorted(modules.values(), key=lambda m: m["module"])


@mcp.tool(annotations=read("Get Node-RED info"))
def get_info() -> dict:
    """Read the Node-RED add-on's version, runtime settings, flow run state and installed palette modules.

    Calls `GET /settings` (kept: `version`, `httpNodeRoot`, `context`,
    `flowEncryptionType`, `flowFilePretty`, `externalModules`), `GET
    /flows/state` and `GET /nodes` (grouped per npm module with version and
    type count). Changes nothing.

    Use when: checking which Node-RED and palette versions run, e.g. whether
    `node-red-contrib-home-assistant-websocket` is installed, before
    building a flow.
    Not for: host, memory and OS details — use `nodered_get_diagnostics`;
    the add-on's own options — use `supervisor_get_addon`.
    Returns: `{"addon_slug", "addon_version", "nodered_version", "settings",
    "flows_state", "modules": [{module, version, types, enabled,
    user_installed}]}`; a section Node-RED answers with an HTTP error holds
    `{"error": "HTTP <status>", ...}` instead.
    Errors: the add-on lookup errors of `nodered_list_flows`.
    """
    try:
        addon = _locate_addon()
        settings = _request("GET", "/settings")
        if not isinstance(settings, dict):
            raise _NodeRedError("nodered_protocol_error", "GET /settings is not a JSON object.")
        state = _optional(lambda: _request("GET", "/flows/state"))
        modules = _optional(lambda: _project_modules(_request("GET", "/nodes")))
    except _NodeRedError as e:
        return e.as_dict()
    kept = {k: settings[k] for k in _SETTINGS_KEYS if k in settings}
    kept, _ = _redacted(kept)
    return {
        "addon_slug": addon["slug"],
        "addon_version": addon["version"],
        "nodered_version": settings.get("version"),
        "settings": kept,
        "flows_state": state,
        "modules": modules,
    }


@mcp.tool(annotations=read("Get Node-RED diagnostics"))
def get_diagnostics() -> dict:
    """Read Node-RED's own diagnostics report: Node.js, OS, memory, runtime state and module versions.

    Calls `GET /diagnostics` (the report behind the editor's Help →
    diagnostics), which already reduces sensitive settings to a set/unset
    marker; any remaining secret-looking field is replaced by the placeholder.
    Changes nothing.

    Use when: Node-RED is slow, flows are stopped, or a bug report needs
    versions.
    Not for: the add-on log — use `supervisor_get_addon_logs`; installed
    palette modules in a compact form — use `nodered_get_info`.
    Returns: `{"addon_slug", "report": {...}}` as Node-RED sends it.
    Errors: `HTTP <status>` when diagnostics are disabled in settings.js,
    plus the add-on lookup errors of `nodered_list_flows`.
    """
    try:
        addon = _locate_addon()
        report = _request("GET", "/diagnostics")
    except _NodeRedError as e:
        return e.as_dict()
    report, _ = _redacted(report)
    return {"addon_slug": addon["slug"], "report": report}


@mcp.tool(annotations=read("List Node-RED flow backups"))
def list_backups() -> dict:
    """List the Node-RED flow backups nexus wrote before its own writes, newest first.

    Reads `/data/nodered_backups/` inside the nexus add-on (private to
    nexus, kept with nexus's add-on backup, not under `/config`). Every
    `nodered_*` write tool saves the complete flow list there before it
    deploys; the newest 30 are kept. No call to Node-RED.

    Use when: choosing a `backup_id` for `nodered_restore_backup`, or
    checking what changed recently through nexus.
    Not for: Supervisor backups of the whole system — use
    `supervisor_list_backups`.
    Returns: `{"backups": [{backup_id, created, reason, rev, flow_count,
    node_count}], "count"}`; an unreadable file is listed with `error`.
    Errors: none beyond an unreadable file entry; a missing directory
    returns an empty list.
    """
    entries: list[dict] = []
    for path in _backup_files(_backup_dir()):
        entry: dict[str, Any] = {"backup_id": path.stem}
        try:
            data = _load_backup(path.stem)
        except _NodeRedError as e:
            entry["error"] = e.code
        else:
            flows = data["flows"]
            entry.update(
                {
                    "created": data.get("created"),
                    "reason": data.get("reason"),
                    "rev": data.get("rev"),
                    "flow_count": len(_tabs(flows)),
                    "node_count": len(flows),
                }
            )
        entries.append(entry)
    return {"backups": entries, "count": len(entries)}


# --- write tools ------------------------------------------------------------

_NodesParam = Annotated[
    list[dict[str, Any]],
    Field(
        description=(
            "Every node of the flow as Node-RED flow JSON objects, each with a unique `id` and an "
            "installed `type`, wired by id, e.g. `[{\"id\": \"n1\", \"type\": \"inject\", \"x\": 100, "
            "\"y\": 80, \"wires\": [[\"n2\"]]}, {\"id\": \"n2\", \"type\": \"debug\", \"x\": 300, "
            "\"y\": 80, \"wires\": []}]`; `z` is set for you. Config nodes and groups go in the same list."
        )
    ),
]
_ExpectedRevParam = Annotated[
    str | None,
    Field(
        description=(
            "`rev` from the nexus call you read the flows with; if Node-RED's current rev differs, "
            "nothing is written. Omit to skip that check (Node-RED still refuses a concurrent deploy)."
        )
    ),
]


@mcp.tool(annotations=destructive("Create Node-RED flow", idempotent=False, open_world=True))
def create_flow(
    label: Annotated[str, Field(description="Name of the new editor tab, e.g. `Garage lights`.")],
    nodes: _NodesParam,
    info: Annotated[str, Field(description="Optional Markdown description of the flow shown in the editor.")] = "",
    disabled: Annotated[
        bool, Field(description="True creates the flow disabled, so its nodes do not run until enabled.")
    ] = False,
    expected_rev: _ExpectedRevParam = None,
) -> dict:
    """Add a new Node-RED flow (editor tab) with the given nodes and deploy it after backing up all flows.

    Validates the nodes (unique ids not used elsewhere, types installed in
    Node-RED per `GET /nodes` or an existing `subflow:<id>`), reads `GET
    /flows`, writes the backup, appends a new tab with a generated id and
    the nodes, and deploys with `POST /flows` (rev check, only new or
    changed flows restart). Deployed nodes run at once unless `disabled`.

    Available only when the add-on option `nodered_write_enabled` is on
    (off by default: a deployed flow runs any code inside Node-RED).

    Use when: building a new Node-RED automation.
    Not for: changing an existing flow — use `nodered_update_flow`; HA
    automations — use `automations_set_automation_config`.
    Returns: `{"created": true, "addon_slug", "flow_id", "label",
    "node_count", "rev", "backup_id"}`.
    Errors: `invalid_label`, `invalid_info`, `invalid_nodes` (with
    `problems`), `id_conflict`, `unknown_node_types` (with `types`),
    `invalid_expected_rev`, `rev_conflict`, `backup_failed`, `HTTP <status>`
    with Node-RED's `detail`, and the add-on lookup errors of
    `nodered_list_flows`. Nothing is deployed on any error.
    """
    try:
        clean_label = _clean_text(label, "label", _MAX_LABEL_LEN, allow_empty=False)
        clean_info = _clean_text(info, "info", _MAX_INFO_LEN, allow_empty=True)
        rev_check = _validate_expected_rev(expected_rev)
        addon = _locate_addon()
        rev, flows = _get_flows()
        _check_expected_rev(rev_check, rev)
        existing_ids = {n.get("id") for n in flows}
        flow_id = secrets.token_hex(8)
        while flow_id in existing_ids:
            flow_id = secrets.token_hex(8)
        members = _prepare_nodes(nodes, flow_id, flows, _installed_types)
        tab = {"id": flow_id, "type": "tab", "label": clean_label, "disabled": disabled, "info": clean_info, "env": []}
        after = [*flows, tab, *members]
        backup_id, new_rev = _deploy(addon["slug"], rev, flows, after, f"before create_flow {clean_label!r}")
    except _NodeRedError as e:
        return e.as_dict()
    return {
        "created": True,
        "addon_slug": addon["slug"],
        "flow_id": flow_id,
        "label": clean_label,
        "node_count": len(members),
        "rev": new_rev,
        "backup_id": backup_id,
    }


@mcp.tool(annotations=destructive("Replace Node-RED flow", idempotent=True, open_world=True))
def update_flow(
    flow_id: Annotated[str, Field(description="Flow (tab) id from `nodered_list_flows` or `nodered_get_flow`.")],
    nodes: _NodesParam,
    label: Annotated[str | None, Field(description="New tab name; omit to keep the current one.")] = None,
    info: Annotated[str | None, Field(description="New Markdown description; omit to keep the current one.")] = None,
    expected_rev: _ExpectedRevParam = None,
) -> dict:
    """Replace all nodes of one Node-RED flow (and optionally its name) and deploy after backing up all flows.

    `nodes` becomes the complete content of the flow: nodes of this flow
    missing from it are removed, others are added or replaced by id.
    Placeholders from `nodered_get_flow` are restored from the current
    values; nodes sent without `credentials` keep their stored ones. Same
    validation, backup and `POST /flows` deploy as `nodered_create_flow`;
    the tab's `disabled` state and `env` stay as they are.

    Available only when the add-on option `nodered_write_enabled` is on
    (off by default: a deployed flow runs any code inside Node-RED).

    Use when: editing a flow read with `nodered_get_flow`.
    Not for: only enabling or disabling it — use `nodered_set_flow_enabled`;
    a new flow — use `nodered_create_flow`.
    Returns: `{"updated": true, "addon_slug", "flow_id", "label", "added",
    "removed", "kept", "node_count", "rev", "backup_id"}` (`added`/`removed`
    are node ids, `kept` a count).
    Errors: `flow_not_found`, `invalid_nodes` (also for a placeholder in a
    new node), `id_conflict`, `unknown_node_types`, `invalid_label`,
    `invalid_info`, `invalid_expected_rev`, `rev_conflict`, `backup_failed`,
    `HTTP <status>`, and the add-on lookup errors of `nodered_list_flows`.
    Nothing is deployed on any error.
    """
    try:
        clean_label = None if label is None else _clean_text(label, "label", _MAX_LABEL_LEN, allow_empty=False)
        clean_info = None if info is None else _clean_text(info, "info", _MAX_INFO_LEN, allow_empty=True)
        rev_check = _validate_expected_rev(expected_rev)
        addon = _locate_addon()
        rev, flows = _get_flows()
        _check_expected_rev(rev_check, rev)
        tab = dict(_find_tab(flows, flow_id))
        members = _prepare_nodes(nodes, flow_id, flows, _installed_types)
        if clean_label is not None:
            tab["label"] = clean_label
        if clean_info is not None:
            tab["info"] = clean_info
        old_ids = {n.get("id") for n in flows if n.get("z") == flow_id}
        new_ids = {n["id"] for n in members}
        after = _replace_members(flows, flow_id, tab, members)
        backup_id, new_rev = _deploy(addon["slug"], rev, flows, after, f"before update_flow {flow_id}")
    except _NodeRedError as e:
        return e.as_dict()
    return {
        "updated": True,
        "addon_slug": addon["slug"],
        "flow_id": flow_id,
        "label": tab.get("label"),
        "added": sorted(new_ids - old_ids),
        "removed": sorted(i for i in old_ids - new_ids if isinstance(i, str)),
        "kept": len(new_ids & old_ids),
        "node_count": len(members),
        "rev": new_rev,
        "backup_id": backup_id,
    }


@mcp.tool(annotations=destructive("Delete Node-RED flow", idempotent=True))
def delete_flow(
    flow_id: Annotated[str, Field(description="Flow (tab) id from `nodered_list_flows`.")],
    expected_rev: _ExpectedRevParam = None,
) -> dict:
    """Delete one Node-RED flow (editor tab) and all its nodes, deploying after backing up all flows.

    Reads `GET /flows`, writes the backup, removes the tab and every node
    whose `z` is that tab (including config nodes scoped to it), and
    deploys with `POST /flows` (rev check). Global config nodes and
    subflow definitions stay. Node-RED drops the stored credentials of the
    removed nodes, so a restore brings the nodes back without them.

    Available only when the add-on option `nodered_write_enabled` is on
    (off by default: a deployed flow runs any code inside Node-RED).

    Use when: removing a flow that is no longer needed.
    Not for: pausing a flow — use `nodered_set_flow_enabled`; undoing —
    use `nodered_restore_backup` with the returned `backup_id`.
    Returns: `{"deleted": true, "addon_slug", "flow_id", "label",
    "removed_nodes", "rev", "backup_id"}`.
    Errors: `flow_not_found`, `invalid_expected_rev`, `rev_conflict`,
    `backup_failed`, `HTTP <status>`, and the add-on lookup errors of
    `nodered_list_flows`. Nothing is deployed on any error.
    """
    try:
        rev_check = _validate_expected_rev(expected_rev)
        addon = _locate_addon()
        rev, flows = _get_flows()
        _check_expected_rev(rev_check, rev)
        tab = _find_tab(flows, flow_id)
        removed = sum(1 for n in flows if n.get("z") == flow_id)
        after = [n for n in flows if n is not tab and n.get("z") != flow_id]
        backup_id, new_rev = _deploy(addon["slug"], rev, flows, after, f"before delete_flow {flow_id}")
    except _NodeRedError as e:
        return e.as_dict()
    return {
        "deleted": True,
        "addon_slug": addon["slug"],
        "flow_id": flow_id,
        "label": tab.get("label"),
        "removed_nodes": removed,
        "rev": new_rev,
        "backup_id": backup_id,
    }


@mcp.tool(annotations=destructive("Enable or disable Node-RED flow", idempotent=True, open_world=True))
def set_flow_enabled(
    flow_id: Annotated[str, Field(description="Flow (tab) id from `nodered_list_flows`.")],
    enabled: Annotated[bool, Field(description="True starts the flow's nodes, false stops them and keeps the flow.")],
    expected_rev: _ExpectedRevParam = None,
) -> dict:
    """Turn one Node-RED flow on or off (the editor's "Disable flow" switch) without changing its nodes.

    Reads `GET /flows`; if the flow already is in the requested state
    nothing is written. Otherwise writes the backup, flips the tab's
    `disabled` flag and deploys with `POST /flows` (rev check, only this
    flow restarts).

    Available only when the add-on option `nodered_write_enabled` is on
    (off by default: a deployed flow runs any code inside Node-RED).

    Use when: pausing or resuming a whole flow, e.g. while debugging an HA
    automation that does the same job.
    Not for: deleting it — use `nodered_delete_flow`; changing nodes — use
    `nodered_update_flow`.
    Returns: `{"changed", "addon_slug", "flow_id", "label", "enabled",
    "rev", "backup_id"}`; `backup_id` is null when nothing changed.
    Errors: `flow_not_found`, `invalid_expected_rev`, `rev_conflict`,
    `backup_failed`, `HTTP <status>`, and the add-on lookup errors of
    `nodered_list_flows`.
    """
    try:
        rev_check = _validate_expected_rev(expected_rev)
        addon = _locate_addon()
        rev, flows = _get_flows()
        _check_expected_rev(rev_check, rev)
        tab = _find_tab(flows, flow_id)
        if (tab.get("disabled") is True) is (not enabled):
            return {
                "changed": False,
                "addon_slug": addon["slug"],
                "flow_id": flow_id,
                "label": tab.get("label"),
                "enabled": enabled,
                "rev": rev,
                "backup_id": None,
            }
        new_tab = dict(tab)
        new_tab["disabled"] = not enabled
        after = [new_tab if n is tab else n for n in flows]
        action = "enable" if enabled else "disable"
        backup_id, new_rev = _deploy(addon["slug"], rev, flows, after, f"before {action} flow {flow_id}")
    except _NodeRedError as e:
        return e.as_dict()
    return {
        "changed": True,
        "addon_slug": addon["slug"],
        "flow_id": flow_id,
        "label": tab.get("label"),
        "enabled": enabled,
        "rev": new_rev,
        "backup_id": backup_id,
    }


@mcp.tool(annotations=destructive("Restore Node-RED flows from backup", idempotent=True, open_world=True))
def restore_backup(
    backup_id: Annotated[
        str,
        Field(description="Backup id from `nodered_list_backups` or a write tool's `backup_id`, e.g. `flows-20261111T120000000000Z`."),
    ],
    expected_rev: _ExpectedRevParam = None,
) -> dict:
    """Replace all Node-RED flows with a nexus backup, saving the current flows as a new backup first.

    Loads `/data/nodered_backups/<backup_id>.json`, reads `GET /flows`,
    backs the current flows up, then deploys the saved flow list with
    `POST /flows` (rev check, only changed flows restart). Every flow made
    or changed since the backup is lost unless restored again from the
    safety backup.

    Available only when the add-on option `nodered_write_enabled` is on
    (off by default: a deployed flow runs any code inside Node-RED).

    Use when: undoing a `nodered_*` write that went wrong.
    Not for: restoring the whole system or the Node-RED add-on's files — use
    `supervisor_restore_backup`.
    Returns: `{"restored": true, "addon_slug", "backup_id",
    "safety_backup_id", "flow_count", "node_count", "rev"}`.
    Errors: `invalid_backup_id` (no I/O), `backup_not_found`,
    `backup_unreadable`, `backup_from_other_addon`, `unknown_node_types`,
    `invalid_expected_rev`, `rev_conflict`, `backup_failed`, `HTTP
    <status>`, and the add-on lookup errors of `nodered_list_flows`.
    Limits: credentials of nodes deleted since the backup are not restored.
    """
    try:
        data = _load_backup(backup_id)
        rev_check = _validate_expected_rev(expected_rev)
        addon = _locate_addon()
        if data.get("addon_slug") not in (None, addon["slug"]):
            raise _NodeRedError(
                "backup_from_other_addon",
                f"Backup {backup_id} was taken from {data.get('addon_slug')!r}, not {addon['slug']!r}; nothing was written.",
            )
        rev, flows = _get_flows()
        _check_expected_rev(rev_check, rev)
        restored = data["flows"]
        _check_types(restored, restored, _installed_types)
        safety_id, new_rev = _deploy(addon["slug"], rev, flows, restored, f"before restore_backup {backup_id}")
    except _NodeRedError as e:
        return e.as_dict()
    return {
        "restored": True,
        "addon_slug": addon["slug"],
        "backup_id": backup_id,
        "safety_backup_id": safety_id,
        "flow_count": len(_tabs(restored)),
        "node_count": len(restored),
        "rev": new_rev,
    }
