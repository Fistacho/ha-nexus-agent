import logging
import os
import re
import sys
from fastmcp import FastMCP
from dotenv import load_dotenv

from tools.entities import mcp as entities_mcp
from tools.services import mcp as services_mcp
from tools.automations import mcp as automations_mcp
from tools.areas import mcp as areas_mcp
from tools.history import mcp as history_mcp
from tools.helpers import mcp as helpers_mcp
from tools.system import mcp as system_mcp
from tools.dashboards import mcp as dashboards_mcp
from tools.files import mcp as files_mcp
from tools.git_ops import mcp as git_mcp
from tools.websocket import mcp as ws_mcp
from tools.blueprints import mcp as blueprints_mcp
from tools.calendar import mcp as calendar_mcp
from tools.todo import mcp as todo_mcp
from tools.devices import mcp as devices_mcp
from tools.supervisor import mcp as supervisor_mcp
from tools.hacs import mcp as hacs_mcp
from tools.energy import mcp as energy_mcp
from tools.zones import mcp as zones_mcp
from tools.labels import mcp as labels_mcp
from tools.search import mcp as search_mcp
from tools.integrations import mcp as integrations_mcp
from tools.voice import mcp as voice_mcp
from tools.themes import mcp as themes_mcp
from tools.card_builder import mcp as card_builder_mcp
from tools.snapshot import mcp as snapshot_mcp
from tools.esphome import mcp as esphome_mcp
from tools.statistics import mcp as statistics_mcp
from tools import discover as discover_mod
from tools.discover import mcp as discover_mcp

# Not needed here for correctness (E402 CI-B cleanup, 0.22.0): every
# `tools/<module>.py` above transitively imports `ha_client.py` or `auth.py`
# before it evaluates any of its own module-level `os.getenv(...)` calls, and
# both of those already call `load_dotenv()` at their own top (`ha_client.py`
# line 10, `auth.py` line 8) — as do the handful of tool modules that read
# `HA_CONFIG_PATH`/`HA_URL` directly (`tools/files.py`, `tools/git_ops.py`,
# `tools/themes.py`, `tools/websocket.py`, each with its own `load_dotenv()`
# call before the read). `python-dotenv`'s `load_dotenv()` only *fills in*
# variables absent from `os.environ` (`override=False` by default) and is
# idempotent, so calling it again here — after every import above has
# already had a chance to populate `os.environ` from `.env` — changes
# nothing an MCP client can observe. Kept for readability (this file's own
# entry point, `main()`, also reads env vars, e.g. `NEXUS_PORT`) rather than
# functional necessity; verified via `grep -rn load_dotenv tools/*.py *.py`
# that no module-level `os.getenv` call anywhere in the codebase runs before
# some `load_dotenv()` call.
load_dotenv()

mcp = FastMCP("nexus")

mcp.mount(entities_mcp, namespace="entities")
mcp.mount(services_mcp, namespace="services")
mcp.mount(automations_mcp, namespace="automations")
mcp.mount(areas_mcp, namespace="areas")
mcp.mount(history_mcp, namespace="history")
mcp.mount(helpers_mcp, namespace="helpers")
mcp.mount(system_mcp, namespace="system")
mcp.mount(dashboards_mcp, namespace="dashboards")
mcp.mount(files_mcp, namespace="files")
mcp.mount(git_mcp, namespace="git")
mcp.mount(ws_mcp, namespace="ws")
mcp.mount(blueprints_mcp, namespace="blueprints")
mcp.mount(calendar_mcp, namespace="calendar")
mcp.mount(todo_mcp, namespace="todo")
mcp.mount(devices_mcp, namespace="devices")
mcp.mount(supervisor_mcp, namespace="supervisor")
mcp.mount(hacs_mcp, namespace="hacs")
mcp.mount(energy_mcp, namespace="energy")
mcp.mount(zones_mcp, namespace="zones")
mcp.mount(labels_mcp, namespace="labels")
mcp.mount(search_mcp, namespace="search")
mcp.mount(integrations_mcp, namespace="integrations")
mcp.mount(voice_mcp, namespace="voice")
mcp.mount(themes_mcp, namespace="themes")
mcp.mount(card_builder_mcp, namespace="card_builder")
mcp.mount(snapshot_mcp, namespace="snapshot")
mcp.mount(esphome_mcp, namespace="esphome")
mcp.mount(statistics_mcp, namespace="statistics")
mcp.mount(discover_mcp, namespace="discover")

