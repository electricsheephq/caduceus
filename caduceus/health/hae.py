"""Health Auto Export JSON and MCP sync share one local, counts-only importer."""
from __future__ import annotations

import json
import math
import re
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath

from ..core import sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from .apple import _key
from .trends import recompute_rollups, sleep_morning

try:
    import fcntl
except ImportError:  # Windows: the process-local lock still serializes runs.
    fcntl = None

_RUN_LOCKS = {}
_RUN_LOCKS_GUARD = threading.Lock()

PARSER_VERSION = "1"
BATCH = 10_000
MAX_JSON = 512 << 20
MAX_TOTAL = 16 << 30
MAPPING = {
    "body_mass": "BodyMass", "weight_body_mass": "BodyMass",
    "body_mass_index": "BodyMassIndex", "body_fat_percentage": "BodyFatPercentage",
    "lean_body_mass": "LeanBodyMass", "waist_circumference": "WaistCircumference",
    "protein": "DietaryProtein", "step_count": "StepCount", "active_energy": "ActiveEnergyBurned",
    "resting_heart_rate": "RestingHeartRate", "heart_rate": "HeartRate",
    "heart_rate_variability": "HeartRateVariabilitySDNN", "vo2_max": "VO2Max",
    "blood_glucose": "BloodGlucose", "blood_pressure": "BloodPressure", "sleep_analysis": "SleepAnalysis",
}
UNITS = {unit: unit for unit in (
    "count/min", "mL/min·kg", "kcal", "kJ", "kg", "lb", "g", "mg", "cm", "in", "m", "mm",
    "mg/dL", "mmol/L", "mmHg", "ms", "%", "count")}
UNITS.update({"bpm": "count/min", "beats/min": "count/min", "lbs": "lb", "Cal": "kcal",
              "mg/dl": "mg/dL", "mmol/l": "mmol/L"})
UNITS.update({u: "mL/min·kg" for u in (
    "ml/(kg·min)", "mL/(kg·min)", "ml/kg/min", "mL/kg·min", "ml/(kg*min)")})
SLEEP = {name: "HKCategoryValueSleepAnalysis" + suffix for name, suffix in {
    "Core": "AsleepCore", "Deep": "AsleepDeep", "REM": "AsleepREM", "Asleep": "AsleepUnspecified",
    "Unspecified": "AsleepUnspecified", "In Bed": "InBed", "Awake": "Awake"}.items()}
IGNORED_ARRAYS = ("stateOfMind", "medications", "symptoms", "cycleTracking", "ecg", "heartRateNotifications")


class _Cancelled(Exception):
    pass


class _ServerError(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise _Cancelled


@contextmanager
def _run_lock(samples, vault, run_id, cancel):
    """Serialize a whole HAE run per samples file, including rollup recomputation."""
    path = next(row[2] for row in samples.execute("PRAGMA database_list") if row[1] == "main")
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.setdefault(path or id(samples), threading.Lock())
    waiting = False

    def wait():
        nonlocal waiting
        _check_cancel(cancel)
        if not waiting:
            sources.update_progress(vault, run_id, {"waiting": "another Health Auto Export run"})
            waiting = True
        if cancel is not None:
            cancel.wait(0.25)
        else:
            time.sleep(0.25)
        _check_cancel(cancel)

    _check_cancel(cancel)
    while not lock.acquire(blocking=False):
        wait()
    try:
        _check_cancel(cancel)
        if path and fcntl is not None:
            with open(path + "-hae-lock", "a+b") as file:
                while True:
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        wait()
                try:
                    _check_cancel(cancel)
                    yield
                finally:
                    fcntl.flock(file, fcntl.LOCK_UN)
        else:
            yield
    finally:
        lock.release()


def _date(text):
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2}):(\d{2}) ?([AaPp])\.?[Mm]\.? ([+-]\d{2}:?\d{2})",
                         text) if isinstance(text, str) else None
    if match:
        day, hour, minute, second, meridiem, offset = match.groups()
        hour = int(hour)
        if not 1 <= hour <= 12:
            raise ValueError("invalid 12-hour time")
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
        text = f"{day} {hour:02d}:{minute}:{second} {offset}"
    try:
        value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    except (TypeError, ValueError):
        value = datetime.fromisoformat(text)
    if value.utcoffset() is None:
        raise ValueError("date requires an offset")
    return value


