"""The search index: one ``search_doc`` row per searchable record, mirrored into FTS5 by triggers.

Writers call ``index_doc`` inside their own write transaction. ``search`` filters every hit through
the privacy policy and reports how many it withheld, never what they were.
"""
from __future__ import annotations

import re
import sqlite3

from .policy import Category, Policy, allowed

_TOKEN = re.compile(r"[\w][\w.'*-]*", re.UNICODE)
KINDS = ("annotation", "assertion", "research", "observation", "condition", "medication", "journal", "finding", "pgx")


def sanitize(query: object, *, max_terms: int = 16) -> str:
    """Free text to a safe FTS5 query: every token quoted (so FTS syntax in user text is inert)."""
    tokens = _TOKEN.findall(str(query or ""))[:max_terms]
    cleaned = [t.strip(".'-*") for t in tokens]
    return " ".join('"' + t.replace('"', '""') + '"' for t in cleaned if t)


def index_doc(conn: sqlite3.Connection, *, kind: str, ref: str, title: str, body: str, category, ts=None) -> None:
    """Insert or replace one searchable record. Call inside ``write_txn``."""
    if kind not in KINDS:
        raise ValueError(f"unknown search kind {kind!r}")
    category = Category(category).value
    conn.execute(
        "INSERT INTO search_doc(kind, ref, title, body, category, ts) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(kind, ref) DO UPDATE SET title=excluded.title, body=excluded.body, "
        "category=excluded.category, ts=excluded.ts",
        (kind, ref, title or "", body or "", category, ts),
    )


def remove_doc(conn: sqlite3.Connection, *, kind: str, ref: str) -> None:
    conn.execute("DELETE FROM search_doc WHERE kind=? AND ref=?", (kind, ref))


def _query(conn, match: str, kinds, limit: int):
    sql = (
        "SELECT d.id, d.kind, d.ref, d.title, d.category, d.ts, "
        "snippet(search_fts, 1, '[', ']', '…', 16) AS snip, bm25(search_fts, 4.0, 1.0) AS score "
        "FROM search_fts JOIN search_doc d ON d.id = search_fts.rowid WHERE search_fts MATCH ?"
    )
    params: list = [match]
    if kinds:
        sql += f" AND d.kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def search(conn: sqlite3.Connection, query: object, *, policy: Policy, kinds=None, limit: int = 8) -> dict:
    """Ranked hits allowed by *policy*: all terms first, any term when that finds nothing."""
    kinds = [k for k in (kinds or []) if k in KINDS]
    limit = max(1, min(int(limit or 8), 25))
    match = sanitize(query)
    if not match:
        return {"results": [], "withheld": 0, "query": match}
    rows = _query(conn, match, kinds, limit * 5)
    mode = "all_terms"
    if not rows and " " in match:
        rows = _query(conn, " OR ".join(match.split(" ")), kinds, limit * 5)
        mode = "any_term"
    results, withheld = [], 0
    for row in rows:
        if not allowed(policy, row["category"]):
            withheld += 1
            continue
        if len(results) < limit:
            results.append({
                "ref": f"{row['kind']}:{row['ref']}", "kind": row["kind"], "title": row["title"],
                "snippet": row["snip"], "category": row["category"], "date": row["ts"],
            })
    return {"results": results, "withheld": withheld, "match": mode}


def fetch(conn: sqlite3.Connection, ref: str, *, policy: Policy) -> dict | None:
    """One indexed record in full, by the ``kind:ref`` a search returned. None when absent or withheld."""
    kind, _, key = str(ref or "").partition(":")
    row = conn.execute(
        "SELECT kind, ref, title, body, category, ts FROM search_doc WHERE kind=? AND ref=?", (kind, key)
    ).fetchone()
    if row is None:
        return None
    if not allowed(policy, row["category"]):
        return {"ref": ref, "withheld": True, "reason": "hidden by your privacy settings"}
    return {"ref": ref, "kind": row["kind"], "title": row["title"], "body": row["body"],
            "category": row["category"], "date": row["ts"]}
