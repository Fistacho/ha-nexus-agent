r"""Regression guard for the add-on's base image (Dockerfile <-> Supervisor apps/build).

Context (0.22.0 -> 0.22.1 hotfix): 0.22.0 changed the Dockerfile from a hardcoded
`FROM python:3.12-alpine` to `ARG BUILD_FROM` / `FROM ${BUILD_FROM}`, fed by a new
`build.yaml` mapping every architecture to the same value, `python:3.12-alpine`.
That broke installing/updating the add-on under the real Home Assistant Supervisor
(2026.09.2): `update.install` failed with "An unknown error occurred while trying to
build the image".

Root cause, confirmed against `home-assistant/supervisor` at tag `2026.09.2`
(`supervisor/apps/validate.py`, `SCHEMA_BUILD_CONFIG`): a `build.yaml`'s `build_from:`
value is validated against

    RE_DOCKER_IMAGE_BUILD = re.compile(
        r"^([a-zA-Z\-\.:\d{}]+/)*?([\-\w{}]+)/([\-\w{}]+)(:[\.\-\w{}]+)?$"
    )

which *requires* at least one `/` (a `namespace/repository[:tag]` split) -- it does
NOT accept a bare official-image reference like `python:3.12-alpine` (no namespace).
`docker pull python:3.12-alpine` works everywhere because Docker Hub implicitly
resolves unqualified names to `library/<name>`, but Supervisor's own schema does not
do that resolution: `python:3.12-alpine` fails `vol.Invalid`, and `AppBuild._read_build_config`
(`supervisor/apps/build.py`) then silently falls back to
`ATTR_BUILD_FROM = "ghcr.io/home-assistant/base:latest"` (a bare OS image, no Python,
no `apk`) instead of raising — which is what actually broke the Docker build, several
layers away from anything visible in the add-on's own config.

`supervisor/apps/build.py`'s `AppBuild.create()` also logs, whenever *any* build.yaml
is found at all (regardless of content): "App %s uses build.yaml which is deprecated.
Move build parameters into the Dockerfile directly." -- Supervisor's own recommended
fix for an add-on like this one, that needs no per-architecture build differences at
all (`python:3.12-alpine`'s Docker Hub manifest list already covers all 5 declared
architectures — verified against the registry in the Dockerfile's own header comment).

The fix restores exactly what every release through 0.21.0 shipped and verified
working: a hardcoded `FROM python:3.12-alpine` with no `ARG BUILD_FROM` at all, and no
`build.yaml`. This test suite guards two ways that can regress again:

1. Dockerfile goes back to sourcing its base image from an `ARG` (`BUILD_FROM` or any
   other name) instead of a literal `FROM <image>` -- reintroducing a dependency on
   whatever Supervisor decides to inject (or not) for `--build-arg`.
2. A `build.yaml` reappears in the repo. Given (1), Supervisor would ignore its
   `build_from:` value in the resulting `docker build` anyway (nothing in the
   Dockerfile consumes it) — but *not* silently: `supervisor/apps/build.py` still
   validates and warns/deprecates it before it's ever passed to Docker, and reasoning
   about that "read but unused" mismatch is exactly the trap this repo fell into
   twice now (first when the hardcoded `FROM` made 0.20.0's ghcr.io-per-arch map dead,
   then when 0.22.0's `ARG BUILD_FROM` picked it back up with an incompatible value).
   The image this add-on ships from is the Dockerfile's own literal `FROM`, full stop.
3. The Dockerfile stops installing a CLI tool the runtime actually needs on top of a
   bare `python:3.12-alpine` (curl for HEALTHCHECK, jq + bash for run.sh, git for
   GitPython/tools/git_ops.py) — `python:3.12-alpine` ships none of these.

TDD note: every check function is exercised against a tmp_path copy of the real
Dockerfile with the regression re-introduced (RED), and against the real repo (GREEN,
via the `dockerfile` fixture below), proving each check can actually catch the class
of bug it exists for. `check_build_from_would_fail_supervisor_validation` further
guards this specific regex against the exact class of "unqualified official image"
bug encountered here, independent of whether build.yaml exists in this repo today —
so a future contributor re-adding one gets a clear failure message pointing at the
regex, not another silent Supervisor-side fallback.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_REQUIRED_APK_PACKAGES = {"curl", "jq", "bash", "git"}

# Verbatim from home-assistant/supervisor `supervisor/apps/validate.py`
# (`RE_DOCKER_IMAGE_BUILD`), tag 2026.09.2 — the validator that rejected this add-on's
# own `build.yaml` (see module docstring). Kept here, independent of whether this repo
# has a build.yaml today, so a future one is checked against the same rule that broke
# the real install, not just "does it look like an image reference".
_RE_SUPERVISOR_BUILD_FROM = re.compile(
    r"^([a-zA-Z\-\.:\d{}]+/)*?([\-\w{}]+)/([\-\w{}]+)(:[\.\-\w{}]+)?$"
)


def check_dockerfile_has_no_build_arg_from(dockerfile_path: Path) -> list[str]:
    """The Dockerfile must source its base image from a literal FROM, not an ARG.

    An `ARG`-sourced `FROM` makes the effective base image depend on whatever
    `--build-arg` Supervisor's `AppBuild.get_docker_args()` decides to pass (or not) —
    see module docstring for how that silently substituted an incompatible image once
    already.
    """
    text = dockerfile_path.read_text(encoding="utf-8")
    problems = []

    if re.search(r"^ARG\s+\w+", text, re.MULTILINE):
        problems.append(
            "Dockerfile declares an ARG feeding FROM — the base image must be a "
            "literal, not sourced from a build-arg Supervisor may or may not set "
            "(see module docstring)."
        )

    from_targets = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    if not from_targets:
        problems.append("Dockerfile has no FROM instruction at all")
    else:
        non_literal = [f for f in from_targets if f.startswith("$")]
        if non_literal:
            problems.append(f"Dockerfile's FROM references a build-arg, not a literal image: {non_literal}")

    return problems


def check_dockerfile_installs_required_packages(dockerfile_path: Path) -> list[str]:
    """python:3.12-alpine ships none of curl/jq/bash/git — the Dockerfile must
    install all four itself (curl: HEALTHCHECK; bash+jq: run.sh; git: GitPython)."""
    text = dockerfile_path.read_text(encoding="utf-8")
    problems = []
    for package in sorted(_REQUIRED_APK_PACKAGES):
        if not re.search(rf"apk add\b[^\n]*(\\\n\s*)*{re.escape(package)}\b", text) and not re.search(
            rf"^\s*{re.escape(package)}\s*(\\)?\s*$", text, re.MULTILINE
        ):
            problems.append(f"Dockerfile does not appear to `apk add` {package!r}")
    return problems


def check_build_from_would_fail_supervisor_validation(value: str) -> bool:
    """True if `value` would be REJECTED by Supervisor's own build_from schema.

    Mirrors `supervisor/apps/validate.py`'s `RE_DOCKER_IMAGE_BUILD` (see module
    docstring) — a bare, unqualified official-image reference like
    `python:3.12-alpine` (no `namespace/repository` split) fails it, even though
    `docker pull`/`FROM` accept it directly.
    """
    return not _RE_SUPERVISOR_BUILD_FROM.match(value)


def check_dockerfile_has_literal_mcp_label(dockerfile_path: Path) -> list[str]:
    """ADR-0005: the MCP Registry label must be a literal LABEL, not fed by an
    ARG -- same reasoning as `check_dockerfile_has_no_build_arg_from` above,
    the label needs to be identical whether Supervisor builds this Dockerfile
    locally (pre-ghcr.io users on 0.24.x) or CI builds/pushes it for ghcr.io.
    """
    text = dockerfile_path.read_text(encoding="utf-8")
    if not re.search(
        r'^LABEL\s+io\.modelcontextprotocol\.server\.name\s*=\s*"io\.github\.Fistacho/ha-nexus-agent"',
        text,
        re.MULTILINE,
    ):
        return [
            "Dockerfile is missing a literal "
            'LABEL io.modelcontextprotocol.server.name="io.github.Fistacho/ha-nexus-agent"'
        ]
    return []


def check_no_build_yaml(repo_root: Path) -> list[str]:
    """This add-on ships no `build.yaml` — see module docstring for why."""
    if (repo_root / "build.yaml").exists():
        return [
            "build.yaml exists but the Dockerfile no longer consumes ARG BUILD_FROM "
            "(see check_dockerfile_has_no_build_arg_from) — any build_from: value in "
            "it is validated and warned-about by Supervisor but never reaches Docker, "
            "and re-wiring it back in risks the exact regression this test guards "
            "against (module docstring)."
        ]
    return []


# --- Green: the real repo today ------------------------------------------------


def test_dockerfile_has_no_build_arg_from():
    problems = check_dockerfile_has_no_build_arg_from(REPO_ROOT / "Dockerfile")
    assert not problems, problems


def test_dockerfile_installs_curl_jq_bash_git():
    problems = check_dockerfile_installs_required_packages(REPO_ROOT / "Dockerfile")
    assert not problems, problems


def test_dockerfile_has_literal_mcp_registry_label():
    problems = check_dockerfile_has_literal_mcp_label(REPO_ROOT / "Dockerfile")
    assert not problems, problems


def test_no_build_yaml_in_repo():
    problems = check_no_build_yaml(REPO_ROOT)
    assert not problems, problems


def test_dockerfile_from_target_is_the_documented_image():
    """The literal FROM must be `python:3.12-alpine` — the same multi-arch Docker Hub
    image verified (Dockerfile header comment) to cover all 5 architectures this
    add-on declares in config.yaml."""
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_targets = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    assert from_targets == ["python:3.12-alpine"], from_targets


# --- Red-then-green proof: each checker actually detects the regression -------


def test_detects_arg_sourced_from_regression(tmp_path):
    # Simulate the 0.22.0 regression this batch reverted.
    (tmp_path / "Dockerfile").write_text(
        "ARG BUILD_FROM\nFROM ${BUILD_FROM}\n\nRUN apk add --no-cache git bash curl jq\n",
        encoding="utf-8",
    )

    problems = check_dockerfile_has_no_build_arg_from(tmp_path / "Dockerfile")
    assert problems, "expected an ARG-sourced FROM to be reported"


def test_detects_missing_mcp_registry_label(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-alpine\n\nRUN apk add --no-cache git bash curl jq\n",
        encoding="utf-8",
    )

    problems = check_dockerfile_has_literal_mcp_label(tmp_path / "Dockerfile")
    assert problems, "expected a missing MCP Registry LABEL to be reported"


def test_detects_missing_required_package(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-alpine\n\n"
        "RUN apk add --no-cache git bash curl\n"  # jq missing on purpose
        '\nCMD ["./run.sh"]\n',
        encoding="utf-8",
    )

    problems = check_dockerfile_installs_required_packages(tmp_path / "Dockerfile")
    assert any("jq" in p for p in problems), problems


def test_detects_build_yaml_presence(tmp_path):
    (tmp_path / "build.yaml").write_text("build_from:\n  amd64: python:3.12-alpine\n", encoding="utf-8")

    problems = check_no_build_yaml(tmp_path)
    assert problems, "expected a re-added build.yaml to be reported"


@pytest.mark.parametrize(
    ("value", "expected_would_fail"),
    [
        # The exact value that broke the real install — a bare official image, no
        # namespace/repository split.
        ("python:3.12-alpine", True),
        # Supervisor's own fallback default, and the original (working) build.yaml
        # entries this add-on shipped through 0.20.0 — both have the required '/'.
        ("ghcr.io/home-assistant/base:latest", False),
        ("ghcr.io/home-assistant/amd64-base-python:3.12", False),
    ],
)
def test_supervisor_build_from_regex_reproduction(value, expected_would_fail):
    assert check_build_from_would_fail_supervisor_validation(value) is expected_would_fail
