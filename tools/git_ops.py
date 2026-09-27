from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from dotenv import load_dotenv
from pydantic import Field

from tools._contract import destructive, read, write

load_dotenv()

mcp = FastMCP("git_ops")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))


def _resolve_within_config(relative_path: str) -> tuple[Path | None, dict | None]:
    """Guard a path handed to a raw `git` command against escaping /config.

    Defense in depth: `tools.files._safe_path` already blocks this for direct
    file reads/writes, but git tools build pathspecs from user input too
    (e.g. `git checkout <sha> -- <relative_path>`), so the same boundary
    check is applied here before the path reaches `repo.git.*`.
    Returns `(resolved_path, None)` on success or `(None, error_dict)`.
    """
    config_root = _CONFIG_PATH.resolve()
    try:
        candidate = (_CONFIG_PATH / relative_path).resolve()
    except (OSError, RuntimeError) as err:
        return None, {"error": "invalid_path", "relative_path": relative_path, "detail": str(err)}
    if not candidate.is_relative_to(config_root):
        return None, {
            "error": "path_outside_config",
            "relative_path": relative_path,
            "detail": f"'{relative_path}' resolves outside {config_root}",
        }
    return candidate, None


def _repo():
    """Get or initialize git repo for HA config directory."""
    try:
        import git
    except ImportError as err:
        raise RuntimeError("gitpython not installed. Run: pip install gitpython") from err

    try:
        return git.Repo(_CONFIG_PATH)
    except git.exc.InvalidGitRepositoryError as err:
        raise RuntimeError(
            f"No git repo at {_CONFIG_PATH}. Run git_init_config() first."
        ) from err


@mcp.tool(annotations=write("Initialize the config git repository", idempotent=True))
def git_init_config() -> dict:
    """Initialize a git repository in the HA config directory.

    Creates a git repo at HA_CONFIG_PATH via `git.Repo.init` unless a .git/
    directory already exists there, writes a default .gitignore
    (secrets.yaml, .storage/, *.db*, home-assistant.log) when none exists,
    and makes one initial commit adding that .gitignore.

    Use when: preparing /config before using any other `git_*` tool or
    `git_safe_write_with_checkpoint` for the first time.
    Returns: dict `{"status": "initialized"|"already_initialized", "path": ...}`.
    """
    import git
    if (_CONFIG_PATH / ".git").exists():
        return {"status": "already_initialized", "path": str(_CONFIG_PATH)}
    repo = git.Repo.init(_CONFIG_PATH)
    gitignore = _CONFIG_PATH / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "secrets.yaml\n.storage/\n*.db\n*.db-shm\n*.db-wal\nhome-assistant.log\n"
        )
    repo.index.add([".gitignore"])
    repo.index.commit("chore: init HA config repository")
    return {"status": "initialized", "path": str(_CONFIG_PATH)}


@mcp.tool(annotations=read("Show config git status"))
def git_status() -> dict:
    """Show git status of the HA config directory.

    Calls `_repo()` for the /config git repo and returns the active branch,
    staged/modified/untracked file lists (`repo.index.diff`,
    `repo.untracked_files`) and an overall dirty flag.

    Use when: checking what changed in /config before committing or
    writing a file.
    Returns: dict `{"branch", "staged", "modified", "untracked", "is_dirty"}`.
    Errors: raises `RuntimeError` (from `_repo()`) when gitpython is not
    installed or no git repo exists yet at HA_CONFIG_PATH — run
    `git_git_init_config` first.
    """
    repo = _repo()
    changed = [item.a_path for item in repo.index.diff(None)]
    untracked = repo.untracked_files
    staged = [item.a_path for item in repo.index.diff("HEAD")] if repo.head.is_valid() else []
    return {
        "branch": repo.active_branch.name if not repo.head.is_detached else "detached",
        "staged": staged,
        "modified": changed,
        "untracked": untracked,
        "is_dirty": repo.is_dirty(untracked_files=True),
    }