# Bind the root catalogue to discover.* so tool_search can introspect every
# mounted namespace. Must run AFTER all other mount() calls.
discover_mod.bind_root(mcp)


# uvicorn's access logger logs the full request line, including the raw query
# string — so `GET /mcp?token=<API_KEY>` would otherwise be written to the
# add-on log in plaintext on every request. Both uvicorn HTTP protocol
# implementations (h11, httptools) log via
# `access_logger.info('%s - "%s %s HTTP/%s" %d', client_addr, method,
# full_path, http_version, status_code)` — `full_path` (args[2]) is where the
# query string lives.
_TOKEN_QS_RE = re.compile(r"([?&]token=)[^&\s]+", re.IGNORECASE)

# Logged once per process, the first time a request authenticates via
# `?token=` instead of the `Authorization: Bearer` header. Never logs the
# token value itself — only that the (already deprecated, see README/
# Security) query-string method was used.
_query_token_deprecation_logged = False


def _warn_query_token_deprecated_once() -> None:
    global _query_token_deprecation_logged
    if _query_token_deprecation_logged:
        return
    _query_token_deprecation_logged = True
    logging.getLogger(__name__).warning(
        "Authenticated via the '?token=' query string. This method is deprecated "
        "and will be removed in nexus 1.0.0 — switch to an "
        "'Authorization: Bearer <API_KEY>' header instead."
    )


