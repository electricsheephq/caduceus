"""Originals and import runs. A file is copied once (by sha256) and never overwritten; an import run
is recorded per (source, parser, version), so re-importing is a no-op and a crashed run resumes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ImportRun, RunStatus, SourceArtifact, SourceKind
from .db import db_dir, write_txn

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(row: sqlite3.Row) -> SourceArtifact:
    return SourceArtifact(
        id=row["id"], kind=row["kind"], sha256=row["sha256"], original_name=row["original_name"],
        stored_path=row["stored_path"], size_bytes=row["size_bytes"], added_at=row["added_at"],
    )


def store_source(conn: sqlite3.Connection, path, kind, *, subject_id: int = 1) -> tuple[SourceArtifact, bool]:
    """Copy *path* into ``sources/<sha256>/`` beside the vault and record it.

    Returns ``(artifact, existed)``. An identical file already in the vault is not copied again."""
    path = Path(path)
    kind = SourceKind(kind).value
    digest = sha256_file(path)
    row = conn.execute("SELECT * FROM source_artifact WHERE sha256=?", (digest,)).fetchone()
    if row is not None:
        return _artifact(row), True
    name = _SAFE_NAME.sub("_", path.name).strip("._") or "source"
    rel = Path("sources") / digest / name
    dest = db_dir(conn) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        # Each copy has its own temporary name: two imports of the same new file must not share one.
        fd, part = tempfile.mkstemp(dir=dest.parent, prefix=name + ".", suffix=".part")
        os.close(fd)
        try:
            shutil.copyfile(path, part)
            os.replace(part, dest)  # identical content either way, so the last rename wins harmlessly
        finally:
            if os.path.exists(part):
                os.unlink(part)
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO source_artifact(subject_id, kind, sha256, original_name, stored_path, "
            "size_bytes, added_at) VALUES (?,?,?,?,?,?,?)",
            (subject_id, kind, digest, path.name, rel.as_posix(), dest.stat().st_size, now_iso()),
        )
    row = conn.execute("SELECT * FROM source_artifact WHERE sha256=?", (digest,)).fetchone()
    return _artifact(row), False


def get_source(conn: sqlite3.Connection, source_id: int) -> SourceArtifact | None:
    row = conn.execute("SELECT * FROM source_artifact WHERE id=?", (source_id,)).fetchone()
    return _artifact(row) if row is not None else None


def source_file(conn: sqlite3.Connection, src: SourceArtifact) -> Path:
    """The stored copy of *src* (importers read this, never the user's original path)."""
    return db_dir(conn) / src.stored_path


def begin_import(conn: sqlite3.Connection, src: SourceArtifact, parser: str, version: str) -> ImportRun | None:
    """Start a run, or return None when this parser version already imported *src*.

    Runs left ``running`` by a crash or restart are marked ``interrupted`` first. Importers write
    with natural keys (INSERT OR IGNORE / upsert), so the new run resumes without duplicates."""
    started = now_iso()
    with write_txn(conn):
        done = conn.execute(
            "SELECT 1 FROM import_run WHERE source_id=? AND parser=? AND parser_version=? AND status='succeeded'",
            (src.id, parser, version),
        ).fetchone()
        if done:
            return None
        conn.execute(
            "UPDATE import_run SET status='interrupted', finished_at=? "
            "WHERE source_id=? AND parser=? AND status='running'",
            (started, src.id, parser),
        )
        cur = conn.execute(
            "INSERT INTO import_run(source_id, parser, parser_version, status, started_at) "
            "VALUES (?,?,?,'running',?)",
            (src.id, parser, version, started),
        )
        return ImportRun(id=cur.lastrowid, source_id=src.id, parser=parser, parser_version=version, started_at=started)


def update_progress(conn: sqlite3.Connection, run_id: int, progress: dict) -> None:
    with write_txn(conn):
        conn.execute("UPDATE import_run SET progress_json=? WHERE id=?", (json.dumps(progress, sort_keys=True), run_id))


def finish_import(conn: sqlite3.Connection, run_id: int, *, counts: dict, status: str, errors=()) -> None:
    status = RunStatus(status).value
    if status == RunStatus.RUNNING:
        raise ValueError("finish_import needs a final status")
    with write_txn(conn):
        conn.execute(
            "UPDATE import_run SET status=?, finished_at=?, counts_json=?, errors_json=? WHERE id=?",
            (status, now_iso(), json.dumps(counts, sort_keys=True), json.dumps([str(e) for e in errors][:50]), run_id),
        )


def run_info(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute(
        "SELECT r.*, s.kind, s.original_name FROM import_run r JOIN source_artifact s ON s.id=r.source_id "
        "WHERE r.id=?",
        (run_id,),
    ).fetchone()
    return _run_dict(row) if row is not None else None


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, s.kind, s.original_name FROM import_run r JOIN source_artifact s ON s.id=r.source_id "
        "ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_run_dict(row) for row in rows]


def running_run_for(conn: sqlite3.Connection, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM import_run WHERE source_id=? AND status='running' ORDER BY id DESC LIMIT 1", (source_id,)
    ).fetchone()
    return row["id"] if row is not None else None


def _run_dict(row: sqlite3.Row) -> dict:
    return {
        "import_run_id": row["id"], "source_id": row["source_id"], "file": row["original_name"],
        "kind": row["kind"], "parser": row["parser"], "parser_version": row["parser_version"],
        "status": row["status"], "started_at": row["started_at"], "finished_at": row["finished_at"],
        "counts": json.loads(row["counts_json"] or "{}"), "errors": json.loads(row["errors_json"] or "[]"),
        "progress": json.loads(row["progress_json"] or "{}"),
    }


def _peek_text(raw: bytes) -> str:
    return raw[:4096].decode("utf-8-sig", "replace")


def sniff_kind(path: Path) -> str:
    """Guess what a file is from its name and first bytes. Never parses it fully."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist()[:5000] if not i.is_dir()]
            names = [i.filename.lower() for i in infos]
            if any(n.endswith("export.xml") for n in names):
                return SourceKind.APPLE_HEALTH.value
            if any(n.endswith("promethease.html") for n in names):
                return SourceKind.PROMETHEASE.value
            texts = [i for i in infos if i.filename.lower().endswith((".txt", ".csv", ".tsv"))]
            if texts:
                with zf.open(texts[0]) as fh:
                    head = _peek_text(fh.read(4096))
                if _looks_dtc(head):
                    return SourceKind.DTC.value
            jsons = [i for i in infos if i.filename.lower().endswith(".json")]
            if jsons:
                with zf.open(jsons[0]) as fh:
                    if '"resourceType"' in _peek_text(fh.read(4096)):
                        return SourceKind.FHIR.value
        return SourceKind.OTHER.value
    with open(path, "rb") as fh:
        head = _peek_text(fh.read(4096))
    if _looks_dtc(head):
        return SourceKind.DTC.value
    lowered = path.name.lower()
    if lowered.endswith((".html", ".htm")) and ("promethease" in lowered or "promethease" in head.lower()):
        return SourceKind.PROMETHEASE.value
    if '"resourceType"' in head:
        return SourceKind.FHIR.value
    if "<HealthData" in head:
        return SourceKind.APPLE_HEALTH.value
    return SourceKind.OTHER.value


def _looks_dtc(head: str) -> bool:
    lowered = head.lower()
    return ("23andme" in lowered or "ancestrydna" in lowered or lowered.lstrip("# \ufeff").startswith("rsid")
            or "\nrsid\t" in lowered or "# rsid" in lowered)
