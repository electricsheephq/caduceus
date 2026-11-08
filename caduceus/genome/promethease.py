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
MAX_RECORDS_PER_CHUNK = 1000
MAX_KEYS_PER_CHUNK = 10000
MAX_RECORDS_TOTAL = 500000
MAX_GENOTYPE_INFO_TOTAL = 500000
_CHUNK = re.compile(r"decompressString\('([A-Za-z0-9+/=]+)'\)")
_CONTEXT = re.compile(r"mygenos\.push\.apply\s*\(|var\s+(rsinfo|othersinfo)\s*=\s*\$\.extend\s*\(")
_SCALAR = re.compile(
    r'''["']?([\w]+)["']?\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'''
)
_DRUG = re.compile(r"\b(drugs?|medications?|medicines?|dose|dosage|statins?|warfarin|clopidogrel|metabolism|metaboli[sz]er)\b", re.I)
_PGX_GENES = frozenset("CYP2C19 CYP2C9 CYP2D6 CYP3A5 CYP3A4 CYP1A2 CYP2B6 VKORC1 SLCO1B1 TPMT NUDT15 DPYD "
                       "UGT1A1 IFNL3 HLA-A HLA-B G6PD NAT2 ABCG2 RYR1 CACNA1S MT-RNR1".split())


class _Warnings(list):
    """Retain only the first 20 safe messages and a count of the remainder."""
    def __init__(self, values=()):
        super().__init__(values)
        self.total = len(self)

    def add(self, message):
        self.total += 1
        if self.total <= 20:
            self.append(message)
        elif self.total == 21:
            self.append("1 additional warnings suppressed")
        else:
            self[-1] = f"{self.total - 20} additional warnings suppressed"


class ReportTooLarge(ValueError):
    """An HTML member or decompressed payload exceeded its configured limit."""


@dataclass
class DecodedReport:
    records: list[dict] = field(default_factory=list)
    # Qualified keys preserve a marker appearing in both maps. Values carry the set tag.
    genotype_info: dict[str, dict] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    chunks: int = 0
    warnings: list = field(default_factory=_Warnings)


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
    return raw


def _chunk_items(raw, *, mapping, limit):
    """Parse one top-level item at a time, refusing excess objects before allocation."""
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    opening, closing = ("{", "}") if mapping else ("[", "]")

    def whitespace(pos):
        while pos < len(text) and text[pos] in " \r\n\t":
            pos += 1
        return pos

    pos = whitespace(0)
    if text[pos:pos + 1] != opening:
        raise ValueError("incorrect chunk shape")
    pos, count = whitespace(pos + 1), 0
    while text[pos:pos + 1] != closing:
        if count >= limit:
            raise ReportTooLarge("report object count exceeds size limit")
        key = None
        if mapping:
            key, pos = decoder.raw_decode(text, pos)
            pos = whitespace(pos)
            if not isinstance(key, str) or text[pos:pos + 1] != ":":
                raise ValueError("invalid map key")
            pos = whitespace(pos + 1)
        value, pos = decoder.raw_decode(text, pos)
        count += 1
        yield key, value
        pos = whitespace(pos)
        if text[pos:pos + 1] == closing:
            break
        if text[pos:pos + 1] != ",":
            raise ValueError("invalid chunk separator")
        pos = whitespace(pos + 1)
        if text[pos:pos + 1] == closing:
            raise ValueError("trailing chunk comma")
    if text[pos:pos + 1] != closing or text[whitespace(pos + 1):]:
        raise ValueError("incomplete or trailing JSON data")


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
    raw_html = _read_html(path, min(64 << 20, max_total))
    warnings = _Warnings()
    try:
        html = raw_html.decode("utf-8-sig")
    except UnicodeDecodeError:
        html = raw_html.decode("utf-8-sig", errors="replace")
        warnings.add("HTML contains non-UTF-8 bytes; replaced")
    decoded = DecodedReport(metadata=_metadata(html), warnings=warnings)
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
            set_name = context[1]
            if set_name is None:
                limit = min(MAX_RECORDS_PER_CHUNK, MAX_RECORDS_TOTAL - len(decoded.records))
                value = [row for _, row in _chunk_items(raw, mapping=False, limit=limit)]
                if not all(isinstance(row, dict) for row in value):
                    raise ValueError("record chunk must be a list of objects")
                for ordinal, record in enumerate(value, len(decoded.records)):
                    _record_warnings(record, ordinal, warnings)
                decoded.records.extend(value)
            else:
                value, new_keys = {}, set()
                for marker, genotypes in _chunk_items(raw, mapping=True, limit=MAX_KEYS_PER_CHUNK):
                    key = f"{set_name}:{marker}"
                    if key not in decoded.genotype_info:
                        new_keys.add(key)
                    if len(decoded.genotype_info) + len(new_keys) > MAX_GENOTYPE_INFO_TOTAL:
                        raise ReportTooLarge("genotype-info count exceeds size limit")
                    value[key] = {"set": set_name, "genotypes": genotypes}
                decoded.genotype_info.update(value)
        except ReportTooLarge:
            raise
        except (ValueError, zlib.error, UnicodeError, RecursionError) as exc:
            warnings.add(f"chunk {decoded.chunks}: {type(exc).__name__}")
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