class RedactTokenFilter(logging.Filter):
    """Mask `?token=...` in uvicorn.access log records before they're emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            full_path = record.args[2]
            if isinstance(full_path, str) and "token=" in full_path:
                args = list(record.args)
                args[2] = _TOKEN_QS_RE.sub(r"\1***", full_path)
                record.args = tuple(args)
        return True


def _startup_log_lines(port: int, http_mode: bool) -> list[str]:
    """Startup banner lines — deliberately never include the raw API key.

    It used to be printed with `print(f"API key → {API_KEY}")` on every
    start, landing in the add-on's persisted log (and any terminal history in
    standalone mode) in plaintext.

    Used for stdio mode only since 0.23.0 (ADR-0004) — HTTP mode's banner
    must instead reflect a `ListenPlan`, which may bind more than one socket;
    see `_startup_log_lines_for_plan`.
    """
    from auth import _KEY_FILE

    if http_mode:
        lines = [
            f"Nexus starting (HTTP) on port {port}",
            f"Setup UI  → http://localhost:{port}",
            f"MCP       → http://localhost:{port}/mcp",
        ]
    else:
        lines = ["Nexus starting (stdio)"]

    lines.append(f"API key stored in {_KEY_FILE}; open the Nexus panel (ingress) to see client config")
    return lines


def _startup_log_lines_for_plan(plan) -> list[str]:
    """HTTP-mode startup banner for a `ListenPlan` (ADR-0004 D1b).

    Unlike `_startup_log_lines`, this reports every socket nexus actually
    binds — one line each — instead of assuming a single `0.0.0.0:<port>`.
    Under `host_network` that can be one wildcard LAN socket (today's
    behaviour, port left at its default), a wildcard LAN socket *plus* a
    dedicated ingress socket (port remapped), or ingress-only with no LAN
    socket at all (port disabled) — see `addon_network.ListenPlan`.

    Never includes the raw API key, for the same reason as
    `_startup_log_lines`.
    """
    from auth import _KEY_FILE

    lines = ["Nexus starting (HTTP)"]
    for host, sock_port in plan.sockets:
        lines.append(f"Listening on {host}:{sock_port}")

    if plan.lan_port is not None:
        lines.append(f"Setup UI  → http://localhost:{plan.lan_port}")
        lines.append(f"MCP       → http://localhost:{plan.lan_port}/mcp")
    else:
        lines.append(
            "MCP + Setup UI are NOT exposed on the LAN (port disabled in this "
            "add-on's Network settings) — reach Nexus via Home Assistant ingress instead"
        )

    lines.append(f"API key stored in {_KEY_FILE}; open the Nexus panel (ingress) to see client config")
    return lines


# --- Origin validation (MCP spec 2025-11-25, "Transports" > Streamable HTTP >
# Security Warning): -----------------------------------------------------
#
#   "1. Servers MUST validate the Origin header on all incoming connections
#      to prevent DNS rebinding attacks. If the Origin header is present and
#      invalid, servers MUST respond with HTTP 403 Forbidden. [...]
#    2. When running locally, servers SHOULD bind only to localhost
#      (127.0.0.1) rather than all network interfaces (0.0.0.0)."
#
# Point 2 is a separate, pre-existing tradeoff (ADR-0004: nexus deliberately
# binds the LAN wildcard so operators can reach it from other devices on
# their network) this change does not revisit — only point 1 (Origin) is in
# scope here.
#
# Checked before writing a custom implementation, per that same section's
# intent ("use the SDK's own mechanism if there is one"): the installed `mcp`
# SDK (1.27.0) ships exactly this as `mcp.server.transport_security.
# TransportSecurityMiddleware` / `TransportSecuritySettings`, wired into the
# lower-level `mcp.server.streamable_http.StreamableHTTPServerTransport` via a
# `security_settings=` constructor parameter that flows down from
# `mcp.server.streamable_http_manager.StreamableHTTPSessionManager`. But
# FastMCP 3.2.4's own `FastMCP.http_app()` -> `fastmcp.server.http.
# create_streamable_http_app()` constructs that `StreamableHTTPSessionManager`
# itself and never accepts or forwards a `security_settings` (or any
# equivalent) parameter — grepped both files, no such parameter exists on
# either public function. There is therefore no way to reach the SDK's own
# DNS-rebinding protection through FastMCP's public API in this version;
# reaching into `mcp_app.state`/`mcp_app.router` to mutate a private
# `StreamableHTTPSessionManager` instance after construction would be more
# fragile (undocumented internals, silently broken by a FastMCP upgrade)
# than this equivalent, small ASGI middleware — same tradeoff already made
# for `TokenAuthMiddleware` above (a pure ASGI middleware instead of
# `BaseHTTPMiddleware`, which buffers responses and breaks streaming).
#
# Deliberately does NOT compare an incoming Origin against the request's own
# `Host` header ("is this the server's own origin?") — that comparison is
# worthless against the exact attack it's meant to stop: in a DNS rebinding
# attack, `Host` is exactly as attacker-controlled as `Origin` (both are set
# by the browser from the URL the attacker's script chose to fetch, not from
# the IP address that URL's hostname actually resolved to). An attacker who
# serves their page from a hostname:port matching nexus's own declared LAN
# port would pass a naive Origin-vs-Host check every time. The only sound
# comparison is against origins the *server* — not the incoming request —
# considers legitimate: a small hardcoded set of the server's own well-known
# direct-access origins at its actual (Supervisor-reported, or `NEXUS_PORT`
# in standalone mode) LAN port, plus whatever an operator explicitly opts
# into via `NEXUS_ALLOWED_ORIGINS`.
def _default_allowed_origins(lan_port: int | None) -> tuple[str, ...]:
    """Origins nexus is reachable as directly (not via ingress) on its own
    LAN port — `127.0.0.1`/`[::1]`/`localhost` (the standalone-mode trust
    boundary `auth.is_localhost_request` already relies on) and
    `homeassistant.local` (the HA host's conventional mDNS name; nexus's
    add-on normally runs on the same host as HA Core). `lan_port=None`
    (ADR-0004 D1b/S3: the operator disabled the port on this add-on's
    Network tab) means nothing is directly LAN-reachable at all — only HA
    ingress (exempted separately by `OriginValidationMiddleware`, see below)
    or an operator's own `NEXUS_ALLOWED_ORIGINS` entry can reach nexus, so
    there is no legitimate direct-Origin case left to default here.
    """
    if lan_port is None:
        return ()
    return (
        f"http://127.0.0.1:{lan_port}",
        f"http://[::1]:{lan_port}",
        f"http://localhost:{lan_port}",
        f"http://homeassistant.local:{lan_port}",
    )


def _parse_allowed_origins_env() -> tuple[str, ...]:
    """Extra Origin values an operator explicitly allow-lists via
    `NEXUS_ALLOWED_ORIGINS` (comma-separated full origins, e.g.
    "https://my-web-client.example.com,https://my-web-client.example.com:*")
    — for a browser-based MCP client, or an ingress-fronted one, that isn't
    covered by `_default_allowed_origins`. Not read from `config.yaml` —
    deliberately env-only for now; see CHANGELOG/handoff note for whether a
    `config.yaml` option should be added on top of this (schema/UX decision,
    left to whoever owns that file).
    """
    raw = os.getenv("NEXUS_ALLOWED_ORIGINS", "")
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


_WILDCARD_PORT_SUFFIX_RE = re.compile(r"[0-9]+")


def _origin_matches_allowlist(origin: str, allowed: tuple[str, ...]) -> bool:
    """Exact match (case-insensitive), or the same `scheme://host:*`
    wildcard-port convention `mcp.server.transport_security.
    TransportSecuritySettings.allowed_origins` itself supports — kept
    identical so operators migrating notes between the two aren't surprised.

    The part of `origin` after `"<base>:"` must be *only* digits (a bare
    port number) — a plain `str.startswith(base + ":")` also matches
    `"<base>:9443.evil.com"` (an attacker-chosen hostname that happens to
    start with the allow-listed one, followed by a colon and anything at
    all), which would silently defeat the wildcard's own purpose (loosening
    only the *port*, never the host).
    """
    origin_lower = origin.lower()
    for entry in allowed:
        entry_lower = entry.lower()
        if entry_lower.endswith(":*"):
            base = entry_lower[:-2]
            prefix = base + ":"
            if origin_lower.startswith(prefix) and _WILDCARD_PORT_SUFFIX_RE.fullmatch(
                origin_lower[len(prefix):]
            ):
                return True
        elif origin_lower == entry_lower:
            return True
    return False


def _build_app(listen_plan=None):
    """Combine MCP + setup UI into one ASGI app for HTTP mode.

    `listen_plan` (an `addon_network.ListenPlan`, or `None`) is only used to
    work out `_default_allowed_origins`'s LAN port — `None` (every existing
    caller of `_build_app()` with no arguments, including the whole of
    `tests/test_audit_http_auth.py`) falls back to `NEXUS_PORT`
    (default 7123), reproducing `setup_ui._lan_port()`'s own fallback so both
    stay consistent without importing one module's private helper into the
    other.
    """
    from fastapi import FastAPI, Request as FastAPIRequest
    from fastapi.responses import HTMLResponse
    from starlette.datastructures import Headers
    import setup_ui
    from setup_ui import setup_page, health, regenerate
    from auth import API_KEY, is_ingress_request
    from urllib.parse import parse_qs

    # Streamable HTTP transport — modern MCP spec, more robust than SSE
    mcp_app = mcp.http_app(path="/mcp", transport="http", stateless_http=True)

    # Pure ASGI middleware — BaseHTTPMiddleware buffers responses and breaks streaming
    class TokenAuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
                query = parse_qs(scope.get("query_string", b"").decode())
                query_token = query.get("token", [None])[0]
                token = query_token
                if not token:
                    # `Headers(scope=scope)` instead of the old
                    # `dict(scope.get("headers", []))`: a plain dict built
                    # from the raw header-tuple list silently keeps only the
                    # *last* occurrence of a repeated header name. Same
                    # semantics reproduced explicitly here via
                    # `getlist(...)[-1]` — unlike Origin below, a repeated
                    # `Authorization` header isn't given special "reject as
                    # ambiguous" treatment; it isn't the header the DNS
                    # rebinding protection this middleware sits next to
                    # cares about, and changing *auth* behaviour was not
                    # asked for here.
                    headers = Headers(scope=scope)
                    auth_values = headers.getlist("authorization")
                    token = auth_values[-1].removeprefix("Bearer ").strip() if auth_values else ""
                if token != API_KEY:
                    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
                    return
                if query_token:
                    _warn_query_token_deprecated_once()
            await self.app(scope, receive, send)

    lan_port = listen_plan.lan_port if listen_plan is not None else int(os.getenv("NEXUS_PORT", "7123"))
    allowed_origins = _default_allowed_origins(lan_port) + _parse_allowed_origins_env()

    # Pure ASGI middleware, same reasoning as TokenAuthMiddleware above.
    # Applies to every path except `/health` (public/unauthenticated by
    # design — see `setup_ui.health`'s own docstring) — including `/`,
    # `/regenerate` and `/mcp` alike, so a browser-based DNS-rebinding
    # attempt is rejected before it ever reaches app logic or the token
    # check, not just on the MCP endpoint the spec literally names.
    #
    # `scope["type"] != "http"` also skips every `"websocket"` scope
    # unconditionally — harmless *today*: nothing in this ASGI app (FastAPI's
    # own routes, or FastMCP's `mcp_app` mounted alongside them) declares a
    # WebSocket route, so no `"websocket"` scope ever reaches this
    # middleware in the first place (`tools/websocket.py` is an *outbound*
    # client of HA's own WS API, unrelated to this inbound ASGI app). If an
    # inbound WS transport is ever added here, re-audit this branch — a
    # browser's WebSocket handshake also carries an `Origin` header the same
    # DNS-rebinding logic would need to validate, via `websocket.close(...)`
    # rather than an HTTP 403 response.
    class OriginValidationMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope.get("path", "") == "/health":
                await self.app(scope, receive, send)
                return

            # `Headers(scope=scope)` instead of `dict(scope.get("headers",
            # []))` — see TokenAuthMiddleware's own comment for why a plain
            # dict is the wrong tool here. Unlike Authorization, a repeated
            # `Origin` header is NOT resolved by picking one: more than one
            # value is inherently ambiguous input (which one did whatever
            # sits in front of nexus — if anything — actually act on?) and
            # is rejected outright below, regardless of whether any
            # individual value would have been allowed on its own.
            headers = Headers(scope=scope)
            origin_values = headers.getlist("origin")
            if not origin_values:
                # Spec only requires validation when the header is present;
                # every non-browser MCP client nexus documents never sends
                # one, and same-origin browser GETs typically don't either.
                await self.app(scope, receive, send)
                return

            # HA ingress (172.30.32.2 + X-Ingress-Path) cannot be forged by a
            # remote attacker's browser — see auth.is_ingress_request's own
            # docstring for why that peer check is itself the trust
            # boundary here. Its Origin is the *HA frontend's* own origin
            # (a Nabu Casa URL, a LAN IP:8123, a custom domain, ...), never
            # nexus's own host:port, so validating it against
            # `allowed_origins` would only ever break the Setup UI's own
            # same-page `fetch('regenerate', {method:'POST'})` for every
            # add-on user reaching nexus through ingress. Checked before the
            # duplicate-header rejection too — ingress is a fully trusted
            # channel, not merely a specific Origin value.
            if is_ingress_request(FastAPIRequest(scope)):
                await self.app(scope, receive, send)
                return

            if len(origin_values) > 1:
                logging.getLogger(__name__).warning(
                    "Rejected %s %s: %d Origin headers (ambiguous)",
                    scope.get("method", ""), scope.get("path", ""), len(origin_values),
                )
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"Forbidden: multiple Origin headers"}'})
                return

            origin_str = origin_values[0].strip()
            if not origin_str or origin_str.lower() == "null" or not _origin_matches_allowlist(origin_str, allowed_origins):
                logging.getLogger(__name__).warning(
                    "Rejected %s %s: disallowed Origin %r",
                    scope.get("method", ""), scope.get("path", ""), origin_str,
                )
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"Forbidden: invalid Origin header"}'})
                return

            await self.app(scope, receive, send)

    app = FastAPI(title="Nexus", docs_url=None, redoc_url=None, lifespan=mcp_app.lifespan)
    # add_middleware() prepends (Starlette wraps outermost = called first
    # with the *last* one added) — Origin is checked before the API key so a
    # DNS-rebinding attempt never even reaches the token comparison.
    app.add_middleware(TokenAuthMiddleware)
    app.add_middleware(OriginValidationMiddleware)

    setup_ui.set_mcp(mcp)

    app.get("/", response_class=HTMLResponse)(setup_page)
    app.get("/health")(health)
    app.post("/regenerate")(regenerate)

    app.mount("/", mcp_app)

    return app


def _apply_tool_policy_or_exit():
    """Read and enforce the tool-exposure policy (ADR-0003 P1/P2/P3).

    Runs once, here, after every `mcp.mount()` call above has already
    executed at import time — `import server` on its own never reaches this
    function, so tests/tooling that only need the full, unfiltered catalogue
    (T1/T2, `tests/contract/generate_tool_surface.py`) are unaffected.

    A malformed option (unknown namespace, non-boolean `read_only`, invalid
    `tool_mode`, or `tool_mode=search` with `disabled_namespaces` including
    `discover`) aborts startup with a readable message instead of silently
    applying a narrower — or wider — policy than the operator configured.
    `apply_policy()` itself can raise the last of those (it re-checks the
    `discover`/`tool_mode` invariant even for a `ToolPolicy` built outside
    `from_env()`), so both calls share one try/except.
    """
    from policy import PolicyConfigError, ToolPolicy, apply_policy

    try:
        tool_policy = ToolPolicy.from_env()
        application = apply_policy(mcp, tool_policy)
    except PolicyConfigError as exc:
        print(f"Nexus refused to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    logging.getLogger(__name__).info(
        "Tool policy applied: read_only=%s disabled_namespaces=%s tool_mode=%s -> "
        "%d/%d tools visible, %d/%d prompts visible",
        tool_policy.read_only,
        sorted(tool_policy.disabled_namespaces),
        tool_policy.tool_mode,
        application.tools_total - application.tools_disabled,
        application.tools_total,
        application.prompts_total - application.prompts_disabled,
        application.prompts_total,
    )
    return tool_policy


def main():
    port = int(os.getenv("NEXUS_PORT", "7123"))

    tool_policy = _apply_tool_policy_or_exit()

    # HTTP mode: add-on or explicit NEXUS_HTTP=1
    if os.getenv("SUPERVISOR_TOKEN") or os.getenv("NEXUS_HTTP"):
        import uvicorn

        import setup_ui
        from addon_network import AddonNetworkUnavailable, resolve_listen_plan

        # ADR-0004 D1b: with `host_network: true`, Docker no longer publishes
        # `config.yaml`'s `ports:` mapping — nexus must reconstruct the add-on
        # Network tab's semantics itself before it can even pick a socket to
        # bind. Standalone/NEXUS_HTTP-without-Supervisor short-circuits back
        # to today's single `0.0.0.0:<port>` (see `ListenPlan.standalone`).
        # A Supervisor that stays unreachable, or answers with something this
        # module doesn't recognize, aborts startup instead of guessing a
        # plan — the same fail-closed pattern as `_apply_tool_policy_or_exit`
        # above, for the same reason: a wrong guess here could either expose
        # the LAN port an operator deliberately disabled, or leave nexus
        # unreachable in a way that looks like a crash instead of a config
        # problem.
        try:
            listen_plan = resolve_listen_plan(
                supervisor_token=os.getenv("SUPERVISOR_TOKEN"),
                nexus_port=port,
            )
        except AddonNetworkUnavailable as exc:
            print(f"Nexus refused to start: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        # W3 Security review M4: record nexus's own add-on slug once, here,
        # from the same self-info fetch `resolve_listen_plan()` already made
        # (no second Supervisor call) — `self_protection.is_own_addon()`
        # fails closed for every slug until this runs, so `tools/
        # supervisor.py`/`tools/services.py`/`tools/websocket.py`'s D-2
        # guards only ever see the inert default in a process that never
        # reaches this line (standalone/`NEXUS_HTTP=1` without
        # `SUPERVISOR_TOKEN` skips this whole branch's `if`, see below).
        if os.getenv("SUPERVISOR_TOKEN"):
            import self_protection

            self_protection.set_own_slug(listen_plan.own_slug)

        for line in _startup_log_lines_for_plan(listen_plan):
            print(line)

        # Redact ?token=... from uvicorn's access log before any request is logged.
        logging.getLogger("uvicorn.access").addFilter(RedactTokenFilter())

        app = _build_app(listen_plan)
        setup_ui.set_policy(tool_policy)
        setup_ui.set_listen_plan(listen_plan)

        # uvicorn.Server.run(sockets=[...]) serves one app on multiple
        # pre-bound sockets — the officially supported mechanism behind both
        # systemd socket activation and Gunicorn's multi-worker mode
        # (uvicorn.server.Server.startup(): `for sock in sockets:
        # loop.create_server(create_protocol, sock=sock, ...)`), so every
        # socket in `listen_plan.sockets` is served by the identical ASGI
        # app/middleware stack — never a second, differently-configured
        # server. `uvicorn.Config(...).bind_socket()` is uvicorn's own helper
        # for turning a (host, port) pair into a correctly configured
        # `socket.socket` (SO_REUSEADDR, AF_INET6 for a literal IPv6 host,
        # `set_inheritable(True)`) — using it keeps every socket's low-level
        # setup identical to what `uvicorn.run()` itself would have produced
        # for a single socket, the exact behaviour this preserves for the
        # `host_network: false`-equivalent single-socket plans (default LAN
        # port, or standalone/NEXUS_HTTP).
        sockets = [
            uvicorn.Config(app, host=sock_host, port=sock_port, log_level="info").bind_socket()
            for sock_host, sock_port in listen_plan.sockets
        ]
        uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=sockets)
    else:
        # stdio mode for Claude Desktop / local MCP client
        #
        # Per the MCP spec ("Transports", 2025-11-25): "The server MUST NOT
        # write anything to its stdout that is not a valid MCP message." A
        # bare `print(line)` here used to land on stdout and corrupt the
        # JSON-RPC stream for the stdio client reading it — stderr is the
        # only safe destination for anything nexus prints itself in this
        # branch, same as the two `print(..., file=sys.stderr)` startup-abort
        # messages above.
        for line in _startup_log_lines(port, http_mode=False):
            print(line, file=sys.stderr)
        mcp.run()


if __name__ == "__main__":
    main()
