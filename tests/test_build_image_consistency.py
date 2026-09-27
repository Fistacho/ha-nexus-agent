"""Regression guard for the Dockerfile <-> build.yaml pairing.

Context: the add-on's Dockerfile used to hardcode `FROM python:3.12-alpine`,
which made build.yaml's `build_from:` map dead (every architecture silently built
from the same hardcoded image, regardless of what build.yaml said). The fix is
`ARG BUILD_FROM` / `FROM ${BUILD_FROM}` in the Dockerfile, fed by build.yaml. This
test suite guards three ways that pairing can silently break again:

1. Dockerfile regresses back to a hardcoded `FROM <literal image>` (the original
   bug) instead of `ARG BUILD_FROM` / `FROM ${BUILD_FROM}`.
2. build.yaml's `build_from:` entries drift apart between architectures (there is
   currently no reason for them to differ — all 5 point at the same multi-arch
   Docker Hub image, `python:3.12-alpine` — see build.yaml's own comments... note
   build.yaml itself has no comments, the reasoning lives in Dockerfile's header).
3. The Dockerfile stops installing a CLI tool the runtime actually needs on top of
   a bare `python:3.12-alpine` (curl for HEALTHCHECK, jq + bash for run.sh, git for
   GitPython/tools/git_ops.py) — `python:3.12-alpine` ships none of these.

TDD note: every check function is exercised against a tmp_path copy of the real
Dockerfile/build.yaml with the regression re-introduced (RED), and against the
real repo (GREEN, via the `dockerfile`/`build_yaml_from` fixtures below), proving
each check can actually catch the class of bug it exists for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_ARCHES = ["aarch64", "amd64", "armhf", "armv7", "i386"]
_REQUIRED_APK_PACKAGES = {"curl", "jq", "bash", "git"}


def _read_build_from_map(build_yaml_path: Path) -> dict[str, str]:
    data = yaml.safe_load(build_yaml_path.read_text(encoding="utf-8"))
    return data["build_from"]


def check_build_from_is_uniform(build_yaml_path: Path) -> list[str]:
    """All 5 architectures must currently resolve to the same BUILD_FROM value."""
    build_from = _read_build_from_map(build_yaml_path)
    problems = []

    missing = set(_ARCHES) - build_from.keys()
    if missing:
        problems.append(f"build.yaml is missing build_from entries for: {sorted(missing)}")

    values = {arch: build_from[arch] for arch in _ARCHES if arch in build_from}
    distinct = set(values.values())
    if len(distinct) > 1:
        problems.append(f"build_from values differ across architectures: {values}")

    return problems


def check_dockerfile_uses_build_arg(dockerfile_path: Path) -> list[str]:
    """The Dockerfile must consume BUILD_FROM via ARG, not hardcode a base image."""
    text = dockerfile_path.read_text(encoding="utf-8")
    problems = []

    if not re.search(r"^ARG\s+BUILD_FROM\b", text, re.MULTILINE):
        problems.append("Dockerfile has no 'ARG BUILD_FROM' — build.yaml's build_from: would be dead")

    if not re.search(r"^FROM\s+\$\{?BUILD_FROM\}?", text, re.MULTILINE):
        problems.append(
            "Dockerfile's FROM does not reference ${BUILD_FROM} — "
            "it may be hardcoding a literal base image again"
        )

    hardcoded_from = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    literal_bases = [f for f in hardcoded_from if not f.startswith("$")]
    if literal_bases:
        problems.append(f"Dockerfile has hardcoded FROM target(s), ignoring BUILD_FROM: {literal_bases}")

    return problems


def check_dockerfile_installs_required_packages(dockerfile_path: Path) -> list[str]:
    """python:3.12-alpine ships none of curl/jq/bash/git — the Dockerfile must
    install all four itself (curl: HEALTHCHECK: bash+jq: run.sh; git: GitPython)."""
    text = dockerfile_path.read_text(encoding="utf-8")
    problems = []
    for package in sorted(_REQUIRED_APK_PACKAGES):
        if not re.search(rf"apk add\b[^\n]*(\\\n\s*)*{re.escape(package)}\b", text) and not re.search(
            rf"^\s*{re.escape(package)}\s*(\\)?\s*$", text, re.MULTILINE
        ):
            problems.append(f"Dockerfile does not appear to `apk add` {package!r}")
    return problems


# --- Green: the real repo today ------------------------------------------------

def test_build_from_is_uniform_across_architectures():
    problems = check_build_from_is_uniform(REPO_ROOT / "build.yaml")
    assert not problems, problems


def test_dockerfile_uses_build_arg_not_a_hardcoded_from():
    problems = check_dockerfile_uses_build_arg(REPO_ROOT / "Dockerfile")
    assert not problems, problems


def test_dockerfile_installs_curl_jq_bash_git():
    problems = check_dockerfile_installs_required_packages(REPO_ROOT / "Dockerfile")
    assert not problems, problems


# --- Red-then-green proof: each checker actually detects the regression -------

def test_detects_build_from_drift(tmp_path):
    build_from = _read_build_from_map(REPO_ROOT / "build.yaml")
    build_from["amd64"] = "some/other-image:latest"
    (tmp_path / "build.yaml").write_text(
        yaml.safe_dump({"build_from": build_from}), encoding="utf-8"
    )

    problems = check_build_from_is_uniform(tmp_path / "build.yaml")
    assert problems, "expected drifted amd64 value to be reported"


def test_detects_hardcoded_from_regression(tmp_path):
    # Simulate the original bug this batch fixed: a literal FROM, no ARG BUILD_FROM.
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-alpine\n\nRUN apk add --no-cache git bash curl jq\n",
        encoding="utf-8",
    )

    problems = check_dockerfile_uses_build_arg(tmp_path / "Dockerfile")
    assert problems, "expected a hardcoded FROM (no ARG BUILD_FROM) to be reported"


def test_detects_missing_required_package(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "ARG BUILD_FROM\n"
        "FROM ${BUILD_FROM}\n\n"
        "RUN apk add --no-cache git bash curl\n"  # jq missing on purpose
        '\nCMD ["./run.sh"]\n',
        encoding="utf-8",
    )

    problems = check_dockerfile_installs_required_packages(tmp_path / "Dockerfile")
    assert any("jq" in p for p in problems), problems


def test_missing_arch_in_build_yaml_is_reported(tmp_path):
    (tmp_path / "build.yaml").write_text(
        yaml.safe_dump({"build_from": {"amd64": "python:3.12-alpine"}}), encoding="utf-8"
    )

    problems = check_build_from_is_uniform(tmp_path / "build.yaml")
    assert any("missing" in p for p in problems), problems