def _string(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        values = value.values() if isinstance(value, dict) else value
        return " ".join(part for item in values if (part := _string(item)))
    return str(value)


def _marker(record):
    value = record.get("rsnum")
    return value if isinstance(value, str) and re.fullmatch(r"(?:rs|i)\d+", value, re.ASCII) else None


def _alias(record):
    value = record.get("redirect_from")
    return _string(value) if not isinstance(value, (list, dict)) else None


def _record_warnings(record, ordinal, warnings):
    for name in ("rsnum", "geno", "title", "genosummary", "summary", "redirect_from", "magnitude", "repute"):
        if isinstance(record.get(name), (list, dict)) or (name == "rsnum" and name in record and _marker(record) is None):
            warnings.add(f"record {ordinal}: invalid field {name}")


def _magnitude(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _kind(record):
    if "rsnum" in record:
        return "snp" if _marker(record) else "other"
    return "genoset" if _string(record.get("title")) else "other"


def _repute(record):
    value = record.get("repute")
    return value if value in ("Good", "Bad") else None


def _body(record):
    # Prioritize annotation text for the search excerpt; classify using the full text.
    keys = ("title", "summary", "genosummary", "genobody", "rstext", "topic", "cond", "med")
    values = [record[k] for k in keys if k in record]
    values.extend(value for key, value in record.items() if key not in keys)
    return " ".join(_plain(_string(value)) for value in values if value is not None)


def _category(record, text=None):
    text = _body(record) if text is None else text
    pgx = any(g.strip().upper() in _PGX_GENES for g in _genes(record))
    default = Category.PGX if record.get("med") or _DRUG.search(text) or pgx else Category.GENERAL
    return categorize(genes=_genes(record), text=text, default=default)


def _summary(records, info):
    """Shared comparison vocabulary; callers supply independent representations."""
    kinds = Counter(_kind(row) for row in records)
    kinds["genotype_info"] = len(info)
    return {
        "records": len(records), "by_kind": {kind: kinds[kind] for kind in ("snp", "genoset", "other", "genotype_info")},
        "magnitude_hist": dict(sorted(Counter(str(_magnitude(r.get("magnitude"))) for r in records).items())),
        "repute_hist": dict(sorted(Counter(_repute(r) or "None" for r in records).items())),
        "pairs": sorted({(_marker(r) or _string(r.get("title")) or "", _string(r.get("geno")) or "") for r in records}),
    }


def _snapshot_rows(decoded):
    by_marker = {}
    for ordinal, record in enumerate(decoded.records):
        marker, title = _marker(record), _string(record.get("title"))
        text = _body(record)
        category = _category(record, text)
        by_marker.setdefault(marker or title, []).append((record, text, category))
        yield (ordinal, _kind(record), marker, title, _string(record.get("geno")),
               _magnitude(record.get("magnitude")), _repute(record), _string(record.get("genosummary", record.get("summary"))),
               _json(_genes(record)), category.value, _json(record), record, text)
    for ordinal, (key, info) in enumerate(sorted(decoded.genotype_info.items(), key=lambda item: (
        item[1]["set"] != "rsinfo", item[0].split(":", 1)[1]
    )), len(decoded.records)):
        marker = key.split(":", 1)[1]
        related = by_marker.get(marker, [])
        category = categorize(genes=[g for r, _, _ in related for g in _genes(r)],
                              text=" ".join(text for _, text, _ in related),
                              default=Category.PGX if any(cat == Category.PGX for _, _, cat in related) else Category.GENERAL)
        yield (ordinal, "genotype_info", marker, None, None, None, None, None, "[]", category.value, _json(info), None, "")


def import_promethease(conn, src, *, cancel=None) -> ImportResult:
    run = sources.begin_import(conn, src, PARSER, PARSER_VERSION)
    if run is None:
        return ImportResult(status="skipped", source_id=src.id)
    counts, warnings = {}, _Warnings()
    try:
        if cancel is not None and cancel.is_set():
            sources.finish_import(conn, run.id, counts=counts, status="cancelled")
            return ImportResult("cancelled", run.id, src.id, counts)
        decoded = decode_report(sources.source_file(conn, src))
        warnings = decoded.warnings
        if not decoded.records:
            message = "No records found in the report"
            warnings.add(message)
            errors = warnings if message in warnings else [message, *warnings]
            sources.finish_import(conn, run.id, counts=counts, status="failed", errors=errors)
            return ImportResult("failed", run.id, src.id, counts, warnings)
        summary = _summary(decoded.records, decoded.genotype_info)
        aliases = {(_alias(r), _marker(r)) for r in decoded.records if _alias(r) and _marker(r)}
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
        warnings.add(f"import failed: {type(exc).__name__}")
        sources.finish_import(conn, run.id, counts=counts, status="failed", errors=warnings)
        return ImportResult("failed", run.id, src.id, counts, warnings)


def _write_batch(conn, source_id, batch):
    with write_txn(conn):
        for row in batch:
            conn.execute(
                "INSERT OR IGNORE INTO annotation_snapshot(source_id, ordinal, kind, marker, title, geno, "
                "magnitude, repute, summary, genes_json, category, record_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, *row[:11]),
            )
            record = row[11]
            if record is None:
                continue
            if _alias(record) and _marker(record):
                conn.execute("INSERT OR IGNORE INTO marker_alias(alias, marker, source) VALUES (?,?,'promethease')",
                             (_alias(record), _marker(record)))
            title = " ".join(str(v) for v in (row[2] or row[3], row[4],
                                              *_genes(record)) if v)
            search.index_doc(conn, kind="annotation", ref=f"{source_id}:{row[0]}", title=title,
                             body=row[12][:2000], category=row[9])