@mcp.tool(annotations=write("Commit all config changes", idempotent=False))
def git_commit_all(
    message: Annotated[
        str,
        Field(description="Commit message describing this checkpoint or change."),
    ],
) -> dict:
    """Stage and commit every change in the HA config directory.

    Calls `_repo()`, stages everything with `git add -A`, and commits
    `message` unless nothing is dirty, in which case no commit is made.

    Use when: creating a checkpoint before a risky change to /config.
    Not for: writing one file and checkpointing it in a single call — use
    `git_safe_write_with_checkpoint`.
    Returns: dict `{"status": "committed", "sha", "message"}`, or
    `{"status": "nothing_to_commit"}` when the working tree is clean.
    Errors: raises `RuntimeError` (from `_repo()`) when gitpython is not
    installed or no git repo exists yet — run `git_git_init_config` first.
    """
    repo = _repo()
    repo.git.add(A=True)
    if not repo.is_dirty(untracked_files=True):
        return {"status": "nothing_to_commit"}
    commit = repo.index.commit(message)
    return {"status": "committed", "sha": commit.hexsha[:8], "message": message}


@mcp.tool(annotations=read("List config git commits"))
def git_log(
    limit: Annotated[
        int,
        Field(description="Maximum number of commits to return, most recent first. Defaults to 20."),
    ] = 20,
) -> list[dict]:
    """List recent commits in the HA config git repository.

    Calls `_repo()` and returns up to `limit` commits from
    `repo.iter_commits`, most recent first, with short SHA, message, author
    name and ISO commit date.

    Use when: reviewing recent config changes before a rollback or diff.
    Returns: list[dict] `{"sha", "message", "author", "date"}`.
    Errors: raises `RuntimeError` (from `_repo()`) when gitpython is not
    installed or no git repo exists yet — run `git_git_init_config` first.
    Limits: `limit` caps the number of commits returned; no further
    pagination is offered.
    """
    repo = _repo()
    return [
        {
            "sha": c.hexsha[:8],
            "message": c.message.strip(),
            "author": c.author.name,
            "date": c.committed_datetime.isoformat(),
        }
        for c in repo.iter_commits(max_count=limit)
    ]


@mcp.tool(annotations=read("Show config git diff"))
def git_diff(
    sha: Annotated[
        str | None,
        Field(
            description=(
                "Commit SHA or ref to show a stat summary for, e.g. "
                "'a1b2c3d'. Omit to show the unstaged diff instead."
            )
        ),
    ] = None,
) -> str:
    """Show unstaged changes, or one commit's stat summary.

    Without `sha`, calls `repo.git.diff()` for the unified diff of unstaged
    changes to tracked files in /config, excluding staged changes and
    untracked new files (see `git_git_status`). With `sha`, calls `git show
    --stat <sha>` for the commit header and per-file changed-line counts,
    not the actual patch.

    Use when: reviewing what changed before committing, or summarising one
    past commit.
    Returns: str, the raw diff or stat text.
    Errors: raises `RuntimeError` (from `_repo()`) when no git repo exists;
    raises an exception from gitpython when `sha` does not resolve to a
    commit.
    Limits: output is raw, unformatted text and can be large for big diffs.
    """
    repo = _repo()
    if sha:
        repo.commit(sha)  # validates sha resolves to a commit before the git-show call
        return repo.git.show(sha, stat=True)
    return repo.git.diff()


