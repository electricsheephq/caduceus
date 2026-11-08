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
ICD_SYSTEMS = {"http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10",
               "urn:oid:2.16.840.1.113883.6.90", "urn:oid:2.16.840.1.113883.6.3"}
# Public code-list receipts and lookup sources are recorded in LANE_REPORT.md.
MENTAL_LOINC = frozenset({"69737-5", "70274-6", "44249-1", "44261-6", "44260-8", "55757-9", "55758-7",
                         "71354-5", "99046-5", "72109-2", "75626-2"})
REPRODUCTIVE_LOINC = frozenset({"82810-3", "11449-6", "77996-7", "2106-3", "2107-1", "2118-8", "2119-6",
                               "45194-8", "80384-1", "19080-1"})
MENTAL_SNOMED = frozenset({"35489007", "370143000", "197480006", "21897009", "371631005", "13746004",
                          "58214004", "47505003", "191736004", "72366004", "56882008", "78004001",
                          "7200002", "66590003", "11061003", "75544000", "6471006", "248062006"})
REPRODUCTIVE_SNOMED = frozenset({"77386006", "17369002", "34801009", "129103003", "237055002", "8619003",
                               "860914002", "397803000"})


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
            if system in (LOINC, "http://snomed.info/sct"):
                mental = MENTAL_LOINC if system == LOINC else MENTAL_SNOMED
                reproductive = REPRODUCTIVE_LOINC if system == LOINC else REPRODUCTIVE_SNOMED
                if code in mental:
                    return Category.MENTAL_HEALTH
                if code in reproductive:
                    return Category.REPRODUCTIVE
            if system in ICD_SYSTEMS or (not system and re.fullmatch(r"[A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]+)?", code)):
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


def _raw(resource, precision, **metadata):
    return json.dumps({**resource, "_caduceus": {"date_precision": precision, **metadata}}, sort_keys=True, separators=(",", ":"))


def _status(concept):
    return next((c["code"] for c in (concept or {}).get("coding", []) if c.get("code")), _display(concept))


def _resource_ids(document):
    return {value for value in [document.get("id"), *(document.get("_caduceus") or {}).get("resource_ids", [])]
            if isinstance(value, str)}


def _skip(counts, kind, reason):
    key = f"skipped:{kind}:{reason}"
    counts[key] = counts.get(key, 0) + 1


def _medications(vault, resource, source_id, name):
    """Write only dates provided by the source, with activity status in metadata."""
    status = resource.get("status")
    period = resource.get("effectivePeriod") or {}
    start, precision = _date_value(period.get("start") or resource.get("effectiveDateTime") or resource.get("authoredOn"))
    end, end_precision = _date_value(period.get("end"))
    # A stop with no date keeps only its start, marked not current in the note: no date is ever invented.
    events = [("start", start, precision)] if start else []
    if status in ("completed", "stopped") and end:
        events.append(("stop", end, end_precision))
    category = _category([resource.get("medicationCodeableConcept") or {}], Category.MEDICATION)
    inserted = 0
    for event, when, precision in events:
        when = when[:10] if precision == "timestamp" else when
        metadata = {"date_precision": precision, "status": status, "current": status == "active"}
        note = json.dumps({"dedupe_key": _key([resource["resourceType"], name, event, when]), **metadata})
        cur = vault.execute(
            "INSERT OR IGNORE INTO medication_event(drug,event,occurred_on,source,source_id,note,created_at) "
            "VALUES (?,?,?,'fhir',?,?,?)", (name, event, when, source_id, note, sources.now_iso()))
        if cur.rowcount:
            search.index_doc(vault, kind="medication", ref=str(cur.lastrowid), title=name,
                             body=_raw(resource, precision, event=event, current=metadata["current"]),
                             category=category, ts=when)
            inserted += 1
    return inserted


