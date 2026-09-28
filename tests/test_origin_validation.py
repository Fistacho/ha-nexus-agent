"""Origin header validation (MCP spec 2025-11-25, "Transports" > Streamable
HTTP > Security Warning):

    "Servers MUST validate the Origin header on all incoming connections to
    prevent DNS rebinding attacks. If the Origin header is present and
    invalid, servers MUST respond with HTTP 403 Forbidden."

Absent Origin (every non-browser MCP client this add-on documents — Claude
Code, Codex, Claude Desktop, curl — never sends one) is not required to be
validated by the spec and must keep working exactly as before this change.

`OriginValidationMiddleware` (`server.py`) compares a *present* Origin
against:
  - a small, hardcoded set of the server's own well-known direct-access
    origins at its actual LAN port (`127.0.0.1`, `[::1]`, `localhost`,
    `homeassistant.local`) — never derived from the request's own `Host`
    header, which is exactly as attacker-controlled as `Origin` itself in a
    DNS rebinding attack;
  - any operator-configured entries in `NEXUS_ALLOWED_ORIGINS` (comma
    separated, same `host:*` wildcard-port convention as the `mcp` SDK's own
    `TransportSecuritySettings.allowed_origins`);
  - HA ingress traffic (172.30.32.2 + `X-Ingress-Path`) is exempted entirely
    — it cannot be forged by a remote attacker's browser (see
    `auth.is_ingress_request`), and its Origin is the *HA frontend's* own
    origin (a Nabu Casa URL, a custom domain, ...), never nexus's own
    host:port, so validating it against nexus's own origins would only ever
    break the Setup UI's own same-page `POST /regenerate`.

Confirmed against the installed `mcp` SDK (1.27.0) and FastMCP (3.2.4)
before writing this: `mcp.server.transport_security.TransportSecurityMiddleware`
exists and does the equivalent Origin/Host validation, but neither
`FastMCP.http_app()` nor `fastmcp.server.http.create_streamable_http_app()`
accept or forward a `security_settings` parameter to the
`StreamableHTTPSessionManager` they construct — there is no way to reach
that SDK-level protection through FastMCP 3.2.4's public API, hence this
equivalent, deliberately small ASGI middleware instead.
"""
from __future__ import annotations

from contextlib import contextmanager

from starlette.testclient import TestClient

import auth
import server


INGRESS_HOST = "172.30.32.2"


@contextmanager
def _make_client(
    monkeypatch,
    addon_mode: bool = True,
    client=("203.0.113.5", 51234),
    port: str = "7123",
    allowed_origins: str | None = None,
):
    if addon_mode:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
    else:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("NEXUS_PORT", port)
    if allowed_origins is None:
        monkeypatch.delenv("NEXUS_ALLOWED_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("NEXUS_ALLOWED_ORIGINS", allowed_origins)

    app = server._build_app()
    with TestClient(app, client=client) as tc:
        yield tc


def _mcp_post(tc, origin: str | None = None):
    headers = {
        "Authorization": f"Bearer {auth.API_KEY}",
        "Accept": "application/json, text/event-stream",
    }
    if origin is not None:
        headers["Origin"] = origin
    return tc.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1}, headers=headers)


# --- 1. No Origin header -> always passes (spec doesn't require it) --------

def test_mcp_without_origin_header_is_not_rejected_for_origin(monkeypatch):
    with _make_client(monkeypatch) as tc:
        resp = _mcp_post(tc, origin=None)

    assert resp.status_code != 403


def test_setup_page_without_origin_header_is_not_rejected(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})

    assert resp.status_code != 403


# --- 2. A valid (self) Origin -> passes -------------------------------------

def test_mcp_localhost_origin_matching_lan_port_is_allowed(monkeypatch):
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="http://localhost:7123")

    assert resp.status_code != 403


def test_mcp_loopback_ip_origin_is_allowed(monkeypatch):
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="http://127.0.0.1:7123")

    assert resp.status_code != 403


def test_mcp_homeassistant_local_origin_is_allowed(monkeypatch):
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="http://homeassistant.local:7123")

    assert resp.status_code != 403


# --- 3. A foreign Origin -> 403 ---------------------------------------------

def test_mcp_foreign_origin_is_forbidden(monkeypatch):
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="http://evil.example")

    assert resp.status_code == 403
    assert resp.json()["error"]


