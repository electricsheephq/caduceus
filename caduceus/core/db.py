"""SQLite connections, schema files and the single-writer rule (WAL, so readers never wait).

``vault.db`` holds provenance, genome, records, research and the search index; ``samples.db`` holds
bulky Apple Health samples. Schema files live in ``schema/vault/`` and ``schema/samples/``, are
applied in name order, and must be idempotent (``IF NOT EXISTS`` everywhere).
"""
from __future__ import annotations

import json
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
    _migrate_vault(conn)
    with write_txn(conn):
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.execute(
            "INSERT OR IGNORE INTO subject(id, label, created_at) "
            "VALUES (1, 'me', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )
    return conn


def _migrate_vault(conn: sqlite3.Connection) -> None:
    """Columns added after a vault was created. CREATE TABLE IF NOT EXISTS never adds them to an existing table."""
    with write_txn(conn):
        # A virtual source has many successful syncs; file imports still succeed only once per parser version.
        index_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='import_run_once'").fetchone()
        if index_sql and "hae_sync" not in index_sql[0]:
            conn.execute("DROP INDEX import_run_once")
            conn.execute("CREATE UNIQUE INDEX import_run_once ON import_run(source_id,parser,parser_version) "
                         "WHERE status='succeeded' AND parser <> 'hae_sync'")
        if "comparison_json" not in {row[1] for row in conn.execute("PRAGMA table_info(callset)")}:
            conn.execute("ALTER TABLE callset ADD COLUMN comparison_json TEXT NOT NULL DEFAULT '[]'")
        if "recorded" not in {row[1] for row in conn.execute("PRAGMA table_info(condition)")}:
            conn.execute("ALTER TABLE condition ADD COLUMN recorded TEXT")
        if not conn.execute("SELECT 1 FROM settings WHERE key='migration:fhir_uncoded_names_v1'").fetchone():
            from ..health.fhir import _observation_key, _observation_value

            for row in conn.execute("SELECT id,name,effective_at,value_num,value_text,unit,raw_json FROM observation "
                                    "WHERE source='fhir' AND code_system IS NULL AND code IS NULL").fetchall():
                num, text, unit = row["value_num"], row["value_text"], row["unit"]
                document = json.loads(row["raw_json"] or "{}")
                part = (document.get("component") or [document])[0]
                if any(key.startswith("value") for key in part):
                    num, text, unit = _observation_value(part)
                key = _observation_key(None, None, row["effective_at"], num, text, unit, False, row["name"])
                # A colliding row keeps its old key and all its data.
                conn.execute("UPDATE OR IGNORE observation SET dedupe_key=? WHERE id=?", (key, row["id"]))
            conn.execute("INSERT INTO settings(key,value_json,updated_at) "
                         "VALUES ('migration:fhir_uncoded_names_v1','true',strftime('%Y-%m-%dT%H:%M:%SZ','now'))")

    def has_column():
        return "category" in {row[1] for row in conn.execute("PRAGMA table_info(medication_event)")}

    if has_column():
        return
    from .policy import SENSITIVE, medication_category

    sensitive = {c.value for c in SENSITIVE}
    with write_txn(conn):
        if has_column():  # another connection migrated it while this one waited for the lock
            return
        conn.execute("ALTER TABLE medication_event ADD COLUMN category TEXT NOT NULL DEFAULT 'medication'")
        for row in conn.execute("SELECT id, drug, brand, source FROM medication_event").fetchall():
            category = medication_category(" ".join(str(x) for x in (row[1], row[2]) if x)).value
            ref = f"user:{row[0]}" if row[3] == "user" else str(row[0])
            indexed = conn.execute("SELECT category FROM search_doc WHERE kind='medication' AND ref=?", (ref,)).fetchone()
            if indexed and indexed[0] in sensitive:
                category = indexed[0]  # the index already filed it under a sensitive category: it keeps it
            conn.execute("UPDATE medication_event SET category=? WHERE id=?", (category, row[0]))
            if indexed and indexed[0] != category:  # the search index follows, so search and fetch agree
                conn.execute("UPDATE search_doc SET category=? WHERE kind='medication' AND ref=?", (category, ref))


def open_samples(path=None) -> sqlite3.Connection:
    from .paths import samples_path

    conn = connect(path or samples_path())
    apply_schema(conn, "samples")
    _migrate_samples(conn)
    return conn


def _migrate_samples(conn: sqlite3.Connection) -> None:
    def has_column():
        return "provider" in {row[1] for row in conn.execute("PRAGMA table_info(sample)")}

    if not has_column():
        with write_txn(conn):
            if not has_column():
                conn.execute("ALTER TABLE sample ADD COLUMN provider TEXT NOT NULL DEFAULT 'apple'")


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
