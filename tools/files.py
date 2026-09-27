from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import yaml
from fastmcp import FastMCP
from dotenv import load_dotenv
from pydantic import Field

from tools._contract import destructive, read, write

load_dotenv()

mcp = FastMCP("files")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


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


@mcp.tool(annotations=read("Read a config file"))
def read_config_file(
    relative_path: Annotated[
        str,
        Field(
            description=(
                "Path to the file relative to the HA config directory "
                "(HA_CONFIG_PATH), e.g. 'automations.yaml' or "
                "'packages/lights.yaml'. Must resolve inside that directory, "
                "use an allowed extension (.yaml/.yml/.json/.txt), and not "
                "touch secrets.yaml or .storage/."
            )
        ),
    ],
) -> str:
    """Read one config file's raw text from the HA config directory.

    Resolves `relative_path` under HA_CONFIG_PATH via `_safe_path` (blocking
    paths outside the directory, disallowed extensions, and secrets.yaml/
    .storage) and returns its content as a UTF-8 string; nothing is parsed
    or validated.

    Use when: reading the current text of a YAML/JSON/txt file before
    editing it.
    Not for: checking whether that text is valid YAML — pass it to
    `files_validate_yaml_content`; discovering which files exist first —
    use `files_list_config_files`.
    Returns: str, the file's raw text (UTF-8).
    Errors: raises `FileNotFoundError` when the file does not exist, and
    raises `PermissionError` (from `_safe_path`) when `relative_path`
    resolves outside the config directory, uses a disallowed extension, or
    targets secrets.yaml/.storage.
    """
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return path.read_text(encoding="utf-8")


@mcp.tool(annotations=destructive("Overwrite a config file", idempotent=True))
def write_config_file(
    relative_path: Annotated[
        str,
        Field(
            description=(
                "Path to the file relative to the HA config directory "
                "(HA_CONFIG_PATH), e.g. 'automations.yaml' or "
                "'packages/lights.yaml'. Parent directories are created if "
                "missing. Must use an allowed extension (.yaml/.yml/.json/"
                ".txt) and not touch secrets.yaml or .storage/."
            )
        ),
    ],
    content: Annotated[
        str,
        Field(
            description="Full text to write as the file's new content, replacing whatever was there before.",
        ),
    ],
    validate_yaml: Annotated[
        bool,
        Field(
            description=(
                "If True (default) and the extension is .yaml/.yml, `content` "
                "is parsed with a HA-tolerant YAML loader before writing; a "
                "parse error aborts the write. Set False to skip that check "
                "for non-YAML or intentionally unusual content."
            )
        ),
    ] = True,
) -> dict:
    """Overwrite or create one config file under /config with new content.

    Resolves `relative_path` via `_safe_path`, YAML-checks `content` first
    unless `validate_yaml=False` or the extension isn't .yaml/.yml, then
    creates parent directories as needed and writes the file, replacing any
    previous content at that path.

    Use when: no rollback point for the previous content is needed.
    Not for: keeping a rollback point — use `git_safe_write_with_checkpoint`,
    which commits the pre-write state first; ESPHome device YAML — use
    `esphome_write_config`.
    Returns: dict `{"success": True, "path": ...}` on success, or
    `{"success": False, "error": ...}` when `validate_yaml` catches invalid
    YAML before writing.
    Errors: raises `PermissionError` (from `_safe_path`) for a path outside
    the config directory, a disallowed extension, or secrets.yaml/.storage;
    returns `{"success": False, "error": ...}` instead of raising for
    invalid YAML.
    Limits: no backup is taken — the previous content is permanently lost.
    Does not reload Home Assistant.
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


@mcp.tool(annotations=read("List config files"))
def list_config_files(
    subdirectory: Annotated[
        str,
        Field(
            description=(
                "Subdirectory relative to the HA config root to list, e.g. "
                "'packages'. Omit (default '') to list from the config root."
            )
        ),
    ] = "",
) -> list[str]:
    """Recursively list config files with allowed extensions under /config.

    Resolves `subdirectory` (default the config root) under HA_CONFIG_PATH,
    recurses with `Path.rglob('*')`, and returns config-root-relative paths
    for files whose extension is in .yaml/.yml/.json/.txt, skipping
    secrets.yaml and .storage/.

    Use when: discovering which config files exist before reading or
    writing one.
    Not for: reading a file's content once you have its path — use
    `files_read_config_file`.
    Returns: list[str] of config-root-relative file paths.
    Limits: output can be large for the /config root; a `subdirectory` that
    resolves (after following symlinks) outside /config returns an empty
    list instead of listing files outside the config directory.
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


