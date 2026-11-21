"""Regression tests for the 2026-09-27 P0 HTTP-auth security audit
(Setup UI / Home Assistant ingress / token leakage).

Confirmed on the live add-on (nexus 0.19.1, 192.168.8.141:7123):

1. `GET /` without any authentication returns HTTP 200 with the full API key
   embedded 7x in the page (`setup_ui.py`, `mcp_url = ...?token={API_KEY}`).
   The `/mcp` auth middleware in `server.py` only guards paths starting with
   `/mcp`, so `/` and `/regenerate` were wide open on the LAN.
2. `POST /regenerate` had no authentication at all — anyone on the LAN could
   invalidate the running API key (DoS).
3. uvicorn's access logger (`log_level="info"`) logs the full request line,
   including `?token=<API_KEY>` in the query string, in plaintext.
4. `/health` and the Setup UI hard-coded `"tools": 100` instead of reading
   the live FastMCP tool registry (currently 323 tools).
5. `main()` printed the raw API key to stdout/add-on log on every startup.

Per Home Assistant developer docs (Add-ons > Presentation > Ingress,
https://developers.home-assistant.io/docs/add-ons/presentation/#ingress):
"Only connections from `172.30.32.2` must be allowed. You should deny access
to all other IP addresses" and "Ingress adds a request header
`X-Ingress-Path`". Both signals are required by `auth.is_ingress_request`.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from types import SimpleNamespace

from starlette.testclient import TestClient

import auth
import server


INGRESS_HOST = "172.30.32.2"


@contextmanager
def _make_client(monkeypatch, addon_mode: bool = True, client=("203.0.113.5", 51234)):
    """Fresh ASGI app + TestClient with a controllable "source IP" for each test.

    `addon_mode=True` simulates running as a Supervisor add-on (SUPERVISOR_TOKEN
    set); `addon_mode=False` simulates the standalone `.env` deployment.

    Used as a context manager (`with _make_client(...) as tc:`) so the ASGI
    app's lifespan (needed by FastMCP's StreamableHTTP session manager) starts
    and stops cleanly around each test.
    """
    if addon_mode:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
    else:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    app = server._build_app()
    with TestClient(app, client=client) as tc:
        yield tc


# --- A. Setup UI (`GET /`) must not leak the key outside ingress -------------

def test_setup_page_without_ingress_or_bearer_hides_api_key(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=("203.0.113.5", 51234)) as tc:
        resp = tc.get("/")

    assert resp.status_code == 200
    assert auth.API_KEY not in resp.text


def test_setup_page_via_ingress_shows_full_config(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})

    assert resp.status_code == 200
    assert auth.API_KEY in resp.text


def test_setup_page_offers_no_query_token_url(monkeypatch):
    """1.0.0 removed `?token=` auth, so no snippet on the page may use it.
    Codex gets `--bearer-token-env-var` (codex-rs/cli/src/mcp_cmd.rs) and the
    key is read from an environment variable, not typed into the URL."""
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})

    assert resp.status_code == 200
    assert "?token=" not in resp.text
    assert "--bearer-token-env-var NEXUS_API_KEY" in resp.text


def test_setup_page_ingress_ip_without_header_is_not_trusted(monkeypatch):
    """Defense in depth: the source IP alone is not sufficient — some other
    process spoofing 172.30.32.2 without the ingress header must not pass."""
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.get("/")  # no X-Ingress-Path header

    assert resp.status_code == 200
    assert auth.API_KEY not in resp.text


def test_setup_page_valid_bearer_token_shows_full_config(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=("203.0.113.5", 51234)) as tc:
        resp = tc.get("/", headers={"Authorization": f"Bearer {auth.API_KEY}"})

    assert resp.status_code == 200
    assert auth.API_KEY in resp.text


def test_setup_page_standalone_mode_allows_localhost(monkeypatch):
    """Standalone (.env, no Supervisor) has no ingress proxy — localhost is the
    trust boundary instead."""
    with _make_client(monkeypatch, addon_mode=False, client=("127.0.0.1", 51234)) as tc:
        resp = tc.get("/")

    assert resp.status_code == 200
    assert auth.API_KEY in resp.text


def test_setup_page_standalone_mode_rejects_remote_host(monkeypatch):
    with _make_client(monkeypatch, addon_mode=False, client=("203.0.113.5", 51234)) as tc:
        resp = tc.get("/")

    assert resp.status_code == 200
    assert auth.API_KEY not in resp.text


def test_setup_page_addon_mode_does_not_trust_localhost_without_ingress(monkeypatch):
    """In add-on mode the trust boundary is ingress, not localhost — the add-on
    container's loopback is reachable by anything sharing its network namespace."""
    with _make_client(monkeypatch, addon_mode=True, client=("127.0.0.1", 51234)) as tc:
        resp = tc.get("/")

    assert resp.status_code == 200
    assert auth.API_KEY not in resp.text


