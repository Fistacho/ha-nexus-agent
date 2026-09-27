"""Tests for `addon_network.py` — ADR-0004 D1b (ListenPlan for host_network).

Facts these tests are pinned to (verified live in the source of
`home-assistant/supervisor` @ `main`, 2026-09-27 — see ADR-0004 D1/D1b and the
citations in `addon_network.py`'s module docstring):

- `GET /addons/self/info` wraps its payload in `{"result": "ok", "data": {...}}`
  (`supervisor/api/utils.py::api_process`, matches every other Supervisor
  endpoint this codebase already parses, e.g. `tools/supervisor.py`).
- `data["network"]["7123/tcp"]` is `apps/app.py::App.ports` — the user-facing
  merged config-default + Network-tab-override port map. It keeps being
  populated (int host port, or `null` if the user cleared/disabled the port in
  the Network tab) **regardless of `host_network`** — only the *Docker-level*
  `docker/app.py::DockerApp.ports` (what actually becomes `-p` flags) collapses
  to `None` under `host_network`, because Docker itself needs no `-p` there.
- `data["ip_address"]` is `apps.py::info_data`'s `str(app.ip_address)`; for a
  `host_network` app `docker/app.py::DockerApp.ip_address` returns
  `self.sys_docker.network.gateway`, which is pinned to
  `DOCKER_IPV4_NETWORK_MASK[1]` = `172.30.32.1` for the standard
  `172.30.32.0/23` "hassio" network (`supervisor/const.py`,
  `supervisor/docker/network.py`).
- `ingress_port` is a separate, host_network-independent key
  (`ATTR_INGRESS_PORT: app.ingress_port`) — irrelevant to `ListenPlan` itself
  (nexus pins its own `ingress_port: 7123` in `config.yaml`), but confirms the
  add-on's ingress traffic always targets `ip_address:7123`, matching Supervisor's
  own `watchdog_application()` fallback-to-declared-port behavior when the
  Network-tab port is `null` (`apps/app.py` ~line 840).
"""
from __future__ import annotations

import httpx
import pytest

import addon_network as an


# --- ListenPlan.from_self_info: the three Network-tab states -----------------

def test_from_self_info_default_port_unchanged_is_single_lan_socket():
    """`network["7123/tcp"] == 7123` (untouched default) → today's behaviour:
    one socket, `0.0.0.0:7123`. No separate ingress-only bind needed because
    0.0.0.0:7123 already accepts Supervisor's ingress connection too."""
    data = {"network": {"7123/tcp": 7123}, "ip_address": "172.30.32.1"}

    plan = an.ListenPlan.from_self_info(data)

    assert plan.sockets == (("0.0.0.0", 7123),)
    assert plan.lan_port == 7123


def test_from_self_info_remapped_port_adds_lan_socket_and_keeps_ingress_socket():
    """`network["7123/tcp"] == X != 7123` (user remapped the LAN port in the
    Network tab) → two sockets: `<ip_address>:7123` for ingress + Supervisor's
    watchdog, and `0.0.0.0:X` for the LAN."""
    data = {"network": {"7123/tcp": 9000}, "ip_address": "172.30.32.1"}

    plan = an.ListenPlan.from_self_info(data)

    assert plan.sockets == (("172.30.32.1", 7123), ("0.0.0.0", 9000))
    assert plan.lan_port == 9000


def test_from_self_info_disabled_port_is_ingress_only():
    """`network["7123/tcp"] is None` (user cleared the port in the Network
    tab) → only `<ip_address>:7123`, no LAN socket at all (ADR-0004 S3)."""
    data = {"network": {"7123/tcp": None}, "ip_address": "172.30.32.1"}

    plan = an.ListenPlan.from_self_info(data)

    assert plan.sockets == (("172.30.32.1", 7123),)
    assert plan.lan_port is None


