import os
import yaml
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("files")


class _HALoader(yaml.SafeLoader):
    """SafeLoader that tolerates Home Assistant's custom YAML tags.

    HA configs routinely contain `!include`, `!include_dir_merge_named`,
    `!secret`, `!env_var`, etc. The stock SafeLoader rejects them, so any
    validation of a real HA file fails. We can't resolve them (we don't have
    file/secret access from here), but for syntax checking it's enough to keep
    them as opaque tagged values.
    """


def _ha_tag_passthrough(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return {"__ha_tag__": tag_suffix, "value": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {"__ha_tag__": tag_suffix, "value": loader.construct_sequence(node, deep=True)}
    if isinstance(node, yaml.MappingNode):
        return {"__ha_tag__": tag_suffix, "value": loader.construct_mapping(node, deep=True)}
    return None


for _tag in (
    "!include",
    "!include_dir_list",
    "!include_dir_merge_list",
    "!include_dir_named",
    "!include_dir_merge_named",
    "!secret",
    "!env_var",
    "!input",
):
    _HALoader.add_constructor(_tag, lambda loader, node, t=_tag: _ha_tag_passthrough(loader, t, node))


def _ha_yaml_load(content: str):
    return yaml.load(content, Loader=_HALoader)

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))

_ALLOWED_EXTENSIONS = {".yaml", ".yml", ".json", ".txt"}
_BLOCKED_PATHS = {"secrets.yaml", ".storage"}


def _safe_path(relative_path: str) -> Path:
    """Resolve path safely within HA config directory."""
    config_root = _CONFIG_PATH.resolve()
    path = (_CONFIG_PATH / relative_path).resolve()
    if not path.is_relative_to(config_root):
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
    """Overwrite (or create, with parent directories) a file under /config with `content`. Only
    .yaml/.yml/.json/.txt; secrets.yaml and .storage/ are refused. YAML is syntax-checked unless
    validate_yaml=False (HA tags accepted). No backup — the previous content is lost (use
    git_safe_write_with_checkpoint to keep one). Does not reload HA.
    """
    path = _safe_path(relative_path)

    if validate_yaml and path.suffix in {".yaml", ".yml"}:
        try:
            _ha_yaml_load(content)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"YAML validation failed: {e}"}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"success": True, "path": str(path)}


@mcp.tool()
def list_config_files(subdirectory: str = "") -> list[str]:
    """Recursively list .yaml/.yml/.json/.txt files under /config or `subdirectory` (paths relative to
    /config; secrets.yaml and .storage/ excluded). Output can be large for the /config root.
    A `subdirectory` that resolves (after following symlinks) outside /config
    (e.g. '..', an absolute path, or a symlink pointing out) returns [] instead
    of listing files outside the config directory.
    """
    config_root = _CONFIG_PATH.resolve()
    base = _CONFIG_PATH / subdirectory if subdirectory else _CONFIG_PATH
    base = base.resolve()
    if not base.is_relative_to(config_root):
        return []
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
    """Check YAML syntax without writing anything. Returns {valid: true, type: 'dict'|'list'|…} (top-
    level type only, not the parsed content) or {valid: false, error}. HA tags (!include*, !secret,
    !env_var, !input) are accepted but not resolved, so this checks syntax only, not HA schema
    validity (use system_check_config for that).
    """
    try:
        parsed = _ha_yaml_load(content)
        return {"valid": True, "type": type(parsed).__name__}
    except yaml.YAMLError as e:
        return {"valid": False, "error": str(e)}


@mcp.tool()
def delete_config_file(relative_path: str) -> dict:
    """Permanently delete a .yaml/.yml/.json/.txt file under /config (secrets.yaml and .storage/ are
    refused). No confirmation, backup or undo.
    """
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    path.unlink()
    return {"success": True, "deleted": str(path)}


@mcp.tool()
def append_to_config_file(relative_path: str, content: str, validate_yaml: bool = False) -> dict:
    """Append `content` after a newline to an existing file under /config. YAML is NOT checked unless
    validate_yaml=True. For automations prefer automations_set_automation_config; raw appends must
    match the file's list indentation.
    """
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    existing = path.read_text(encoding="utf-8")
    combined = existing + "\n" + content

    if validate_yaml and path.suffix in {".yaml", ".yml"}:
        try:
            _ha_yaml_load(combined)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"YAML validation failed after append: {e}"}

    path.write_text(combined, encoding="utf-8")
    return {"success": True, "path": str(path)}