@mcp.tool(annotations=read("Validate YAML syntax"))
def validate_yaml_content(
    content: Annotated[
        str,
        Field(description="YAML text to check for syntax errors; nothing is written or read from disk."),
    ],
) -> dict:
    """Check YAML syntax for a string without writing anything.

    Parses `content` with a Home-Assistant-tolerant YAML loader that
    accepts !include*/!secret/!env_var/!input as opaque values, and reports
    only whether parsing succeeded and the top-level type.

    Use when: checking a YAML snippet's syntax before calling
    `files_write_config_file` or `automations_set_automation_config`.
    Not for: full HA schema validation — use `system_check_config`; HA tags
    here are accepted but not resolved, so only syntax is checked, not HA
    schema validity.
    Returns: dict `{"valid": True, "type": ...}` (top-level type name only,
    not the parsed content) or `{"valid": False, "error": ...}`.
    Errors: returns `{"valid": False, "error": str(e)}` for a YAML parse
    error rather than raising.
    """
    try:
        parsed = _ha_yaml_load(content)
        return {"valid": True, "type": type(parsed).__name__}
    except yaml.YAMLError as e:
        return {"valid": False, "error": str(e)}


@mcp.tool(annotations=destructive("Delete a config file", idempotent=True))
def delete_config_file(
    relative_path: Annotated[
        str,
        Field(
            description=(
                "Path to the file relative to the HA config directory "
                "(HA_CONFIG_PATH) to delete, e.g. 'packages/old.yaml'. Must "
                "use an allowed extension (.yaml/.yml/.json/.txt) and not "
                "touch secrets.yaml or .storage/."
            )
        ),
    ],
) -> dict:
    """Permanently delete one config file under /config.

    Resolves `relative_path` via `_safe_path` and calls `Path.unlink()`;
    there is no confirmation prompt, backup or trash in this tool —
    deletion is immediate and irreversible from here alone.

    Use when: removing a YAML/JSON/txt file that HA no longer needs.
    Not for: keeping the ability to undo — commit the config to git first
    (`git_git_commit_all`) or write through
    `git_safe_write_with_checkpoint` so a rollback stays possible.
    Returns: dict `{"success": True, "deleted": ...}`.
    Errors: raises `FileNotFoundError` when the file does not exist, and
    raises `PermissionError` (from `_safe_path`) for a path outside the
    config directory, a disallowed extension, or secrets.yaml/.storage.
    Limits: no `confirm` parameter guards this call, unlike most other
    destructive tools in this add-on; the deletion runs immediately.
    """
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    path.unlink()
    return {"success": True, "deleted": str(path)}


@mcp.tool(annotations=write("Append to a config file", idempotent=False))
def append_to_config_file(
    relative_path: Annotated[
        str,
        Field(
            description=(
                "Path to an existing file relative to the HA config "
                "directory (HA_CONFIG_PATH), e.g. 'automations.yaml'. Must "
                "use an allowed extension (.yaml/.yml/.json/.txt) and not "
                "touch secrets.yaml or .storage/."
            )
        ),
    ],
    content: Annotated[
        str,
        Field(description="Text appended after a newline to the file's existing content."),
    ],
    validate_yaml: Annotated[
        bool,
        Field(
            description=(
                "If True and the extension is .yaml/.yml, the combined text "
                "(existing content plus the appended `content`) is YAML-"
                "checked before writing. Defaults to False, so an invalid "
                "append is written without warning unless set True."
            )
        ),
    ] = False,
) -> dict:
    """Append text to the end of an existing config file.

    Resolves `relative_path` via `_safe_path`, reads its current text,
    appends `content` after a newline, YAML-checks the combined text only
    when `validate_yaml=True` and the extension is .yaml/.yml, then writes
    the combined text back.

    Use when: adding raw YAML/JSON/txt lines that already match the file's
    existing list indentation.
    Not for: automations — use `automations_set_automation_config`, which
    edits by config id instead of a raw text append.
    Returns: dict `{"success": True, "path": ...}` on success, or
    `{"success": False, "error": ...}` when `validate_yaml=True` and the
    combined text fails to parse.
    Errors: raises `FileNotFoundError` when the file does not exist, and
    raises `PermissionError` (from `_safe_path`) for a path outside the
    config directory, a disallowed extension, or secrets.yaml/.storage.
    Limits: `validate_yaml` defaults to False, so an invalid append is
    written without warning unless explicitly checked.
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
