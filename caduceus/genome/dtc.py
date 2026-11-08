"""Stream consumer DNA exports into the local vault; never read the original path."""
from __future__ import annotations

import re
import zipfile
from contextlib import contextmanager
from pathlib import PurePosixPath

from ..core import sources
from ..core.contracts import ImportResult
from ..core.db import write_txn

PARSER = "dtc"
PARSER_VERSION = "1"
BATCH_SIZE = 20_000
MAX_INFLATE = 1 << 30
_MARKER = re.compile(r"(?:rs|i)[0-9]+\Z")
_GENOTYPE = re.compile(r"(?:[ACGT]{1,2}|[DI]{1,2}|--)\Z")
_BUILD = re.compile(r"(?:build\s*|grch\s*|assembly\s*(?:build\s*)?)(37|38)\b", re.I)
_CHROMOSOMES = frozenset(map(str, range(1, 23))) | {"X", "Y", "MT"}
_ANCESTRY_CHROMS = {"23": "X", "24": "Y", "25": "XY", "26": "MT"}
_INSERT = "INSERT OR IGNORE INTO genotype_call(callset_id, marker, chrom, pos, genotype) VALUES (?,?,?,?,?)"


def _lines(raw, max_inflate):
    used = 0
    while True:
        line = raw.readline(max_inflate - used + 1)
        if not line:
            return
        used += len(line)
        if used > max_inflate:
            raise ValueError("DTC decompressed byte limit exceeded")
        yield line.decode("utf-8-sig").rstrip("\r\n")


@contextmanager
def _open_text(path, *, max_inflate=MAX_INFLATE):
    """Yield (lines, member name), bounded by actual decompressed bytes, without extraction."""
    if max_inflate < 0:
        raise ValueError("DTC byte limit must be nonnegative")
    if not zipfile.is_zipfile(path):
        with open(path, "rb") as raw:
            yield _lines(raw, max_inflate), path.name
        return
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        for member in members:
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in PurePosixPath(name).parts or ":" in name:
                raise ValueError("Unsafe DTC archive member path")
        texts = [m for m in members if not m.is_dir() and m.filename.lower().endswith(".txt")]
        if not texts:
            raise ValueError("DTC archive has no text member")
        member = max(texts, key=lambda m: m.file_size)
        if member.file_size > max_inflate:
            raise ValueError("DTC decompressed byte limit exceeded")
        with archive.open(member) as raw:
            yield _lines(raw, max_inflate), member.filename


def _header(lines):
    build = "unknown"
    for number, line in lines:
        text = line.strip()
        match = _BUILD.search(text) if text.startswith("#") else None
        if match:
            build = match[1]
        fields = text.lstrip("# ").lower().split("\t")
        if fields == ["rsid", "chromosome", "position", "genotype"]:
            return "23andMe", build
        if fields == ["rsid", "chromosome", "position", "allele1", "allele2"]:
            return "AncestryDNA", build
        if text and not text.startswith("#"):
            raise ValueError("Unrecognized DTC header")
    raise ValueError("Missing DTC header")


def _parse(line, vendor):
    fields = line.strip().split("\t")
    if len(fields) != (5 if vendor == "AncestryDNA" else 4):
        return None
    marker, chrom, position = fields[:3]
    marker, chrom = marker.lower(), chrom.upper()
    if (not _MARKER.fullmatch(marker) or len(position) > 19
            or not position.isascii() or not position.isdecimal()):
        return None
    pos = int(position)
    if not 0 < pos <= 2**63 - 1:
        return None
    if vendor == "AncestryDNA":
        chrom = _ANCESTRY_CHROMS.get(chrom, chrom)
        alleles = [a.upper() for a in fields[3:]]
        if any(a not in {"A", "C", "G", "T", "D", "I", "0"} for a in alleles):
            return None
        genotype = "--" if "0" in alleles else "".join(alleles)
        valid_chroms = _CHROMOSOMES | {"XY"}
    else:
        genotype = fields[3].upper()
        valid_chroms = _CHROMOSOMES
    if chrom not in valid_chroms or not _GENOTYPE.fullmatch(genotype):
        return None
    return marker, chrom, pos, genotype


