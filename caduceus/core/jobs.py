"""Background imports. Hermes cuts a tool call off after 420 s, so an import runs in a daemon thread:
the tool waits a little, then returns and the agent reads progress from ``import_run``.

One job per key at a time (the key is the source sha256). Each job opens its own connections.
"""
from __future__ import annotations

import threading
from typing import Callable

_LOCK = threading.Lock()
_ACTIVE: dict[str, threading.Thread] = {}


def submit(key: str, fn: Callable[[], object], *, wait_s: float = 45.0) -> dict:
    """Run *fn* in a daemon thread under *key* and wait up to *wait_s* seconds.

    Returns ``{"state": "done", "result": ...}``, ``{"state": "error", "error": "..."}``,
    ``{"state": "running"}`` (still going; read import_run), or ``{"state": "busy"}`` when a job
    with this key is already running."""
    box: dict = {}
    finished = threading.Event()
    with _LOCK:
        current = _ACTIVE.get(key)
        if current is not None and current.is_alive():
            return {"state": "busy"}

        def target() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # reported to the caller; the importer records its own failure
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                with _LOCK:
                    if _ACTIVE.get(key) is threading.current_thread():
                        _ACTIVE.pop(key, None)
                finished.set()

        thread = threading.Thread(target=target, name=f"caduceus-job-{key[:12]}", daemon=True)
        _ACTIVE[key] = thread
        thread.start()
    if finished.wait(max(0.0, wait_s)):
        if "error" in box:
            return {"state": "error", "error": box["error"]}
        return {"state": "done", "result": box.get("result")}
    return {"state": "running"}


def is_active(key: str) -> bool:
    with _LOCK:
        thread = _ACTIVE.get(key)
        return thread is not None and thread.is_alive()


def any_active() -> bool:
    """True while any background job (an import, the demo or a research watch) is still running."""
    with _LOCK:
        return any(thread.is_alive() for thread in _ACTIVE.values())


def join(key: str, timeout: float | None = None) -> bool:
    """Wait for the job under *key* to finish (tests and the dev CLI). True when nothing is running."""
    with _LOCK:
        thread = _ACTIVE.get(key)
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()
