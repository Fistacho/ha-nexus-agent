import os
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


def _build_app():
    """Combine MCP + setup UI into one ASGI app for HTTP mode."""
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware
    from setup_ui import setup_page, health
    from auth import API_KEY

    class TokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path.startswith("/mcp"):
                token = (
                    request.query_params.get("token")
                    or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                )
                if token != API_KEY:
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

    app = FastAPI(title="Nexus", docs_url=None, redoc_url=None)
    app.add_middleware(TokenAuthMiddleware)

    app.get("/", response_class=HTMLResponse)(setup_page)
    app.get("/health")(health)

    mcp_app = mcp.http_app(path="/mcp")
    app.mount("/", mcp_app)

    return app


def main():
    from auth import API_KEY
    port = int(os.getenv("NEXUS_PORT", "7123"))

    # HTTP mode: add-on or explicit NEXUS_HTTP=1
    if os.getenv("SUPERVISOR_TOKEN") or os.getenv("NEXUS_HTTP"):
        import uvicorn
        print(f"Nexus starting (HTTP) on port {port}")
        print(f"Setup UI  → http://localhost:{port}")
        print(f"MCP       → http://localhost:{port}/mcp")
        print(f"API key   → {API_KEY}")
        app = _build_app()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        # stdio mode for Claude Desktop / local MCP client
        print(f"Nexus starting (stdio)")
        print(f"API key → {API_KEY}")
        mcp.run()


if __name__ == "__main__":
    main()
