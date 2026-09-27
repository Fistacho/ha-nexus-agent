"""Add-on Network-tab semantics for `host_network: true` (ADR-0004 D1/D1b).

Before 0.23.0 nexus always bound `uvicorn.run(host="0.0.0.0", port=NEXUS_PORT)`
because Docker's own port mapping (`ports: 7123/tcp: 7123` in `config.yaml`,
overridable per-install on the add-on's Network tab) did all the work of
honouring what the operator configured — including refusing to publish the
port to the LAN at all if they cleared it there. `host_network: true` removes
Docker from that path entirely (no `-p` flags are possible once the container
shares the host's network namespace), so nexus itself must now reproduce the
Network tab's semantics, or an operator who disabled the LAN port would find
themselves back on the LAN after upgrading to 0.23.0.

Facts below are pinned to `home-assistant/supervisor` @ `main`, read
2026-09-27 (see ADR-0004 for the accompanying decision) — re-verify against
whatever Supervisor version is actually running if these ever look wrong:

- `GET /addons/self/info` (`supervisor/api/apps.py::APIApps.info_data`) always
  wraps its payload as `{"result": "ok", "data": {...}}`
  (`supervisor/api/utils.py::api_process`); every other Supervisor call in
  this codebase already parses that same envelope (see `tools/supervisor.py`,
  `tools/esphome.py::_sup_json`).
- `data["network"]` is `ATTR_NETWORK: app.ports`, where `app` is
  `supervisor/apps/app.py::App` (**not** `supervisor/docker/app.py::DockerApp`
  — a different class with its own, unrelated `ports` property that *does*
  collapse to `None` under `host_network`, because Docker itself needs no
  `-p` flags there). `App.ports` merges `config.yaml`'s declared ports with
  any user override persisted from the Network tab, and — confirmed by
  reading `App.watchdog_application()`, which explicitly branches on
  `if self.host_network and self.ports:` — keeps doing this **regardless of**
  `host_network`. So `data["network"]["7123/tcp"]` is:
    - the (possibly user-remapped) LAN port, as an `int`, if the operator left
      the port mapped;
    - `None` (JSON `null`) if the operator cleared/disabled it on the Network
      tab;
    - indistinguishable, once parsed, from the key being entirely absent —
      this module treats both the same way (see `ListenPlan.from_self_info`).
- `data["ip_address"]` is `ATTR_IP_ADDRESS: str(app.ip_address)`; for a
  `host_network` app, `supervisor/docker/app.py::DockerApp.ip_address`
  returns `self.sys_docker.network.gateway`, which
  `supervisor/docker/network.py::DockerNetwork.gateway` pins to
  `DOCKER_IPV4_NETWORK_MASK[1]` — `172.30.32.1` for the standard
  `172.30.32.0/23` "hassio" bridge network declared in
  `supervisor/const.py`. That address is a real interface on the *host*
  (Docker assigns it directly to the bridge device backing the "hassio"
  network), which is exactly why a `host_network` container can bind to it:
  sharing the host's network namespace gives it visibility of every host
  interface, bridge devices included.
- Supervisor's own ingress proxy (`supervisor/api/ingress.py::_create_url`)
  and its watchdog (`App.watchdog_application`, which falls back to the
  container's declared default port — here always 7123 — when the Network
  tab value is `None`) both target `ip_address:<port>` for exactly this
  reason: they need one address that keeps working whether or not the LAN
  port is mapped. `ListenPlan` binds an ingress/watchdog socket at that same
  `ip_address:7123` in every case except the one where the LAN port is left
  at its unmodified default (7123) — there, `0.0.0.0:7123` already serves
  both purposes, so no second socket is needed.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass

import httpx

_LOGGER = logging.getLogger(__name__)

# The add-on's declared container-side port (`config.yaml` `ports: 7123/tcp`,
# `ingress_port: 7123`). Both are pinned and — per `config.yaml`'s own
# comment next to `host_network` — must not change before 1.0.0, so this is
# safe to hard-code rather than re-derive from config.yaml at runtime.
INGRESS_CONTAINER_PORT = 7123

# A LAN socket only ever appears in a `ListenPlan` because the operator's own
# Network-tab setting asked for one (the port left at its default, or
# remapped to something else) — never as a hardcoded fallback. The ingress
# socket always binds the specific Supervisor-assigned bridge address
# instead (see `INGRESS_CONTAINER_PORT` usage below), never this wildcard.
_LAN_WILDCARD_HOST = "0.0.0.0"  # noqa: S104 — see comment above

_SUPERVISOR_BASE_URL = "http://supervisor"
_SELF_INFO_PATH = "/addons/self/info"
_DEFAULT_ATTEMPTS = 3
_DEFAULT_RETRY_DELAY = 1.0


class AddonNetworkUnavailable(RuntimeError):
    """`main()` must treat this as fail-closed: abort startup with a readable
    message rather than falling back to a guessed `ListenPlan`. A guessed
    fallback (e.g. "just bind 0.0.0.0:7123") could silently re-expose the
    MCP endpoint and Setup UI on the LAN after an operator had deliberately
    disabled the port on the add-on's Network tab (ADR-0004 S3).
    """


@dataclass(frozen=True)
class ListenPlan:
    """The (host, port) sockets uvicorn should bind, worked out from the
    add-on's Network-tab settings (ADR-0004 D1b). Always at least one socket.

    `own_slug` (W3 Security review M4) is nexus's own add-on slug from the
    same `GET /addons/self/info` call, exposed here so `server.main()` can
    feed it to `self_protection.set_own_slug(...)` without a second fetch —
    `None` in standalone mode (there is no "own add-on") or if the field is
    ever missing/malformed in add-on mode (`self_protection` then fails
    closed for D-2 specifically; this module's own fail-closed guarantee
    stays scoped to the LAN-port/`ip_address` concerns it already covers).
    """

    sockets: tuple[tuple[str, int], ...]
    own_slug: str | None = None

    @property
    def lan_port(self) -> int | None:
        """The port reachable from the LAN, if any — the Setup UI needs this
        to build the MCP URL a LAN-based client should connect to (or to
        show the "MCP is not exposed on the LAN" message instead)."""
        for host, port in self.sockets:
            if host == _LAN_WILDCARD_HOST:
                return port
        return None

    @classmethod
    def standalone(cls, port: int) -> ListenPlan:
        """Non-add-on deployment (`.env`, `NEXUS_HTTP=1` without
        `SUPERVISOR_TOKEN`): no Supervisor, no Network tab, so this is just
        today's single wildcard socket — unchanged from pre-0.23.0 nexus."""
        return cls(sockets=((_LAN_WILDCARD_HOST, port),), own_slug=None)

    @classmethod
    def from_self_info(cls, data: dict) -> ListenPlan:
        """Build a `ListenPlan` from `GET /addons/self/info`'s `data` object.

        Raises `AddonNetworkUnavailable` (fail-closed) on any shape this
        module doesn't recognize — an unrecognized shape must never be
        silently treated as "port disabled" *or* "port enabled"; either
        guess could be wrong in a way that either exposes or breaks nexus.
        """
        ip_address_raw = data.get("ip_address")
        if not isinstance(ip_address_raw, str) or not ip_address_raw:
            raise AddonNetworkUnavailable(
                f"GET {_SELF_INFO_PATH} response is missing a usable 'ip_address': {ip_address_raw!r}"
            )
        try:
            ipaddress.ip_address(ip_address_raw)
        except ValueError as exc:
            raise AddonNetworkUnavailable(
                f"GET {_SELF_INFO_PATH} returned an invalid 'ip_address': {ip_address_raw!r}"
            ) from exc

        network = data.get("network")
        port_key = f"{INGRESS_CONTAINER_PORT}/tcp"
        port_value = network.get(port_key) if isinstance(network, dict) else None

        if port_value is not None:
            if isinstance(port_value, bool) or not isinstance(port_value, int):
                raise AddonNetworkUnavailable(
                    f"GET {_SELF_INFO_PATH} returned a non-integer port for {port_key!r}: {port_value!r}"
                )
            if not (1 <= port_value <= 65535):
                raise AddonNetworkUnavailable(
                    f"GET {_SELF_INFO_PATH} returned an out-of-range port for {port_key!r}: {port_value!r}"
                )

        if port_value is None:
            # Network tab: port cleared/disabled → ingress (and Supervisor's
            # watchdog) only, no LAN socket at all.
            sockets = ((ip_address_raw, INGRESS_CONTAINER_PORT),)
        elif port_value == INGRESS_CONTAINER_PORT:
            # Network tab: unmodified default → today's behaviour, one
            # socket that already serves both LAN and ingress.
            sockets = ((_LAN_WILDCARD_HOST, INGRESS_CONTAINER_PORT),)
        else:
            # Network tab: remapped to a different LAN port → ingress needs
            # its own dedicated socket, separate from the new LAN port.
            sockets = ((ip_address_raw, INGRESS_CONTAINER_PORT), (_LAN_WILDCARD_HOST, port_value))

        own_slug_raw = data.get("slug")
        own_slug = own_slug_raw if isinstance(own_slug_raw, str) and own_slug_raw else None

        return cls(sockets=sockets, own_slug=own_slug)


