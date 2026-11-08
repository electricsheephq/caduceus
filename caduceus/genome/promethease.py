"""Decode Promethease's compressed data without executing its JavaScript."""
from __future__ import annotations

import base64
import json
import math
import re
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from ..core import search, sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from ..core.policy import Category, categorize

PARSER = "promethease"
PARSER_VERSION = "1"
_CHUNK = re.compile(r"decompressString\('([A-Za-z0-9+/=]+)'\)")
_CONTEXT = re.compile(r"mygenos\.push\.apply\s*\(|var\s+(rsinfo|othersinfo)\s*=\s*\$\.extend\s*\(")
_SCALAR = re.compile(
    r'''["']?([\w]+)["']?\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'''
)
_DRUG = re.compile(r"\b(drugs?|medications?|medicines?|dose|dosage|statins?|warfarin|clopidogrel|metabolism)\b", re.I)


class ReportTooLarge(ValueError):
    """An HTML member or decompressed payload exceeded its configured limit."""


@dataclass
class DecodedReport:
    records: list[dict] = field(default_factory=list)
    # Qualified keys preserve a marker appearing in both maps. Values carry the set tag.
    genotype_info: dict[str, dict] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    chunks: int = 0
    warnings: list = field(default_factory=list)


def _read_html(path, cap):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            for member in members:
                name = member.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                    raise ValueError("unsafe zip member path")
                if member.file_size > cap:
                    raise ReportTooLarge("zip member exceeds size limit")
            html = [m for m in members if not m.is_dir() and m.filename.lower().endswith((".html", ".htm"))]
            if not html:
                raise ValueError("zip contains no HTML report")
            html.sort(key=lambda m: (PurePosixPath(m.filename).name.lower() != "promethease.html", m.filename))
            with archive.open(html[0]) as stream:
                raw = stream.read(cap + 1)
    else:
        with path.open("rb") as stream:
            raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise ReportTooLarge("HTML exceeds size limit")
    return raw.decode("utf-8-sig")


def _metadata(html):
    start = re.search(r"var\s+mygenos\s*=\s*\[\s*\]", html)
    if start is None:
        return {}
    literal = html[html.rfind("{", 0, start.start()):start.start()]
    result = {}
    for match in _SCALAR.finditer(literal):
        key, value = match.groups()
        if value.startswith("'"):
            result[key] = re.sub(r"\\(['\\])", r"\1", value[1:-1])
        else:
            try:
                result[key] = json.loads(value)
            except ValueError:
                continue
    return result


def decode_report(path, *, max_inflate=64 << 20, max_total=1 << 30) -> DecodedReport:
    """Read list and map chunks by offset; malformed chunks warn and are skipped.

    HTML/zip members have a 64 MiB cap, also bounded by max_total. max_inflate
    bounds each chunk and max_total bounds the sum of inflated chunk bytes.
    """
    if max_inflate <= 0 or max_total <= 0:
        raise ValueError("inflate limits must be positive")
    html = _read_html(path, min(64 << 20, max_total))
    decoded = DecodedReport(metadata=_metadata(html))
    contexts = list(_CONTEXT.finditer(html))
    context_index, total = -1, 0
    for chunk in _CHUNK.finditer(html):
        decoded.chunks += 1
        while context_index + 1 < len(contexts) and contexts[context_index + 1].start() < chunk.start():
            context_index += 1
        try:
            if context_index < 0:
                raise ValueError("chunk outside data expression")
            context = contexts[context_index]
            end = html.find(";", context.end())
            if end != -1 and chunk.start() > end:
                raise ValueError("chunk outside data expression")
            inflater = zlib.decompressobj(15 | 32)
            limit = min(max_inflate, max_total - total)
            raw = inflater.decompress(base64.b64decode(chunk[1], validate=True), limit + 1)
            if len(raw) > limit:
                raise ReportTooLarge("inflated report exceeds size limit")
            total += len(raw)
            if not inflater.eof or inflater.unused_data:
                raise ValueError("incomplete or trailing compressed data")
            value = json.loads(raw)
            set_name = context[1]
            if set_name is None:
                if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
                    raise ValueError("record chunk must be a list of objects")
                decoded.records.extend(value)
            else:
                if not isinstance(value, dict):
                    raise ValueError("genotype chunk must be an object")
                for marker, genotypes in value.items():
                    decoded.genotype_info[f"{set_name}:{marker}"] = {"set": set_name, "genotypes": genotypes}
        except ReportTooLarge:
            raise
        except (ValueError, zlib.error, UnicodeError, RecursionError) as exc:
            decoded.warnings.append(f"chunk {decoded.chunks}: {type(exc).__name__}")
    return decoded


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain(value):
    parser = _Text()
    parser.feed(str(value or ""))
    return " ".join(" ".join(parser.parts).split())


def _genes(record):
    genes = record.get("genes") or []
    return [str(g) for g in genes] if isinstance(genes, list) else [str(genes)]


