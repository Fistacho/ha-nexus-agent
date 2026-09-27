import logging
import os
import secrets
from pathlib import Path
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

_LOGGER = logging.getLogger(__name__)

load_dotenv()

_KEY_FILE = Path(os.getenv("HA_CONFIG_PATH", "/config")) / ".nexus_api_key"
_bearer = HTTPBearer(auto_error=False)

_supervisor_token: str | None = os.getenv("SUPERVISOR_TOKEN")
_ha_token: str | None = os.getenv("HA_TOKEN")


def get_ha_token() -> str:
    """Return whichever HA token is available. SUPERVISOR_TOKEN wins (running as add-on)."""
    token = _supervisor_token or _ha_token or ""
    if not token:
        raise RuntimeError(
            "No HA token found. Set HA_TOKEN in .env or run as a Home Assistant add-on."
        )
    return token


def get_or_create_api_key() -> str:
    """Load API key from disk, or generate and persist a new one."""
    env_key = os.getenv("NEXUS_API_KEY")
    if env_key:
        return env_key

    if _KEY_FILE.exists():
        return _KEY_FILE.read_text().strip()

    key = secrets.token_urlsafe(32)
    try:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(key)
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


API_KEY = get_or_create_api_key()


def delete_api_key() -> None:
    """Delete the persisted key file so a new key is generated on next startup."""
    try:
        _KEY_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def verify_token(credentials: HTTPAuthorizationCredentials = Security(_bearer)) -> str:
    if credentials is None or credentials.credentials != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return credentials.credentials


# --- Setup UI / /regenerate trust boundary -----------------------------------
#
# The Setup UI (`GET /`) and `POST /regenerate` are NOT protected by the
# `/mcp` bearer/query-token middleware in server.py (they intentionally show
# the key to a first-time user). They must instead be restricted to callers
# Home Assistant itself has already authenticated, or to a valid API key.
#
# Per Home Assistant developer docs (Add-ons > Presentation > Ingress,
# https://developers.home-assistant.io/docs/add-ons/presentation/#ingress):
#   "Only connections from `172.30.32.2` must be allowed. You should deny
#   access to all other IP addresses within your app server."
#   "Ingress adds a request header `X-Ingress-Path` ..."
#   "Users are previously authenticated via Home Assistant. Authentication is
#   not required [again by the add-on]."
#
# ADR-0004 S1 re-verification (2026-09-27, `home-assistant/supervisor` @
# `main` and `home-assistant/core` @ `dev`), done because host_network
# changes which address Supervisor's ingress proxy targets (see below) and
# the coordinator's security review specifically asked for this to be
# checked in source rather than assumed:
#
# - `X-Ingress-Path` (and `X-Hass-Source: core.ingress`) is added by **HA
#   Core**, not directly by Supervisor: `homeassistant/components/hassio/
#   ingress.py::_init_header()` sets both on every request Core proxies from
#   the browser's `/api/hassio_ingress/<token>/...` to Supervisor's own
#   `/ingress/<token>/...`. Supervisor's `supervisor/api/ingress.py::
#   _init_header()` then forwards that request on to the add-on container;
#   its own header-filter list only drops `Content-Length`,
#   `Content-Encoding`, `Transfer-Encoding`, the `Sec-WebSocket-*` triplet and
#   `HEADER_REMOTE_USER_*`/`HEADER_TOKEN*` — `X-Ingress-Path` and
#   `X-Hass-Source` are NOT in that list, so they reach the add-on unchanged.
#   Net effect: the header this function checks for genuinely arrives here,
#   confirmed by reading both hops, not just the add-on-facing one.
# - The peer IP: Supervisor's proxy (`api/ingress.py::_handle_request` /
#   `_handle_websocket`) connects out to `http://{app.ip_address}:
#   {app.ingress_port}/...` using Supervisor's *own* aiohttp
#   `ClientSession` — i.e. from Supervisor's own container, whose address on
#   the "hassio" bridge network is fixed at `172.30.32.2`
#   (`supervisor/docker/network.py::DockerNetwork.supervisor` ==
#   `DOCKER_IPV4_NETWORK_MASK[2]`, `172.30.32.0/23`). That source address
#   does not depend on the *destination* `app.ip_address` at all — under
#   `host_network`, `supervisor/docker/app.py::DockerApp.ip_address` returns
#   `sys_docker.network.gateway` (`172.30.32.1`, the same bridge's gateway,
#   an address Docker assigns directly to a host-visible bridge interface)
#   instead of a dedicated container IP, but Supervisor is still the one
#   *initiating* the connection, from the same container, over the same
#   bridge network, either way. So `172.30.32.2` as the expected peer IP is
#   not expected to change under host_network — but this is inferred from
#   Docker bridge-networking semantics (a container connecting to its own
#   bridge's gateway address is intra-network traffic Docker does not NAT),
#   not from a packet capture. `is_ingress_request()` logs the observed peer
#   at DEBUG specifically so this can be confirmed live once nexus actually
#   runs with `host_network: true` (ADR-0004 acceptance criteria).
# - Consequence for this function: do NOT add `172.30.32.1` (the gateway) or
#   `127.0.0.1` to the trusted-peer set. Both are host-reachable under
#   `host_network` by any process or add-on sharing the host's network
#   namespace, not just Supervisor — trusting either would let anything else
#   on the host impersonate ingress. If the peer-IP assumption above ever
#   turns out wrong on live HA, this function is meant to fail CLOSED (return
#   False, keep the Setup UI locked) rather than have its trusted-peer set
#   widened to compensate.
#
# CSRF note: a POST to /regenerate cannot be forged from a third-party page
# through the user's browser, because (a) the add-on only accepts it from
# 172.30.32.2 — an address only the Supervisor's internal proxy can connect
# from, never a browser — and (b) the Supervisor itself only forwards ingress
# requests that carry the `ingress_session` cookie, which Home Assistant sets
# with `SameSite=Strict` (home-assistant/frontend#6550), so browsers refuse to
# attach it to any cross-site request in the first place. POST (not GET) is
# still used so the action can never be triggered by a passive resource load
# (e.g. an `<img>` tag) even in a same-site context.

