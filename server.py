import logging
import os
import re
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

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
                token = query.get("token", [None])[0]
                if not token:
                    headers = dict(scope.get("headers", []))
                    auth = headers.get(b"authorization", b"").decode()
                    token = auth.removeprefix("Bearer ").strip()
                if token != API_KEY:
                    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
                    return
            await self.app(scope, receive, send)

    app = FastAPI(title="Nexus", docs_url=None, redoc_url=None, lifespan=mcp_app.lifespan)
    app.add_middleware(TokenAuthMiddleware)

    setup_ui.set_mcp(mcp)

    app.get("/", response_class=HTMLResponse)(setup_page)
    app.get("/health")(health)
    app.post("/regenerate")(regenerate)

    app.mount("/", mcp_app)

    return app


def main():
    port = int(os.getenv("NEXUS_PORT", "7123"))

    # HTTP mode: add-on or explicit NEXUS_HTTP=1
    if os.getenv("SUPERVISOR_TOKEN") or os.getenv("NEXUS_HTTP"):
        import uvicorn

        for line in _startup_log_lines(port, http_mode=True):
            print(line)

        # Redact ?token=... from uvicorn's access log before any request is logged.
        logging.getLogger("uvicorn.access").addFilter(RedactTokenFilter())

        app = _build_app()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        # stdio mode for Claude Desktop / local MCP client
        for line in _startup_log_lines(port, http_mode=False):
            print(line)
        mcp.run()


if __name__ == "__main__":
    main()
