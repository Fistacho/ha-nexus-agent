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


def _read_server_json_version(root: Path) -> str:
    import json

    data = json.loads((root / "server.json").read_text(encoding="utf-8"))
    return str(data["version"])


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


def check_image_arch_label_consistency(root: Path) -> list[str]:
    """ADR-0005: config.yaml's `image`/`arch`, the Dockerfile's MCP Registry
    ownership LABEL, server.json's `name`/package identifier, and
    publish-image.yml's `ARCHITECTURES` must all agree -- a drift here is
    exactly the class of bug that makes Supervisor try to pull an image tag
    (or architecture) nothing ever published, or makes the MCP Registry
    reject the image for an ownership-label mismatch.

    `image:` is deliberately a single multi-arch manifest tag (no `{arch}`
    placeholder) per the orchestrator's 2026-09-28 decision, following
    current HA guidance (developers.home-assistant.io/docs/apps/publishing)
    over ADR-0005's own literal `{arch}`-per-tag text -- confirmed against
    home-assistant/supervisor `main` that this is actually supported:
    `App._image()` (supervisor/apps/model.py) does
    `config[ATTR_IMAGE].format(arch=arch)`, a no-op on a string with no
    `{arch}` placeholder, and `Interface.install()` (supervisor/docker/
    interface.py) always passes an explicit `platform=` to the Docker Engine
    pull itself -- architecture selection never actually depended on the
    image *name* containing `{arch}`.
    """
    problems: list[str] = []

    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    config_arch = config.get("arch", [])
    config_image = config.get("image", "")

    if sorted(config_arch) != ["aarch64", "amd64"]:
        problems.append(
            f"config.yaml arch is {config_arch!r}, expected exactly "
            "['aarch64', 'amd64'] (ADR-0005 — 32-bit dropped with the ghcr.io image)"
        )

    if "{arch}" in config_image:
        problems.append(
            f"config.yaml image {config_image!r} still has a '{{arch}}' placeholder -- "
            "the orchestrator decided on a single multi-arch manifest tag instead "
            "(no {arch}); Supervisor resolves architecture via an explicit `platform=` "
            "pull argument regardless, see this check's own docstring"
        )

    if config_image and config_image != "ghcr.io/fistacho/nexus-agent":
        problems.append(
            f"config.yaml image is {config_image!r}, expected the combined multi-arch "
            "manifest tag 'ghcr.io/fistacho/nexus-agent' (no per-arch prefix)"
        )

    server_json_path = root / "server.json"
    if server_json_path.exists() and config_image:
        import json as _json2

        server_json = _json2.loads(server_json_path.read_text(encoding="utf-8"))
        for package in server_json.get("packages", []):
            identifier = package.get("identifier", "")
            image_part = identifier.rsplit(":", 1)[0] if ":" in identifier else identifier
            image_part_no_registry = image_part.removeprefix("ghcr.io/")
            config_image_no_registry = config_image.removeprefix("ghcr.io/")
            if image_part_no_registry != config_image_no_registry:
                problems.append(
                    f"server.json package identifier image {image_part!r} != "
                    f"config.yaml image {config_image!r}"
                )

    dockerfile_text = (root / "Dockerfile").read_text(encoding="utf-8")
    label_match = re.search(
        r'^LABEL\s+io\.modelcontextprotocol\.server\.name\s*=\s*"([^"]+)"',
        dockerfile_text,
        re.MULTILINE,
    )
    if not label_match:
        problems.append(
            "Dockerfile is missing the io.modelcontextprotocol.server.name LABEL "
            "the MCP Registry uses to verify OCI package ownership"
        )
    else:
        label_value = label_match.group(1)
        server_json_path = root / "server.json"
        if server_json_path.exists():
            import json

            server_json = json.loads(server_json_path.read_text(encoding="utf-8"))
            server_json_name = server_json.get("name", "")
            if label_value != server_json_name:
                problems.append(
                    f"Dockerfile LABEL io.modelcontextprotocol.server.name={label_value!r} "
                    f"!= server.json name {server_json_name!r}"
                )

    publish_workflow = root / ".github" / "workflows" / "publish-image.yml"
    if publish_workflow.exists():
        import json as _json

        workflow_text = publish_workflow.read_text(encoding="utf-8")
        archs_match = re.search(r'ARCHITECTURES:\s*\'(\[[^\]]*\])\'', workflow_text)
        if not archs_match:
            problems.append("publish-image.yml has no parseable ARCHITECTURES env value")
        else:
            workflow_archs = _json.loads(archs_match.group(1))
            expected = {"aarch64" if a == "arm64" else a for a in workflow_archs}
            if sorted(expected) != sorted(config_arch):
                problems.append(
                    f"publish-image.yml ARCHITECTURES {workflow_archs!r} != "
                    f"config.yaml arch {config_arch!r}"
                )

    return problems


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

    # 4th place (ADR-0005): server.json is what the MCP Registry and any
    # standalone-image client see. If it drifts, `mcp-registry.yml`'s own
    # version-guard step refuses to publish -- but that only fires on a
    # tagged release; this test catches the drift immediately, at commit
    # time, same as the other three.
    if (root / "server.json").exists():
        server_json_version = _read_server_json_version(root)
        if pyproject_version != server_json_version:
            problems.append(
                f"pyproject.toml version {pyproject_version!r} != "
                f"server.json version {server_json_version!r}"
            )

        import json

        server_json = json.loads((root / "server.json").read_text(encoding="utf-8"))
        for package in server_json.get("packages", []):
            identifier = package.get("identifier", "")
            if ":" in identifier:
                identifier_version = identifier.rsplit(":", 1)[1]
                if identifier_version != pyproject_version:
                    problems.append(
                        f"server.json packages[].identifier {identifier!r} tag "
                        f"{identifier_version!r} != pyproject.toml version {pyproject_version!r}"
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


def test_image_arch_label_consistency():
    problems = check_image_arch_label_consistency(REPO_ROOT)
    assert not problems, "; ".join(problems)


def test_apparmor_txt_does_not_exist_yet():
    """ADR-0005 D2 / apparmor.txt.draft's own header: the literal filename
    `apparmor.txt` must not appear in this repo until an owner has explicitly
    tested the drafted profile against a live add-on install and decided to
    activate it -- Supervisor loads whatever is at that exact path into the
    host's AppArmor the very next install/update, unconditionally (see
    apparmor.txt.draft's header for the verified supervisor/apps/app.py
    behaviour this guards against). A stray `git mv apparmor.txt.draft
    apparmor.txt` must fail this test, not silently ship."""
    assert not (REPO_ROOT / "apparmor.txt").exists(), (
        "apparmor.txt exists -- this activates AppArmor enforcement on the next "
        "Supervisor install/update. Confirm it was tested live and intentionally "
        "activated (see apparmor.txt.draft header) before removing this guard."
    )
    assert (REPO_ROOT / "apparmor.txt.draft").exists(), (
        "the drafted (inactive) AppArmor profile is missing entirely"
    )


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


def test_detects_arch_drift_back_to_five_architectures(tmp_path):
    for name in ("config.yaml", "Dockerfile", "server.json"):
        (tmp_path / name).write_text(
            (REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "publish-image.yml").write_text(
        (REPO_ROOT / ".github" / "workflows" / "publish-image.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    config["arch"] = ["aarch64", "amd64", "armhf", "armv7", "i386"]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_image_arch_label_consistency(tmp_path)
    assert any("arch" in p for p in problems), problems


def test_detects_arch_placeholder_reintroduced(tmp_path):
    for name in ("Dockerfile", "server.json"):
        (tmp_path / name).write_text(
            (REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    config["image"] = "ghcr.io/fistacho/{arch}-nexus-agent"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_image_arch_label_consistency(tmp_path)
    assert any("{arch}" in p for p in problems), problems


def test_detects_image_server_json_mismatch(tmp_path):
    for name in ("Dockerfile", "server.json"):
        (tmp_path / name).write_text(
            (REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    config["image"] = "ghcr.io/fistacho/nexus-agent-renamed"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_image_arch_label_consistency(tmp_path)
    assert any("server.json" in p for p in problems), problems


def test_detects_missing_mcp_registry_label(tmp_path):
    for name in ("config.yaml", "server.json"):
        (tmp_path / name).write_text(
            (REPO_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-alpine\nRUN apk add --no-cache git bash curl jq\n",
        encoding="utf-8",
    )

    problems = check_image_arch_label_consistency(tmp_path)
    assert any("LABEL" in p for p in problems), problems


def test_detects_label_server_json_name_mismatch(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "Dockerfile").write_text(
        'FROM python:3.12-alpine\n'
        'LABEL io.modelcontextprotocol.server.name="io.github.Someone/wrong-name"\n'
        "RUN apk add --no-cache git bash curl jq\n",
        encoding="utf-8",
    )
    (tmp_path / "server.json").write_text(
        (REPO_ROOT / "server.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    problems = check_image_arch_label_consistency(tmp_path)
    assert any("server.json name" in p for p in problems), problems


def test_detects_host_network_missing(tmp_path):
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    del config["host_network"]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    problems = check_host_network_enabled(tmp_path)
    assert any("host_network" in p for p in problems), problems


def test_detects_server_json_version_drift(tmp_path):
    _copy_release_files(tmp_path)
    (tmp_path / "server.json").write_text(
        (REPO_ROOT / "server.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    import json

    server_json = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    server_json["version"] = "0.0.0-drifted"
    (tmp_path / "server.json").write_text(json.dumps(server_json), encoding="utf-8")

    problems = check_release_consistency(tmp_path)
    assert any("server.json" in p for p in problems), problems


def test_detects_server_json_package_identifier_tag_drift(tmp_path):
    _copy_release_files(tmp_path)
    (tmp_path / "server.json").write_text(
        (REPO_ROOT / "server.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    import json

    server_json = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    server_json["packages"][0]["identifier"] = "ghcr.io/fistacho/nexus-agent:0.0.0-drifted"
    (tmp_path / "server.json").write_text(json.dumps(server_json), encoding="utf-8")

    problems = check_release_consistency(tmp_path)
    assert any("identifier" in p for p in problems), problems


def test_server_json_matches_downloaded_registry_schema():
    """server.json must validate against the MCP Registry's own published
    JSON Schema -- not just "look like JSON". Skips (not fails) if the
    `jsonschema` package or network access to fetch the schema isn't
    available, since this is a factual, external-schema check rather than a
    guard against a regression this repo's own code could introduce."""
    import json

    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema package not installed")

    import urllib.error
    import urllib.request

    schema_url = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    try:
        with urllib.request.urlopen(schema_url, timeout=5) as resp:
            schema = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        pytest.skip(f"could not fetch {schema_url} (offline?)")

    server_json = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))
    jsonschema.validate(server_json, schema)


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