def _magnitude(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _kind(record):
    return "snp" if record.get("rsnum") else "genoset" if record.get("title") else "other"


def _repute(record):
    value = record.get("repute")
    return value if value in ("Good", "Bad") else None


def _body(record):
    values = [record.get(k) for k in ("genosummary", "genobody", "rstext")]
    for key in ("topic", "cond", "med"):
        value = record.get(key) or []
        values.extend(value if isinstance(value, list) else [value])
    return " ".join(_plain(v) for v in values if v)


def _category(record):
    text = _body(record)
    default = Category.PGX if record.get("med") or _DRUG.search(text) else Category.GENERAL
    return categorize(genes=_genes(record), text=text, default=default)


def _summary(records, info):
    """Shared comparison vocabulary; callers supply independent representations."""
    kinds = Counter(_kind(row) for row in records)
    kinds["genotype_info"] = len(info)
    return {
        "records": len(records), "by_kind": {kind: kinds[kind] for kind in ("snp", "genoset", "other", "genotype_info")},
        "magnitude_hist": dict(sorted(Counter(str(_magnitude(r.get("magnitude"))) for r in records).items())),
        "repute_hist": dict(sorted(Counter(_repute(r) or "None" for r in records).items())),
        "pairs": sorted({(str(r.get("rsnum") or r.get("title") or ""), str(r.get("geno") or "")) for r in records}),
    }


def _snapshot_rows(decoded):
    for ordinal, record in enumerate(decoded.records):
        yield (ordinal, _kind(record), record.get("rsnum"), record.get("title"), record.get("geno"),
               _magnitude(record.get("magnitude")), _repute(record), record.get("genosummary"),
               _json(_genes(record)), _category(record).value, _json(record), record)
    by_marker = {}
    for record in decoded.records:
        marker = record.get("rsnum") or record.get("title")
        by_marker.setdefault(marker, []).append(record)
    for ordinal, (key, info) in enumerate(sorted(decoded.genotype_info.items(), key=lambda item: (
        item[1]["set"] != "rsinfo", item[0].split(":", 1)[1]
    )), len(decoded.records)):
        marker = key.split(":", 1)[1]
        related = by_marker.get(marker, [])
        category = categorize(genes=[g for r in related for g in _genes(r)],
                              text=" ".join(_body(r) for r in related),
                              default=Category.PGX if any(_category(r) == Category.PGX for r in related) else Category.GENERAL)
        yield (ordinal, "genotype_info", marker, None, None, None, None, None, "[]", category.value, _json(info), None)


def import_promethease(conn, src, *, cancel=None) -> ImportResult:
    run = sources.begin_import(conn, src, PARSER, PARSER_VERSION)
    if run is None:
        return ImportResult(status="skipped", source_id=src.id)
    counts, warnings = {}, []
    try:
        if cancel is not None and cancel.is_set():
            sources.finish_import(conn, run.id, counts=counts, status="cancelled")
            return ImportResult("cancelled", run.id, src.id, counts)
        decoded = decode_report(sources.source_file(conn, src))
        warnings = decoded.warnings
        summary = _summary(decoded.records, decoded.genotype_info)
        aliases = {(str(r["redirect_from"]), str(r["rsnum"])) for r in decoded.records
                   if r.get("redirect_from") and r.get("rsnum")}
        counts = {"records": summary["records"], **{k: summary["by_kind"][k] for k in ("snp", "genoset", "other")},
                  "genotype_info_markers": len(decoded.genotype_info), "chunks": decoded.chunks, "aliases": len(aliases),
                  "magnitude_hist": summary["magnitude_hist"], "repute_hist": summary["repute_hist"]}
        batch, written = [], 0
        for row in _snapshot_rows(decoded):
            batch.append(row)
            if len(batch) == 2000:
                if cancel is not None and cancel.is_set():
                    break
                _write_batch(conn, src.id, batch)
                written += len(batch)
                batch = []
                if written % 10000 == 0:
                    sources.update_progress(conn, run.id, {"rows": written})
        else:
            if cancel is None or not cancel.is_set():
                if batch:
                    _write_batch(conn, src.id, batch)
                sources.finish_import(conn, run.id, counts=counts, status="succeeded", errors=warnings)
                return ImportResult("succeeded", run.id, src.id, counts, warnings)
        sources.finish_import(conn, run.id, counts=counts, status="cancelled", errors=warnings)
        return ImportResult("cancelled", run.id, src.id, counts, warnings)
    except Exception as exc:
        # Do not echo input records or paths into diagnostics.
        warnings = [*warnings, f"import failed: {type(exc).__name__}"]
        sources.finish_import(conn, run.id, counts=counts, status="failed", errors=warnings)
        return ImportResult("failed", run.id, src.id, counts, warnings)


def _write_batch(conn, source_id, batch):
    with write_txn(conn):
        for row in batch:
            conn.execute(
                "INSERT OR IGNORE INTO annotation_snapshot(source_id, ordinal, kind, marker, title, geno, "
                "magnitude, repute, summary, genes_json, category, record_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, *row[:-1]),
            )
            record = row[-1]
            if record is None:
                continue
            if record.get("redirect_from") and record.get("rsnum"):
                conn.execute("INSERT OR IGNORE INTO marker_alias(alias, marker, source) VALUES (?,?,'promethease')",
                             (record["redirect_from"], record["rsnum"]))
            title = " ".join(str(v) for v in (record.get("rsnum") or record.get("title"), record.get("geno"),
                                              *_genes(record)) if v)
            search.index_doc(conn, kind="annotation", ref=f"{source_id}:{row[0]}", title=title,
                             body=_body(record)[:2000], category=row[9])
