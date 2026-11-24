"""#63: a background job keeps the caller's Hermes profile when the profile lives in a context variable only."""
import contextvars
import sys
import types
from pathlib import Path

from caduceus.core import jobs, paths

_PROFILE = contextvars.ContextVar("fake_hermes_home", default=None)


def _install_fake_storage(monkeypatch, default_home: Path):
    module = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name):
        home = _PROFILE.get() or default_home
        return Path(home) / "plugin-data" / name

    module.plugin_data_dir = plugin_data_dir
    package = types.ModuleType("plugins")
    package.plugin_storage = module
    monkeypatch.setitem(sys.modules, "plugins", package)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", module)


def test_job_sees_the_callers_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("CADUCEUS_DATA_DIR", raising=False)
    default_home, profile_home = tmp_path / "default", tmp_path / "profiles" / "perso"
    _install_fake_storage(monkeypatch, default_home)
    token = _PROFILE.set(str(profile_home))
    try:
        assert paths.data_dir() == profile_home / "plugin-data" / "caduceus"
        outcome = jobs.submit("test-job-context", lambda: str(paths.data_dir()), wait_s=10)
    finally:
        _PROFILE.reset(token)
    assert outcome == {"state": "done", "result": str(profile_home / "plugin-data" / "caduceus")}


def test_job_without_a_profile_uses_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CADUCEUS_DATA_DIR", raising=False)
    default_home = tmp_path / "default"
    _install_fake_storage(monkeypatch, default_home)
    outcome = jobs.submit("test-job-default", lambda: str(paths.data_dir()), wait_s=10)
    assert outcome == {"state": "done", "result": str(default_home / "plugin-data" / "caduceus")}


def test_settings_reports_a_default_profile_folder_by_counts(monkeypatch, tmp_path):
    from caduceus import ops
    from caduceus.core.db import open_vault, write_txn

    default_home, active = tmp_path / "default-home", tmp_path / "profile-data"
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("CADUCEUS_DATA_DIR", str(active))

    class Ctx:
        config: dict = {}

        def get_config(self, key, default=None):
            return default

    assert "default_profile_folder" not in ops._settings(Ctx())
    stray = default_home / "plugin-data" / "caduceus" / "vault.db"
    conn = open_vault(stray)
    with write_txn(conn):
        conn.execute("INSERT INTO source_artifact(subject_id, kind, sha256, original_name, stored_path, size_bytes, added_at) "
                     "VALUES (1, 'fhir', 'ab', 'x.json', 'sources/ab', 1, '2026-01-01T00:00:00Z')")
    conn.close()
    found = ops._settings(Ctx())["default_profile_folder"]
    assert found["path"] == str(stray.parent) and found["files"] == 1 and found["import_runs"] == 0
    assert stray.is_file()  # never deleted


def test_settings_skips_the_default_folder_when_it_is_the_active_one(monkeypatch, tmp_path):
    from caduceus import ops
    from caduceus.core.db import open_vault

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CADUCEUS_DATA_DIR", str(home / "plugin-data" / "caduceus"))
    open_vault(home / "plugin-data" / "caduceus" / "vault.db").close()

    class Ctx:
        def get_config(self, key, default=None):
            return default

    assert "default_profile_folder" not in ops._settings(Ctx())