def test_mcp_correct_host_wrong_port_is_forbidden(monkeypatch):
    """Regression guard for the naive "compare Origin to this request's own
    Host header" design this module deliberately does NOT use: an attacker
    who serves their page from the same hostname:port as nexus's declared
    LAN port could otherwise pass by matching Origin to Host. Here the
    server's declared LAN port is 7123; an Origin claiming port 9999 must
    never be treated as self-origin no matter what Host header accompanies
    it."""
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="http://localhost:9999")

    assert resp.status_code == 403


def test_mcp_origin_with_different_declared_lan_port_is_forbidden(monkeypatch):
    with _make_client(monkeypatch, port="8443") as tc:
        resp = _mcp_post(tc, origin="http://localhost:7123")

    assert resp.status_code == 403


# --- 4. Literal "null" Origin (sandboxed iframe / file:// contexts) -> 403 --

def test_mcp_null_origin_is_forbidden(monkeypatch):
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="null")

    assert resp.status_code == 403


# --- 5. Case-insensitivity of scheme/host; ports still matched exactly -----

def test_mcp_origin_scheme_and_host_case_is_ignored(monkeypatch):
    with _make_client(monkeypatch, port="7123") as tc:
        resp = _mcp_post(tc, origin="HTTP://LOCALHOST:7123")

    assert resp.status_code != 403


# --- HA ingress is exempted: its Origin is the HA frontend's own, never ----
# --- nexus's own host:port, so validating it would break /regenerate's own -
# --- same-page `fetch('regenerate', {method:'POST'})` for every add-on user.

