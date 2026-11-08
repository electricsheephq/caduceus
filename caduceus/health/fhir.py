"""Local FHIR R4 ingestion; raw resources and date precision stay in the vault."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime

from ..core import search, sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from ..core.policy import Category, SENSITIVE, categorize

PARSER = "fhir"
PARSER_VERSION = "1"
MAX_JSON = 64 << 20
LOINC = "http://loinc.org"
ALIASES = {"hba1c": ("4548-4", "17856-6", "59261-8"),
           "glucose": ("2345-7", "2339-0", "1558-6"),
           "ldl": ("13457-7", "18262-6", "2089-1")}
ICD_SYSTEMS = {"http://hl7.org/fhir/sid/icd-10-cm", "urn:oid:2.16.840.1.113883.6.90"}


def _display(concept):
    concept = concept or {}
    return concept.get("text") or next((c.get("display") for c in concept.get("coding", []) if c.get("display")), None)


def _concept(value, *, loinc=False):
    value = value or {}
    coding = value.get("coding") or []
    chosen = next((c for c in coding if loinc and c.get("system") == LOINC), coding[0] if coding else {})
    return chosen.get("system"), chosen.get("code"), _display(value) or chosen.get("code", "Unknown")


def _category(concepts, default):
    text = " ".join(str(text) for concept in concepts for text in
                    [concept.get("text", ""), *[c.get("display", "") for c in concept.get("coding", [])]])
    category = categorize(text=text, default=default)
    if category in SENSITIVE:
        return category
    for concept in concepts:
        for coding in concept.get("coding", []):
            system, code = coding.get("system"), str(coding.get("code", "")).upper()
            if system == LOINC and code in {"44249-1", "44261-6", "70274-6"}:
                return Category.MENTAL_HEALTH
            if system in ICD_SYSTEMS:
                if re.match(r"F(?:0[1-9]|[1-9][0-9])", code):
                    return Category.MENTAL_HEALTH
                if (re.match(r"O(?:[0-8][0-9]|9[0-9A])|Z3[0-9]|N9[6-8]|N80|N52", code)
                        or code == "E28.2"):
                    return Category.REPRODUCTIVE
    return category


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date_value(value):
    """Validate FHIR date/dateTime without inventing missing month/day fields."""
    if not isinstance(value, str):
        return None, None
    try:
        if re.fullmatch(r"\d{4}", value):
            date(int(value), 1, 1)
            return value, "year"
        if re.fullmatch(r"\d{4}-\d{2}", value):
            date(int(value[:4]), int(value[5:]), 1)
            return value, "month"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
            return value, "day"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", value):
            datetime.fromisoformat(value)
            return value, "timestamp"
    except ValueError:
        pass
    return None, None


def _key(parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _raw(resource, precision):
    return json.dumps({**resource, "_caduceus": {"date_precision": precision}}, sort_keys=True, separators=(",", ":"))


def _skip(counts, kind, reason):
    key = f"skipped:{kind}:{reason}"
    counts[key] = counts.get(key, 0) + 1


def warning_messages(counts):
    reasons = {"date": "missing or invalid date", "status": "unsupported medication status",
               "name": "missing medication display name"}
    return [f"{key.split(':')[1]}: skipped {n} resource(s) ({reasons[key.split(':')[2]]})"
            for key, n in sorted(counts.items()) if key.startswith("skipped:")]


def _observations(vault, resource, source_id, when, precision):
    components = resource.get("component") or []
    parts = ([(resource, None)] if not components or any(k.startswith("value") for k in resource) else [])
    parts += [(component, component.get("code")) for component in components]
    inserted = 0
    for part, component in parts:
        concept = part.get("code") or {}
        system, code, name = _concept(concept, loinc=True)
        quantity = part.get("valueQuantity") or {}
        num, text = _number(quantity.get("value")), part.get("valueString")
        if "valueCodeableConcept" in part:
            text = _display(part["valueCodeableConcept"])
        if quantity.get("comparator"):
            num = None
            text = str(quantity["comparator"]) + str(quantity.get("value", ""))
        unit = quantity.get("unit") or quantity.get("code")
        rr = (part.get("referenceRange") or [{}])[0]
        interpretations = [_concept(c)[2] for c in part.get("interpretation", [])]
        category = _category([resource.get("code") or {}, concept], Category.RECORDS)
        key = _key(["Observation", system, code, when, [num, text], unit, code if component is not None else None])
        cur = vault.execute(
            "INSERT OR IGNORE INTO observation(code_system,code,name,value_num,value_text,unit,effective_at,"
            "ref_low,ref_high,interpretation,category,source,source_id,dedupe_key,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (system, code, name, num, text, unit, when, _number((rr.get("low") or {}).get("value")),
             _number((rr.get("high") or {}).get("value")), "; ".join(interpretations) or None,
             category.value, "fhir", source_id, key, _raw(resource, precision)))
        if cur.rowcount:
            search.index_doc(vault, kind="observation", ref=str(cur.lastrowid), title=name,
                             body=_raw(resource, precision), category=category, ts=when)
            inserted += 1
    return inserted


def ingest(vault, document, source_id, *, cancel=None, run_id=None):
    """Shared local ingestion; each resource and its index rows commit together."""
    counts = {"observation": 0, "condition": 0, "medication": 0, "ignored": 0}
    resources, processed = [document], 0
    while resources:
        if cancel is not None and cancel.is_set():
            break
        if processed and processed % 10_000 == 0 and run_id is not None:
            sources.update_progress(vault, run_id, counts)
        resource = resources.pop()
        kind = resource.get("resourceType")
        if kind == "Bundle":
            resources.extend(reversed([e["resource"] for e in resource.get("entry", []) if "resource" in e]))
            continue
        processed += 1
        if kind not in ("Observation", "Condition", "MedicationRequest", "MedicationStatement"):
            counts["ignored"] += 1
            continue
        event = "start"
        if kind.startswith("Medication"):
            status = resource.get("status")
            allowed = {"active", "completed", "on-hold"} | ({"intended"} if kind == "MedicationStatement" else set())
            if status not in allowed and status != "stopped":
                _skip(counts, kind, "status")
                continue
            event = "stop" if status == "stopped" else "start"
            name = _display(resource.get("medicationCodeableConcept"))
            if not name or not name.strip():
                _skip(counts, kind, "name")
                continue
            name = name.strip().lower()
            period = resource.get("effectivePeriod") or {}
            when = (period.get("end") if event == "stop" else None) or resource.get("effectiveDateTime") or period.get("start") or resource.get("authoredOn")
        elif kind == "Observation":
            when = resource.get("effectiveDateTime") or (resource.get("effectivePeriod") or {}).get("start") or resource.get("issued")
        else:
            when = resource.get("onsetDateTime") or (resource.get("onsetPeriod") or {}).get("start")
        when, precision = _date_value(when)
        if when is None:
            _skip(counts, kind, "date")
            continue
        with write_txn(vault):
            if kind == "Observation":
                counts["observation"] += _observations(vault, resource, source_id, when, precision)
                continue
            if kind == "Condition":
                concept = resource.get("code") or {}
                system, code, name = _concept(concept)
                category = _category([concept], Category.RECORDS)
                key = _key([system, code, when, name])
                cur = vault.execute(
                    "INSERT OR IGNORE INTO condition(code_system,code,name,clinical_status,onset,category,source,source_id,dedupe_key,raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (system, code, name, _display(resource.get("clinicalStatus")) or
                     next((c.get("code") for c in (resource.get("clinicalStatus") or {}).get("coding", [])), None),
                     when, category.value, "fhir", source_id, key, _raw(resource, precision)))
                search_kind = "condition"
            else:
                when = when[:10] if precision == "timestamp" else when
                category = _category([resource.get("medicationCodeableConcept") or {}], Category.MEDICATION)
                note = json.dumps({"dedupe_key": _key([kind, name, event, when]), "date_precision": precision})
                cur = vault.execute(
                    "INSERT OR IGNORE INTO medication_event(drug,event,occurred_on,source,source_id,note,created_at) "
                    "VALUES (?,?,?,'fhir',?,?,?)", (name, event, when, source_id, note, sources.now_iso()))
                search_kind = "medication"
            if cur.rowcount:
                search.index_doc(vault, kind=search_kind, ref=str(cur.lastrowid), title=name,
                                 body=_raw(resource, precision), category=category, ts=when)
                counts[search_kind] += 1
    return counts


def import_fhir(vault, src, *, cancel=None) -> ImportResult:
    run = sources.begin_import(vault, src, PARSER, PARSER_VERSION)
    if run is None:
        return ImportResult("skipped", source_id=src.id)
    counts, errors, status = {}, [], "succeeded"
    try:
        if cancel is not None and cancel.is_set():
            status = "cancelled"
        else:
            with sources.source_file(vault, src).open("rb") as stream:
                raw = stream.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise ValueError("FHIR JSON exceeds size limit")
            counts = ingest(vault, json.loads(raw), src.id, cancel=cancel, run_id=run.id)
            if cancel is not None and cancel.is_set():
                status = "cancelled"
    except Exception as exc:
        status, errors = "failed", [f"{type(exc).__name__}: FHIR import failed"]
    errors += warning_messages(counts)
    sources.finish_import(vault, run.id, counts=counts, status=status, errors=errors)
    return ImportResult(status, run.id, src.id, counts, errors)


def observation_series(vault, name_or_code, *, start=None, end=None) -> dict:
    """Return one compatible unit per series, with explicit conversions/exclusions."""
    from .trends import _bound, coverage

    name = str(name_or_code).strip()
    codes = ALIASES.get(name.casefold(), (name,))
    where = "((code IN (" + ",".join("?" for _ in codes) + ") AND (code_system IS NULL OR code_system='' OR code_system=?)) OR (lower(name)=? AND (code IS NULL OR code='')))"
    rows = vault.execute("SELECT * FROM observation WHERE " + where + " ORDER BY effective_at,id", [*codes, LOINC, name.lower()])
    points = [{"date": r["effective_at"], "date_precision": _date_value(r["effective_at"])[1],
               "code": r["code"], "value": r["value_num"], "value_text": r["value_text"], "unit": r["unit"],
               "reference_range": {"low": r["ref_low"], "high": r["ref_high"]},
               "interpretation": r["interpretation"], "source": r["source"], "source_id": r["source_id"],
               "category": r["category"]} for r in rows]
    low, high = _bound(start) if start else None, _bound(end, end=True) if end else None
    if (start and low is None) or (end and high is None):
        raise ValueError("invalid series date bound")
    points = [p for p in points if _bound(p["date"]) is not None and
              (low is None or _bound(p["date"], end=True) >= low) and
              (high is None or _bound(p["date"]) <= high)]
    for point in points:
        scale, offset = None, 0
        if (point["code"] in ALIASES["hba1c"] or name.casefold() == "hba1c") and point["unit"] == "mmol/mol":
            scale, offset, target = 1/10.929, 2.15, "%"
        elif (point["code"] in ALIASES["glucose"] or name.casefold() == "glucose") and (
                point["unit"] == "mmol/L" or re.fullmatch(r"mmol<[^>]+>/L", point["unit"] or "")):
            scale, target = 18.016, "mg/dL"
        if scale is not None:
            point["converted_from"], point["unit"] = point["unit"], target
            convert = lambda value: value*scale + offset
            if point["value"] is not None:
                point["value"] = convert(point["value"])
            elif point["value_text"] and (match := re.fullmatch(r"([<>]=?)([0-9.]+)", point["value_text"])):
                number = _number(match[2])
                if number is not None:
                    point["value_text"] = match[1] + f"{convert(number):g}"
            point["reference_range"] = {k: convert(v) if v is not None else None for k, v in point["reference_range"].items()}
    units = Counter(p["unit"] for p in points)
    unit = units.most_common(1)[0][0] if units else None
    other_units = {u or "unknown": n for u, n in units.items() if u != unit}
    points = [p for p in points if p["unit"] == unit]
    return {"name": name, "points": points, "unit": unit, "other_units": other_units,
            "source_policy": "individual clinical observations; most common unit after known conversions",
            "coverage": coverage([p["date"] for p in points], start, end), "calc_version": "trend/1"}
