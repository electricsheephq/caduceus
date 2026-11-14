"""Local FHIR R4 ingestion; raw resources and date precision stay in the vault."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime, timezone

from ..core import search, sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from ..core.policy import Category, SENSITIVE, categorize, medication_category

PARSER = "fhir"
PARSER_VERSION = "1"
MAX_JSON = 64 << 20
LOINC = "http://loinc.org"
ALIASES = {"hba1c": ("4548-4", "17856-6", "59261-8"),
           "glucose": ("2345-7", "2339-0", "1558-6"),
           "ldl": ("13457-7", "18262-6", "2089-1")}
ICD_SYSTEMS = {"http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10",
               "urn:oid:2.16.840.1.113883.6.90", "urn:oid:2.16.840.1.113883.6.3"}
ICD9_SYSTEMS = {"http://hl7.org/fhir/sid/icd-9-cm", "urn:oid:2.16.840.1.113883.6.103", "urn:oid:2.16.840.1.113883.6.2"}
SNOMED = "http://snomed.info/sct"
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
ALZHEIMERS_SNOMED = frozenset({"26929004", "52448006"})  # Alzheimer's disease; dementia


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
            found = _code_category(coding.get("system"), str(coding.get("code", "")).strip().upper())
            if found is not None:
                return found
    return category


def _code_category(system, code):
    """Sensitive categories from codes alone. A code with no system is matched by its shape: privacy fails closed."""
    if system == LOINC or (not system and re.fullmatch(r"\d{1,7}-\d", code)):
        if code in MENTAL_LOINC:
            return Category.MENTAL_HEALTH
        if code in REPRODUCTIVE_LOINC:
            return Category.REPRODUCTIVE
    if system == SNOMED or (not system and re.fullmatch(r"\d{6,18}", code)):
        if code in ALZHEIMERS_SNOMED:
            return Category.ALZHEIMERS
        if code in MENTAL_SNOMED:
            return Category.MENTAL_HEALTH
        if code in REPRODUCTIVE_SNOMED:
            return Category.REPRODUCTIVE
    if system in ICD_SYSTEMS or (not system and re.fullmatch(r"[A-Z][0-9][0-9A-Z](?:\.?[0-9A-Z]{1,4})?", code)):
        flat = code.replace(".", "")
        if re.match(r"G30|F0[0-3]", flat):  # Alzheimer's disease and the dementias
            return Category.ALZHEIMERS
        if re.match(r"Z15|Z80", flat):  # genetic susceptibility to cancer; family history of cancer
            return Category.CANCER_RISK
        if re.match(r"Z14|Z8481", flat):  # genetic carrier status
            return Category.CARRIER
        if re.match(r"F(?:0[4-9]|[1-9][0-9])", flat):
            return Category.MENTAL_HEALTH
        if re.match(r"O(?:[0-8][0-9]|9[0-9A])|Z3[0-9]|N9[6-8]|N80|N52", flat) or flat == "E282":
            return Category.REPRODUCTIVE
    # ICD-9-CM, written with or without its dot ("296.20" and "29620" are the same code).
    if system in ICD9_SYSTEMS or (not system and re.fullmatch(r"(?:\d{3}|V\d{2})(?:\.?\d{1,2})?", code)):
        flat, head = code.replace(".", ""), code[:3]
        if head.isdigit():
            number = int(head)
            if number == 290 or flat.startswith(("3310", "29410", "29411")):  # dementias, Alzheimer's disease
                return Category.ALZHEIMERS
            if 290 <= number <= 319:  # ICD-9-CM mental disorders
                return Category.MENTAL_HEALTH
            if 630 <= number <= 679 or 614 <= number <= 629 or number == 606 or flat.startswith("60784"):
                return Category.REPRODUCTIVE
        elif head in ("V22", "V23", "V24", "V25", "V26", "V27", "V28"):  # pregnancy, contraception, procreation
            return Category.REPRODUCTIVE
        elif head == "V16" or flat.startswith("V840"):  # family history of, or genetic susceptibility to, cancer
            return Category.CANCER_RISK
        elif head == "V83":  # genetic carrier status
            return Category.CARRIER
    return None


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


def _normalized_name(name):
    return " ".join(str(name).lower().split())


def _observation_key(system, code, when, num, text, unit, component, name):
    parts = ["Observation", system, code, when, [num, text], unit, code if component else None]
    if system is None and code is None:
        parts.append(_normalized_name(name))
    return _key(parts)


def _observation_value(part):
    """Key values before SQLite's column affinities can coerce their JSON types."""
    quantity = part.get("valueQuantity") or {}
    num, text = _number(quantity.get("value")), part.get("valueString")
    if "valueCodeableConcept" in part:
        text = _display(part["valueCodeableConcept"])
    if quantity.get("comparator"):
        num = None
        text = str(quantity["comparator"]) + str(quantity.get("value", ""))
    return num, text, quantity.get("unit") or quantity.get("code")