def test_from_self_info_missing_port_key_is_treated_as_disabled():
    """Defensive: an absent `"7123/tcp"` key is indistinguishable from an
    explicit `null` once parsed from JSON — both must take the narrower,
    ingress-only branch (fail-closed), never the LAN-exposing one."""
    data = {"network": {}, "ip_address": "172.30.32.1"}

    plan = an.ListenPlan.from_self_info(data)

    assert plan.sockets == (("172.30.32.1", 7123),)


# --- ListenPlan.from_self_info: malformed shapes fail closed ------------------

def test_from_self_info_rejects_non_int_non_null_port_value():
    data = {"network": {"7123/tcp": "not-a-port"}, "ip_address": "172.30.32.1"}

    with pytest.raises(an.AddonNetworkUnavailable):
        an.ListenPlan.from_self_info(data)


def test_from_self_info_rejects_out_of_range_port_value():
    data = {"network": {"7123/tcp": 0}, "ip_address": "172.30.32.1"}

    with pytest.raises(an.AddonNetworkUnavailable):
        an.ListenPlan.from_self_info(data)


def test_from_self_info_rejects_invalid_ip_address():
    data = {"network": {"7123/tcp": 9000}, "ip_address": "not-an-ip"}

    with pytest.raises(an.AddonNetworkUnavailable):
        an.ListenPlan.from_self_info(data)


def test_from_self_info_rejects_missing_ip_address():
    data = {"network": {"7123/tcp": 7123}}

    with pytest.raises(an.AddonNetworkUnavailable):
        an.ListenPlan.from_self_info(data)


# --- ListenPlan.standalone -----------------------------------------------------

def test_standalone_plan_is_single_wildcard_socket_on_configured_port():
    plan = an.ListenPlan.standalone(7123)

    assert plan.sockets == (("0.0.0.0", 7123),)
    assert plan.lan_port == 7123


# --- resolve_listen_plan: standalone vs add-on dispatch -----------------------

def test_resolve_listen_plan_without_supervisor_token_is_standalone():
    plan = an.resolve_listen_plan(supervisor_token=None, nexus_port=7123)

    assert plan.sockets == (("0.0.0.0", 7123),)


def test_resolve_listen_plan_addon_mode_calls_self_info_and_builds_plan():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/addons/self/info"
        assert request.headers["authorization"] == "Bearer fake-token"
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "data": {"network": {"7123/tcp": 9000}, "ip_address": "172.30.32.1"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://supervisor")

    plan = an.resolve_listen_plan(supervisor_token="fake-token", nexus_port=7123, client=client)

    assert plan.sockets == (("172.30.32.1", 7123), ("0.0.0.0", 9000))


# --- fail-closed retry behaviour on an unreachable Supervisor -----------------

def test_resolve_listen_plan_retries_then_fails_closed_when_supervisor_unreachable():
    """`/addons/self/info` unreachable after short retrying → readable error,
    never a silent fallback to a guessed plan (that could re-expose the LAN
    port the operator disabled, ADR-0004 D1b/S3)."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://supervisor")

    with pytest.raises(an.AddonNetworkUnavailable):
        an.resolve_listen_plan(
            supervisor_token="fake-token",
            nexus_port=7123,
            client=client,
            attempts=3,
            retry_delay=0,
        )

    assert call_count == 3


def test_resolve_listen_plan_succeeds_after_transient_failure():
    """One transient failure followed by a good response must still succeed —
    "krótkie ponawianie" (ADR-0004 D1b) is a real retry, not just one attempt."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "data": {"network": {"7123/tcp": 7123}, "ip_address": "172.30.32.1"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://supervisor")

    plan = an.resolve_listen_plan(
        supervisor_token="fake-token", nexus_port=7123, client=client, attempts=3, retry_delay=0
    )

    assert plan.sockets == (("0.0.0.0", 7123),)
    assert call_count == 2


def test_resolve_listen_plan_fails_closed_on_malformed_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "ok", "data": "not-a-dict"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://supervisor")

    with pytest.raises(an.AddonNetworkUnavailable):
        an.resolve_listen_plan(
            supervisor_token="fake-token",
            nexus_port=7123,
            client=client,
            attempts=1,
            retry_delay=0,
        )
