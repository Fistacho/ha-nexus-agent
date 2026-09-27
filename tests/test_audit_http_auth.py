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
from contextlib import contextmanager

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


# --- C. `/mcp` accepts Bearer header (recommended) and query token (legacy) --

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


def test_mcp_with_query_token_is_not_rejected_for_auth_backward_compat(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.post(
            f"/mcp?token={auth.API_KEY}",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream"},
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


# --- API key must never be printed at startup --------------------------------

def test_startup_log_lines_never_contain_the_api_key():
    lines = server._startup_log_lines(port=7123, http_mode=True)

    assert not any(auth.API_KEY in line for line in lines)
    assert any("key" in line.lower() for line in lines)  # still tells the user where to find it


def test_startup_log_lines_stdio_mode_never_contain_the_api_key():
    lines = server._startup_log_lines(port=7123, http_mode=False)

    assert not any(auth.API_KEY in line for line in lines)


# --- D. tool count is read from the live FastMCP registry --------------------

def _tool_count_sync() -> int:
    import asyncio

    return len(asyncio.run(server.mcp.list_tools()))


def test_health_reports_live_tool_count(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True) as tc:
        resp = tc.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] > 100  # the hard-coded, wrong value was 100
    assert body["tools"] == _tool_count_sync()


def test_setup_page_shows_live_tool_count_not_hardcoded_100(monkeypatch):
    with _make_client(monkeypatch, addon_mode=True, client=(INGRESS_HOST, 51234)) as tc:
        resp = tc.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})

    assert "100 tools" not in resp.text
