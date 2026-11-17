"""The search index: one ``search_doc`` row per searchable record, mirrored into FTS5 by triggers.

Writers call ``index_doc`` inside their own write transaction. ``search`` filters every hit through
the privacy policy and reports how many it withheld, never what they were.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .policy import Category, Policy, text_visible, visible

_TOKEN = re.compile(r"[\w][\w.'*-]*", re.UNICODE)
KINDS = ("annotation", "assertion", "research", "observation", "condition", "medication", "report", "journal", "finding", "pgx", "phenotype")


def _visible(conn, policy, row, resolver):
    # Reports share the records base category, and cannot repeat any currently hidden linked result or study.
    kind = row["kind"]
    if not visible(policy, row["category"], kind="observation" if kind == "report" else kind):
        return False
    if kind in {"journal", "phenotype", "medication", "report"} and not text_visible(
            policy, f"{row['title']} {row['body']}"):
        return False
    if kind != "report":
        return True
    report = conn.execute("SELECT json_extract(raw_json,'$._caduceus') AS metadata FROM clinical_report WHERE id=?",
                          (row["ref"],)).fetchone()
    if report is None:
        return False
    metadata = json.loads(report["metadata"] or "{}")
    linked = [*resolver.resolve(metadata.get("result"), broad=True),
              *resolver.resolve(metadata.get("imagingStudy"), broad=True, resource_type="ImagingStudy")]
    return all(visible(policy, row["category"], kind="observation") for row in linked)


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


def medication_text(conn, row) -> str:
    """All stored medication words, including FHIR note/reason text kept only in the index."""
    ref = f"user:{row['id']}" if row["source"] == "user" else str(row["id"])
    doc = conn.execute("SELECT body FROM search_doc WHERE kind='medication' AND ref=?", (ref,)).fetchone()
    return " ".join([*(row[key] or "" for key in ("drug", "brand", "note")), doc["body"] if doc else ""])


def _query(conn, match: str, kinds, limit: int):
    sql = (
        "SELECT d.id, d.kind, d.ref, d.title, d.body, d.category, d.ts, "
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


def search(conn: sqlite3.Connection, query: object, *, policy: Policy, kinds=None, limit: int = 8, allow=None) -> dict:
    """Ranked hits allowed by *policy*: all terms first, any term when that finds nothing."""
    from ..health.fhir import ReportResolver

    resolver = ReportResolver(conn)
    if isinstance(kinds, str):
        kinds = [kinds]
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
        if not _visible(conn, policy, row, resolver) or (allow is not None and not allow(row)):
            withheld += 1
            continue
        if len(results) < limit:
            results.append({
                "ref": f"{row['kind']}:{row['ref']}", "kind": row["kind"], "title": row["title"],
                "snippet": row["snip"], "category": row["category"], "date": row["ts"],
            })
    return {"results": results, "withheld": withheld, "match": mode}


def browse(conn: sqlite3.Connection, *, policy: Policy, kinds, limit: int = 8, allow=None) -> dict:
    """List recorded kinds newest first, filtering and counting hidden rows before limiting visible hits."""
    from ..health.fhir import ReportResolver

    resolver = ReportResolver(conn)
    if isinstance(kinds, str):
        kinds = [kinds]
    kinds = [k for k in (kinds or []) if k in KINDS]
    limit = max(1, min(int(limit or 8), 25))
    results, withheld = [], 0
    if kinds:
        rows = conn.execute(
            "SELECT kind, ref, title, body, category, ts FROM search_doc "
            f"WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY ts DESC, id DESC", kinds)
        for row in rows:
            if not _visible(conn, policy, row, resolver) or (allow is not None and not allow(row)):
                withheld += 1
                continue
            if len(results) < limit:
                results.append({
                    "ref": f"{row['kind']}:{row['ref']}", "kind": row["kind"], "title": row["title"],
                    "snippet": row["body"][:240], "category": row["category"], "date": row["ts"],
                })
    return {"results": results, "withheld": withheld, "match": "browse"}


def _linked_rows(conn, references, *, source_ids, full_url, resource_type, policy, resolver):

    results, withheld = [], 0
    linked = resolver.resolve(references, source_ids=source_ids, full_url=full_url, resource_type=resource_type)
    table = {"Observation": "observation", "ImagingStudy": "clinical_report"}[resource_type]
    rows = [conn.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone() for row in linked]
    for row in sorted(rows, key=lambda row: (row["effective_at"] or "", row["id"])):
        document = json.loads(row["raw_json"] or "{}")
        kind = "observation" if resource_type == "Observation" else "report"
        if not visible(policy, row["category"], kind="observation"):
            withheld += 1
            continue
        if kind == "report" and not text_visible(
                policy, " ".join(row[key] or "" for key in ("name", "conclusion", "narrative"))):
            withheld += 1
            continue
        result = {"ref": f"{kind}:{row['id']}", "title": row["name"], "date": row["effective_at"],
                  "category": row["category"], "status": document.get("status")}
        if resource_type == "Observation":
            result.update({key: row[key] for key in ("name", "value_num", "value_text", "unit", "code", "code_system")})
        else:
            meta = document.get("_caduceus", {})
            result.update({key: meta[key] for key in ("modalities", "body_sites", "numberOfSeries", "numberOfInstances")
                           if key in meta})
        results.append(result)
    return results, withheld


def _fetch_report(conn, key, *, policy, resolver):
    from ..health.fhir import report_source_ids

    row = conn.execute("SELECT * FROM clinical_report WHERE id=?", (key,)).fetchone()
    if row is None:
        return None
    document = json.loads(row["raw_json"] or "{}")
    metadata = document.get("_caduceus", {})
    results, withheld = _linked_rows(conn, metadata.get("result"),
                                   source_ids=report_source_ids(metadata, row["source_id"]), full_url=metadata.get("full_url"),
                                   resource_type="Observation", policy=policy, resolver=resolver)
    imaging, hidden_imaging = _linked_rows(conn, metadata.get("imagingStudy"),
                                         source_ids=report_source_ids(metadata, row["source_id"]), full_url=metadata.get("full_url"),
                                         resource_type="ImagingStudy", policy=policy, resolver=resolver)
    return {"status": row["status"], "conclusion": row["conclusion"], "narrative": row["narrative"],
            "attachments": metadata.get("attachments", []), "results": results, "imaging": imaging,
            "withheld": withheld + hidden_imaging}


def fetch(conn: sqlite3.Connection, ref: str, *, policy: Policy, allow=None) -> dict | None:
    """One indexed record in full, by the ``kind:ref`` a search returned. None when absent or withheld."""
    from ..health.fhir import ReportResolver

    resolver = ReportResolver(conn)
    kind, _, key = str(ref or "").partition(":")
    row = conn.execute(
        "SELECT kind, ref, title, body, category, ts FROM search_doc WHERE kind=? AND ref=?", (kind, key)
    ).fetchone()
    if row is None:
        return None
    if not _visible(conn, policy, row, resolver) or (allow is not None and not allow(row)):
        return {"ref": ref, "withheld": True, "reason": "hidden by your privacy settings"}
    result = {"ref": ref, "kind": row["kind"], "title": row["title"], "body": row["body"],
              "category": row["category"], "date": row["ts"]}
    if kind == "report":
        report = _fetch_report(conn, key, policy=policy, resolver=resolver)
        if report is None:
            return None
        result.update(report)
    return result
