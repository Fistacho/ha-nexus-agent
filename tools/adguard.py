"""AdGuard Home add-on — read config, change DNS settings, check a host.

Transport (verified against source, not assumed):

- The community AdGuard Home add-on (`hassio-addons/addon-adguard-home`,
  installed as `<repo hash>_adguard`, e.g. `a0d7b954_adguard`) declares
  `host_network: true` and starts AdGuard Home itself with
  `--host 127.0.0.1 --port 45158` (`rootfs/etc/s6-overlay/s6-rc.d/adguard/run`).
  Its own Supervisor discovery message hands HA Core exactly
  `{"host": "127.0.0.1", "port": 45158}` (`.../s6-rc.d/discovery/run`), and HA
  Core's `adguard` config flow connects to that with `ssl=False` and no
  username/password (`homeassistant/components/adguard/config_flow.py`,
  `async_step_hassio_confirm`).
- nexus also runs with `host_network: true` (ADR-0004, `config.yaml`), so it
  shares the host's loopback and reaches the same `http://127.0.0.1:45158`
  HA Core's integration uses — no ingress session, no extra Supervisor role.
- The add-on's ingress nginx (`templates/ingress.gtpl`) only allows
  `172.30.32.2` (Supervisor), and an ingress session can't be minted by an
  add-on: Supervisor's `POST /ingress/session` is `@require_home_assistant`
  (`supervisor/api/ingress.py`), so that route is closed to nexus by design.

The Supervisor API is used only to find the add-on (`GET /addons`) and check
it is started and host-networked (`GET /addons/<slug>/info`); the
`SUPERVISOR_TOKEN` goes into the `Authorization` header and is never logged
or returned.

The loopback API is unauthenticated (same trust level as HA Core's own
integration), so this module exposes a closed set of AdGuard endpoints — no
generic passthrough: GET `status`, `dns_info`, `filtering/status`,
`safesearch/status`, `access/list`, `dhcp/status`, `filtering/check_host`;
POST `test_upstream_dns`, `dns_config`. Adding an endpoint means adding a
named call here, not widening an existing one.

AdGuard Home API reference: `openapi/openapi.yaml` and
`internal/dnsforward/http.go` at tag v0.107.79 (`POST /control/dns_config`
decodes into pointer fields and applies only non-nil ones — partial updates
are supported).
"""
from __future__ import annotations

import os
import re
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, ValidationError

import service_guard
from tools._contract import destructive, read

mcp = FastMCP("adguard")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_SUPERVISOR_URL = "http://supervisor"
# Fixed by the add-on itself (`--host 127.0.0.1 --port 45158`), see module docstring.
_ADGUARD_URL = "http://127.0.0.1:45158"
_ADGUARD_SLUG = "adguard"

_TIMEOUT = 15.0
# `test_upstream_dns` exchanges with every upstream (AdGuard's own
# `upstream_timeout`, default 10 s) — give it more room than a plain read.
_TEST_UPSTREAM_TIMEOUT = 45.0

_USER_RULES_PREVIEW = 20
_UINT32_MAX = 2**32 - 1

# Special `filter_list_id` values (AdGuard `internal/filtering/rulelist/rulelist.go`).
_SPECIAL_LIST_NAMES = {
    0: "custom filtering rules",
    -1: "/etc/hosts",
    -2: "blocked services",
    -3: "parental control",
    -4: "safe browsing",
    -5: "safe search",
}

_HOST_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.?$")

_UPSTREAM_FIELDS = ("upstream_dns", "bootstrap_dns", "fallback_dns", "local_ptr_upstreams")


