"""Version consistency across pyproject.toml, config.yaml and CHANGELOG.md.

The release process (see repo root `.claude`/CLAUDE.md "Spojnosc wersji") requires the
same version string in all three places on every release: HACS/the Supervisor decide
whether there's an "update available" purely from `config.yaml`'s `version:`, while
`pyproject.toml`'s `version` is what the Python package itself reports. If they drift,
a user's HACS/Supervisor UI can show "no update available" even though the add-on's
code changed — CHANGELOG.md is the third leg because it's what Supervisor displays as
the update's release notes, so an unbumped heading there means the notes for the new
version don't exist yet.

TDD note: `check_release_consistency()` is exercised against the real repo (must find
zero problems) *and* against tmp_path copies with a single field deliberately
desynced (must report exactly that mismatch) — proving the checker can actually catch
this class of bug, not just that today's three numbers happen to agree.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _read_pyproject_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _read_config_yaml_version(root: Path) -> str:
    data = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    return str(data["version"])


def _read_changelog_version(root: Path) -> str:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^##\s+(\S+)", text, re.MULTILINE)
    if not match:
        raise AssertionError("CHANGELOG.md has no '## <version>' heading")
    return match.group(1)


def check_host_network_enabled(root: Path) -> list[str]:
    """`config.yaml`'s `host_network: true` is required by ADR-0004 D1 — nexus
    reaches the ESPHome Device Builder add-on's trusted-peer site ingress at
    `http://127.0.0.1:<ingress_port>` the same way HA Core itself does, which
    only works if nexus's own container shares the host's network namespace.
    Losing this flag (e.g. an unreviewed revert while chasing an unrelated
    Network-tab/`ports:` regression — see `addon_network.py`'s module
    docstring for the `GET /addons/self/info` reconstruction that flag makes
    necessary) silently breaks every `supervisor_ingress`-mode
    `DashboardClient` call with `esphome_unreachable`, with no signal until
    someone actually exercises an ESPHome tool against a real HA install."""
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    if config.get("host_network") is not True:
        return [
            f"config.yaml host_network is {config.get('host_network')!r}, expected True "
            "(ADR-0004 D1 — required for the ESPHome Device Builder ingress connection)"
        ]
    return []


def check_release_consistency(root: Path) -> list[str]:
    """Return human-readable mismatch descriptions; an empty list means consistent."""
    pyproject_version = _read_pyproject_version(root)
    config_version = _read_config_yaml_version(root)
    changelog_version = _read_changelog_version(root)

    problems = []
    if pyproject_version != config_version:
        problems.append(
            f"pyproject.toml version {pyproject_version!r} != "
            f"config.yaml version {config_version!r}"
        )
    if pyproject_version != changelog_version:
        problems.append(
            f"pyproject.toml version {pyproject_version!r} != "
            f"CHANGELOG.md first heading {changelog_version!r}"
        )
    return problems


def _copy_release_files(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "config.yaml", "CHANGELOG.md"):
        (tmp_path / name).write_text(
            (REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


# --- Green: the real repo today ------------------------------------------------

def test_repo_versions_are_consistent():
    problems = check_release_consistency(REPO_ROOT)
    assert not problems, "; ".join(problems)


def test_host_network_is_enabled():
    problems = check_host_network_enabled(REPO_ROOT)
    assert not problems, "; ".join(problems)


# --- Red-then-green proof: the checker actually detects drift ------------------

def test_detects_config_yaml_drift(tmp_path):
    _copy_release_files(tmp_path)

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    config["version"] = "0.0.0-drifted"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_release_consistency(tmp_path)
    assert any("config.yaml" in p for p in problems), problems


def test_detects_changelog_drift(tmp_path):
    _copy_release_files(tmp_path)

    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    drifted = re.sub(r"^## \S+", "## 0.0.0-drifted", changelog, count=1, flags=re.MULTILINE)
    assert drifted != changelog, "fixture didn't actually change the CHANGELOG heading"
    (tmp_path / "CHANGELOG.md").write_text(drifted, encoding="utf-8")

    problems = check_release_consistency(tmp_path)
    assert any("CHANGELOG" in p for p in problems), problems


def test_detects_pyproject_drift(tmp_path):
    _copy_release_files(tmp_path)

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    drifted = re.sub(
        r'^version = "[^"]+"', 'version = "0.0.0-drifted"', pyproject, count=1, flags=re.MULTILINE
    )
    assert drifted != pyproject, "fixture didn't actually change the pyproject version"
    (tmp_path / "pyproject.toml").write_text(drifted, encoding="utf-8")

    problems = check_release_consistency(tmp_path)
    assert len(problems) == 2, problems  # drifted from both config.yaml AND CHANGELOG


def test_detects_host_network_disabled(tmp_path):
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    config["host_network"] = False
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_host_network_enabled(tmp_path)
    assert any("host_network" in p for p in problems), problems


def test_detects_host_network_missing(tmp_path):
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    del config["host_network"]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_host_network_enabled(tmp_path)
    assert any("host_network" in p for p in problems), problems


def test_missing_changelog_heading_raises(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nno version heading here\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="no '## <version>' heading"):
        check_release_consistency(tmp_path)
