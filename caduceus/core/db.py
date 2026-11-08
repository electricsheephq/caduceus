"""SQLite connections, schema files and the single-writer rule (WAL, so readers never wait).

``vault.db`` holds provenance, genome, records, research and the search index; ``samples.db`` holds
bulky Apple Health samples. Schema files live in ``schema/vault/`` and ``schema/samples/``, are
applied in name order, and must be idempotent (``IF NOT EXISTS`` everywhere).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent / "schema"
SCHEMA_VERSION = "1"
_WRITE = threading.RLock()


def connect(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:  # some network filesystems refuse WAL; rollback journal still works
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def apply_schema(conn: sqlite3.Connection, kind: str) -> None:
    with _WRITE:
        for path in sorted((SCHEMA / kind).glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))


def open_vault(path=None) -> sqlite3.Connection:
    from .paths import vault_path

    conn = connect(path or vault_path())
    apply_schema(conn, "vault")
    with write_txn(conn):
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.execute(
            "INSERT OR IGNORE INTO subject(id, label, created_at) "
            "VALUES (1, 'me', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )
    return conn


def open_samples(path=None) -> sqlite3.Connection:
    from .paths import samples_path

    conn = connect(path or samples_path())
    apply_schema(conn, "samples")
    return conn


def db_dir(conn: sqlite3.Connection) -> Path:
    """The directory of the connection's main database file (sources and reports live beside it)."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            if not row[2]:
                raise ValueError("an in-memory database has no directory")
            return Path(row[2]).parent
    raise ValueError("no main database")


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """One writer per process. Joins a transaction already open on the same connection."""
    with _WRITE:
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
