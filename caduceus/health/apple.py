"""Stream stored Apple Health XML files or exports in zips, without extracting members."""
from __future__ import annotations

import hashlib
import json
import math
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from ..core import sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from . import fhir
from .trends import recompute_rollups

PARSER = "apple_health"
PARSER_VERSION = "1"
BATCH = 10_000
MAX_MEMBER = 8 << 30
MAX_TOTAL = 16 << 30
EXPORT_XML_ERROR = "zip must contain exactly one Apple Health export XML"
REPRODUCTIVE = frozenset({"MenstrualFlow", "IntermenstrualBleeding", "OvulationTestResult",
                         "CervicalMucusQuality", "SexualActivity", "Contraceptive", "Pregnancy",
                         "Lactation", "PregnancyTestResult", "ProgesteroneTestResult",
                         "InfrequentMenstrualCycles", "IrregularMenstrualCycles", "ProlongedMenstrualPeriods",
                         "PersistentIntermenstrualBleeding", "BasalBodyTemperature"})


class UnsafeXml(ValueError):
    """An export declares an entity or uses an unsupported XML encoding."""


class _XmlStream:
    def __init__(self, stream):
        self.stream, self.tail, self.size = stream, b"", 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.size += len(data)
        if self.size > MAX_MEMBER:
            raise ValueError("XML exceeds inflate limit")
        check = self.tail + data
        if b"<!ENTITY" in check.upper() or b"\x00" in check:
            raise UnsafeXml("XML entity declaration or unsupported encoding")
        self.tail = check[-16:]
        return data


