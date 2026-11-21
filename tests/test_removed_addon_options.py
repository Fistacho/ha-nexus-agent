"""1.0.0 removed three add-on options: `port`, `git_versioning_auto` and
`max_backups`.

- `port` stopped doing anything in 0.23.0 (ADR-0004, `host_network: true`):
  the LAN port comes from the add-on's Network tab (`addon_network.py`) and
  `ingress_port` is fixed at 7123.
- `git_versioning_auto` and `max_backups` were exported by `run.sh` but no
  Python code ever read them.

An install upgraded from 0.x still has these keys stored by Supervisor.
Supervisor drops keys that are no longer in the schema with a warning
(`supervisor/apps/options.py`, `AppOptions.__call__`: "Option '%s' does not
exist in the schema"), both when it checks the new schema on update
(`App.test_update_schema`) and when it writes `/data/options.json`
(`App.write_options`), so the add-on still starts.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
REMOVED = ("port", "git_versioning_auto", "max_backups")


def _config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", REMOVED)
def test_option_removed_from_config_yaml(key):
    config = _config()
    assert key not in config["options"]
    assert key not in config["schema"]


@pytest.mark.parametrize("key", REMOVED)
def test_option_removed_from_translations(key):
    for path in (ROOT / "translations").glob("*.yaml"):
        translations = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert key not in translations.get("configuration", {}), path.name


def test_options_and_schema_list_the_same_keys():
    config = _config()
    assert set(config["options"]) == set(config["schema"])


def test_every_option_has_a_translation():
    config = _config()
    translations = yaml.safe_load((ROOT / "translations" / "en.yaml").read_text(encoding="utf-8"))
    assert set(translations["configuration"]) == set(config["schema"])


def test_run_sh_no_longer_reads_removed_options():
    text = (ROOT / "run.sh").read_text(encoding="utf-8")
    for needle in (".port", ".git_versioning_auto", ".max_backups", "GIT_VERSIONING_AUTO", "MAX_BACKUPS"):
        assert needle not in text, needle


def test_ingress_port_stays_7123():
    """The fixed port nexus binds for ingress, and the one `run.sh` exports
    as NEXUS_PORT in add-on mode now that the `port` option is gone."""
    assert _config()["ingress_port"] == 7123