def _stored_counts(conn, callset_id):
    counts = dict(markers=0, rs_ids=0, internal_ids=0, no_calls=0, haploid=0,
                  indel_codes=0, duplicates=0, skipped_lines=0, chromosomes={})
    rows = conn.execute(
        "SELECT chrom, count(*) AS n, sum(marker LIKE 'rs%') AS rs, "
        "sum(marker LIKE 'i%') AS internal, sum(genotype IN ('--', '')) AS nc, "
        "sum(length(genotype)=1) AS hap, sum(genotype GLOB '*[DI]*') AS indel "
        "FROM genotype_call WHERE callset_id=? GROUP BY chrom", (callset_id,)
    )
    for row in rows:
        for key, column in (("markers", "n"), ("rs_ids", "rs"), ("internal_ids", "internal"),
                            ("no_calls", "nc"), ("haploid", "hap"), ("indel_codes", "indel")):
            counts[key] += row[column]
        counts["chromosomes"][row["chrom"]] = row["n"]
    return counts


def _flush(conn, run_id, callset_id, batch, counts):
    added = []
    with write_txn(conn):
        for marker, chrom, pos, genotype in batch:
            if conn.execute(_INSERT, (callset_id, marker, chrom, pos, genotype)).rowcount:
                added.append((marker, chrom, genotype))
        conn.execute("UPDATE callset SET marker_count=marker_count+? WHERE id=?", (len(added), callset_id))
    counts["duplicates"] += len(batch) - len(added)
    for marker, chrom, genotype in added:
        counts["markers"] += 1
        counts["rs_ids"] += marker.startswith("rs")
        counts["internal_ids"] += marker.startswith("i")
        counts["no_calls"] += genotype == "--"
        counts["haploid"] += len(genotype) == 1
        counts["indel_codes"] += "D" in genotype or "I" in genotype
        counts["chromosomes"][chrom] = counts["chromosomes"].get(chrom, 0) + 1
    sources.update_progress(conn, run_id, counts)


def import_dtc(conn, src, *, cancel=None) -> ImportResult:
    """Import a stored source. Counts describe the callset; duplicates include resumed rows."""
    run = sources.begin_import(conn, src, PARSER, PARSER_VERSION)
    if run is None:
        return ImportResult(status="skipped", source_id=src.id)
    result = ImportResult(status="succeeded", run_id=run.id, source_id=src.id)
    counts = result.counts
    skipped = []
    try:
        if cancel is not None and cancel.is_set():
            result.status = "cancelled"
        else:
            with _open_text(sources.source_file(conn, src)) as (text, member_name):
                lines = enumerate(text, 1)
                vendor, build = _header(lines)
                if build == "unknown":
                    result.warnings.append("Reference build is unknown.")
                elif build == "38":
                    result.warnings.append("GRCh38 coordinates retained; no conversion to build 37 was performed.")
                chip = re.search(r"_v([345])_", src.original_name, re.I)
                if chip is None:
                    chip = re.search(r"_v([345])_", member_name, re.I)
                with write_txn(conn):
                    conn.execute(
                        "INSERT OR IGNORE INTO callset(source_id, vendor, chip_version, build, created_at) "
                        "VALUES (?,?,?,?,?)", (src.id, vendor, "v" + chip[1] if chip else "", build, sources.now_iso())
                    )
                callset_id = conn.execute("SELECT id FROM callset WHERE source_id=?", (src.id,)).fetchone()[0]
                counts.update(_stored_counts(conn, callset_id))
                batch = []
                for number, line in lines:
                    if cancel is not None and cancel.is_set() and not batch:
                        result.status = "cancelled"
                        break
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    row = _parse(line, vendor)
                    if row is None:
                        counts["skipped_lines"] += 1
                        if len(skipped) < 5:
                            skipped.append(number)
                        continue
                    batch.append(row)
                    if len(batch) == BATCH_SIZE:
                        _flush(conn, run.id, callset_id, batch, counts)
                        batch = []
                        if cancel is not None and cancel.is_set():
                            result.status = "cancelled"
                            break
                if batch:
                    _flush(conn, run.id, callset_id, batch, counts)
                if cancel is not None and cancel.is_set():
                    result.status = "cancelled"
    except Exception as exc:
        result.status = "failed"
        # Error strings from decoders can include private input bytes.
        result.warnings.append(f"DTC import failed ({type(exc).__name__}).")
    if skipped:
        result.warnings.append("Skipped malformed lines (first 5): " + ", ".join(map(str, skipped)))
    sources.finish_import(conn, run.id, counts=counts, status=result.status,
                          errors=result.warnings if result.status == "failed" else ())
    return result
