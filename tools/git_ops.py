import os
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("git_ops")

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))


def _repo():
    """Get or initialize git repo for HA config directory."""
    try:
        import git
    except ImportError:
        raise RuntimeError("gitpython not installed. Run: pip install gitpython")

    try:
        return git.Repo(_CONFIG_PATH)
    except git.exc.InvalidGitRepositoryError:
        raise RuntimeError(
            f"No git repo at {_CONFIG_PATH}. Run git_init_config() first."
        )


@mcp.tool()
def git_init_config() -> dict:
    """Initialize git repository in HA config directory. Run once before using other git tools."""
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


@mcp.tool()
def git_status() -> dict:
    """Show current git status of HA config directory."""
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


@mcp.tool()
def git_commit_all(message: str) -> dict:
    """Stage all changes and commit them with a message. Use before making risky changes as a checkpoint."""
    repo = _repo()
    repo.git.add(A=True)
    if not repo.is_dirty(untracked_files=True):
        return {"status": "nothing_to_commit"}
    commit = repo.index.commit(message)
    return {"status": "committed", "sha": commit.hexsha[:8], "message": message}


@mcp.tool()
def git_log(limit: int = 20) -> list[dict]:
    """Show recent git commits in the HA config directory."""
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


@mcp.tool()
def git_diff(sha: str | None = None) -> str:
    """Show diff of uncommitted changes, or diff of a specific commit (by SHA)."""
    repo = _repo()
    if sha:
        commit = repo.commit(sha)
        return repo.git.show(sha, stat=True)
    return repo.git.diff()


@mcp.tool()
def git_rollback_file(relative_path: str, sha: str = "HEAD") -> dict:
    """Restore a single file to its state at a specific commit (default: last commit = undo changes)."""
    repo = _repo()
    repo.git.checkout(sha, "--", relative_path)
    return {"status": "restored", "file": relative_path, "to": sha}


@mcp.tool()
def git_rollback_to_commit(sha: str) -> dict:
    """Hard-reset config to a previous commit. WARNING: all changes after that commit are lost."""
    repo = _repo()
    repo.git.reset("--hard", sha)
    return {"status": "reset", "to": sha}


@mcp.tool()
def git_create_branch(branch_name: str) -> dict:
    """Create a new branch (useful before experimental changes)."""
    repo = _repo()
    branch = repo.create_head(branch_name)
    branch.checkout()
    return {"status": "created_and_checked_out", "branch": branch_name}


@mcp.tool()
def git_checkout_branch(branch_name: str) -> dict:
    """Switch to an existing branch."""
    repo = _repo()
    repo.git.checkout(branch_name)
    return {"status": "switched", "branch": branch_name}


@mcp.tool()
def git_list_branches() -> list[str]:
    """List all local branches in HA config repo."""
    repo = _repo()
    return [b.name for b in repo.branches]


@mcp.tool()
def safe_write_with_checkpoint(relative_path: str, content: str, commit_message: str | None = None) -> dict:
    """Write a config file AND automatically git-commit the current state before writing.
    This is the safest way to modify config files — you always have a rollback point.
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
