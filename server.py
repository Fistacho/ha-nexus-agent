import threading
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


def main():
    from auth import API_KEY
    import os
    port = int(os.getenv("NEXUS_PORT", "7123"))
    print(f"Nexus MCP server starting…")
    print(f"Setup UI → http://localhost:{port}")
    print(f"API key  → {API_KEY}")

    from setup_ui import start_ui
    ui_thread = threading.Thread(target=start_ui, daemon=True)
    ui_thread.start()

    mcp.run()


if __name__ == "__main__":
    main()
