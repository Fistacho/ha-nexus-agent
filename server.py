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


def _build_app():
    """Combine MCP + setup UI into one ASGI app for HTTP mode."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    import setup_ui
    from setup_ui import setup_page, health, regenerate
    from auth import API_KEY
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
                    headers = dict(scope.get("headers", []))
                    auth = headers.get(b"authorization", b"").decode()
                    token = auth.removeprefix("Bearer ").strip()
                if token != API_KEY:
                    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
                    return
                if query_token:
                    _warn_query_token_deprecated_once()
            await self.app(scope, receive, send)

    app = FastAPI(title="Nexus", docs_url=None, redoc_url=None, lifespan=mcp_app.lifespan)
    app.add_middleware(TokenAuthMiddleware)

    setup_ui.set_mcp(mcp)

    app.get("/", response_class=HTMLResponse)(setup_page)
    app.get("/health")(health)
    app.post("/regenerate")(regenerate)

    app.mount("/", mcp_app)

    return app


def _apply_tool_policy_or_exit():
    """Read and enforce the tool-exposure policy (ADR-0003 P1/P2).

    Runs once, here, after every `mcp.mount()` call above has already
    executed at import time — `import server` on its own never reaches this
    function, so tests/tooling that only need the full, unfiltered catalogue
    (T1/T2, `tests/contract/generate_tool_surface.py`) are unaffected.

    A malformed option (unknown namespace, non-boolean `read_only`) aborts
    startup with a readable message instead of silently applying a narrower
    — or wider — policy than the operator configured.
    """
    from policy import PolicyConfigError, ToolPolicy, apply_policy

    try:
        tool_policy = ToolPolicy.from_env()
    except PolicyConfigError as exc:
        print(f"Nexus refused to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    application = apply_policy(mcp, tool_policy)
    logging.getLogger(__name__).info(
        "Tool policy applied: read_only=%s disabled_namespaces=%s -> "
        "%d/%d tools visible, %d/%d prompts visible",
        tool_policy.read_only,
        sorted(tool_policy.disabled_namespaces),
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

        for line in _startup_log_lines_for_plan(listen_plan):
            print(line)

        # Redact ?token=... from uvicorn's access log before any request is logged.
        logging.getLogger("uvicorn.access").addFilter(RedactTokenFilter())

        app = _build_app()
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
        for line in _startup_log_lines(port, http_mode=False):
            print(line)
        mcp.run()


if __name__ == "__main__":
    main()