def _fetch_self_info(
    token: str,
    *,
    client: httpx.Client | None = None,
    attempts: int = _DEFAULT_ATTEMPTS,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
) -> dict:
    """`GET /addons/self/info` with short retrying (ADR-0004 D1b).

    A short retry window absorbs Supervisor still starting up / briefly
    unreachable right after boot — a single failed attempt must not abort
    the whole add-on. Exhausting every attempt does abort it (fail-closed):
    see `AddonNetworkUnavailable`.
    """
    owns_client = client is None
    http_client = client or httpx.Client(
        base_url=_SUPERVISOR_BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    last_error: Exception | None = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                response = http_client.get(
                    _SELF_INFO_PATH,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                _LOGGER.warning(
                    "GET %s attempt %d/%d failed: %s", _SELF_INFO_PATH, attempt, attempts, exc
                )
                if attempt < attempts:
                    time.sleep(retry_delay)
                continue

            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                last_error = AddonNetworkUnavailable(
                    f"Unexpected GET {_SELF_INFO_PATH} response shape (no 'data' object): "
                    f"{str(body)[:200]!r}"
                )
                if attempt < attempts:
                    time.sleep(retry_delay)
                continue

            return data
    finally:
        if owns_client:
            http_client.close()

    raise AddonNetworkUnavailable(
        f"Could not get a usable response from GET {_SELF_INFO_PATH} after {attempts} "
        f"attempt(s): {last_error}"
    )


def resolve_listen_plan(
    *,
    supervisor_token: str | None,
    nexus_port: int,
    client: httpx.Client | None = None,
    attempts: int = _DEFAULT_ATTEMPTS,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
) -> ListenPlan:
    """Entry point for `server.main()`.

    Standalone (no `SUPERVISOR_TOKEN`): today's single-socket behaviour,
    unconditionally — there is no Supervisor and no Network tab to honour.

    Add-on mode: fetch `GET /addons/self/info` (short-retried) and derive the
    `ListenPlan` from its `network`/`ip_address` fields. Raises
    `AddonNetworkUnavailable` — meant to abort startup, never to be caught
    and silently worked around — if Supervisor stays unreachable or returns
    something this module doesn't recognize.
    """
    if not supervisor_token:
        return ListenPlan.standalone(nexus_port)

    data = _fetch_self_info(supervisor_token, client=client, attempts=attempts, retry_delay=retry_delay)
    return ListenPlan.from_self_info(data)
