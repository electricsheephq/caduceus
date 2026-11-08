"""Local FHIR R4 ingestion. The stored original remains the provenance record."""
from __future__ import annotations

import hashlib
import json
import math

from ..core import search, sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from ..core.policy import Category, categorize

PARSER = "fhir"
PARSER_VERSION = "1"
MAX_JSON = 64 << 20
ALIASES = {"hba1c": ("4548-4", "17856-6"), "glucose": ("2345-7",),
           "ldl": ("13457-7", "18262-6")}


def _concept(value, *, loinc=False):
    value = value or {}
    coding = value.get("coding") or []
    chosen = next((c for c in coding if loinc and "loinc.org" in c.get("system", "")),
                  coding[0] if coding else {})
    return chosen.get("system"), chosen.get("code"), value.get("text") or chosen.get("display") or chosen.get("code", "Unknown")


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def ingest(vault, document, source_id, *, cancel=None, run_id=None):
    """Shared by standalone JSON and zip members; writes one resource per transaction.

    Medication ids are retained in note, alongside the core's frozen natural key.
    Observation/Condition keys include resource type because FHIR ids are type-local.
    """
    counts = {"observation": 0, "condition": 0, "medication": 0, "ignored": 0}
    resources = [document]
    processed = 0
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
        raw = json.dumps(resource, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        key = kind + ":" + str(resource.get("id") or hashlib.sha256(raw.encode()).hexdigest()) if kind else ""
        with write_txn(vault):
            if kind == "Observation":
                system, code, name = _concept(resource.get("code"), loinc=True)
                quantity = resource.get("valueQuantity") or {}
                ranges = resource.get("referenceRange") or [{}]
                rr = ranges[0]
                interpretations = [_concept(c)[2] for c in resource.get("interpretation", [])]
                when = resource.get("effectiveDateTime") or (resource.get("effectivePeriod") or {}).get("start") or resource.get("issued") or ""
                category = categorize(text=name, default=Category.RECORDS)
                cur = vault.execute(
                    "INSERT OR IGNORE INTO observation(code_system,code,name,value_num,value_text,unit,effective_at,"
                    "ref_low,ref_high,interpretation,category,source,source_id,dedupe_key,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (system, code, name, _number(quantity.get("value")), resource.get("valueString"),
                     quantity.get("unit") or quantity.get("code"), when,
                     _number((rr.get("low") or {}).get("value")), _number((rr.get("high") or {}).get("value")),
                     "; ".join(interpretations) or None, category.value, "fhir", source_id, key, raw))
                search_kind = "observation"
            elif kind == "Condition":
                system, code, name = _concept(resource.get("code"))
                when = resource.get("onsetDateTime") or (resource.get("onsetPeriod") or {}).get("start")
                category = categorize(text=name, default=Category.RECORDS)
                cur = vault.execute(
                    "INSERT OR IGNORE INTO condition(code_system,code,name,clinical_status,onset,category,source,source_id,dedupe_key,raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (system, code, name, _concept(resource.get("clinicalStatus"))[2], when,
                     category.value, "fhir", source_id, key, raw))
                search_kind = "condition"
            elif kind in ("MedicationRequest", "MedicationStatement"):
                if vault.execute("SELECT 1 FROM medication_event WHERE source='fhir' AND note=?", (key,)).fetchone():
                    continue
                _, _, name = _concept(resource.get("medicationCodeableConcept"))
                if name == "Unknown":
                    name = (resource.get("medicationReference") or {}).get("display", "Unknown")
                name = name.strip().lower()
                when = (resource.get("effectivePeriod") or {}).get("start") or resource.get("effectiveDateTime") or resource.get("authoredOn") or ""
                category = categorize(text=name, default=Category.MEDICATION)
                cur = vault.execute(
                    "INSERT OR IGNORE INTO medication_event(drug,event,occurred_on,source,source_id,note,created_at) "
                    "VALUES (?,'start',?,'fhir',?,?,?)", (name, when, source_id, key, sources.now_iso()))
                search_kind = "medication"
            else:
                counts["ignored"] += 1
                continue
            if cur.rowcount:
                search.index_doc(vault, kind=search_kind, ref=str(cur.lastrowid), title=name,
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
    sources.finish_import(vault, run.id, counts=counts, status=status, errors=errors)
    return ImportResult(status, run.id, src.id, counts, errors)


def observation_series(vault, name_or_code, *, start=None, end=None) -> dict:
    """Return lab points without pooling potentially incompatible lab units."""
    name = str(name_or_code).strip()
    codes = ALIASES.get(name.casefold(), (name,))
    where = "(code IN (" + ",".join("?" for _ in codes) + ") OR lower(name)=?)"
    params = [*codes, name.lower()]
    if start:
        where += " AND substr(effective_at,1,10)>=?"
        params.append(str(start)[:10])
    if end:
        where += " AND substr(effective_at,1,10)<=?"
        params.append(str(end)[:10])
    rows = vault.execute("SELECT * FROM observation WHERE " + where + " ORDER BY effective_at,id", params)
    points = [{"date": r["effective_at"], "code": r["code"], "value": r["value_num"],
               "value_text": r["value_text"], "unit": r["unit"],
               "reference_range": {"low": r["ref_low"], "high": r["ref_high"]},
               "interpretation": r["interpretation"], "source": r["source"], "source_id": r["source_id"],
               "category": r["category"]} for r in rows]
    from .trends import coverage

    units = sorted({p["unit"] for p in points if p["unit"]})
    return {"name": name, "points": points, "unit": units[0] if len(units) == 1 else units,
            "source_policy": "individual clinical observations; no unit pooling",
            "coverage": coverage([p["date"][:10] for p in points], start, end), "calc_version": "trend/1"}