# --- ADR-0004 S2 regression: 127.0.0.1 is not a trust boundary in add-on mode,
# on ALL three endpoints (Setup UI, /regenerate, /mcp) — not just `/`. Written
# for host_network specifically: once nexus binds a socket at the ingress
# address (not just 0.0.0.0), it becomes even more important that *loopback*
# still isn't trusted, since host_network means literally any other process
# or add-on sharing the host's network namespace can also reach 127.0.0.1.

def test_regenerate_addon_mode_127_0_0_1_without_bearer_is_forbidden(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=("127.0.0.1", 51234)) as tc:
        resp = tc.post("/regenerate")

    assert resp.status_code == 403


def test_mcp_addon_mode_127_0_0_1_without_token_is_unauthorized(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=("127.0.0.1", 51234)) as tc:
        resp = tc.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})

    assert resp.status_code == 401


def test_locked_page_hides_tool_count_policy_and_ha_url(monkeypatch):
    """`_locked_page_html()` (the unauthenticated response) must reveal
    *nothing* addon-internal beyond the static marketing copy: no live tool
    count, no "Active Policy" section, and not even the configured
    `ha_url` — all three are addon-internal facts reserved for the
    `can_access_ui`-gated full page (`_full_page_html`/`_policy_section_html`).

    Regression this guards against: any of those three leaking onto the
    locked page the way the API key itself, the live tool count and the
    policy used to leak onto `/` (see module docstring, point 1) and onto
    `/health` (point 4) before the 2026-09-27 audit — this time via the
    *locked* branch specifically, not just the authenticated one already
    covered by `test_setup_page_shows_live_tool_count_not_hardcoded_100`.
    """
    import setup_ui

    with _make_client(monkeypatch, addon_mode=True, client=("203.0.113.5", 51234)) as tc:
        resp = tc.get("/")

    assert resp.status_code == 200
    text = resp.text
    assert auth.API_KEY not in text
    assert setup_ui._ha_url() not in text
    assert "Active Policy" not in text
    assert not re.search(r"\d+\s+tools\b", text)


# --- A. `POST /regenerate` must require the same trust boundary -------------

def test_regenerate_without_ingress_or_bearer_is_forbidden(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=("203.0.113.5", 51234)) as tc:
        resp = tc.post("/regenerate")

    assert resp.status_code == 403


