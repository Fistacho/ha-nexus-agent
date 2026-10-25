"""`tools/adguard.py` — AdGuard Home add-on tools, against a fake Supervisor +
fake AdGuard Home (`httpx.MockTransport`, same pattern as
`test_addon_network.py`/`test_esphome_dashboard_client.py`).

Covered: add-on lookup (not installed / 404 / stopped / not host-networked /
missing token / Supervisor error), AdGuard transport errors (401/403,
connection refused), `get_config` projection, `set_dns_config` allowlist and
fail-closed upstream test, `check_host`.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from tools import adguard

_RealClient = httpx.Client
_TOKEN = "sup-token-do-not-leak"
_SLUG = "a0d7b954_adguard"


def _unwrap(tool):
    return getattr(tool, "fn", tool)


get_config = _unwrap(adguard.get_config)
set_dns_config = _unwrap(adguard.set_dns_config)
check_host = _unwrap(adguard.check_host)


class FakeHA:
    """Supervisor + AdGuard Home behind one MockTransport."""

    def __init__(self) -> None:
        self.addons = [
            {"slug": "core_mosquitto", "state": "started"},
            {"slug": _SLUG, "state": "started"},
        ]
        self.info_status = 200
        self.info = {"slug": _SLUG, "state": "started", "host_network": True}
        self.supervisor_status = 200
        self.adguard_status: dict[str, int] = {}
        self.adguard_refuses = False
        self.dns_info = {
            "upstream_dns": ["tls://dns.quad9.net"],
            "bootstrap_dns": ["1.1.1.1:53"],
            "fallback_dns": [],
            "ratelimit": 20,
            "ratelimit_subnet_len_ipv4": 24,
            "upstream_mode": "",
            "cache_optimistic": False,
            "resolve_clients": False,
            "use_private_ptr_resolvers": True,
            "local_ptr_upstreams": [],
            "protection_enabled": True,
        }
        self.test_results: dict[str, str] | None = None
        self.filtering = {
            "enabled": True,
            "interval": 24,
            "filters": [
                {
                    "id": 1,
                    "name": "AdGuard DNS filter",
                    "url": "https://example.test/filter.txt",
                    "enabled": True,
                    "rules_count": 50000,
                    "last_updated": "2026-10-25T00:00:00Z",
                }
            ],
            "whitelist_filters": [],
            "user_rules": [f"||rule{i}.example^" for i in range(25)],
        }
        self.check_host_result = {
            "reason": "FilteredBlackList",
            "rules": [{"filter_list_id": 1, "text": "||ads.example.com^"}],
        }
        self.requests: list[httpx.Request] = []
        self.posted: dict[str, list] = {}

    # -- transport --
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "supervisor":
            return self._supervisor(request)
        if request.url.host == "127.0.0.1" and request.url.port == 45158:
            if self.adguard_refuses:
                raise httpx.ConnectError("Connection refused", request=request)
            return self._adguard(request)
        raise AssertionError(f"unexpected host {request.url}")

    def _supervisor(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == f"Bearer {_TOKEN}"
        if self.supervisor_status != 200:
            return httpx.Response(self.supervisor_status, text="Unauthorized")
        if request.url.path == "/addons":
            return httpx.Response(200, json={"result": "ok", "data": {"addons": self.addons}})
        if request.url.path == f"/addons/{_SLUG}/info":
            if self.info_status != 200:
                return httpx.Response(self.info_status, json={"result": "error", "message": "App is not installed"})
            return httpx.Response(200, json={"result": "ok", "data": self.info})
        return httpx.Response(404, text="not found")

    def _adguard(self, request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers, "Supervisor token must not reach AdGuard"
        path = request.url.path.removeprefix("/control/")
        if path in self.adguard_status:
            return httpx.Response(self.adguard_status[path], text=f"error on {path}")
        if request.method == "POST":
            body = json.loads(request.content)
            self.posted.setdefault(path, []).append(body)
            if path == "test_upstream_dns":
                if self.test_results is not None:
                    return httpx.Response(200, json=self.test_results)
                servers = body["upstream_dns"] + body.get("fallback_dns", []) + body.get("private_upstream", [])
                return httpx.Response(200, json={s: "OK" for s in servers})
            if path == "dns_config":
                self.dns_info.update(body)
                return httpx.Response(200)
            return httpx.Response(404)
        if path == "status":
            return httpx.Response(200, json={"running": True, "version": "v0.107.79", "protection_enabled": True})
        if path == "dns_info":
            return httpx.Response(200, json=self.dns_info)
        if path == "filtering/status":
            return httpx.Response(200, json=self.filtering)
        if path == "safesearch/status":
            return httpx.Response(200, json={"enabled": False})
        if path == "access/list":
            return httpx.Response(200, json={"allowed_clients": [], "disallowed_clients": [], "blocked_hosts": []})
        if path == "dhcp/status":
            return httpx.Response(
                200,
                json={
                    "enabled": False,
                    "interface_name": "eth0",
                    "v4": {"gateway_ip": "10.0.0.1"},
                    "leases": [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.50", "hostname": "phone"}],
                    "static_leases": [],
                },
            )
        if path == "filtering/check_host":
            return httpx.Response(200, json=self.check_host_result)
        return httpx.Response(404)

    def adguard_paths(self) -> list[str]:
        return [
            f"{r.method} {r.url.path}" for r in self.requests if r.url.host == "127.0.0.1"
        ]


@pytest.fixture
def fake(monkeypatch) -> FakeHA:
    ha = FakeHA()
    transport = httpx.MockTransport(ha.handler)
    monkeypatch.setattr(adguard.httpx, "Client", lambda **kw: _RealClient(transport=transport, **kw))
    monkeypatch.setenv("SUPERVISOR_TOKEN", _TOKEN)
    return ha


def _assert_no_token(result) -> None:
    assert _TOKEN not in json.dumps(result, default=str)


# --- add-on lookup ----------------------------------------------------------


def test_get_config_returns_projected_sections(fake):
    result = get_config()

    assert result["addon_slug"] == _SLUG
    assert result["status"]["version"] == "v0.107.79"
    assert result["dns"]["upstream_dns"] == ["tls://dns.quad9.net"]
    filtering = result["filtering"]
    assert filtering["user_rules_count"] == 25
    assert len(filtering["user_rules_preview"]) == 20
    assert filtering["user_rules_truncated"] is True
    assert "user_rules" not in filtering
    assert filtering["filters"][0]["name"] == "AdGuard DNS filter"
    assert result["dhcp"] == {"enabled": False, "interface_name": "eth0"}
    assert result["safesearch"] == {"enabled": False}
    assert "blocked_hosts" in result["access"]
    _assert_no_token(result)


def test_get_config_section_http_error_stays_local(fake):
    fake.adguard_status["dhcp/status"] = 500

    result = get_config()

    assert result["dhcp"]["error"] == "HTTP 500"
    assert result["dns"]["ratelimit"] == 20


def test_addon_not_installed(fake):
    fake.addons = [{"slug": "core_mosquitto", "state": "started"}]

    result = get_config()

    assert result["error"] == "adguard_not_found"
    assert fake.adguard_paths() == []


def test_addon_info_404_is_not_found(fake):
    fake.info_status = 404

    assert get_config()["error"] == "adguard_not_found"
    assert fake.adguard_paths() == []


def test_addon_stopped(fake):
    fake.info["state"] = "stopped"

    result = get_config()

    assert result["error"] == "adguard_not_running"
    assert result["state"] == "stopped"
    assert fake.adguard_paths() == []


def test_addon_without_host_network_is_refused(fake):
    fake.info["host_network"] = False

    assert get_config()["error"] == "adguard_unsupported_addon"
    assert fake.adguard_paths() == []


def test_supervisor_401_is_reported_without_token(fake):
    fake.supervisor_status = 401

    result = get_config()

    assert result["error"] == "HTTP 401"
    _assert_no_token(result)


def test_missing_supervisor_token_does_no_io(fake, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN")

    result = get_config()

    assert "SUPERVISOR_TOKEN not set" in result["error"]
    assert fake.requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_adguard_auth_required(fake, status):
    fake.adguard_status["status"] = status

    assert get_config()["error"] == "adguard_auth_required"


def test_adguard_connection_refused(fake):
    fake.adguard_refuses = True

    result = get_config()

    assert result["error"] == "adguard_unreachable"
    assert "127.0.0.1:45158" in result["message"]


# --- set_dns_config: allowlist ---------------------------------------------


def test_set_dns_config_rejects_unknown_field_without_io(fake):
    result = set_dns_config({"ratelimit": 30, "protection_enabled": False})

    assert result["error"] == "invalid_changes"
    assert any(d["field"] == "protection_enabled" for d in result["detail"])
    assert "ratelimit" in result["allowed_fields"]
    assert fake.requests == []


def test_set_dns_config_unknown_field_rejected_by_mcp_schema(fake):
    # FastMCP validates arguments against the same model before the function
    # runs (`extra="forbid"`); in-process that surfaces as pydantic's
    # ValidationError, over the MCP wire as a ToolError.
    with pytest.raises((ToolError, ValidationError)) as excinfo:
        asyncio.run(
            adguard.mcp.call_tool("set_dns_config", {"changes": {"upstream_dns_file": "/etc/passwd"}})
        )
    assert "upstream_dns_file" in str(excinfo.value)
    assert fake.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"ratelimit": -1},
        {"ratelimit_subnet_len_ipv4": 33},
        {"upstream_dns": []},
        {"bootstrap_dns": []},
        {"blocking_mode": "custom_ip"},
        {"upstream_mode": "round_robin"},
        {"ratelimit_whitelist": ["not-an-ip"]},
        {"upstream_timeout": 0},
    ],
)
def test_set_dns_config_rejects_invalid_values(fake, changes):
    assert set_dns_config(changes)["error"] == "invalid_changes"
    assert fake.requests == []


def test_set_dns_config_requires_a_change(fake):
    assert set_dns_config({})["error"] == "no_changes"
    assert set_dns_config({"ratelimit": None})["error"] == "no_changes"
    assert fake.requests == []


# --- set_dns_config: upstream test (fail closed) ----------------------------


def test_upstream_test_failure_blocks_write(fake):
    fake.test_results = {
        "tls://dns.quad9.net": "OK",
        "tls://broken.example": 'upstream "tls://broken.example" fails to exchange: i/o timeout',
    }

    result = set_dns_config({"upstream_dns": ["tls://dns.quad9.net", "tls://broken.example"]})

    assert result["error"] == "upstream_test_failed"
    assert result["applied"] is False
    assert list(result["failed"]) == ["tls://broken.example"]
    assert "dns_config" not in fake.posted
    assert "POST /control/dns_config" not in fake.adguard_paths()


def test_upstream_test_warning_also_blocks_write(fake):
    fake.test_results = {"[/lan/]10.0.0.1": "WARNING: couldn't communicate with upstream"}

    result = set_dns_config({"upstream_dns": ["[/lan/]10.0.0.1"]})

    assert result["error"] == "upstream_test_failed"
    assert "dns_config" not in fake.posted


def test_empty_upstream_test_result_blocks_write(fake):
    fake.test_results = {}

    result = set_dns_config({"fallback_dns": ["9.9.9.9"]})

    assert result["error"] == "upstream_test_failed"
    assert "dns_config" not in fake.posted


def test_upstream_change_success_posts_only_changed_fields(fake):
    result = set_dns_config(
        {"upstream_dns": ["tls://dns.quad9.net", "https://dns.cloudflare.com/dns-query"], "upstream_mode": "parallel"}
    )

    assert result["applied"] is True
    assert fake.posted["test_upstream_dns"] == [
        {
            "upstream_dns": ["tls://dns.quad9.net", "https://dns.cloudflare.com/dns-query"],
            "bootstrap_dns": ["1.1.1.1:53"],
            "fallback_dns": [],
        }
    ]
    assert fake.posted["dns_config"] == [
        {"upstream_dns": ["tls://dns.quad9.net", "https://dns.cloudflare.com/dns-query"], "upstream_mode": "parallel"}
    ]
    assert result["changed_fields"] == ["upstream_dns", "upstream_mode"]
    assert result["before"]["upstream_dns"] == ["tls://dns.quad9.net"]
    assert result["after"]["upstream_dns"] == ["tls://dns.quad9.net", "https://dns.cloudflare.com/dns-query"]
    assert result["after"]["upstream_mode"] == "parallel"
    assert "protection_enabled" not in result["after"]  # outside the allowlist
    assert fake.adguard_paths()[-1] == "GET /control/dns_info"  # re-read after write
    _assert_no_token(result)


def test_ptr_change_tests_private_upstreams(fake):
    result = set_dns_config({"local_ptr_upstreams": ["10.0.0.1"]})

    assert result["applied"] is True
    assert fake.posted["test_upstream_dns"][0]["private_upstream"] == ["10.0.0.1"]
    assert fake.posted["dns_config"] == [{"local_ptr_upstreams": ["10.0.0.1"]}]


def test_non_upstream_change_skips_upstream_test(fake):
    result = set_dns_config({"ratelimit": 30, "cache_optimistic": True, "ratelimit_whitelist": ["10.0.0.2"]})

    assert result["applied"] is True
    assert result["upstream_test"] is None
    assert "test_upstream_dns" not in fake.posted
    assert fake.posted["dns_config"] == [
        {"ratelimit": 30, "cache_optimistic": True, "ratelimit_whitelist": ["10.0.0.2"]}
    ]
    assert result["before"]["ratelimit"] == 20
    assert result["after"]["ratelimit"] == 30


def test_adguard_validation_error_is_returned(fake):
    fake.adguard_status["dns_config"] = 400

    result = set_dns_config({"cache_enabled": True})

    assert result["error"] == "HTTP 400"
    assert result["detail"] == "error on dns_config"
    assert fake.adguard_paths().count("GET /control/dns_info") == 1  # no re-read after a refused write


def test_set_dns_config_addon_missing_does_no_adguard_io(fake):
    fake.addons = []

    assert set_dns_config({"ratelimit": 5})["error"] == "adguard_not_found"
    assert fake.adguard_paths() == []


# --- check_host --------------------------------------------------------------


def test_check_host_blocked_with_list_name(fake):
    result = check_host("ads.example.com")

    assert result["blocked"] is True
    assert result["name"] == "ads.example.com"
    assert result["rules"][0]["filter_name"] == "AdGuard DNS filter"
    check_requests = [r for r in fake.requests if r.url.path == "/control/filtering/check_host"]
    assert check_requests[0].url.params["name"] == "ads.example.com"


def test_check_host_custom_rule_and_not_filtered(fake):
    fake.check_host_result = {"reason": "NotFilteredWhiteList", "rules": [{"filter_list_id": 0, "text": "@@||ok.example^"}]}

    result = check_host("ok.example")

    assert result["blocked"] is False
    assert result["rules"][0]["filter_name"] == "custom filtering rules"


@pytest.mark.parametrize("name", ["", "   ", "https://ads.example.com", "a b.example", "x.example/path", "a" * 254])
def test_check_host_rejects_bad_name_without_io(fake, name):
    assert check_host(name)["error"] == "invalid_name"
    assert fake.requests == []


def test_check_host_addon_not_running(fake):
    fake.info["state"] = "error"

    assert check_host("ads.example.com")["error"] == "adguard_not_running"
