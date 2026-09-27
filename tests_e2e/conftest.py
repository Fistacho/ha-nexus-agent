"""Shared pytest setup for the e2e smoke suite.

This suite is deliberately **separate** from `tests/` (its own directory, its own
conftest, no shared fixtures) so that it is never picked up by a bare `pytest`/
`python -m pytest` invocation from the repo root: `pyproject.toml`'s
`[tool.pytest.ini_options] testpaths = ["tests"]` restricts default collection to
`tests/` only, and `tests_e2e/test_smoke_domains.py::test_testpaths_excludes_e2e_dir`
asserts that in code so a future edit to `testpaths` can't silently re-include it.

Unlike `tests/conftest.py` (which points `ha_client`/`tools.*` at a fake
`http://ha.test:8123` and mocks every HTTP/WS call), this suite points them at a
*real*, just-onboarded Home Assistant container (see
`scripts/e2e_onboard_ha.py` and `.github/workflows/e2e.yml`) and makes real network
calls — hence it needs its own `HA_URL`/`HA_TOKEN`, set here from a credentials file
*before* anything imports `ha_client` (`ha_client.py` reads `HA_URL`/`HA_TOKEN` at
module import time, not per-call).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_CREDENTIALS_ENV_VAR = "E2E_HA_CREDENTIALS_FILE"


def _load_credentials() -> dict:
    path = os.environ.get(_CREDENTIALS_ENV_VAR)
    if not path:
        pytest.exit(
            f"{_CREDENTIALS_ENV_VAR} is not set — this suite needs a real Home "
            "Assistant instance onboarded by scripts/e2e_onboard_ha.py first; see "
            ".github/workflows/e2e.yml for the expected sequence.",
            returncode=1,
        )
    credentials_path = Path(path)
    if not credentials_path.is_file():
        pytest.exit(
            f"{_CREDENTIALS_ENV_VAR}={path!r} does not exist — did "
            "scripts/e2e_onboard_ha.py run (and succeed) before pytest?",
            returncode=1,
        )
    return json.loads(credentials_path.read_text(encoding="utf-8"))


_credentials = _load_credentials()

# Must happen before any test module imports ha_client / tools.* — both read
# HA_URL/HA_TOKEN once, at import time (see ha_client.py's module-level
# `_HA_URL`/`_HA_TOKEN`), not per call.
os.environ["HA_URL"] = _credentials["url"]
os.environ["HA_TOKEN"] = _credentials["token"]
os.environ.setdefault("NEXUS_API_KEY", "e2e-test-api-key")
