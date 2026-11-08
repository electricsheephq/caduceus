"""Where Caduceus keeps its files, and the read check for paths the user hands in."""
from __future__ import annotations

import os
import re
from pathlib import Path

PLUGIN = "caduceus"


def data_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/caduceus``, resolved on every call because it follows the active
    profile. ``CADUCEUS_DATA_DIR`` overrides it for tests and local tooling."""
    override = os.environ.get("CADUCEUS_DATA_DIR")
    if override:
        root = Path(override).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN)


def vault_path() -> Path:
    return data_dir() / "vault.db"


def samples_path() -> Path:
    return data_dir() / "samples.db"


def reports_dir() -> Path:
    path = data_dir() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", "gcloud", "1Password", "Keychains"}
_SECRET_NAME = re.compile(r"^\.env(\..*)?$|\.env$|^\.netrc$|^auth\.json$|^credentials|\.(pem|key|p12|pfx|kdbx)$"
                          r"|^id_(rsa|dsa|ecdsa|ed25519)|^\.git-credentials$|^\.pgpass$|^\.npmrc$|^\.pypirc$", re.I)


def _own_block_error(path: Path) -> str | None:
    """A minimal stand-in when Hermes's guard is unavailable: obvious credential files are never read."""
    if _SECRET_NAME.search(path.name) or _SECRET_DIRS.intersection(path.parts):
        return "it looks like a credentials file"
    return None


def read_block_error(path: Path) -> str | None:
    """Hermes's own read guard (credential stores, other profiles' secrets, ``.env`` files).

    Hermes does not yet expose it through ``ctx``; if a release moves it, a minimal guard of our own still applies."""
    try:
        from agent.file_safety import get_read_block_error
    except Exception:
        return _own_block_error(path)
    try:
        return get_read_block_error(str(path))
    except Exception as exc:  # a guard that cannot decide refuses
        return f"read check failed ({type(exc).__name__})"


def check_user_path(raw: object) -> Path:
    """Resolve a path the user or agent supplied and refuse what Hermes refuses. Raises ValueError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path is required")
    text = raw.strip()
    if text.startswith("@file:"):
        text = text[len("@file:"):].strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("give the file's absolute path")
    path = path.resolve()
    blocked = read_block_error(path)
    if blocked:
        raise ValueError(f"that path cannot be read: {blocked}")
    if not path.is_file():
        raise ValueError(f"no file at {path}")
    return path
