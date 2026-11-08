"""Where Caduceus keeps its files, and the read check for paths the user hands in."""
from __future__ import annotations

import os
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


def read_block_error(path: Path) -> str | None:
    """Hermes's own read guard (credential stores, other profiles' secrets, ``.env`` files)."""
    try:
        from agent.file_safety import get_read_block_error
    except Exception:  # outside Hermes (unit tests, local tooling) there is no guard to apply
        return None
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
