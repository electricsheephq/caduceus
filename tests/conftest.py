"""Shared fixtures. FakeCtx follows heartbeat2's Hermes-loader package pattern; the network is
blocked for every test; each test gets its own data directory; real-file checks are opt-in."""
import importlib.util
import os
import socket
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))
# The repo root has its own __init__.py (the Hermes loader entry). Import the inner package now, so pytest never
# imports the root under the name "caduceus" when a single test file runs.
import caduceus  # noqa: E402,F401


class FakeCtx:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.tools, self.commands, self.cli_commands = {}, {}, {}
        self.skills, self.blueprints = {}, {}

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_command(self, name, **kwargs):
        self.commands[name] = kwargs

    def register_cli_command(self, name, **kwargs):
        self.cli_commands[name] = kwargs

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = {"path": Path(path), "description": description}

    def register_automation_blueprint(self, key, **kwargs):
        self.blueprints[key] = kwargs

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


def load_plugin_package():
    name = "hermes_plugins_test.caduceus"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("hermes_plugins_test", type(sys)("hermes_plugins_test"))
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def pytest_configure(config):
    config.addinivalue_line("markers", "private: reads real files from CADUCEUS_PRIVATE_DIR; local only, never CI")
    config.addinivalue_line("markers", "network: may use the network; opt-in with CADUCEUS_NETWORK_TESTS=1")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("private") and not os.environ.get("CADUCEUS_PRIVATE_DIR"):
            item.add_marker(pytest.mark.skip(reason="set CADUCEUS_PRIVATE_DIR to run real-file checks locally"))
        if item.get_closest_marker("network") and os.environ.get("CADUCEUS_NETWORK_TESTS") != "1":
            item.add_marker(pytest.mark.skip(reason="network tests are opt-in"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    if request.node.get_closest_marker("network"):
        return

    def refused(*args, **kwargs):
        raise AssertionError("unit tests must not use the network")

    for attr in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, attr, refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "plugin-data"
    root.mkdir()
    monkeypatch.setenv("CADUCEUS_DATA_DIR", str(root))
    return root


@pytest.fixture
def vault(data_dir):
    from caduceus.core.db import open_vault

    conn = open_vault(data_dir / "vault.db")
    yield conn
    conn.close()


@pytest.fixture
def samples(data_dir):
    from caduceus.core.db import open_samples

    conn = open_samples(data_dir / "samples.db")
    yield conn
    conn.close()


@pytest.fixture
def fake_ctx():
    return FakeCtx()


@pytest.fixture
def private_dir():
    """Directory with the maintainer's real files, for local-only checks. Never set in CI."""
    return Path(os.environ["CADUCEUS_PRIVATE_DIR"])