def _utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("finite number required")
    return float(value)


def _source(entry):
    value = entry.get("source", "Health Auto Export")
    if not isinstance(value, str):
        raise ValueError("source must be text")
    return value


def _sample(kind, entry, source_id, unit, value=None, text=None, *, start=None, end=None):
    when = entry["date"] if "date" in entry else entry["start"]
    start = start if start is not None else _date(entry.get("start", when))
    end = end if end is not None else _date(entry.get("end", when))
    if end < start:
        raise ValueError("invalid interval")
    source = _source(entry)
    is_sleep = kind == "SleepAnalysis"
    kind = ("HKCategoryTypeIdentifier" if is_sleep else "HKQuantityTypeIdentifier") + kind
    local = end if is_sleep else start
    first, last = _utc(start), _utc(end)
    day = sleep_morning(start.astimezone(end.tzinfo)) if is_sleep else local.date()
    identity = ["hae", kind, source, first, last]
    if is_sleep:
        identity.append(text)
    return (source_id, kind, source, unit, first, last, int(local.utcoffset().total_seconds() // 60),
            day.isoformat(), value, text, _key(identity), "wellness")


def _sleep_bucket_start(entry):
    """A day bucket belongs to the night of its start's morning, not its evening."""
    return _date(entry["start"])


def _metric_rows(name, metric, entry, source_id):
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")
    # Files supply date; MCP day buckets supply start. Both require an offset.
    _date(entry["date"] if "date" in entry else entry["start"])
    unit = metric.get("units")
    if unit is not None and not isinstance(unit, str):
        raise ValueError("unit must be text")
    unit = UNITS.get(unit, unit)
    if name == "sleep_analysis":
        if "value" in entry:
            text = SLEEP[entry["value"]]
            start = _date(entry.get("start", entry.get("startDate")))
            end = _date(entry.get("end", entry.get("endDate")))
        else:
            hours = (_number(entry["totalSleep"]) if "totalSleep" in entry else
                     sum(_number(entry.get(k, 0)) for k in ("asleep", "core", "deep", "rem")))
            if not ("totalSleep" in entry or "asleep" in entry) or not 0 < hours <= 24:
                raise ValueError("invalid sleep duration")
            sleep_end = entry.get("sleepEnd") or entry.get("inBedEnd")
            if sleep_end:
                end = _date(sleep_end)
                start = end - timedelta(hours=hours)
            else:
                start = _sleep_bucket_start(entry)
                end = start + timedelta(hours=hours)
            text = SLEEP["Asleep"]
        return [_sample("SleepAnalysis", entry, source_id, "h", text=text, start=start, end=end)]
    if name == "blood_pressure":
        return [_sample("BloodPressure" + side.title(), entry, source_id, unit, _number(entry[side]))
                for side in ("systolic", "diastolic")]
    value = _number(entry["Avg"] if name == "heart_rate" and "Avg" in entry else entry["qty"])
    if name == "body_fat_percentage":
        value = value / 100 if value > 1 else value
        unit = "%"
    elif name == "body_mass_index":
        unit = "count"
    return [_sample(MAPPING[name], entry, source_id, unit, value)]


def _workout(entry, source_id):
    name = entry["name"]
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError("invalid workout name")
    start, end = _date(entry["start"]), _date(entry["end"])
    if end < start:
        raise ValueError("invalid workout interval")
    duration = entry.get("duration")
    if isinstance(duration, (int, float)):
        duration = _number(duration)
    else:
        duration = (end - start).total_seconds()
    stats = {}
    for key, value in entry.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stats[key] = _number(value)
        elif isinstance(value, dict) and set(value) == {"qty", "units"} and isinstance(value["units"], str):
            stats[key] = {"qty": _number(value["qty"]), "units": value["units"]}
    first, last = _utc(start), _utc(end)
    return (source_id, name, _source(entry), first, last, duration, "s", json.dumps(stats, sort_keys=True),
            _key(["hae", "workout", name, first, last]))


def _counts():
    return {"samples": 0, "samples_replaced": 0, "workouts": 0, "entries_skipped": 0, "metrics_ignored": {}}


def ingest_payload(samples, data: dict, source_id, *, run_state, counts, warnings, cancel=None):
    """Write at most 10,000 rows per transaction; clear each HAE metric-day once per run."""
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    cleared = run_state.setdefault("cleared", set())
    sleep_clear_from = run_state.get("sleep_clear_from")
    sleep_keep = run_state.get("sleep_keep", set())
    pending, workouts = [], []

    def count_entry(kind, outcome):
        if "sync_entries" in run_state:
            run_state["sync_entries"][kind][outcome] += 1

    def flush():
        _check_cancel(cancel)
        pairs = {(r[1], r[7]) for r in pending} - cleared
        pairs = {(kind, day) for kind, day in pairs
                 if kind != "HKCategoryTypeIdentifierSleepAnalysis" or
                 (day not in sleep_keep and (sleep_clear_from is None or day >= sleep_clear_from.isoformat()))}
        replaced = 0
        with write_txn(samples):
            samples.execute("CREATE INDEX IF NOT EXISTS sample_hae_type_day ON sample(provider,type,local_day)")
            for kind, day in sorted(pairs):
                replaced += samples.execute("DELETE FROM sample WHERE provider='hae' AND type=? AND local_day=?",
                                            (kind, day)).rowcount
            before = samples.total_changes
            samples.executemany(
                "INSERT INTO sample(source_id,type,source_name,unit,start_utc,end_utc,tz_offset_min,local_day,"
                "value,value_text,dedupe_key,category,provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'hae') "
                "ON CONFLICT(dedupe_key) DO UPDATE SET value=excluded.value,value_text=excluded.value_text,"
                "unit=excluded.unit,source_id=excluded.source_id,tz_offset_min=excluded.tz_offset_min,"
                "local_day=excluded.local_day,category=excluded.category", pending)
            written = samples.total_changes - before
            before = samples.total_changes
            samples.executemany(
                "INSERT OR IGNORE INTO workout(source_id,type,source_name,start_utc,end_utc,duration,"
                "duration_unit,statistics_json,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)", workouts)
            added_workouts = samples.total_changes - before
        cleared.update(pairs)
        counts["samples_replaced"] += replaced
        counts["samples"] += written
        counts["workouts"] += added_workouts
        pending.clear()
        workouts.clear()

    for metric in data.get("metrics", []):
        _check_cancel(cancel)
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str):
            counts["entries_skipped"] += 1
            count_entry(run_state.get("sync_kind", "metrics"), "skipped")
            continue
        name = metric["name"]
        if name not in MAPPING:
            ignored = counts["metrics_ignored"]
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or (
                    name not in ignored and len(ignored) - ("other" in ignored) >= 50):
                name = "other"
            ignored[name] = ignored.get(name, 0) + 1
            continue
        skipped = 0
        kind = "sleep" if name == "sleep_analysis" else "metrics"
        for entry in metric.get("data", []):
            _check_cancel(cancel)
            try:
                rows = _metric_rows(name, metric, entry, source_id)
            except (KeyError, ValueError, TypeError, OverflowError):
                skipped += 1
                count_entry(kind, "skipped")
                continue
            count_entry(kind, "parsed")
            if (name == "sleep_analysis" and "value" in entry
                    and rows[0][2] in run_state.get("sleep_summaries", {}).get(rows[0][7], set())):
                counts["sleep_segments_omitted"] = counts.get("sleep_segments_omitted", 0) + 1
                continue
            if len(pending) + len(rows) > BATCH:
                flush()
            pending.extend(rows)
            if len(pending) >= BATCH:
                flush()
        counts["entries_skipped"] += skipped
        if skipped:
            warnings.append(f"{name}: skipped {skipped} malformed entries")
    for entry in data.get("workouts", []):
        _check_cancel(cancel)
        try:
            row = _workout(entry, source_id)
        except (KeyError, ValueError, TypeError, OverflowError):
            counts["entries_skipped"] += 1
            count_entry("workouts", "skipped")
            continue
        count_entry("workouts", "parsed")
        if len(pending) + len(workouts) >= BATCH:
            flush()
        workouts.append(row)
    for name in IGNORED_ARRAYS:
        if isinstance(data.get(name), list):
            ignored = counts.setdefault("ignored_arrays", {})
            ignored[name] = ignored.get(name, 0) + len(data[name])
    flush()