@mcp.tool(annotations=destructive("Restore a config file from git history", idempotent=True))
def git_rollback_file(
    relative_path: Annotated[
        str,
        Field(
            description=(
                "Path to the file relative to the HA config directory to "
                "restore, e.g. 'automations.yaml'. Must resolve inside "
                "/config after following symlinks."
            )
        ),
    ],
    sha: Annotated[
        str,
        Field(
            description=(
                "Commit SHA or ref to restore the file from. Defaults to "
                "'HEAD', which discards uncommitted changes to this file."
            )
        ),
    ] = "HEAD",
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Must be True to actually restore the file. False (default) "
                "returns a safety prompt describing what would happen "
                "instead of touching anything."
            )
        ),
    ] = False,
) -> dict:
    """Restore one file to its state at a specific commit.

    Refuses a `relative_path` that resolves outside /config
    (`_resolve_within_config`) before running anything, and without
    `confirm=True` returns a safety prompt instead of touching the file;
    with `confirm=True` it calls `repo.git.checkout(sha, '--',
    relative_path)`, replacing the file's on-disk content with the version
    from `sha` and losing any uncommitted changes to that file.

    Use when: undoing an accidental edit to a single file.
    Not for: resetting the whole config tree — use
    `git_git_rollback_to_commit`.
    Returns: dict `{"status": "restored", "file", "to"}` on success.
    Errors: returns `{"error": "invalid_path"|"path_outside_config", ...}`
    when `relative_path` escapes /config; returns `{"error":
    "confirmation_required", ...}` when `confirm` is not True; raises
    `RuntimeError` (from `_repo()`) when no git repo exists.
    Limits: requires `confirm=True`; uncommitted changes to the target
    file are permanently lost once it runs.
    """
    _, path_err = _resolve_within_config(relative_path)
    if path_err:
        return path_err
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will restore '{relative_path}' to {sha}. Uncommitted changes to this file will be lost.",
            "action": f"git_rollback_file(relative_path='{relative_path}', sha='{sha}', confirm=True)",
        }
    repo = _repo()
    repo.git.checkout(sha, "--", relative_path)
    return {"status": "restored", "file": relative_path, "to": sha}


@mcp.tool(annotations=destructive("Hard-reset config to a commit", idempotent=True))
def git_rollback_to_commit(
    sha: Annotated[
        str,
        Field(description="Commit SHA or ref to hard-reset the entire config directory to."),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Must be True to actually perform the reset. False "
                "(default) returns a safety prompt naming the current HEAD "
                "instead of touching anything."
            )
        ),
    ] = False,
) -> dict:
    """Hard-reset the entire HA config directory to a previous commit.

    Without `confirm=True` returns a safety prompt naming the current head commit;
    with `confirm=True` calls `repo.git.reset('--hard', sha)`, which
    discards every commit and uncommitted change made after `sha`.

    Use when: reverting /config entirely after a bad batch of changes, and
    the target commit is already known.
    Not for: restoring a single file — use `git_git_rollback_file`.
    Returns: dict `{"status": "reset", "to": sha}` on success.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is not True; raises `RuntimeError` (from
    `_repo()`) when no git repo exists.
    Limits: requires `confirm=True`. WARNING: every commit and uncommitted
    change made after `sha` is permanently and irreversibly lost.
    """
    if not confirm:
        try:
            repo = _repo()
            current = next(iter(repo.iter_commits(max_count=1))).hexsha[:8]
        except Exception:
            current = "unknown"
        return {
            "error": "confirmation_required",
            "message": (
                f"This will hard-reset ALL config files to commit {sha}. "
                f"Current HEAD is {current}. Every change after {sha} will be PERMANENTLY LOST."
            ),
            "action": f"git_rollback_to_commit(sha='{sha}', confirm=True)",
        }
    repo = _repo()
    repo.git.reset("--hard", sha)
    return {"status": "reset", "to": sha}


@mcp.tool(annotations=write("Create and switch to a new git branch", idempotent=False))
def git_create_branch(
    branch_name: Annotated[
        str,
        Field(description="Name of the new git branch to create, e.g. 'automation-refactor'."),
    ],
) -> dict:
    """Create a new git branch from the current commit and switch to it.

    Calls `repo.create_head(branch_name)` then `branch.checkout()`;
    uncommitted changes carry over onto the new branch, and live /config
    files now follow it.

    Use when: starting an isolated set of changes without touching the
    current branch's commit history.
    Not for: switching to a branch that already exists — use
    `git_git_checkout_branch`.
    Returns: dict `{"status": "created_and_checked_out", "branch": ...}`.
    Errors: raises an exception from gitpython when a branch named
    `branch_name` already exists; raises `RuntimeError` (from `_repo()`)
    when no git repo exists.
    """
    repo = _repo()
    branch = repo.create_head(branch_name)
    branch.checkout()
    return {"status": "created_and_checked_out", "branch": branch_name}