class _AdGuardError(Exception):
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
        raise _AdGuardError(
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
        raise _AdGuardError("supervisor_unreachable", type(e).__name__) from None
    if r.status_code >= 400:
        raise _AdGuardError(f"HTTP {r.status_code}", detail=r.text[:300])
    try:
        body = r.json()
    except ValueError:
        raise _AdGuardError("supervisor_protocol_error", "non-JSON Supervisor response") from None
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _is_adguard_slug(slug: str) -> bool:
    return slug == _ADGUARD_SLUG or slug.endswith("_" + _ADGUARD_SLUG)


def _locate_addon() -> str:
    """Return the slug of a started, host-networked AdGuard Home add-on, or raise."""
    addons = _supervisor_get("/addons").get("addons") or []
    matches = [
        a for a in addons
        if isinstance(a, dict) and isinstance(a.get("slug"), str) and _is_adguard_slug(a["slug"])
    ]
    if not matches:
        raise _AdGuardError(
            "adguard_not_found",
            "No AdGuard Home add-on (slug '*_adguard') is installed.",
        )
    matches.sort(key=lambda a: (a.get("state") != "started", a["slug"]))
    slug = matches[0]["slug"]
    try:
        service_guard.path_segment(slug, "identifier")
    except ValueError:
        raise _AdGuardError("adguard_not_found", f"Unusable add-on slug {slug!r}.") from None
    try:
        info = _supervisor_get(f"/addons/{slug}/info")
    except _AdGuardError as e:
        if e.code in ("HTTP 400", "HTTP 404"):
            raise _AdGuardError("adguard_not_found", f"Add-on {slug} is not installed.", slug=slug) from None
        raise
    if info.get("state") != "started":
        raise _AdGuardError(
            "adguard_not_running",
            f"Add-on {slug} is not started.",
            slug=slug,
            state=info.get("state"),
        )
    if info.get("host_network") is not True:
        raise _AdGuardError(
            "adguard_unsupported_addon",
            f"Add-on {slug} does not use host networking, so its API is not on 127.0.0.1:45158.",
            slug=slug,
        )
    return slug


# --- AdGuard Home: closed set of calls -------------------------------------


def _adguard_request(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: float = _TIMEOUT,
) -> Any:
    try:
        with httpx.Client(base_url=_ADGUARD_URL, timeout=timeout, follow_redirects=False) as c:
            r = c.request(method, path, json=json, params=params)
    except httpx.TimeoutException:
        raise _AdGuardError("timeout", f"AdGuard Home did not answer {path} within {timeout:.0f} s.") from None
    except httpx.HTTPError as e:
        raise _AdGuardError(
            "adguard_unreachable",
            f"Cannot reach AdGuard Home at {_ADGUARD_URL} ({type(e).__name__}).",
        ) from None
    if r.status_code in (401, 403):
        raise _AdGuardError(
            "adguard_auth_required",
            "AdGuard Home refused the request; it has a web user configured, "
            "which this tool cannot authenticate as.",
        )
    if r.status_code >= 300:
        raise _AdGuardError(f"HTTP {r.status_code}", detail=r.text[:300])
    if not r.content.strip():
        return None
    try:
        return r.json()
    except ValueError:
        raise _AdGuardError("adguard_protocol_error", f"Non-JSON response from {path}.") from None


def _get(path: str, *, params: dict | None = None) -> Any:
    return _adguard_request("GET", "/control/" + path, params=params)


def _optional_section(path: str, project) -> Any:
    """One `get_config` section; an AdGuard HTTP error stays local to it."""
    try:
        return project(_get(path))
    except _AdGuardError as e:
        if e.code.startswith("HTTP "):
            return e.as_dict()
        raise


def _project_filter(f: Any) -> Any:
    if not isinstance(f, dict):
        return f
    keys = ("id", "name", "url", "enabled", "rules_count", "last_updated")
    return {k: f.get(k) for k in keys if k in f}


def _project_filtering(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    rules = [r for r in (data.get("user_rules") or []) if isinstance(r, str)]
    return {
        "enabled": data.get("enabled"),
        "interval": data.get("interval"),
        "filters": [_project_filter(f) for f in data.get("filters") or []],
        "whitelist_filters": [_project_filter(f) for f in data.get("whitelist_filters") or []],
        "user_rules_count": len(rules),
        "user_rules_preview": rules[:_USER_RULES_PREVIEW],
        "user_rules_truncated": len(rules) > _USER_RULES_PREVIEW,
    }


def _project_dhcp(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    return {"enabled": data.get("enabled"), "interface_name": data.get("interface_name")}


# --- tools -----------------------------------------------------------------


@mcp.tool(annotations=read("Get AdGuard Home configuration"))
def get_config() -> dict:
    """Read the AdGuard Home add-on's status, DNS settings, filtering, safe search, access lists and dhcp state.

    Finds the AdGuard Home add-on through the Supervisor, then reads its
    local API (`http://127.0.0.1:45158/control/...`): `status`, `dns_info`
    (upstreams, bootstrap, fallback, rate limit, cache, PTR), `filtering/status`
    (lists with name/url/rules_count; custom rules as a count plus the first
    20), `safesearch/status`, `access/list` and `dhcp/status` reduced to
    `enabled` and `interface_name` (no leases). Changes nothing.

    Use when: inspecting AdGuard before changing it, or answering what DNS
    upstreams and filters are active.
    Not for: changing DNS settings — use `adguard_set_dns_config`; testing one
    domain against the filters — use `adguard_check_host`.
    Returns: `{"addon_slug", "status", "dns", "filtering", "safesearch",
    "access", "dhcp"}`; a section AdGuard answers with an HTTP error holds
    `{"error": "HTTP <status>", "detail"}` instead.
    Errors: `adguard_not_found`, `adguard_not_running`,
    `adguard_unsupported_addon`, `adguard_unreachable`, `adguard_auth_required`,
    `timeout`, `HTTP <status>`, or a missing `SUPERVISOR_TOKEN`.
    """
    try:
        slug = _locate_addon()
        status = _get("status")
        return {
            "addon_slug": slug,
            "status": status,
            "dns": _optional_section("dns_info", lambda d: d),
            "filtering": _optional_section("filtering/status", _project_filtering),
            "safesearch": _optional_section("safesearch/status", lambda d: d),
            "access": _optional_section("access/list", lambda d: d),
            "dhcp": _optional_section("dhcp/status", _project_dhcp),
        }
    except _AdGuardError as e:
        return e.as_dict()


_UpstreamLine = Annotated[str, Field(min_length=1, max_length=512)]


class DnsConfigChanges(BaseModel):
    """Allowlisted subset of AdGuard Home's `DNSConfig` (openapi v0.107.79).

    `extra="forbid"`: any other key is rejected before anything is sent.
    A field left out (or `null`) is not sent, so AdGuard keeps its value.
    """

    model_config = ConfigDict(extra="forbid")

    upstream_dns: Annotated[
        list[_UpstreamLine] | None,
        Field(
            min_length=1,
            max_length=100,
            description=(
                "Upstream DNS servers, one per item, e.g. `tls://dns.quad9.net`, "
                "`https://dns.example/dns-query`, `9.9.9.9`, `[/lan/]10.0.0.1`. Must not be empty."
            ),
        ),
    ] = None
    upstream_mode: Annotated[
        Literal["", "load_balance", "parallel", "fastest_addr"] | None,
        Field(description="How upstreams are queried; `\"\"` is a deprecated alias of `load_balance`."),
    ] = None
    fallback_dns: Annotated[
        list[_UpstreamLine] | None,
        Field(max_length=100, description="Fallback DNS servers used when upstreams fail; `[]` clears the list."),
    ] = None
    bootstrap_dns: Annotated[
        list[_UpstreamLine] | None,
        Field(
            min_length=1,
            max_length=100,
            description="Plain-IP resolvers used to resolve encrypted upstream hostnames, e.g. `1.1.1.1:53`. Must not be empty.",
        ),
    ] = None
    ratelimit: Annotated[
        int | None,
        Field(ge=0, le=_UINT32_MAX, description="Requests per second allowed per client subnet; 0 disables rate limiting."),
    ] = None
    ratelimit_subnet_len_ipv4: Annotated[
        int | None, Field(ge=0, le=32, description="IPv4 prefix length that groups clients for rate limiting (default 24).")
    ] = None
    ratelimit_subnet_len_ipv6: Annotated[
        int | None, Field(ge=0, le=128, description="IPv6 prefix length that groups clients for rate limiting (default 56).")
    ] = None
    ratelimit_whitelist: Annotated[
        list[IPvAnyAddress] | None,
        Field(max_length=1000, description="Client IP addresses exempt from rate limiting; `[]` clears the list."),
    ] = None
    cache_enabled: Annotated[
        bool | None, Field(description="Enable the DNS response cache; needs a cache_size above 0 (here or already set).")
    ] = None
    cache_size: Annotated[
        int | None, Field(ge=0, le=_UINT32_MAX, description="DNS cache size in bytes.")
    ] = None
    cache_optimistic: Annotated[
        bool | None, Field(description="Serve expired cache entries while refreshing them in the background.")
    ] = None
    cache_ttl_min: Annotated[
        int | None, Field(ge=0, le=_UINT32_MAX, description="Minimum TTL in seconds applied to cached answers (0 = no override).")
    ] = None
    cache_ttl_max: Annotated[
        int | None, Field(ge=0, le=_UINT32_MAX, description="Maximum TTL in seconds applied to cached answers (0 = no override).")
    ] = None
    resolve_clients: Annotated[
        bool | None, Field(description="Resolve client IP addresses to hostnames via reverse DNS.")
    ] = None
    use_private_ptr_resolvers: Annotated[
        bool | None, Field(description="Use private reverse DNS resolvers for private-network PTR queries.")
    ] = None
    local_ptr_upstreams: Annotated[
        list[_UpstreamLine] | None,
        Field(max_length=100, description="Private reverse DNS resolvers; `[]` falls back to the system resolvers."),
    ] = None
    dnssec_enabled: Annotated[
        bool | None, Field(description="Set the DNSSEC flag on outgoing queries and validate it in responses.")
    ] = None
    blocking_mode: Annotated[
        Literal["default", "refused", "nxdomain", "null_ip"] | None,
        Field(description="Response for blocked queries; `custom_ip` is not supported by this tool."),
    ] = None
    upstream_timeout: Annotated[
        int | None, Field(ge=1, le=3600, description="Seconds to wait for an upstream answer.")
    ] = None


_ALLOWED_FIELDS = tuple(DnsConfigChanges.model_fields)


def _project_dns(dns: Any) -> Any:
    if not isinstance(dns, dict):
        return dns
    return {k: dns.get(k) for k in _ALLOWED_FIELDS if k in dns}


def _upstream_test_body(body: dict, before: dict) -> dict:
    """Effective lists AdGuard will use after the change, in `UpstreamsConfig` shape."""
    def effective(field: str) -> list:
        value = body.get(field, before.get(field))
        return list(value) if isinstance(value, list) else []

    test = {
        "upstream_dns": effective("upstream_dns"),
        "bootstrap_dns": effective("bootstrap_dns"),
        "fallback_dns": effective("fallback_dns"),
    }
    if "local_ptr_upstreams" in body:
        test["private_upstream"] = list(body["local_ptr_upstreams"])
    return test


@mcp.tool(annotations=destructive("Set AdGuard Home DNS settings", idempotent=False, open_world=True))
def set_dns_config(
    changes: Annotated[
        DnsConfigChanges,
        Field(
            description=(
                "Only the DNS settings to change, e.g. `{\"upstream_dns\": [\"tls://dns.quad9.net\"], "
                "\"cache_optimistic\": true}`. Unknown keys are rejected; omitted keys keep their value."
            )
        ),
    ],
) -> dict:
    """Change selected AdGuard Home DNS settings after the new upstreams pass AdGuard's own test.

    Reads `dns_info`, then — when `upstream_dns`, `bootstrap_dns`,
    `fallback_dns` or `local_ptr_upstreams` change — runs
    `POST /control/test_upstream_dns` on the resulting lists and writes
    nothing unless every server answers `OK`. Then sends only the given
    fields to `POST /control/dns_config` (AdGuard keeps the rest), and
    re-reads `dns_info`. Applies immediately to the whole network.

    Use when: changing upstream/fallback/bootstrap/PTR resolvers, rate
    limit, cache, `dnssec_enabled`, blocking mode or upstream timeout.
    Not for: reading the current values — use `adguard_get_config`; filter
    lists or toggles — use the HA `adguard` integration services.
    Returns: `{"applied": true, "addon_slug", "changed_fields", "before",
    "after", "upstream_test"}`; `before`/`after` list the allowlisted fields.
    Errors: `invalid_changes`, `no_changes`, `upstream_test_failed` (with
    `failed`, nothing written), `HTTP <status>` with AdGuard's `detail`, and
    the add-on lookup errors of `adguard_get_config`.
    Limits: a domain-specific upstream reported as a WARNING also blocks the write.
    """
    if not isinstance(changes, DnsConfigChanges):
        try:
            changes = DnsConfigChanges.model_validate(changes)
        except ValidationError as e:
            return {
                "error": "invalid_changes",
                "detail": [
                    {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
                    for err in e.errors()
                ],
                "allowed_fields": list(_ALLOWED_FIELDS),
            }
    body = changes.model_dump(mode="json", exclude_none=True)
    if not body:
        return {"error": "no_changes", "message": "Pass at least one field to change.", "allowed_fields": list(_ALLOWED_FIELDS)}

    try:
        slug = _locate_addon()
        before = _get("dns_info")
        if not isinstance(before, dict):
            raise _AdGuardError("adguard_protocol_error", "dns_info is not a JSON object.")

        upstream_test = None
        if any(field in body for field in _UPSTREAM_FIELDS):
            test_body = _upstream_test_body(body, before)
            results = _adguard_request(
                "POST", "/control/test_upstream_dns", json=test_body, timeout=_TEST_UPSTREAM_TIMEOUT
            )
            if not isinstance(results, dict) or not results:
                return {
                    "error": "upstream_test_failed",
                    "message": "AdGuard returned no upstream test results; nothing was written.",
                    "tested": test_body,
                    "applied": False,
                }
            failed = {addr: msg for addr, msg in results.items() if msg != "OK"}
            if failed:
                return {
                    "error": "upstream_test_failed",
                    "message": "At least one DNS server failed AdGuard's upstream test; nothing was written.",
                    "failed": failed,
                    "upstream_test": results,
                    "applied": False,
                }
            upstream_test = results

        _adguard_request("POST", "/control/dns_config", json=body)
        after = _get("dns_info")
    except _AdGuardError as e:
        return e.as_dict()

    return {
        "applied": True,
        "addon_slug": slug,
        "changed_fields": sorted(body),
        "before": _project_dns(before),
        "after": _project_dns(after),
        "upstream_test": upstream_test,
    }


def _filter_names(filtering: Any) -> dict[int, str]:
    names: dict[int, str] = {}
    if isinstance(filtering, dict):
        for key in ("filters", "whitelist_filters"):
            for f in filtering.get(key) or []:
                if isinstance(f, dict) and isinstance(f.get("id"), int):
                    names[f["id"]] = f.get("name") or f.get("url") or str(f["id"])
    return names


@mcp.tool(annotations=read("Check a host against AdGuard filters"))
def check_host(
    name: Annotated[
        str,
        Field(description="Domain name to check, e.g. `ads.example.com` (no scheme, path or port)."),
    ],
) -> dict:
    """Check whether AdGuard Home would filter a domain, and by which rule and list.

    Calls `GET /control/filtering/check_host?name=<name>` on the AdGuard Home
    add-on and resolves each matched rule's `filter_list_id` to the list
    name from `filtering/status` (0 = custom rules, negative ids = built-in
    sources such as blocked services or safe browsing). Sends no DNS query
    and changes nothing.

    Use when: explaining why a domain is blocked or allowed.
    Not for: the full filtering setup — use `adguard_get_config`.
    Returns: AdGuard's answer (`reason`, `rules`, `service_name`, `cname`,
    `ip_addrs`) plus `name`, `blocked` (true when `reason` starts with
    `Filtered`) and `filter_name` on each rule.
    Errors: `invalid_name` for an empty or malformed domain; the add-on
    lookup errors of `adguard_get_config`.
    """
    host = (name or "").strip()
    if not host or len(host) > 253 or not _HOST_RE.match(host):
        return {"error": "invalid_name", "message": f"Not a domain name: {name!r}."}
    try:
        _locate_addon()
        result = _get("filtering/check_host", params={"name": host})
        if not isinstance(result, dict):
            raise _AdGuardError("adguard_protocol_error", "check_host is not a JSON object.")
        try:
            names = _filter_names(_get("filtering/status"))
        except _AdGuardError:
            names = {}
    except _AdGuardError as e:
        return e.as_dict()

    rules = []
    for rule in result.get("rules") or []:
        if isinstance(rule, dict):
            list_id = rule.get("filter_list_id")
            rule = dict(rule)
            rule["filter_name"] = names.get(list_id) or _SPECIAL_LIST_NAMES.get(list_id)
        rules.append(rule)
    reason = result.get("reason")
    out = dict(result)
    out["rules"] = rules
    out["name"] = host
    out["blocked"] = isinstance(reason, str) and reason.startswith("Filtered")
    return out
