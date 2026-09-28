r"""Regression guard: `run.sh` must honor `-e HA_URL`/`-e HA_CONFIG_PATH` in
standalone mode (no `/data/options.json`, i.e. no real Supervisor).

Context (ADR-0005 "Kanaly dystrybucji", condition (b)): the ghcr.io image this
add-on now also ships (see `.github/workflows/publish-image.yml`) is meant to run
two ways: as the Supervisor-managed add-on (`/data/options.json` present) and
standalone via `docker run -e HA_URL -e HA_TOKEN -p 7123:7123 <image>` (HA
Container / any plain Docker host, no Supervisor at all). Before this fix,
`run.sh` unconditionally set `HA_URL="http://supervisor/core"` and
`HA_CONFIG_PATH="/config"` *after* the add-on/standalone `if`/`else`, so a
standalone caller's own `-e HA_URL=...` was silently discarded and nexus always
tried (and failed) to reach a Supervisor that doesn't exist in that mode.

This test runs the real `run.sh` end-to-end with a fake `python3` on `PATH`
that just dumps its environment and exits, instead of parsing the script's
source with a regex — a regex could pass while the actual `sh`/`bash`
control-flow still misbehaves (e.g. a stray unconditional line after the
`fi`, exactly the bug this guards against).

Skipped (not xfail'd) when `bash` isn't on `PATH`: `run.sh`'s own `#!/usr/bin/env
sh` plus this test's fake-`python3`-on-`PATH` trick both need a POSIX shell,
which every CI runner (`ubuntu-latest`) and the Alpine container itself always
have; a Windows dev machine without Git Bash/WSL does not, and skipping there
is honest about that gap rather than silently vacuous.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="run.sh and this test's fake-python3 shim both require a POSIX shell (bash) on PATH",
)


def _run_with_fake_python3(tmp_path: Path, env_overrides: dict[str, str]) -> dict[str, str]:
    """Run `bash run.sh` with a fake `python3` first on PATH that dumps its
    environment to a file and exits 0 instead of `exec`ing the real server.

    Returns the dumped environment as a dict. `run.sh` ends in `exec python3
    server.py`, so the fake binary's dump captures exactly the environment
    run.sh itself constructed and exported.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dump_file = tmp_path / "env_dump.txt"

    fake_python3 = fake_bin / "python3"
    fake_python3.write_text(
        "#!/usr/bin/env bash\n"
        f'env > "{dump_file.as_posix()}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python3.chmod(fake_python3.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    # tests/conftest.py sets a session-wide HA_URL default (`os.environ.
    # setdefault`) for the *Python* test suite's own imports of ha_client.py
    # -- this subprocess must start from a clean slate instead, so each test
    # here controls HA_URL/HA_CONFIG_PATH explicitly via env_overrides.
    env.pop("HA_URL", None)
    env.pop("HA_CONFIG_PATH", None)
    env.update(env_overrides)
    # Fake python3 must win the PATH lookup; keep the rest of PATH so `bash`
    # and other coreutils run.sh doesn't even use are still resolvable.
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    # Guarantee `/data/options.json` (real Supervisor marker) does not exist
    # for this process, whatever the host machine happens to have at that
    # absolute path.
    env.pop("SUPERVISOR_TOKEN", None)

    result = subprocess.run(
        ["bash", str(RUN_SH)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"run.sh exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert dump_file.exists(), (
        "fake python3 never ran -- run.sh did not reach `exec python3 server.py`\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    dumped: dict[str, str] = {}
    for line in dump_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            dumped[key] = value
    return dumped


@pytest.mark.skipif(
    Path("/data/options.json").exists(),
    reason="this machine has a real /data/options.json -- would exercise add-on mode instead",
)
def test_standalone_honors_ha_url_env_override(tmp_path):
    env = _run_with_fake_python3(
        tmp_path,
        {"HA_URL": "http://my-real-ha.example:8123", "HA_TOKEN": "standalone-token"},
    )
    assert env.get("HA_URL") == "http://my-real-ha.example:8123", (
        "run.sh must not override a standalone caller's own -e HA_URL "
        f"(got {env.get('HA_URL')!r})"
    )


@pytest.mark.skipif(
    Path("/data/options.json").exists(),
    reason="this machine has a real /data/options.json -- would exercise add-on mode instead",
)
def test_standalone_honors_ha_config_path_env_override(tmp_path):
    env = _run_with_fake_python3(tmp_path, {"HA_CONFIG_PATH": "/custom/config"})
    assert env.get("HA_CONFIG_PATH") == "/custom/config"


@pytest.mark.skipif(
    Path("/data/options.json").exists(),
    reason="this machine has a real /data/options.json -- would exercise add-on mode instead",
)
def test_standalone_falls_back_to_ha_client_default_when_ha_url_unset(tmp_path):
    """No -e HA_URL at all: run.sh must fall back to the exact same default
    ha_client.py's own `os.getenv("HA_URL", "http://homeassistant.local:8123")`
    uses, so standalone behaviour is identical whether run.sh or ha_client.py
    resolves the default."""
    env = _run_with_fake_python3(tmp_path, {})
    assert env.get("HA_URL") == "http://homeassistant.local:8123"


def test_addon_branch_still_forces_supervisor_url_and_config_path():
    """Static guard for the add-on/Supervisor branch (`if [ -f "$OPTIONS" ]`):
    it must keep unconditionally forcing HA_URL/HA_CONFIG_PATH regardless of
    any env passed in -- a real Supervisor add-on has no legitimate reason to
    point at anything else, and every release through 0.24.0 relied on this.

    Execution-testing this branch the same way as the standalone tests above
    would need a real file at the absolute path `/data/options.json`, which
    is unsafe to fake on a shared/dev machine; the CI `build-smoke` job (see
    `.github/workflows/ci.yml`) and `tests/test_addon_network.py` already
    exercise the add-on-shaped container path end-to-end. This regex check
    only guards that the two forced-value lines stay inside the `if` branch,
    immediately after the `jq`-based option reads and before the matching
    `else` -- exactly where the standalone fix (above) moved them from their
    previous, buggy, unconditional-after-`fi` position.
    """
    text = RUN_SH.read_text(encoding="utf-8")

    if_branch_match = re.search(
        r'if \[ -f "\$OPTIONS" \]; then(.*?)\nelse\n', text, re.DOTALL
    )
    assert if_branch_match, "run.sh's if/else structure around $OPTIONS changed shape"
    if_branch = if_branch_match.group(1)

    assert 'HA_URL="http://supervisor/core"' in if_branch, (
        "add-on branch must still force HA_URL=http://supervisor/core"
    )
    assert 'HA_CONFIG_PATH="/config"' in if_branch, (
        "add-on branch must still force HA_CONFIG_PATH=/config"
    )

    after_fi = text.split("\nfi\n", 1)[1] if "\nfi\n" in text else ""
    assert 'HA_URL="http://supervisor/core"' not in after_fi, (
        "HA_URL=http://supervisor/core must not be forced unconditionally after the "
        "if/else again -- that is the exact standalone-mode regression this test suite "
        "guards against (see test_standalone_honors_ha_url_env_override)"
    )