def _safe_members(archive):
    total, members = 0, []
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename or item.is_dir():
            continue
        if path.suffix.lower() != ".json":
            continue
        if item.file_size > MAX_JSON or item.file_size > max(1, item.compress_size) * 200:
            raise ValueError("zip member exceeds inflate limit")
        total += item.file_size
        if total > MAX_TOTAL:
            raise ValueError("zip exceeds total inflate limit")
        members.append(item)
    if not members:
        raise ValueError("zip has no JSON files")
    return members


def _read_json(stream):
    raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("JSON exceeds size limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("missing data object")
    return payload["data"]


def _sleep_nights(data, *, cancel=None):
    """Collect valid segment and summary nights before any file rows are ingested."""
    segments, summaries = {}, {}
    for metric in data.get("metrics", []):
        if not isinstance(metric, dict) or metric.get("name") != "sleep_analysis":
            continue
        for entry in metric.get("data", []):
            _check_cancel(cancel)
            try:
                row = _metric_rows("sleep_analysis", metric, entry, None)[0]
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            (segments if "value" in entry else summaries).setdefault(row[7], set()).add(row[2])
    return segments, summaries


def _sleep_state(segments, summaries, warnings):
    edges = {min(segments), max(segments)} if segments else set()
    mixed = sorted(day for day, names in segments.items() if names & summaries.get(day, set()))
    if mixed:
        warnings.append("Sleep summaries used instead of same-source segments for nights: " + ", ".join(mixed))
    return {"sleep_keep": {day for day in edges if not segments[day] <= summaries.get(day, set())},
            "sleep_summaries": summaries}


def import_hae(samples, vault, src, *, cancel=None) -> ImportResult:
    run = sources.begin_import(vault, src, "hae_json", PARSER_VERSION)
    if run is None:
        return ImportResult("skipped", source_id=src.id)
    counts, warnings, state, status = _counts(), [], {}, "succeeded"
    try:
        with _run_lock(samples, vault, run.id, cancel):
            _check_cancel(cancel)
            stored = sources.source_file(vault, src)
            if zipfile.is_zipfile(stored):
                with zipfile.ZipFile(stored) as archive:
                    members = _safe_members(archive)
                    segments, summaries = {}, {}
                    # Scan every member before ingesting: zip order need not be date order.
                    for member in members:
                        _check_cancel(cancel)
                        with archive.open(member) as stream:
                            member_segments, member_summaries = _sleep_nights(_read_json(stream), cancel=cancel)
                        for target, member_nights in ((segments, member_segments), (summaries, member_summaries)):
                            for day, names in member_nights.items():
                                target.setdefault(day, set()).update(names)
                    state.update(_sleep_state(segments, summaries, warnings))
                    for member in members:
                        _check_cancel(cancel)
                        with archive.open(member) as stream:
                            data = _read_json(stream)
                        ingest_payload(samples, data, src.id, run_state=state, counts=counts, warnings=warnings, cancel=cancel)
                        sources.update_progress(vault, run.id, counts)
            else:
                if stored.stat().st_size > MAX_JSON:
                    raise ValueError("JSON exceeds size limit")
                with stored.open("rb") as stream:
                    data = _read_json(stream)
                segments, summaries = _sleep_nights(data, cancel=cancel)
                state.update(_sleep_state(segments, summaries, warnings))
                ingest_payload(samples, data, src.id, run_state=state, counts=counts, warnings=warnings, cancel=cancel)
            _check_cancel(cancel)
            warnings.extend(recompute_rollups(samples))
    except _Cancelled:
        status = "cancelled"
    except Exception as exc:
        status = "failed"
        warnings.append(f"{type(exc).__name__}: Health Auto Export import failed")
    sources.finish_import(vault, run.id, counts=counts, status=status, errors=warnings)
    return ImportResult(status, run.id, src.id, counts, warnings)


def _unwrap(envelope):
    """Accept Hermes text, JSON-RPC content and structured envelopes without logging input."""
    if not isinstance(envelope, dict) or envelope.get("truncated"):
        return None

    def unwrap(value, depth=0):
        if depth > 8:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return None
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("data"), dict):
            return value["data"]
        if isinstance(value.get("metrics"), list) or isinstance(value.get("workouts"), list):
            return value
        for key in ("structuredContent", "result"):
            found = unwrap(value.get(key), depth + 1)
            if found is not None:
                return found
        content = value.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return unwrap(block.get("text"), depth + 1)
        return None

    return unwrap(envelope)


