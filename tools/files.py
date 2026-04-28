import os
import yaml
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("files")

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))

_ALLOWED_EXTENSIONS = {".yaml", ".yml", ".json", ".txt"}
_BLOCKED_PATHS = {"secrets.yaml", ".storage"}


def _safe_path(relative_path: str) -> Path:
    """Resolve path safely within HA config directory."""
    path = (_CONFIG_PATH / relative_path).resolve()
    if not str(path).startswith(str(_CONFIG_PATH.resolve())):
        raise PermissionError(f"Path outside config directory: {path}")
    if path.suffix not in _ALLOWED_EXTENSIONS:
        raise PermissionError(f"File extension not allowed: {path.suffix}")
    for blocked in _BLOCKED_PATHS:
        if blocked in path.parts:
            raise PermissionError(f"Access to '{blocked}' is blocked")
    return path


@mcp.tool()
def read_config_file(relative_path: str) -> str:
    """Read a config file relative to HA config dir. E.g. 'automations.yaml' or 'packages/lights.yaml'."""
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return path.read_text(encoding="utf-8")


@mcp.tool()
def write_config_file(relative_path: str, content: str, validate_yaml: bool = True) -> dict:
    """Write content to a config file. Validates YAML syntax before saving (set validate_yaml=False to skip).
    Creates parent directories as needed.
    """
    path = _safe_path(relative_path)

    if validate_yaml and path.suffix in {".yaml", ".yml"}:
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"YAML validation failed: {e}"}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"success": True, "path": str(path)}


@mcp.tool()
def list_config_files(subdirectory: str = "") -> list[str]:
    """List files in the HA config directory (or a subdirectory)."""
    base = _CONFIG_PATH / subdirectory if subdirectory else _CONFIG_PATH
    base = base.resolve()
    if not base.exists():
        return []
    return [
        str(f.relative_to(_CONFIG_PATH))
        for f in base.rglob("*")
        if f.is_file() and f.suffix in _ALLOWED_EXTENSIONS
        and not any(b in f.parts for b in _BLOCKED_PATHS)
    ]


@mcp.tool()
def validate_yaml_content(content: str) -> dict:
    """Validate YAML content without saving. Returns parsed result or error."""
    try:
        parsed = yaml.safe_load(content)
        return {"valid": True, "type": type(parsed).__name__}
    except yaml.YAMLError as e:
        return {"valid": False, "error": str(e)}


@mcp.tool()
def delete_config_file(relative_path: str) -> dict:
    """Delete a config file. Will NOT delete if it has no known safe extension."""
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    path.unlink()
    return {"success": True, "deleted": str(path)}


@mcp.tool()
def append_to_config_file(relative_path: str, content: str, validate_yaml: bool = False) -> dict:
    """Append content to an existing config file (e.g. adding an automation entry to automations.yaml)."""
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    existing = path.read_text(encoding="utf-8")
    combined = existing + "\n" + content

    if validate_yaml and path.suffix in {".yaml", ".yml"}:
        try:
            yaml.safe_load(combined)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"YAML validation failed after append: {e}"}

    path.write_text(combined, encoding="utf-8")
    return {"success": True, "path": str(path)}
