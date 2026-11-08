"""The real Hermes plugin loader (CI pins Hermes). No model calls and no sockets."""
import importlib
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _require_hermes():
    try:
        importlib.import_module("hermes_cli.plugins")
    except ImportError:
        if os.environ.get("CADUCEUS_REQUIRE_HERMES") == "1":
            pytest.fail("CADUCEUS_REQUIRE_HERMES=1 but Hermes is not importable")
        pytest.skip("Hermes is not importable in this environment")


def _blueprints(manager):
    """Registered plugin blueprints, wherever this Hermes version keeps them (bare keys)."""
    for attr in ("_plugin_blueprints", "_automation_blueprints", "_blueprints"):
        found = getattr(manager, attr, None)
        if found:
            items = found.values() if isinstance(found, dict) else found
            return [str(getattr(b, "key", b)).split(":")[-1] for b in items]
    keys = []
    try:
        from cron import blueprint_catalog
        for name in dir(blueprint_catalog):
            obj = getattr(blueprint_catalog, name)
            if isinstance(obj, dict):
                keys += [str(k).split(":")[-1] for k in obj if str(k).startswith("caduceus:")]
    except ImportError:
        pass
    return keys


def test_real_loader_registers_everything_and_resolves_storage(tmp_path, monkeypatch):
    _require_hermes()
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.delenv("CADUCEUS_DATA_DIR", raising=False)
    plugin = home / "plugins" / "caduceus"
    shutil.copytree(ROOT, plugin, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text("plugins:\n  entries:\n    caduceus:\n      enabled: true\n")
    token = set_hermes_home_override(home)
    manager = PluginManager(scope_key=str(home))
    try:
        manifest = parse_manifest_file(plugin / "plugin.yaml", plugin, "user", "")
        assert manifest is not None
        with monkeypatch.context() as guard:
            guard.setattr(socket, "socket", lambda *a, **k: pytest.fail("loading must not open a socket"))
            manager._load_plugin(manifest)
        loaded = manager._plugins["caduceus"]
        assert loaded.enabled, loaded.error
        assert set(loaded.tools_registered) == set(manifest.provides_tools)
        assert "caduceus" in loaded.commands_registered
        skills = {entry.get("bare_name") for entry in getattr(manager, "_plugin_skills", {}).values()}
        assert {"caduceus-setup", "caduceus-guide", "caduceus-research", "caduceus-report-problem"} <= skills
        assert not (home / "plugin-data" / "caduceus" / "vault.db").exists()  # registration does no I/O
        assert {"research-watch", "daily-check"} <= {getattr(b, "key", b) for b in _blueprints(manager)}

        paths = importlib.import_module(loaded.module.__name__ + ".caduceus.core.paths")
        assert paths.data_dir() == home / "plugin-data" / "caduceus"
        sample = tmp_path / "raw.txt"
        sample.write_text("# rsid\tchromosome\tposition\tgenotype\n")
        assert paths.check_user_path(str(sample)) == sample.resolve()
        (home / ".env").write_text("X=1\n")
        with pytest.raises(ValueError):
            paths.check_user_path(str(home / ".env"))   # Hermes' own read guard refuses its secrets file
    finally:
        reset_hermes_home_override(token)
