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