def test_regenerate_via_ingress_succeeds(monkeypatch, ha_config_dir):
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.post("/regenerate", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_regenerate_with_valid_bearer_succeeds(monkeypatch, ha_config_dir):
    with _make_client(monkeypatch, addon_mode=True, client=("203.0.113.5", 51234)) as tc:
        resp = tc.post("/regenerate", headers={"Authorization": f"Bearer {auth.API_KEY}"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_regenerate_standalone_localhost_succeeds(monkeypatch, ha_config_dir):
    with _make_client(monkeypatch, addon_mode=False, client=("127.0.0.1", 51234)) as tc:
        resp = tc.post("/regenerate")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --- C. `/mcp` accepts only the Bearer header (`?token=` removed in 1.0.0) ---

def test_mcp_without_any_token_is_unauthorized(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})

    assert resp.status_code == 401


def test_mcp_with_bearer_header_is_not_rejected_for_auth(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={
                "Authorization": f"Bearer {auth.API_KEY}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert resp.status_code != 401


def test_mcp_with_query_token_and_no_header_is_unauthorized(monkeypatch):
    """1.0.0 removed `?token=` auth: the right key in the query string alone
    must not authenticate."""
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.post(
            f"/mcp?token={auth.API_KEY}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert resp.status_code == 401
    assert auth.API_KEY not in resp.text
    assert "Authorization: Bearer" in resp.json()["hint"]


def test_mcp_query_token_does_not_override_a_wrong_bearer_header(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.post(
            f"/mcp?token={auth.API_KEY}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={
                "Authorization": "Bearer wrong-key",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert resp.status_code == 401


def test_mcp_bearer_header_still_works_with_a_stale_query_token(monkeypatch):
    """A client upgraded to the header but still carrying an old `?token=`
    in its URL keeps working: the query string is simply ignored."""
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.post(
            "/mcp?token=stale-value",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={
                "Authorization": f"Bearer {auth.API_KEY}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert resp.status_code != 401


# --- B. token redaction in uvicorn's access log ------------------------------

def test_redact_token_filter_masks_query_token_in_access_log():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", "/mcp?token=super-secret-key", "1.1", 401),
        exc_info=None,
    )

    server.RedactTokenFilter().filter(record)

    rendered = record.getMessage()
    assert "super-secret-key" not in rendered
    assert "token=***" in rendered


def test_redact_token_filter_leaves_other_records_untouched():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", "/health", "1.1", 200),
        exc_info=None,
    )

    server.RedactTokenFilter().filter(record)

    assert record.getMessage() == '127.0.0.1:54321 - "GET /health HTTP/1.1" 200'


# --- stale `?token=` URLs: the redaction filter stays (defense in depth) ----
#
# 1.0.0 no longer accepts `?token=`, but clients configured before the upgrade
# keep sending it until their owner edits the URL. uvicorn would log those
# request lines verbatim, so `RedactTokenFilter` still masks the key.

def test_rejected_query_token_request_is_redacted_in_access_log(monkeypatch, caplog):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        with caplog.at_level(logging.DEBUG):
            resp = tc.post(
                f"/mcp?token={auth.API_KEY}",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )
    assert resp.status_code == 401
    # `httpx` is the test client's own logger (it prints the URL it sent);
    # everything the server side logged must be key-free.
    for record in caplog.records:
        if record.name.startswith("httpx"):
            continue
        assert auth.API_KEY not in record.getMessage()

    access_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "POST", f"/mcp?token={auth.API_KEY}", "1.1", 401),
        exc_info=None,
    )
    server.RedactTokenFilter().filter(access_record)
    assert auth.API_KEY not in access_record.getMessage()


# --- ADR-0004 S1: peer IP + X-Ingress-Path detection is logged at DEBUG so it
# can be confirmed against a live add-on running with host_network: true -----

def test_is_ingress_request_logs_peer_and_header_presence_at_debug(caplog):
    request = SimpleNamespace(
        client=SimpleNamespace(host=INGRESS_HOST),
        headers={"x-ingress-path": "/api/hassio_ingress/abc123"},
    )

    with caplog.at_level(logging.DEBUG, logger="auth"):
        result = auth.is_ingress_request(request)

    assert result is True
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(INGRESS_HOST in r.getMessage() for r in debug_records)
    assert any("has_x_ingress_path_header=True" in r.getMessage() for r in debug_records)
    for record in debug_records:
        assert auth.API_KEY not in record.getMessage()


def test_is_ingress_request_still_fails_closed_for_untrusted_peer():
    """Regression: the new DEBUG logging must not change the trust decision
    itself — an untrusted peer with the header spoofed is still rejected."""
    request = SimpleNamespace(
        client=SimpleNamespace(host="172.30.32.1"),  # the host_network gateway, NOT Supervisor
        headers={"x-ingress-path": "/api/hassio_ingress/abc123"},
    )

    assert auth.is_ingress_request(request) is False


# --- ADR-0004 D1b: HTTP-mode startup banner reflects the ListenPlan ----------

def test_startup_log_lines_for_plan_never_contain_the_api_key_lan_exposed():
    import addon_network

    plan = addon_network.ListenPlan.standalone(7123)
    lines = server._startup_log_lines_for_plan(plan)

    assert not any(auth.API_KEY in line for line in lines)
    assert any("7123" in line for line in lines)
    assert any("key" in line.lower() for line in lines)


def test_startup_log_lines_for_plan_reports_ingress_only_when_lan_disabled():
    import addon_network

    plan = addon_network.ListenPlan.from_self_info(
        {"network": {"7123/tcp": None}, "ip_address": "172.30.32.1"}
    )
    lines = server._startup_log_lines_for_plan(plan)

    assert not any(auth.API_KEY in line for line in lines)
    assert any("172.30.32.1:7123" in line for line in lines)
    assert any("not" in line.lower() and "lan" in line.lower() for line in lines)


# --- API key must never be printed at startup --------------------------------

def test_startup_log_lines_never_contain_the_api_key():
    lines = server._startup_log_lines(port=7123, http_mode=True)

    assert not any(auth.API_KEY in line for line in lines)
    assert any("key" in line.lower() for line in lines)  # still tells the user where to find it


def test_startup_log_lines_stdio_mode_never_contain_the_api_key():
    lines = server._startup_log_lines(port=7123, http_mode=False)

    assert not any(auth.API_KEY in line for line in lines)


def test_startup_log_lines_stdio_mode_do_not_point_at_ingress():
    """Outside the add-on there is no ingress panel; in stdio there is no API
    key either. The banner used to say "open the Nexus panel (ingress)"."""
    text = "\n".join(server._startup_log_lines(port=7123, http_mode=False))

    assert "ingress" not in text
    assert "no API key" in text


def test_startup_log_lines_for_plan_standalone_http_does_not_point_at_ingress():
    import addon_network

    plan = addon_network.ListenPlan.standalone(7123)
    text = "\n".join(server._startup_log_lines_for_plan(plan, addon_mode=False))

    assert "ingress" not in text
    assert "http://localhost:7123" in text
    # In Docker the Setup UI is reached through the port mapping, which is
    # not localhost to nexus, so the banner must not promise it shows the key.
    assert "only to requests from localhost" in text
    assert "NEXUS_API_KEY" in text


def test_startup_log_lines_for_plan_addon_mode_still_points_at_ingress():
    import addon_network

    plan = addon_network.ListenPlan.from_self_info(
        {"network": {"7123/tcp": 7123}, "ip_address": "172.30.32.1"}
    )
    text = "\n".join(server._startup_log_lines_for_plan(plan, addon_mode=True))

    assert "Open Web UI" in text


# --- D. tool count is read from the live FastMCP registry --------------------

def _tool_count_sync() -> int:
    import asyncio

    return len(asyncio.run(server.mcp.list_tools()))


# ADR-0003 P: `/health` is unauthenticated (the Supervisor watchdog only ever
# needs a 200) and used to also return `ha_url` and the live tool count to
# any LAN caller before ingress auth was even checked. Both facts now live
# only on the `can_access_ui`-gated Setup UI page (`_full_page_html`) — see
# `test_setup_page_shows_live_tool_count_not_hardcoded_100` below, which keeps
# the original regression guard (no hard-coded "100 tools") on that page
# instead of on `/health`.
def test_health_returns_minimal_status_only(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_setup_page_shows_live_tool_count_not_hardcoded_100(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})

    assert "100 tools" not in resp.text
    assert f"{_tool_count_sync()} tools" in resp.text
