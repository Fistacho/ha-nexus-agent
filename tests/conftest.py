"""Shared pytest setup for the nexus test suite.

Adds the add-on root to sys.path so `import ha_client` / `import tools.system`
resolve the same way they do at runtime, and neutralises the side effects that
`auth.py` performs at import time (it writes an API key to /config).
"""
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# auth.py reads these at import time — set before anything imports it.
os.environ.setdefault("NEXUS_API_KEY", "test-api-key")
os.environ.setdefault("HA_TOKEN", "test-ha-token")
os.environ.setdefault("HA_URL", "http://ha.test:8123")

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def ha_config_dir(tmp_path, monkeypatch):
    """Point HA_CONFIG_PATH at a throwaway directory."""
    monkeypatch.setenv("HA_CONFIG_PATH", str(tmp_path))
    return tmp_path