INGRESS_PROXY_HOST = "172.30.32.2"
_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_ingress_request(request: Request) -> bool:
    """True if this request was proxied by the HA Supervisor's ingress.

    Both the source IP *and* the `X-Ingress-Path` header are required —
    defense in depth in case something else on the docker network shares the
    172.30.32.2 address or spoofs the header alone. Never widen this by
    trusting a different peer (e.g. the host_network gateway `172.30.32.1`,
    or `127.0.0.1`) instead — see the ADR-0004 S1 comment above.

    Logs the observed peer at DEBUG (never any header *value* beyond the
    boolean "is X-Ingress-Path present", and never a secret) so the
    172.30.32.2 assumption above can be confirmed against a live add-on
    running with `host_network: true`.
    """
    client = request.client
    client_host = client.host if client else None
    has_ingress_header = "x-ingress-path" in request.headers
    result = client_host == INGRESS_PROXY_HOST and has_ingress_header

    _LOGGER.debug(
        "is_ingress_request: peer=%s has_x_ingress_path_header=%s -> trusted=%s "
        "(expected peer for ADR-0004 host_network: %s)",
        client_host, has_ingress_header, result, INGRESS_PROXY_HOST,
    )
    return result


def is_addon_mode() -> bool:
    """True when running as a Supervisor add-on (SUPERVISOR_TOKEN present).

    Mirrors the check `server.main()` uses to decide HTTP vs stdio mode. In
    add-on mode, ingress is always available, so localhost is NOT trusted as
    a fallback (the container's loopback is reachable by anything sharing its
    network namespace).
    """
    return bool(os.getenv("SUPERVISOR_TOKEN"))


def is_localhost_request(request: Request) -> bool:
    client = request.client
    return bool(client) and client.host in _LOCALHOST_HOSTS


def bearer_token_matches(request: Request) -> bool:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False
    token = auth_header[len("Bearer "):].strip()
    return bool(token) and token == API_KEY


def can_access_ui(request: Request) -> bool:
    """True if this caller may see the Setup UI's API key / trigger /regenerate.

    Allowed, in order:
    - HA ingress (172.30.32.2 + `X-Ingress-Path`) — HA already authenticated
      the user via the frontend session before Supervisor proxied here.
    - A valid `Authorization: Bearer <API_KEY>` header — the same credential
      that protects `/mcp`, for scripted/CLI access.
    - Standalone mode only (no SUPERVISOR_TOKEN, e.g. `.env` deployment
      outside HA) — a request from localhost, since there is no ingress proxy
      to authenticate the caller in that deployment.
    """
    if is_ingress_request(request):
        return True
    if bearer_token_matches(request):
        return True
    return not is_addon_mode() and is_localhost_request(request)