@lru_cache(maxsize=8192)
def _date(text):
    value = datetime.fromisoformat(text)
    if value.utcoffset() is None:
        raise ValueError("Apple Health dates require an explicit UTC offset")
    return (value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            int(value.utcoffset().total_seconds() // 60), value.date().isoformat())


def _key(parts):
    return hashlib.sha1(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _sample(attrs, source_id):
    start, offset, day = _date(attrs["startDate"])
    end, end_offset, end_day = _date(attrs.get("endDate", attrs["startDate"]))
    text = attrs.get("value")
    try:
        value = float(text)
        if not math.isfinite(value):
            value = None
    except (TypeError, ValueError):
        value = None
    kind, source = attrs["type"], attrs.get("sourceName", "Unknown")
    if not kind:
        raise ValueError("missing sample type")
    if kind == "HKCategoryTypeIdentifierSleepAnalysis":
        offset, day = end_offset, end_day
    key = _key([kind, source, start, end, value if value is not None else text])
    short_type = kind.rsplit("Identifier", 1)[-1]
    category = ("reproductive" if short_type in REPRODUCTIVE else
                "mental_health" if kind.endswith("StateOfMind") or "Mood" in kind else "wellness")
    return (source_id, kind, source, attrs.get("unit"), start, end, offset, day, value, text, key, category)


def _flush(samples, pending, counts):
    if not pending:
        return
    with write_txn(samples):
        before = samples.total_changes
        samples.executemany(
            "INSERT OR IGNORE INTO sample(source_id,type,source_name,unit,start_utc,end_utc,tz_offset_min,local_day,"
            "value,value_text,dedupe_key,category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", pending)
        counts["samples"] += samples.total_changes - before
    pending.clear()


def _xml(samples, vault, stream, source_id, run_id, counts, cancel, warnings):
    prefix = stream.read(64 << 10)
    if b"<!ENTITY" in prefix.upper() or b"\x00" in prefix:
        raise UnsafeXml("XML entity declaration or unsupported encoding")
    stream.seek(0)
    stack, pending, positions = [], [], []
    position = 0
    for event, elem in ET.iterparse(_XmlStream(stream), events=("start", "end")):
        if event == "start":
            position += 1
            stack.append(elem)
            positions.append(position)
            continue
        if cancel is not None and counts["records"] % 1000 == 0 and cancel.is_set():
            _flush(samples, pending, counts)
            return
        if elem.tag == "Record":
            counts["records"] += 1
            try:
                pending.append(_sample(elem.attrib, source_id))
            except (KeyError, TypeError, ValueError, OverflowError):
                counts["skipped"] += 1
                if counts["skipped"] <= 5:
                    warnings.append(f"Skipped malformed Record at element position {positions[-1]}")
            if len(pending) >= BATCH:
                _flush(samples, pending, counts)
                sources.update_progress(vault, run_id, counts)
        elif elem.tag == "Workout":
            attrs = elem.attrib
            stats = [dict(child.attrib) for child in elem if child.tag == "WorkoutStatistics"]
            try:
                start = _date(attrs["startDate"])[0]
                end = _date(attrs["endDate"])[0]
            except (KeyError, TypeError, ValueError, OverflowError):
                counts["skipped"] += 1
                if counts["skipped"] <= 5:
                    warnings.append(f"Skipped malformed Workout at element position {positions[-1]}")
                stack.pop()
                positions.pop()
                if stack:
                    stack[-1].remove(elem)
                elem.clear()
                continue
            key = _key([attrs.get("workoutActivityType"), attrs.get("sourceName"), start, end])
            with write_txn(samples):
                cur = samples.execute(
                    "INSERT OR IGNORE INTO workout(source_id,type,source_name,start_utc,end_utc,duration,"
                    "duration_unit,statistics_json,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)",
                    (source_id, attrs.get("workoutActivityType", "Unknown"), attrs.get("sourceName", "Unknown"),
                     start, end, attrs.get("duration"), attrs.get("durationUnit"), json.dumps(stats), key))
                counts["workouts"] += cur.rowcount
        elif elem.tag == "ActivitySummary":
            raw = json.dumps(elem.attrib, sort_keys=True)
            with write_txn(samples):
                cur = samples.execute(
                    "INSERT OR IGNORE INTO activity_summary(source_id,day,record_json,dedupe_key) VALUES (?,?,?,?)",
                    (source_id, elem.get("dateComponents", ""), raw, _key(raw)))
                counts["activity_summaries"] += cur.rowcount
        elif elem.tag == "Me":
            counts["me_skipped"] += 1
        stack.pop()
        positions.pop()
        # Retain only the small workout subtree until its statistics are stored.
        if not any(parent.tag == "Workout" for parent in stack):
            if stack:
                stack[-1].remove(elem)
            elem.clear()
    _flush(samples, pending, counts)


def _safe_members(archive):
    total, members = 0, []
    for item in archive.infolist():
        name = item.filename
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or item.is_dir():
            continue
        if item.file_size > MAX_MEMBER or item.file_size > max(1, item.compress_size) * 200:
            raise ValueError("zip member exceeds inflate limit")
        total += item.file_size
        if total > MAX_TOTAL:
            raise ValueError("zip exceeds total inflate limit")
        members.append(item)
    return members


def import_apple_health(samples, vault, src, *, cancel=None) -> ImportResult:
    run = sources.begin_import(vault, src, PARSER, PARSER_VERSION)
    if run is None:
        return ImportResult("skipped", source_id=src.id)
    counts = {"records": 0, "samples": 0, "workouts": 0, "activity_summaries": 0, "me_skipped": 0, "skipped": 0}
    status, errors, warnings = "succeeded", [], []
    try:
        if cancel is not None and cancel.is_set():
            status = "cancelled"
        else:
            stored = sources.source_file(vault, src)
            if zipfile.is_zipfile(stored):
                with zipfile.ZipFile(stored) as archive:
                    members = _safe_members(archive)
                    exports = [m for m in members if PurePosixPath(m.filename).name == "export.xml"]
                    if len(exports) != 1:
                        exports = []
                        for member in members:
                            name = member.filename.lower()
                            if not name.endswith(".xml") or name.endswith("export_cda.xml"):
                                continue
                            with archive.open(member) as stream:
                                head = stream.read(4096).decode("utf-8-sig", "replace")
                            if "<!DOCTYPE HealthData" in head or "<HealthData" in head:
                                exports.append(member)
                    if len(exports) != 1:
                        raise ValueError(EXPORT_XML_ERROR)
                    with archive.open(exports[0]) as stream:
                        _xml(samples, vault, stream, src.id, run.id, counts, cancel, warnings)
                    for member in members:
                        if cancel is not None and cancel.is_set():
                            break
                        path = PurePosixPath(member.filename)
                        if "clinical-records" in path.parts and path.suffix.lower() == ".json":
                            if member.file_size > fhir.MAX_JSON:
                                raise ValueError("FHIR JSON exceeds size limit")
                            with archive.open(member) as stream:
                                raw = stream.read(fhir.MAX_JSON + 1)
                            if len(raw) > fhir.MAX_JSON:
                                raise ValueError("FHIR JSON exceeds inflate limit")
                            clinical = fhir.ingest(vault, json.loads(raw), src.id, cancel=cancel, run_id=run.id)
                            for key, value in clinical.items():
                                counts[key] = counts.get(key, 0) + value
            else:
                if stored.stat().st_size > MAX_MEMBER:
                    raise ValueError("XML exceeds size limit")
                warnings.append("Imported an Apple Health XML on its own. Health records are read only from the "
                                "export zip (its clinical-records folder); import export.zip to include them.")
                with stored.open("rb") as stream:
                    _xml(samples, vault, stream, src.id, run.id, counts, cancel, warnings)
            if cancel is not None and cancel.is_set():
                status = "cancelled"
            sleep_warnings = recompute_rollups(samples)
            if sleep_warnings:
                warnings.extend(sleep_warnings)
                counts["sleep_nights_clipped"] = len(sleep_warnings)
    except Exception as exc:
        status = "failed"
        # Input values may be personal: report the error class, not parser excerpts.
        errors = [f"{type(exc).__name__}: Apple Health import failed"]
        if isinstance(exc, ValueError) and str(exc) == EXPORT_XML_ERROR:
            errors = [EXPORT_XML_ERROR]  # Fixed selection error, with no input excerpts.
    warnings += fhir.warning_messages(counts) + errors
    sources.finish_import(vault, run.id, counts=counts, status=status, errors=warnings)
    return ImportResult(status, run.id, src.id, counts, warnings)