def sync_hae(call, samples, vault, *, server, backfill_days, today, cancel=None, local_offset=None) -> ImportResult:
    """Sync phone-local day buckets; local_offset is retained but unused by requests."""
    src = sources.virtual_source(vault, "hae_sync", server, f"Health Auto Export (MCP server {server})")
    run = sources.begin_run(vault, src, "hae_sync", PARSER_VERSION)
    counts = {**_counts(), "windows": 0, "windows_skipped": 0, "windows_empty": 0}
    counts["entries_by_kind"] = {kind: {"parsed": 0, "skipped": 0} for kind in ("metrics", "sleep", "workouts")}
    warnings, status, denied = [], "succeeded", False
    readable_replies = 0
    try:
        with _run_lock(samples, vault, run.id, cancel):
            previous = vault.execute(
                "SELECT counts_json FROM import_run WHERE source_id=? AND parser='hae_sync' AND status='succeeded' "
                "ORDER BY id DESC LIMIT 1", (src.id,)).fetchone()
            through = today
            since = through - timedelta(days=backfill_days - 1)
            if previous:
                since = max(date.fromisoformat(json.loads(previous[0])["through"]) - timedelta(days=2),
                            through - timedelta(days=3650))
            else:
                earlier = vault.execute(
                    "SELECT counts_json FROM import_run WHERE source_id=? AND parser='hae_sync' AND id<? "
                    "AND json_extract(counts_json,'$.since') IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (src.id, run.id)).fetchone()
                if earlier:
                    since = max(min(since, date.fromisoformat(json.loads(earlier[0])["since"])),
                                through - timedelta(days=3650))
            counts.update(since=since.isoformat(), through=through.isoformat())
            with write_txn(vault):
                vault.execute("UPDATE import_run SET counts_json=? WHERE id=?",
                              (json.dumps({"since": counts["since"], "through": counts["through"]},
                                          sort_keys=True), run.id))
            state = {"sleep_clear_from": since, "sync_entries": counts["entries_by_kind"]}
            quantities = ",".join(n for n in MAPPING if n not in ("body_mass", "sleep_analysis"))
            windows = []
            for kind, size in (("quantity", 14), ("sleep", 7), ("workouts", 31)):
                start = since - timedelta(days=1) if kind == "sleep" else since
                while start <= through:
                    end = min(through, start + timedelta(days=size - 1))
                    windows.append((start, end, kind))
                    start = end + timedelta(days=1)
            windows.sort(key=lambda w: (w[0], w[1], w[2]))
            done, total = 0, len(windows)
            while windows:
                _check_cancel(cancel)
                start, end, kind = windows.pop(0)
                args = {"start": start.isoformat(), "end": (end + timedelta(days=1)).isoformat()}
                if kind == "workouts":
                    tool = "get_workouts"
                    args.update(includeMetadata=False, includeRoutes=False)
                else:
                    tool = "get_health_metrics"
                    args.update(metrics="sleep_analysis" if kind == "sleep" else quantities,
                                interval="days", aggregate=kind != "sleep")
                envelope = call(tool, args)
                counts["windows"] += 1
                if isinstance(envelope, dict) and envelope.get("ok") is False:
                    raise _ServerError("Health Auto Export did not answer the request. "
                                       "Check that the app is open and its MCP server is running.")
                data = _unwrap(envelope)
                truncated = isinstance(envelope, dict) and envelope.get("truncated")
                if truncated and start < end:
                    middle = start + timedelta(days=(end - start).days // 2)
                    windows.extend(((start, middle, kind), (middle + timedelta(days=1), end, kind)))
                    windows.sort(key=lambda w: (w[0], w[1], w[2]))
                    total += 2
                elif truncated:
                    counts["windows_skipped"] += 1
                    warnings.append(f"{kind}: skipped request window {start}")
                elif data is None:
                    counts["windows_empty"] += 1
                    warnings.append(f"{kind} {start} to {end}: no data in the reply")
                else:
                    readable_replies += 1
                    state["sync_kind"] = "metrics" if kind == "quantity" else kind
                    ingest_payload(samples, data, src.id, run_state=state, counts=counts, warnings=warnings, cancel=cancel)
                done += 1
                sources.update_progress(vault, run.id, {"windows_done": done, "windows_total": total})
            _check_cancel(cancel)
            warnings.extend(recompute_rollups(samples))
            unreadable = [kind for kind, tally in counts["entries_by_kind"].items()
                          if tally["skipped"] > 0 and tally["parsed"] == 0]
            if unreadable:
                status = "failed"
                counts["unreadable_kinds"] = unreadable
                labels = {"metrics": "health metric", "sleep": "sleep", "workouts": "workout"}
                for kind in unreadable:
                    warnings.append(f"Health Auto Export sent {labels[kind]} data that Caduceus cannot read, "
                                    "so none was imported. The next sync reads these days again.")
            if counts["windows_skipped"]:
                status = "failed"
                warnings.append(f"Health Auto Export replies for {counts['windows_skipped']} one-day requests "
                                "were too large to read; the next sync reads those days again.")
            if counts["windows_empty"] > 0 and readable_replies == 0:
                status = "failed"
                warnings.append("None of Health Auto Export's replies could be read, so nothing was imported. "
                                "The next sync reads these days again.")
    except _Cancelled:
        status = "cancelled"
    except PermissionError:
        status = "failed"
        denied = True
        warnings.append(f"Caduceus is not allowed to read the MCP server '{server}'. To allow it, add '{server}' "
                        "to plugins.entries.caduceus.mcp_allowlist in config.yaml, then run op=sync again.")
    except _ServerError as exc:
        status = "failed"
        warnings.append(str(exc))
    except Exception as exc:
        status = "failed"
        warnings.append(type(exc).__name__)
    sources.finish_import(vault, run.id, counts=counts, status=status, errors=warnings)
    if denied and counts["samples"] == 0 and counts["workouts"] == 0:
        # Denial leaves an unused vault empty. Other failures keep a receipt for the next sync.
        # This path creates no audit/search rows. Keep provenance if another run uses it.
        with write_txn(vault):
            other_run = vault.execute("SELECT 1 FROM import_run WHERE source_id=? AND id<>?",
                                      (src.id, run.id)).fetchone()
            if other_run is None:
                vault.execute("DELETE FROM import_run WHERE id=?", (run.id,))
                vault.execute("DELETE FROM source_artifact WHERE id=?", (src.id,))
    return ImportResult(status, run.id, src.id, counts, warnings)