def warning_messages(counts):
    reasons = {"date": "missing or invalid date", "status": "excluded resource status",
               "name": "missing medication display name"}
    return [f"{key.split(':')[1]}: skipped {n} resource(s) ({reasons[key.split(':')[2]]})"
            for key, n in sorted(counts.items()) if key.startswith("skipped:")]


def _observations(vault, resource, source_id, when, precision, counts):
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
        identities = _resource_ids({"id": resource.get("id")})
        corrected = resource.get("status") in ("corrected", "amended")
        if resource.get("id"):
            previous = vault.execute(
                "SELECT id,dedupe_key,raw_json FROM observation WHERE source='fhir' AND code_system IS ? "
                "AND code IS ? AND effective_at=? AND (json_extract(raw_json,'$.id')=? OR EXISTS "
                "(SELECT 1 FROM json_each(raw_json,'$._caduceus.resource_ids') WHERE value=?))",
                (system, code, when, resource["id"], resource["id"])).fetchall()
            if not corrected and any(json.loads(row["raw_json"]).get("status") in ("corrected", "amended") for row in previous):
                counts["observation_ignored_after_correction"] = counts.get("observation_ignored_after_correction", 0)+1
                continue
            if corrected:
                for row in previous:
                    identities.update(_resource_ids(json.loads(row["raw_json"])))
                    search.remove_doc(vault, kind="observation", ref=str(row["id"]))
                    vault.execute("DELETE FROM observation WHERE id=?", (row["id"],))
                counts["observation_superseded"] = counts.get("observation_superseded", 0)+len(previous)
        existing = vault.execute("SELECT id,raw_json,name,category FROM observation WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            identities.update(_resource_ids(json.loads(existing["raw_json"])))
        document = resource if corrected or existing is None else json.loads(existing["raw_json"])
        raw = _raw(document, precision, resource_ids=sorted(identities))
        cur = vault.execute(
            "INSERT OR IGNORE INTO observation(code_system,code,name,value_num,value_text,unit,effective_at,"
            "ref_low,ref_high,interpretation,category,source,source_id,dedupe_key,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (system, code, name, num, text, unit, when, _number((rr.get("low") or {}).get("value")),
             _number((rr.get("high") or {}).get("value")), "; ".join(interpretations) or None,
             category.value, "fhir", source_id, key, raw))
        if cur.rowcount:
            search.index_doc(vault, kind="observation", ref=str(cur.lastrowid), title=name,
                             body=raw, category=category, ts=when)
            inserted += 1
        elif existing:
            # Natural-key dedupe must retain all resource ids for later corrections.
            if corrected:
                vault.execute("UPDATE observation SET raw_json=?,name=?,category=?,source_id=? WHERE id=?",
                              (raw, name, category.value, source_id, existing["id"]))
            else:
                vault.execute("UPDATE observation SET raw_json=? WHERE id=?", (raw, existing["id"]))
            search.index_doc(vault, kind="observation", ref=str(existing["id"]),
                             title=name if corrected else existing["name"], body=raw,
                             category=category if corrected else existing["category"], ts=when)
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
        if (kind == "Observation" and resource.get("status") in ("entered-in-error", "cancelled")) or (
                kind == "Condition" and _status(resource.get("verificationStatus")) in ("entered-in-error", "refuted")):
            _skip(counts, kind, "status")
            continue
        if kind.startswith("Medication"):
            status = resource.get("status")
            allowed = {"active", "completed", "on-hold"} | ({"intended"} if kind == "MedicationStatement" else set())
            if status not in allowed and status != "stopped":
                _skip(counts, kind, "status")
                continue
            name = _display(resource.get("medicationCodeableConcept"))
            if not name or not name.strip():
                _skip(counts, kind, "name")
                continue
            name = name.strip().lower()
            period = resource.get("effectivePeriod") or {}
            when = period.get("start") or resource.get("effectiveDateTime") or resource.get("authoredOn") or period.get("end")
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
                counts["observation"] += _observations(vault, resource, source_id, when, precision, counts)
                continue
            if kind == "Condition":
                concept = resource.get("code") or {}
                system, code, name = _concept(concept)
                category = _category([concept], Category.RECORDS)
                key = _key([system, code, when, name])
                clinical = _status(resource.get("clinicalStatus"))
                verification = _status(resource.get("verificationStatus"))
                current = False if clinical in ("resolved", "inactive", "remission") else (
                    True if clinical in ("active", "recurrence", "relapse") else None)
                raw = _raw(resource, precision, verification_status=verification, current=current)
                cur = vault.execute(
                    "INSERT OR IGNORE INTO condition(code_system,code,name,clinical_status,onset,category,source,source_id,dedupe_key,raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (system, code, name, clinical, when, category.value, "fhir", source_id, key, raw))
                search_kind = "condition"
                title = name + (f" [verification: {verification}]" if verification else "")
                if current is False:
                    title += f" [not current: {clinical}]"
                if not cur.rowcount:
                    previous = vault.execute("SELECT id,clinical_status,raw_json FROM condition WHERE dedupe_key=?", (key,)).fetchone()
                    old_verification = _status(json.loads(previous["raw_json"]).get("verificationStatus"))
                    if previous["clinical_status"] != clinical or old_verification != verification:
                        vault.execute("UPDATE condition SET clinical_status=?,raw_json=?,source_id=?,category=? WHERE id=?",
                                      (clinical, raw, source_id, category.value, previous["id"]))
                        search.index_doc(vault, kind="condition", ref=str(previous["id"]), title=title,
                                         body=raw, category=category, ts=when)
                        counts["condition_updated"] = counts.get("condition_updated", 0)+1
            else:
                counts["medication"] += _medications(vault, resource, source_id, name)
                continue
            if cur.rowcount:
                search.index_doc(vault, kind=search_kind, ref=str(cur.lastrowid), title=title,
                                 body=raw, category=category, ts=when)
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
               "category": r["category"], "status": json.loads(r["raw_json"] or "{}").get("status"),
               "_quantity": json.loads(r["raw_json"] or "{}").get("valueQuantity", {})} for r in rows]
    low, high = _bound(start) if start else None, _bound(end, end=True) if end else None
    if (start and low is None) or (end and high is None):
        raise ValueError("invalid series date bound")
    points = [p for p in points if _bound(p["date"]) is not None and
              (low is None or _bound(p["date"], end=True) >= low) and
              (high is None or _bound(p["date"]) <= high)]
    for point in points:
        scale, offset = None, 0
        quantity = point.pop("_quantity")
        if point["code"] in ALIASES["hba1c"] or name.casefold() == "hba1c":
            original_unit = point["unit"]
            unit = (quantity.get("code") if quantity.get("system") == "http://unitsofmeasure.org" else None) or original_unit or ""
            unit = re.sub(r"\{[^}]*\}", "", unit)
            if unit == "%" or unit.casefold() == "% of total hgb":
                point["unit"] = "%"
            elif unit == "mmol/mol":
                point["unit"] = "mmol/mol"
            if point["unit"] != original_unit:
                point["unit_normalized_from"] = original_unit
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
    units = Counter(p["unit"] for p in points if p["value"] is not None)
    known_units = Counter({unit: n for unit, n in units.items() if unit})
    votes = known_units or units
    unit = votes.most_common(1)[0][0] if votes else next((p["unit"] for p in points if p["unit"]), None)
    other_units = {u or "unknown": n for u, n in Counter(p["unit"] for p in points).items() if u != unit}
    unplotted = [p for p in points if p["value"] is None]
    points = [p for p in points if p["unit"] == unit]
    return {"name": name, "points": points, "unplotted_points": unplotted, "unit": unit, "other_units": other_units,
            "source_policy": "individual clinical observations; most common unit after known conversions",
            "coverage": coverage([p["date"] for p in points], start, end), "calc_version": "trend/1"}
