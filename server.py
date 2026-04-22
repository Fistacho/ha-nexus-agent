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

mcp = FastMCP("ha-super-mcp")

mcp.mount("entities", entities_mcp)
mcp.mount("services", services_mcp)
mcp.mount("automations", automations_mcp)
mcp.mount("areas", areas_mcp)
mcp.mount("history", history_mcp)
mcp.mount("helpers", helpers_mcp)
mcp.mount("system", system_mcp)
mcp.mount("dashboards", dashboards_mcp)
mcp.mount("files", files_mcp)
mcp.mount("git", git_mcp)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