def test_regenerate_via_ingress_with_foreign_origin_still_succeeds(monkeypatch, ha_config_dir):
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.post(
            "/regenerate",
            headers={
                "X-Ingress-Path": "/api/hassio_ingress/abc123",
                "Origin": "https://my-home-assistant.example.com",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_regenerate_non_ingress_foreign_origin_is_forbidden_even_with_bearer(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=("203.0.113.5", 51234)) as tc:
        resp = tc.post(
            "/regenerate",
            headers={"Authorization": f"Bearer {auth.API_KEY}", "Origin": "http://evil.example"},
        )

    assert resp.status_code == 403


# --- /health stays exempt: public, no state change, Supervisor watchdog only

def test_health_ignores_foreign_origin(monkeypatch):
    with _make_client(monkeypatch) as tc:
        resp = tc.get("/health", headers={"Origin": "http://evil.example"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- Operator opt-in escape hatch: NEXUS_ALLOWED_ORIGINS --------------------

def test_mcp_allows_operator_configured_extra_origin(monkeypatch):
    with _make_client(monkeypatch, port="7123", allowed_origins="https://my-web-client.example.com") as tc:
        resp = _mcp_post(tc, origin="https://my-web-client.example.com")

    assert resp.status_code != 403


def test_mcp_allowed_origins_env_supports_wildcard_port(monkeypatch):
    with _make_client(monkeypatch, port="7123", allowed_origins="https://my-web-client.example.com:*") as tc:
        resp = _mcp_post(tc, origin="https://my-web-client.example.com:9443")

    assert resp.status_code != 403


def test_mcp_unrelated_extra_origin_still_forbidden_even_with_allowlist_configured(monkeypatch):
    with _make_client(monkeypatch, port="7123", allowed_origins="https://my-web-client.example.com") as tc:
        resp = _mcp_post(tc, origin="http://evil.example")

    assert resp.status_code == 403


# --- Security follow-up review (SHOULD items) -------------------------------
#
# 1. `_origin_matches_allowlist`'s `host:*` wildcard-port suffix must be
#    digits only — a bare `startswith(base + ":")` also matches
#    "<base>:<port>.evil.com" (the attacker's own subdomain, chosen so its
#    hostname starts with the allow-listed one followed by ":<anything>").

def test_allowed_origins_env_wildcard_port_rejects_suffix_domain_confusion(monkeypatch):
    with _make_client(monkeypatch, port="7123", allowed_origins="https://client.example.com:*") as tc:
        resp = _mcp_post(tc, origin="https://client.example.com:9443.evil.com")

    assert resp.status_code == 403


def test_default_allowed_origins_wildcard_style_suffix_confusion_would_also_be_rejected(monkeypatch):
    """Same class of bug, exercised directly against `_origin_matches_allowlist`
    (not just through the env-configured allowlist) so the fix is verified at
    the unit level too, not only through one HTTP round trip."""
    assert server._origin_matches_allowlist(
        "https://client.example.com:9443.evil.com", ("https://client.example.com:*",)
    ) is False
    assert server._origin_matches_allowlist(
        "https://client.example.com:9443", ("https://client.example.com:*",)
    ) is True


# 2. Headers must be read via `starlette.datastructures.Headers(scope=scope)`,
#    not `dict(scope.get("headers", []))` (which silently collapses repeated
#    headers to whichever the dict-comprehension happens to keep last). A
#    request smuggling more than one `Origin` header is now rejected outright
#    — ambiguous input is treated as invalid rather than guessing which value
#    to trust.

def test_mcp_duplicate_origin_headers_is_forbidden(monkeypatch):
    """Order matters for this to be a meaningful regression guard: the
    *first* Origin here is the attacker's, the *second* (which a naive
    `dict(scope["headers"])`-based "last one wins" read would silently
    prefer) is a value that would otherwise be allowed on its own. If the
    fix only rejected because it kept using the disallowed value, this
    ordering would slip through — >1 `Origin` header must be rejected
    outright, regardless of which one looks valid."""
    with _make_client(monkeypatch, port="7123") as tc:
        resp = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers=[
                ("Authorization", f"Bearer {auth.API_KEY}"),
                ("Accept", "application/json, text/event-stream"),
                ("Origin", "http://evil.example"),
                ("Origin", "http://localhost:7123"),
            ],
        )

    assert resp.status_code == 403


def test_mcp_single_origin_among_other_headers_still_allowed(monkeypatch):
    """Regression guard alongside the duplicate-Origin test above: switching
    to `Headers(scope=scope)` must not break the ordinary single-Origin
    case."""
    with _make_client(monkeypatch, port="7123") as tc:
        resp = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers=[
                ("Authorization", f"Bearer {auth.API_KEY}"),
                ("Accept", "application/json, text/event-stream"),
                ("Origin", "http://localhost:7123"),
            ],
        )

    assert resp.status_code != 403


def test_mcp_duplicate_authorization_headers_keeps_prior_last_wins_semantics(monkeypatch):
    """`TokenAuthMiddleware`'s switch to `Headers(scope=scope)` must not
    change *auth* semantics — only make the retrieval explicit. Before this
    change, `dict(scope["headers"])` silently kept the *last* occurrence of a
    repeated header; reproduced here via `headers.getlist("authorization")[-1]`
    so a bogus first Authorization value followed by the real token still
    authenticates, exactly as before."""
    with _make_client(monkeypatch, port="7123") as tc:
        resp = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers=[
                ("Authorization", "Bearer not-the-real-key"),
                ("Authorization", f"Bearer {auth.API_KEY}"),
                ("Accept", "application/json, text/event-stream"),
            ],
        )

    assert resp.status_code != 401


# 3. Regression tests explicitly requested by the Security review -----------

def test_allowed_origins_env_wildcard_port_does_not_match_suffixed_hostname(monkeypatch):
    """`NEXUS_ALLOWED_ORIGINS=http://localhost:*` must not match
    `http://localhost.evil.com` — the wildcard only ever widens the *port*
    of an otherwise-exact host, never the host itself via a naive prefix
    check."""
    with _make_client(monkeypatch, port="7123", allowed_origins="http://localhost:*") as tc:
        resp = _mcp_post(tc, origin="http://localhost.evil.com")

    assert resp.status_code == 403


def test_setup_page_standalone_localhost_peer_foreign_origin_is_forbidden(monkeypatch):
    """Standalone mode's `is_localhost_request` fallback (peer IP 127.0.0.1)
    is exactly the DNS-rebinding-exploitable gap Origin validation exists to
    close (see module docstring / `server.py`'s design-decision comment) — a
    foreign Origin must be rejected on `/` even though the peer IP alone
    would otherwise satisfy `auth.can_access_ui`."""
    with _make_client(monkeypatch, addon_mode=False, client=("127.0.0.1", 51234)) as tc:
        resp = tc.get("/", headers={"Origin": "http://evil.example"})

    assert resp.status_code == 403


def test_regenerate_standalone_localhost_peer_foreign_origin_is_forbidden(monkeypatch, ha_config_dir):
    with _make_client(monkeypatch, addon_mode=False, client=("127.0.0.1", 51234)) as tc:
        resp = tc.post("/regenerate", headers={"Origin": "http://evil.example"})

    assert resp.status_code == 403