@mcp.tool(annotations=destructive("Switch config to an existing branch", idempotent=True))
def git_checkout_branch(
    branch_name: Annotated[
        str,
        Field(description="Name of an existing git branch to switch the config repository to."),
    ],
) -> dict:
    """Switch the HA config git repository to an existing branch.

    Calls `repo.git.checkout(branch_name)`, which replaces every tracked
    file under /config with that branch's committed version — the live
    configuration Home Assistant reads from changes as soon as this
    returns.

    Use when: switching between prepared configurations that are tracked
    as branches.
    Not for: creating a branch that does not exist yet — use
    `git_git_create_branch`.
    Returns: dict `{"status": "switched", "branch": ...}`.
    Errors: raises an exception from gitpython when `branch_name` does not
    exist, or when uncommitted local changes would conflict with the
    target branch's files; raises `RuntimeError` (from `_repo()`) when no
    git repo exists.
    Limits: no `confirm` parameter; a conflicting uncommitted change blocks
    the checkout rather than being silently discarded (plain git
    behaviour), but a non-conflicting one carries over onto the new
    branch.
    """
    repo = _repo()
    repo.git.checkout(branch_name)
    return {"status": "switched", "branch": branch_name}


@mcp.tool(annotations=read("List config git branches"))
def git_list_branches() -> list[str]:
    """List all local branches in the HA config git repository.

    Calls `_repo()` and returns every local branch name from
    `repo.branches`.

    Returns: list[str] of local branch names.
    Errors: raises `RuntimeError` (from `_repo()`) when gitpython is not
    installed or no git repo exists yet — run `git_git_init_config` first.
    """
    repo = _repo()
    return [b.name for b in repo.branches]


@mcp.tool(annotations=write("Write a config file with a git checkpoint", idempotent=True))
def safe_write_with_checkpoint(
    relative_path: Annotated[
        str,
        Field(
            description=(
                "Path to the file relative to the HA config directory to "
                "write, e.g. 'automations.yaml'. Same extension and "
                "location limits as `files_write_config_file`."
            )
        ),
    ],
    content: Annotated[
        str,
        Field(description="Full text to write as the file's new content, replacing whatever was there before."),
    ],
    commit_message: Annotated[
        str | None,
        Field(
            description=(
                "Commit message for the new-content commit. Omit to use a "
                "default 'chore: update <relative_path>' message."
            )
        ),
    ] = None,
) -> dict:
    """Write one config file after committing a checkpoint of the prior state.

    Stages and commits every uncommitted change under /config as a
    checkpoint commit (only when the tree is dirty), then calls the same
    `write_config_file` logic `files_write_config_file` uses for
    `relative_path`/`content`, and finally stages and commits that change
    with `commit_message` (or a default message) if the write succeeded.

    Use when: overwriting a config file while keeping a git-based rollback
    point, unlike a plain overwrite.
    Not for: a write without a checkpoint — use
    `files_write_config_file`; ESPHome device YAML — use
    `esphome_write_config`.
    Returns: dict — the underlying write result (`{"success": True,
    "path": ...}` or `{"success": False, "error": ...}`), plus `git_sha`/
    `git_message` when the new-content commit was made.
    Errors: raises `RuntimeError` (from `_repo()`) when no git repo exists
    — run `git_git_init_config` first; returns `{"success": False,
    "error": ...}` unchanged when YAML validation fails, in which case the
    checkpoint commit of the prior dirty state (if any) still happened, but
    no new-content commit is made.
    Limits: does not reload Home Assistant; same file-extension and
    location limits as `files_write_config_file`.
    """
    from tools.files import write_config_file

    repo = _repo()

    # checkpoint current state
    repo.git.add(A=True)
    if repo.is_dirty(untracked_files=True):
        repo.index.commit(f"checkpoint: before modifying {relative_path}")

    result = write_config_file(relative_path, content)
    if not result.get("success"):
        return result

    # commit the new change
    msg = commit_message or f"chore: update {relative_path}"
    repo.git.add(A=True)
    if repo.is_dirty(untracked_files=True):
        commit = repo.index.commit(msg)
        result["git_sha"] = commit.hexsha[:8]
        result["git_message"] = msg

    return result