def _raw(resource, precision, **metadata):
    return json.dumps({**resource, "_caduceus": {"date_precision": precision, **metadata}}, sort_keys=True, separators=(",", ":"))


def _status(concept):
    return next((c["code"] for c in (concept or {}).get("coding", []) if c.get("code")), _display(concept))


def _identity(resource, full_url=None):
    """What names a resource across imports: its absolute fullUrl when its Bundle gives one (ids repeat across
    providers), otherwise its id."""
    if isinstance(full_url, str) and full_url.strip():
        return full_url.strip()
    rid = resource.get("id")
    return rid if isinstance(rid, str) and rid else None


def _resource_ids(document):
    """Every resource that asserts a stored row; the stored list is authoritative once written."""
    meta = document.get("_caduceus") or {}
    ids = meta.get("resource_ids")
    if isinstance(ids, list):
        return {value for value in ids if isinstance(value, str)}
    own = _identity(document, meta.get("full_url"))
    return {own} if own else set()


def _instant(value):
    """A comparable form of a FHIR date: timestamps as UTC instants (so -05:00 and Z agree), others as written."""
    when, precision = _date_value(value)
    if precision == "timestamp":
        parsed = datetime.fromisoformat(when)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    return when


def _note(text):
    try:
        note = json.loads(text or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _stricter(new, stored):
    """The category a stored row keeps, and whether the new text must stay out of the index.

    A sensitive row never changes category: not to an ordinary one, and not to another sensitive one (unlocking
    the new category would show text first filed under the old). Text describing a different sensitive category
    is never indexed under the stored one."""
    if stored in {c.value for c in SENSITIVE}:
        return Category(stored), new in SENSITIVE and new.value != stored
    return new, False


def _stale(document, identity, key, legacy_key=None):
    """Whether a non-correction only re-sends what a stored correction replaced: anything at this code and time from
    the correcting resource, or the identical copy another resource had sent. A different value is new."""
    if document.get("status") not in ("corrected", "amended"):
        return False
    if identity in _resource_ids(document):
        return True
    superseded = ((document.get("_caduceus") or {}).get("superseded") or {}).get(identity)
    return superseded == key or (legacy_key is not None and superseded == legacy_key)


def _restatus(vault, kind, ref, fields, metadata):
    """Update only the status fields of a stored search document, keeping its text, title and category.

    Returns the stored document without its metadata, or None when there is none."""
    row = vault.execute("SELECT body FROM search_doc WHERE kind=? AND ref=?", (kind, str(ref))).fetchone()
    if row is None:
        return None
    try:
        document = json.loads(row["body"])
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    document.update(fields)
    document.setdefault("_caduceus", {}).update(metadata)
    vault.execute("UPDATE search_doc SET body=? WHERE kind=? AND ref=?",
                  (json.dumps(document, sort_keys=True, separators=(",", ":")), kind, str(ref)))
    return {key: value for key, value in document.items() if key != "_caduceus"}


def _skip(counts, kind, reason):
    key = f"skipped:{kind}:{reason}"
    counts[key] = counts.get(key, 0) + 1


def _medications(vault, resource, source_id, name, counts=None, full_url=None):
    """Write only dates provided by the source, with activity status in metadata.

    A later import of the same medication updates the stored status: a record that now says stopped makes an earlier
    start not current. Each row lists the resources that assert it, so a retraction removes only its own."""
    status = resource.get("status")
    period = resource.get("effectivePeriod") or {}
    start, precision = _date_value(period.get("start") or resource.get("effectiveDateTime") or resource.get("authoredOn"))
    end, end_precision = _date_value(period.get("end"))
    # A stop with no date keeps only its start, marked not current in the note: no date is ever invented.
    events = [("start", start, precision)] if start else []
    if status in ("completed", "stopped") and end:
        events.append(("stop", end, end_precision))
    category = _category([resource.get("medicationCodeableConcept") or {}], Category.MEDICATION)
    if category not in SENSITIVE:
        category = medication_category(name)  # a drug prescribed mainly for a sensitive condition
    identity = _identity(resource, full_url)
    scope = {"full_url": full_url} if full_url else {}
    inserted = 0
    held = None  # (category, stored text) once a conflict keeps a row's text: this resource's other events use it too
    for event, when, precision in events:
        when = when[:10] if precision == "timestamp" else when
        metadata = {"date_precision": precision, "status": status, "current": status == "active"}
        if held is not None:
            category = held[0]
        document = resource if held is None else {**held[1], "status": status}
        body = _raw(document, precision, event=event, current=metadata["current"], **scope)
        row = vault.execute("SELECT id, note, category FROM medication_event WHERE drug=? AND event=? AND occurred_on=? "
                            "AND source='fhir'", (name, event, when)).fetchone()
        if row is None:
            note = json.dumps({"dedupe_key": _key([resource["resourceType"], name, event, when]), **metadata,
                               "resource_ids": [identity] if identity else []})
            cur = vault.execute(
                "INSERT INTO medication_event(drug,event,occurred_on,source,source_id,note,category,created_at) "
                "VALUES (?,?,?,'fhir',?,?,?,?)", (name, event, when, source_id, note, category.value, sources.now_iso()))
            ref = cur.lastrowid
            inserted += 1
        else:
            old = _note(row["note"])
            ids = sorted(set(old.get("resource_ids") or []) | ({identity} if identity else set()))
            note = json.dumps({**old, **metadata, "resource_ids": ids})
            kept, conflict = _stricter(category, row["category"])
            changed = note != row["note"] or kept.value != row["category"]
            if changed:
                vault.execute("UPDATE medication_event SET note=?, category=?, source_id=? WHERE id=?",
                              (note, kept.value, source_id, row["id"]))
                if counts is not None:
                    counts["medication_updated"] = counts.get("medication_updated", 0) + 1
            # Whether or not this row changed, the resource's later events keep its category, and after a conflict
            # its stored text as well.
            if conflict:  # the stored text stays under its own category; only the status moves
                stored = _restatus(vault, "medication", row["id"], {"status": status},
                                   {"current": metadata["current"]})
                held = (kept, stored or {"resourceType": resource.get("resourceType"),
                                         "medicationCodeableConcept": {"text": name}})
                continue
            category = kept
            if not changed:
                continue
            ref = row["id"]
        search.index_doc(vault, kind="medication", ref=str(ref), title=name, body=body, category=category, ts=when)
    return inserted


def warning_messages(counts):
    reasons = {"date": "missing or invalid date", "status": "excluded resource status",
               "name": "missing medication display name"}
    return [f"{key.split(':')[1]}: skipped {n} resource(s) ({reasons[key.split(':')[2]]})"
            for key, n in sorted(counts.items()) if key.startswith("skipped:")]


def _observations(vault, resource, source_id, when, precision, counts, full_url=None):
    components = resource.get("component") or []
    # A panel's narrative describes all its parts, so no row of a panel keeps it; a component row keeps none of the
    # panel's own value either.
    base = {key: value for key, value in resource.items() if key != "component" and not (components and key == "text")}
    parent = {key: value for key, value in base.items() if not key.startswith("value")
              and key not in ("interpretation", "referenceRange", "note", "dataAbsentReason")}
    parts = ([(resource, None)] if not components or any(k.startswith("value") for k in resource) else [])
    parts += [(component, component.get("code")) for component in components]
    identity = _identity(resource, full_url)
    corrected = resource.get("status") in ("corrected", "amended")
    inserted = 0
    for part, component in parts:
        # Each row stores only its own part of a panel: a sibling's code or value must never ride along under
        # this row's category (a glucose result beside a depression score).
        own = base if component is None else {**parent, "component": [part]}
        concept = part.get("code") or {}
        system, code, name = _concept(concept, loinc=True)
        num, text, unit = _observation_value(part)
        rr = (part.get("referenceRange") or [{}])[0]
        interpretations = [_concept(c)[2] for c in part.get("interpretation", [])]
        # The value can carry the diagnosis too ("Symptom" = depression), so it counts toward the category.
        category = _category([resource.get("code") or {}, concept, part.get("valueCodeableConcept") or {}],
                             Category.RECORDS)
        key = _observation_key(system, code, when, num, text, unit, component is not None, name)
        legacy_key = _key(["Observation", system, code, when, [num, text], unit, None]) if (
            system is None and code is None) else None
        identities = {identity} if identity else set()
        superseded = {}  # resource -> content key of its identical copy that a correction replaced
        if identity:
            previous = [row for row in vault.execute(
                "SELECT id,dedupe_key,raw_json,effective_at,name FROM observation WHERE source='fhir' AND code_system IS ? "
                "AND code IS ? AND (EXISTS (SELECT 1 FROM json_each(raw_json,'$._caduceus.resource_ids') WHERE value=?) "
                "OR EXISTS (SELECT 1 FROM json_each(raw_json,'$._caduceus.superseded') WHERE key=?))",
                (system, code, identity, identity)).fetchall() if _instant(row["effective_at"]) == _instant(when)
                and (system is not None or code is not None or _normalized_name(row["name"]) == _normalized_name(name))]
            if not corrected and any(_stale(json.loads(row["raw_json"]), identity, key, legacy_key) for row in previous):
                counts["observation_ignored_after_correction"] = counts.get("observation_ignored_after_correction", 0)+1
                continue
            if corrected:
                for row in previous:
                    document = json.loads(row["raw_json"])
                    meta = document.setdefault("_caduceus", {})
                    if identity not in _resource_ids(document):
                        # Another resource's correction already replaced this one's copy: that corrected value stays,
                        # and this correction stands on its own.
                        meta["superseded"] = {k: v for k, v in (meta.get("superseded") or {}).items() if k != identity}
                        if not meta["superseded"]:
                            del meta["superseded"]
                        kept = json.dumps(document, sort_keys=True, separators=(",", ":"))
                        vault.execute("UPDATE observation SET raw_json=? WHERE id=?", (kept, row["id"]))
                        vault.execute("UPDATE search_doc SET body=? WHERE kind='observation' AND ref=?",
                                      (kept, str(row["id"])))
                        continue
                    # A correction replaces every identical copy, but only the correcting resource asserts the new
                    # value: the others are remembered as superseded, so a retraction of the correction removes it.
                    superseded.update(meta.get("superseded") or {})
                    superseded.update({rid: row["dedupe_key"] for rid in _resource_ids(document)})
                    search.remove_doc(vault, kind="observation", ref=str(row["id"]))
                    vault.execute("DELETE FROM observation WHERE id=?", (row["id"],))
                    counts["observation_superseded"] = counts.get("observation_superseded", 0) + 1
        existing = vault.execute("SELECT id,raw_json,name,category FROM observation WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            stored = json.loads(existing["raw_json"])
            identities.update(_resource_ids(stored))
            superseded = {**((stored.get("_caduceus") or {}).get("superseded") or {}), **superseded}
        superseded = {rid: copy for rid, copy in superseded.items() if rid not in identities}
        if corrected or existing is None:
            document, scope = own, full_url
        else:
            document = json.loads(existing["raw_json"])
            scope = (document.get("_caduceus") or {}).get("full_url")
        raw = _raw(document, precision, resource_ids=sorted(identities),
                   **({"superseded": dict(sorted(superseded.items()))} if superseded else {}),
                   **({"full_url": scope} if scope else {}))
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


def _withdraw_row(vault, table, row, identity):
    """Withdraw one identity and its search document, retaining any other resource's assertion."""
    document = json.loads(row["raw_json"])
    others = _resource_ids(document) - {identity}
    if others:
        document.setdefault("_caduceus", {})["resource_ids"] = sorted(others)
        raw = json.dumps(document, sort_keys=True, separators=(",", ":"))
        vault.execute(f"UPDATE {table} SET raw_json=? WHERE id=?", (raw, row["id"]))
        vault.execute("UPDATE search_doc SET body=? WHERE kind=? AND ref=?", (raw, table, str(row["id"])))
        return False
    search.remove_doc(vault, kind=table, ref=str(row["id"]))
    vault.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
    return True


def _retract(vault, resource, kind, counts, full_url=None):
    """A resource re-sent as entered-in-error, cancelled or refuted withdraws what an earlier import stored for it.

    It is matched by identity (fullUrl, else id), then by its time and any of its codes when it gives them. A stored
    value that another resource still asserts is kept, without the withdrawn identity."""
    identity = _identity(resource, full_url)
    if identity is None:
        return
    if kind.startswith("Medication"):
        name = _display(resource.get("medicationCodeableConcept"))
        period = resource.get("effectivePeriod") or {}
        start, _ = _date_value(period.get("start") or resource.get("effectiveDateTime") or resource.get("authoredOn"))
        _retract_medications(vault, identity, counts, drug=name.strip().lower() if name else None, start=start)
        return
    if kind == "Observation":
        table, column, codes = "observation", "effective_at", set()
        when = resource.get("effectiveDateTime") or (resource.get("effectivePeriod") or {}).get("start") or resource.get("issued")
    else:
        table, column = "condition", "onset"
        when = resource.get("onsetDateTime") or (resource.get("onsetPeriod") or {}).get("start")
        codes = {str(c["code"]).strip().upper() for c in (resource.get("code") or {}).get("coding", [])
                 if isinstance(c, dict) and c.get("code")}
    when = _instant(when)
    rows = vault.execute(
        f"SELECT id, code, {column} AS at, raw_json FROM {table} WHERE source='fhir' AND EXISTS "
        "(SELECT 1 FROM json_each(raw_json,'$._caduceus.resource_ids') WHERE value=?)", (identity,)).fetchall()
    removed = 0
    for row in rows:
        if when is not None and not (kind == "Condition" and row["at"] is None) and _instant(row["at"]) != when:
            continue
        if codes and row["code"] is not None and str(row["code"]).strip().upper() not in codes:
            continue
        removed += _withdraw_row(vault, table, row, identity)
    if removed:
        counts[f"{table}_retracted"] = counts.get(f"{table}_retracted", 0) + removed


def _retract_medications(vault, identity, counts, *, drug=None, start=None):
    """Like the other retractions, the identity is cross-checked against the row: the same drug, and the same start
    when the retraction gives one (another provider can reuse an id for a different medication)."""
    removed = 0
    rows = vault.execute("SELECT id, drug, event, occurred_on, note FROM medication_event WHERE source='fhir'").fetchall()
    for row in rows:
        note = _note(row["note"])
        ids = set(note.get("resource_ids") or [])
        if identity not in ids or (drug and row["drug"] != drug):
            continue
        if start and row["event"] == "start" and row["occurred_on"] != (start[:10] if len(start) > 10 else start):
            continue
        if ids - {identity}:
            vault.execute("UPDATE medication_event SET note=? WHERE id=?",
                          (json.dumps({**note, "resource_ids": sorted(ids - {identity})}), row["id"]))
            continue
        search.remove_doc(vault, kind="medication", ref=str(row["id"]))
        vault.execute("DELETE FROM medication_event WHERE id=?", (row["id"],))
        removed += 1
    if removed:
        counts["medication_retracted"] = counts.get("medication_retracted", 0) + removed


def _condition(vault, resource, source_id, when, precision, counts, full_url=None):
    concept = resource.get("code") or {}
    system, code, name = _concept(concept)
    category = _category([concept], Category.RECORDS)
    key = _key([system, code, when, name])
    recorded, recorded_precision = _date_value(resource.get("recordedDate"))
    clinical = _status(resource.get("clinicalStatus"))
    verification = _status(resource.get("verificationStatus"))
    current = False if clinical in ("resolved", "inactive", "remission") else (
        True if clinical in ("active", "recurrence", "relapse") else None)
    identity = _identity(resource, full_url)
    if identity:
        same = [row for row in vault.execute(
            "SELECT id,onset,name,raw_json FROM condition WHERE source='fhir' AND code_system IS ? AND code IS ? "
            "AND EXISTS (SELECT 1 FROM json_each(raw_json,'$._caduceus.resource_ids') WHERE value=?)",
            (system, code, identity)).fetchall()
            if system is not None or code is not None or _normalized_name(row["name"]) == _normalized_name(name)]
        if when is None and any(row["onset"] is not None for row in same):
            return  # an existing known onset wins; like a duplicate, this adds no row
        if when is not None:
            for row in same:
                if row["onset"] is None:
                    _withdraw_row(vault, "condition", row, identity)
    scope = {"full_url": full_url} if full_url else {}
    title = name + (f" [verification: {verification}]" if verification else "")
    if current is False:
        title += f" [not current: {clinical}]"
    if when is None:
        title += f" [onset unknown; recorded {recorded}]"
    previous = vault.execute("SELECT id,clinical_status,raw_json,category,recorded FROM condition WHERE dedupe_key=?", (key,)).fetchone()
    if previous is None:
        raw = _raw(resource, precision, verification_status=verification, current=current,
                   resource_ids=[identity] if identity else [], recorded_precision=recorded_precision, **scope)
        cur = vault.execute(
            "INSERT INTO condition(code_system,code,name,clinical_status,onset,recorded,category,source,source_id,dedupe_key,raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (system, code, name, clinical, when, recorded, category.value, "fhir", source_id, key, raw))
        search.index_doc(vault, kind="condition", ref=str(cur.lastrowid), title=title, body=raw, category=category, ts=when or recorded)
        counts["condition"] += 1
        return
    old = json.loads(previous["raw_json"])
    ids = _resource_ids(old) | ({identity} if identity else set())
    if (previous["clinical_status"] != clinical or _status(old.get("verificationStatus")) != verification
            or previous["recorded"] != recorded):
        category, conflict = _stricter(category, previous["category"])
        document = resource
        if conflict:  # the stored text stays under its own category; only the status moves
            document = {key: value for key, value in old.items() if key not in ("clinicalStatus", "verificationStatus")}
            document.update({key: resource[key] for key in ("clinicalStatus", "verificationStatus") if key in resource})
        raw = _raw(document, precision, verification_status=verification, current=current, resource_ids=sorted(ids),
                   recorded_precision=recorded_precision, **scope)
        vault.execute("UPDATE condition SET clinical_status=?,raw_json=?,source_id=?,category=?,recorded=? WHERE id=?",
                      (clinical, raw, source_id, category.value, recorded, previous["id"]))
        search.index_doc(vault, kind="condition", ref=str(previous["id"]), title=title, body=raw,
                         category=category, ts=when or recorded)
        counts["condition_updated"] = counts.get("condition_updated", 0)+1
    elif ids != _resource_ids(old):  # the same condition from another resource: remember who else asserts it
        old.setdefault("_caduceus", {})["resource_ids"] = sorted(ids)
        raw = json.dumps(old, sort_keys=True, separators=(",", ":"))
        vault.execute("UPDATE condition SET raw_json=? WHERE id=?", (raw, previous["id"]))
        vault.execute("UPDATE search_doc SET body=? WHERE kind='condition' AND ref=?", (raw, str(previous["id"])))


def ingest(vault, document, source_id, *, cancel=None, run_id=None):
    """Shared local ingestion; each resource and its index rows commit together."""
    counts = {"observation": 0, "condition": 0, "medication": 0, "ignored": 0}
    resources, processed = [(document, None)], 0
    while resources:
        if cancel is not None and cancel.is_set():
            break
        if processed and processed % 10_000 == 0 and run_id is not None:
            sources.update_progress(vault, run_id, counts)
        resource, full_url = resources.pop()
        kind = resource.get("resourceType")
        if kind == "Bundle":
            resources.extend(reversed([(e["resource"], e.get("fullUrl")) for e in resource.get("entry", [])
                                       if isinstance(e, dict) and "resource" in e]))
            continue
        processed += 1
        if kind not in ("Observation", "Condition", "MedicationRequest", "MedicationStatement"):
            counts["ignored"] += 1
            continue
        if (kind == "Observation" and resource.get("status") in ("entered-in-error", "cancelled")) or (
                kind == "Condition" and _status(resource.get("verificationStatus")) in ("entered-in-error", "refuted")) or (
                kind.startswith("Medication") and resource.get("status") in ("entered-in-error", "cancelled")):
            _skip(counts, kind, "status")
            with write_txn(vault):
                _retract(vault, resource, kind, counts, full_url)
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
        if when is None and not (kind == "Condition" and _date_value(resource.get("recordedDate"))[0] is not None):
            _skip(counts, kind, "date")
            continue
        with write_txn(vault):
            if kind == "Observation":
                counts["observation"] += _observations(vault, resource, source_id, when, precision, counts, full_url)
            elif kind == "Condition":
                _condition(vault, resource, source_id, when, precision, counts, full_url)
            else:
                counts["medication"] += _medications(vault, resource, source_id, name, counts, full_url)
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


def _stored_quantity(document):
    """The quantity a stored row was read from: its own component's, for a row cut from a panel."""
    parts = document.get("component") or []
    if len(parts) == 1 and isinstance(parts[0], dict):
        return parts[0].get("valueQuantity") or {}
    return document.get("valueQuantity") or {}


def observation_series(vault, name_or_code, *, start=None, end=None, allow=None) -> dict:
    """Return one compatible unit per series, with explicit conversions/exclusions.

    With *allow* (a category predicate), hidden results are removed before anything else is computed from them, so
    units, coverage and unplotted results describe visible results only; ``withheld`` counts the rest."""
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
               "_quantity": _stored_quantity(json.loads(r["raw_json"] or "{}"))} for r in rows]
    low, high = _bound(start) if start else None, _bound(end, end=True) if end else None
    if (start and low is None) or (end and high is None):
        raise ValueError("invalid series date bound")
    points = [p for p in points if _bound(p["date"]) is not None and
              (low is None or _bound(p["date"], end=True) >= low) and
              (high is None or _bound(p["date"]) <= high)]
    withheld = 0
    if allow is not None:
        shown = [p for p in points if allow(p["category"])]
        withheld, points = len(points) - len(shown), shown
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
            "coverage": coverage([p["date"] for p in points], start, end), "calc_version": "trend/1",
            **({"withheld": withheld} if allow is not None else {})}
