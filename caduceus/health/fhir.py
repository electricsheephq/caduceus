"""Local FHIR R4 ingestion; raw resources and date precision stay in the vault."""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from ..core import search, sources
from ..core.contracts import ImportResult
from ..core.db import write_txn
from ..core.policy import Category, SENSITIVE, categorize, medication_category

PARSER = "fhir"
PARSER_VERSION = "1"
MAX_JSON = 64 << 20
REPORT_TYPES = ("DiagnosticReport", "DocumentReference", "Composition", "ImagingStudy")
MAX_ATTACHMENT = 2 << 20
MAX_NARRATIVE = 200_000
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
               "name": "missing medication display name",
               "attachment_unreadable": "unreadable inline text attachment"}
    return [f"{key.split(':')[1]}: skipped {n} resource(s) ({reasons[key.split(':')[2]]})"
            for key, n in sorted(counts.items()) if key.startswith("skipped:")]


def _observations(vault, resource, source_id, when, precision, counts, full_url=None, resolver=None):
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
        ref_low = _number((rr.get("low") or {}).get("value"))
        ref_high = _number((rr.get("high") or {}).get("value"))
        interpretation = "; ".join(interpretations) or None
        # The value can carry the diagnosis too ("Symptom" = depression), so it counts toward the category.
        category = _category([resource.get("code") or {}, concept, part.get("valueCodeableConcept") or {}],
                             Category.RECORDS)
        key = _observation_key(system, code, when, num, text, unit, component is not None, name)
        legacy_key = _key(["Observation", system, code, when, [num, text], unit, None]) if (
            system is None and code is None) else None
        identities = {identity} if identity else set()
        typed_ids = {f"Observation/{rid}"} if (rid := _identity(resource)) else set()
        source_ids = {source_id} - {None}
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
                    if row["dedupe_key"] == key and document.get("status") in ("corrected", "amended"):
                        continue  # re-reading an existing correction upserts it without superseding it again
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
                    # The replaced copy's typed ids stay privacy aliases on the correcting row: a report may name any
                    # of them and must still be withheld when this result is hidden.
                    typed_ids.update(t for t in meta.get("typed_ids", []) if isinstance(t, str))
                    if replaced_id := _identity(document):
                        typed_ids.add(f"Observation/{replaced_id}")
                    search.remove_doc(vault, kind="observation", ref=str(row["id"]))
                    vault.execute("DELETE FROM observation WHERE id=?", (row["id"],))
                    if resolver is not None:
                        resolver.refresh("Observation", row["id"])
                    counts["observation_superseded"] = counts.get("observation_superseded", 0) + 1
        existing = vault.execute("SELECT id,raw_json,name,category,source_id FROM observation WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            stored = json.loads(existing["raw_json"])
            identities.update(_resource_ids(stored))
            meta = stored.get("_caduceus") or {}
            typed_ids.update(meta.get("typed_ids", []))
            if stored_id := _identity(stored):
                typed_ids.add(f"Observation/{stored_id}")
            source_ids.update(report_source_ids(meta, existing["source_id"]))
            superseded = {**((stored.get("_caduceus") or {}).get("superseded") or {}), **superseded}
        superseded = {rid: copy for rid, copy in superseded.items() if rid not in identities}
        if corrected or existing is None:
            document, scope = own, full_url
        else:
            document = json.loads(existing["raw_json"])
            scope = (document.get("_caduceus") or {}).get("full_url")
        raw = _raw(document, precision, resource_ids=sorted(identities),
                   typed_ids=sorted(typed_ids), source_ids=sorted(source_ids),
                   **({"superseded": dict(sorted(superseded.items()))} if superseded else {}),
                   **({"full_url": scope} if scope else {}))
        cur = vault.execute(
            "INSERT OR IGNORE INTO observation(code_system,code,name,value_num,value_text,unit,effective_at,"
            "ref_low,ref_high,interpretation,category,source,source_id,dedupe_key,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (system, code, name, num, text, unit, when, ref_low, ref_high, interpretation,
             category.value, "fhir", source_id, key, raw))
        if cur.rowcount:
            search.index_doc(vault, kind="observation", ref=str(cur.lastrowid), title=name,
                             body=raw, category=category, ts=when)
            inserted += 1
            categories = counts.setdefault("observation_by_category", {})
            for category_code in observation_categories(own):
                categories[category_code] = categories.get(category_code, 0) + 1
        elif existing:
            # Natural-key dedupe must retain all resource ids for later corrections.
            if corrected:
                vault.execute("UPDATE observation SET raw_json=?,name=?,category=?,source_id=?,"
                              "ref_low=?,ref_high=?,interpretation=? WHERE id=?",
                              (raw, name, category.value, source_id, ref_low, ref_high, interpretation, existing["id"]))
            else:
                vault.execute("UPDATE observation SET raw_json=? WHERE id=?", (raw, existing["id"]))
            search.index_doc(vault, kind="observation", ref=str(existing["id"]),
                             title=name if corrected else existing["name"], body=raw,
                             category=category if corrected else existing["category"], ts=when)
        if resolver is not None:
            resolver.refresh("Observation", cur.lastrowid if cur.rowcount else existing["id"])
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

    It is matched by identity (fullUrl, else id), then by its time and any of its codes when it gives them.
    Uncoded Conditions also match by normalized name. A stored value that another resource still asserts is
    kept, without the withdrawn identity."""
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
        concept = resource.get("code")
        coding = concept.get("coding") if isinstance(concept, dict) else None
        codes = {str(c["code"]).strip().upper() for c in (coding if isinstance(coding, list) else [])
                 if isinstance(c, dict) and c.get("code")}
    when = _instant(when)
    retracted_name = None
    # A malformed concept (not an object) matches no uncoded row: a missed match is preferred to a wrong withdrawal.
    if kind == "Condition" and (resource.get("code") is None or isinstance(resource.get("code"), dict)):
        try:
            retracted_name = _normalized_name(_concept(resource.get("code"))[2])
        except (AttributeError, TypeError, IndexError):
            pass
    rows = vault.execute(
        f"SELECT id, code_system, code, name, {column} AS at, raw_json FROM {table} WHERE source='fhir' AND EXISTS "
        "(SELECT 1 FROM json_each(raw_json,'$._caduceus.resource_ids') WHERE value=?)", (identity,)).fetchall()
    removed = 0
    for row in rows:
        if when is not None and not (kind == "Condition" and row["at"] is None) and _instant(row["at"]) != when:
            continue
        if codes and row["code"] is not None and str(row["code"]).strip().upper() not in codes:
            continue
        # Uncoded exactly as _condition's identity filter means it: neither a code system nor a code.
        if (kind == "Condition" and row["code_system"] is None and row["code"] is None
                and _normalized_name(row["name"]) != retracted_name):
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


def _condition(vault, resource, source_id, when, precision, counts, full_url=None, *, row_id=None):
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
    inherited_category = False
    if identity:
        same = [row for row in vault.execute(
            "SELECT id,onset,name,raw_json,category FROM condition WHERE source='fhir' AND code_system IS ? AND code IS ? "
            "AND EXISTS (SELECT 1 FROM json_each(raw_json,'$._caduceus.resource_ids') WHERE value=?) ORDER BY id",
            (system, code, identity)).fetchall()
            if system is not None or code is not None or _normalized_name(row["name"]) == _normalized_name(name)]
        known = [row for row in same if row["onset"] is not None]
        if when is None and known:
            for row in known:
                old = json.loads(row["raw_json"])
                # Keep the known dates and document; only the incoming statuses move.
                document = {k: v for k, v in old.items() if k not in ("clinicalStatus", "verificationStatus")}
                document.update({k: resource[k] for k in ("id", "clinicalStatus", "verificationStatus") if k in resource})
                _condition(vault, document, source_id, row["onset"], old.get("_caduceus", {}).get("date_precision"),
                           counts, full_url, row_id=row["id"])
            return
        if when is not None:
            unknown = [row for row in same if row["onset"] is None]
            stored = next((row for row in unknown if row["category"] in {c.value for c in SENSITIVE}), None)
            if stored is not None:
                inherited_category = True
                category, conflict = _stricter(category, stored["category"])
                if conflict:
                    old = json.loads(stored["raw_json"])
                    moving = ("clinicalStatus", "verificationStatus", "onsetDateTime", "onsetPeriod")
                    document = {k: v for k, v in old.items() if k not in moving}
                    document.update({k: resource[k] for k in moving if k in resource})
                    resource = document
                    name = _concept(resource.get("code"))[2]
                    recorded, recorded_precision = _date_value(resource.get("recordedDate"))
            for row in unknown:
                _withdraw_row(vault, "condition", row, identity)
    scope = {"full_url": full_url} if full_url else {}
    title = name + (f" [verification: {verification}]" if verification else "")
    if current is False:
        title += f" [not current: {clinical}]"
    if when is None:
        title += f" [onset unknown; recorded {recorded}]"
    column, value = ("dedupe_key", key) if row_id is None else ("id", row_id)
    previous = vault.execute(
        f"SELECT id,clinical_status,raw_json,category,recorded FROM condition WHERE {column}=?", (value,)).fetchone()
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
    category, conflict = _stricter(category, previous["category"])
    document = resource
    if conflict:  # the stored text stays under its own category; only the status moves
        document = {key: value for key, value in old.items() if key not in ("clinicalStatus", "verificationStatus")}
        document.update({key: resource[key] for key in ("clinicalStatus", "verificationStatus") if key in resource})
        title = _concept(old.get("code"))[2] + title[len(name):]
        recorded = previous["recorded"]
        recorded_precision = old.get("_caduceus", {}).get("recorded_precision")
        if when is None:
            title = title.rsplit(" [onset unknown; recorded ", 1)[0] + f" [onset unknown; recorded {recorded}]"
    if (previous["clinical_status"] != clinical or _status(old.get("verificationStatus")) != verification
            or previous["recorded"] != recorded or inherited_category):
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


class _ReportHTML(HTMLParser):
    """Extract inert prose, excluding script/style contents and separating block text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden.append(tag)
        if not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _report_text(value, *, html=False):
    if not isinstance(value, str):
        return ""
    if html:
        parser = _ReportHTML()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return " ".join(value.split())


def _report_label(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _report_object(value):
    return value if isinstance(value, dict) else {}


def _report_concept(value):
    """Normalize only readable labels before shared concept/category readers see them."""
    concept = next(iter(_read_objects(value)), {})
    coding = []
    for item in _read_objects(concept.get("coding")):
        normalized = {k: label for k in ("code", "display") if (label := _report_label(item.get(k))) is not None}
        if isinstance(item.get("system"), str):
            normalized["system"] = item["system"]
        if normalized:
            coding.append(normalized)
    text = _report_label(concept.get("text"))
    return {"coding": coding, **({"text": text} if text is not None else {})}


def _report_dates(resource):
    kind = resource["resourceType"]
    if kind == "DiagnosticReport":
        return [resource.get("effectiveDateTime"), _report_object(resource.get("effectivePeriod")).get("start"),
                resource.get("issued")]
    if kind == "DocumentReference":
        context = next(iter(_read_objects(resource.get("context"))), {})
        return [resource.get("date"), _report_object(context.get("period")).get("start")]
    return [resource.get("date" if kind == "Composition" else "started")]


def _report_identity(resource, full_url=None):
    # Export-local UUIDs may change each time the same resource is exported.
    if isinstance(full_url, str) and full_url.strip().startswith("urn:uuid:") and _identity(resource):
        return _identity(resource)
    return _identity(resource, full_url)


def _report_rank(resource):
    status = resource.get("docStatus") if resource.get("resourceType") == "DocumentReference" else None
    status = status or resource.get("status")
    return 0 if status in ("registered", "partial", "preliminary") else (
        2 if status in ("amended", "corrected", "appended") else 1)


class ReportResolver:
    """Call-local identity index: broad privacy, narrowly attributable display.

    Only identity/category metadata crosses the table scan. Later ingest writes
    refresh individual entries; no resolver survives into another public call.
    """
    _projection = ("id,source_id,category,json_extract(raw_json,'$.id') AS resource_id,"
                   "json_extract(raw_json,'$._caduceus.resource_ids') AS resource_ids,"
                   "json_extract(raw_json,'$._caduceus.typed_ids') AS typed_ids,"
                   "json_extract(raw_json,'$._caduceus.source_ids') AS source_ids")
    _tables = {"Observation": "observation", "ImagingStudy": "clinical_report"}

    def __init__(self, vault):
        self.vault = vault
        self.indexes = {}

    def _keys(self, row, kind):
        identities = json.loads(row["resource_ids"] or "[]")
        identities = {x for x in identities if isinstance(x, str)} if isinstance(identities, list) else set()
        relative = {identity[i + 1:] for identity in identities
                    for i, char in enumerate(identity) if char == "/"}
        relative.update(json.loads(row["typed_ids"] or "[]"))
        if isinstance(row["resource_id"], str):
            relative.add(f"{kind}/{row['resource_id']}")
        return identities, relative

    def _put(self, kind, row, ref):
        rows, exact, relative = self.indexes[kind]
        old = rows.pop(ref, None)
        if old:
            for index, keys in zip((exact, relative), self._keys(old, kind)):
                for key in keys:
                    index[key].discard(ref)
                    if not index[key]:
                        del index[key]
        if row is not None:
            row = dict(row)
            row["source_ids"] = report_source_ids({"source_ids": json.loads(row["source_ids"] or "null")}, row["source_id"])
            rows[ref] = row
            for index, keys in zip((exact, relative), self._keys(row, kind)):
                for key in keys:
                    index.setdefault(key, set()).add(ref)

    def _load(self, kind):
        if kind not in self.indexes:
            self.indexes[kind] = ({}, {}, {})
            where = " WHERE resource_type='ImagingStudy'" if kind == "ImagingStudy" else ""
            for row in self.vault.execute(f"SELECT {self._projection} FROM {self._tables[kind]}" + where):
                self._put(kind, row, row["id"])
        return self.indexes[kind]

    def refresh(self, kind, ref):
        if kind in self.indexes:
            row = self.vault.execute(f"SELECT {self._projection} FROM {self._tables[kind]} WHERE id=?",
                                     (ref,)).fetchone()
            self._put(kind, row, ref)

    def resolve(self, references, *, resource_type="Observation", broad=False, source_ids=(), full_url=None):
        refs = {r["reference"] for r in _read_objects(references) if isinstance(r.get("reference"), str)}
        if not refs:
            return []
        rows, exact, relative = self._load(resource_type)
        sources = set(source_ids) - {None}
        base = None
        if isinstance(full_url, str):
            url = urlsplit(full_url)
            if url.scheme in ("http", "https") and url.netloc:
                base = urlunsplit((url.scheme, url.netloc, url.path.rsplit("/", 2)[0] + "/", "", ""))
        matches = set()
        for ref in refs:
            matches.update(exact.get(ref, ()))
            candidates = relative.get(ref, ())
            if broad:
                matches.update(candidates)
                trailing = _trailing_reference(ref, resource_type)
                if trailing:
                    matches.update(relative.get(trailing, ()))
            else:
                matches.update(key for key in candidates if rows[key]["source_ids"] & sources)
                if base and not urlsplit(ref).scheme and not ref.startswith("/"):
                    matches.update(exact.get(base + ref, ()))
        return [rows[key] for key in sorted(matches)]


def _trailing_reference(ref, resource_type):
    """`Type/id` at the end of a reference, without a version suffix, base URL, query or fragment.

    Only for broad (privacy) matching: however an exporter writes the reference, a hidden row it may name is found.
    """
    path = urlsplit(ref).path if urlsplit(ref).scheme in ("http", "https") else ref.split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 4 and parts[-2] == "_history":
        parts = parts[:-2]
    if len(parts) >= 2 and parts[-2] == resource_type and parts[-1]:
        return f"{resource_type}/{parts[-1]}"
    return None


def report_source_ids(metadata, source_id):
    """Report and Observation rows without source history use their original source."""
    values = metadata.get("source_ids")
    return set(values if isinstance(values, list) else [source_id]) - {None}


def _report_attachments(document, counts):
    """Decode bounded inline text only. Strip all inline bytes from the stored resource copy."""
    attachments = _read_objects(document.get("presentedForm"))
    attachments += [a for content in _read_objects(document.get("content"))
                    for a in _read_objects(content.get("attachment"))]
    metadata, texts = [], []
    for attachment in attachments:
        data = attachment.pop("data", None)
        content_type = attachment.get("contentType")
        media_type = str(content_type or "").split(";", 1)[0].strip().lower()
        metadata.append({"content_type": content_type, "size": attachment.get("size"),
                         "title": attachment.get("title"), "has_inline_data": data is not None,
                         "has_url": bool(attachment.get("url"))})
        if data is None or media_type not in ("text/plain", "text/html", "application/xhtml+xml", "text/markdown"):
            continue
        # Reject oversize text before decoding, even if the declared size is missing or misleading.
        if isinstance(data, str):
            data = data.translate(str.maketrans("", "", " \t\n\r\v\f"))
            decoded_size = (len(data) // 4) * 3 - (len(data) - len(data.rstrip("=")))
            if len(data) > 4 * ((MAX_ATTACHMENT + 2) // 3) or decoded_size > MAX_ATTACHMENT:
                continue
        try:
            decoded = base64.b64decode(data, validate=True)
            if len(decoded) > MAX_ATTACHMENT:
                continue
            text = _report_text(decoded.decode("utf-8-sig"), html=media_type in ("text/html", "application/xhtml+xml"))
        except (ValueError, TypeError, binascii.Error, UnicodeError):
            _skip(counts, document["resourceType"], "attachment_unreadable")
            continue
        if text:
            texts.append(text)
    return metadata, texts


def _strip_report_data(document):
    """Remove nested attachment/Binary bytes without decoding or following references."""
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("resourceType") == "Binary" or node.keys() & {"contentType", "url", "title", "size"}:
                node.pop("data", None)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def _section_text(sections):
    # Iterative traversal keeps nested sections in source order without a recursion limit of our own.
    pending, texts = list(reversed(_read_objects(sections))), []
    while pending:
        section = pending.pop()
        texts.extend([_report_text(section.get("title")),
                      _report_text(_report_object(section.get("text")).get("div"), html=True)])
        pending.extend(reversed(_read_objects(section.get("section"))))
    return texts


def _report(vault, resource, source_id, counts, full_url=None, resolver=None):
    kind = resource["resourceType"]
    when, precision = next((parsed for value in _report_dates(resource)
                            if (parsed := _date_value(value))[0] is not None), (None, None))
    if when is None:
        _skip(counts, kind, "date")
        return
    concept = _report_concept(resource.get("code") if kind == "DiagnosticReport" else resource.get("type"))
    system, code, name = _concept(concept)
    if kind == "Composition" and name == "Unknown":
        name = _report_text(resource.get("title")) or name
    if kind == "ImagingStudy":
        name = _report_text(resource.get("description")) or ", ".join(
            str(m.get("display") or m.get("code")) for m in _read_objects(resource.get("modality"))
            if m.get("display") or m.get("code")) or "Imaging study"
    document = copy.deepcopy(resource)
    attachments, texts = _report_attachments(document, counts)
    conclusion_codes = [_report_concept(c) for c in _read_objects(resource.get("conclusionCode"))]
    conclusion = _report_text(resource.get("description") if kind == "DocumentReference" else resource.get("conclusion"))
    if kind == "DiagnosticReport":
        if not texts:
            texts.append(_report_text(_report_object(resource.get("text")).get("div"), html=True))
        texts.extend(_concept(c)[2] for c in conclusion_codes)
    elif kind == "Composition":
        texts.extend([_report_text(resource.get("title")), *_section_text(resource.get("section"))])
    narrative = " ".join(t for t in texts if t)
    category = _category([concept, *conclusion_codes,
                          *[_report_concept(c) for c in _read_objects(resource.get("category"))]], Category.RECORDS)
    privacy_text = " ".join([name, conclusion, narrative, _report_text(resource.get("description")),
                             *[_concept(c)[2] for c in conclusion_codes],
                             *[_report_text(a.get("title")) for a in attachments]])
    sensitive = categorize(text=privacy_text, default=Category.RECORDS)
    if sensitive in SENSITIVE:
        category = sensitive
    linked_rows = [*resolver.resolve(resource.get("result"), broad=True),
                   *resolver.resolve(resource.get("imagingStudy"), broad=True, resource_type="ImagingStudy")]
    for linked in linked_rows:
        if category not in SENSITIVE and linked["category"] in {c.value for c in SENSITIVE}:
            category = Category(linked["category"])
    # Results and studies contained in the report itself (referenced as "#id") are never stored on their own, so
    # their categories are recorded with the report and checked on every read.
    contained = {f"#{c['id']}": c for c in _read_objects(resource.get("contained")) if isinstance(c.get("id"), str)}
    contained_categories = set()
    for field in ("result", "imagingStudy"):
        for link in _read_objects(resource.get(field)):
            target = contained.get(link.get("reference")) if isinstance(link.get("reference"), str) else None
            if not isinstance(target, dict):
                continue
            parts = [target, *_read_objects(target.get("component"))]  # a contained panel's components count too
            for part in parts:
                coded = _category([_report_concept(part.get("code")), _report_concept(part.get("valueCodeableConcept"))],
                                  Category.RECORDS)
                if coded in SENSITIVE:
                    contained_categories.add(coded)
            worded = categorize(text=_report_text(target.get("description")), default=Category.RECORDS)
            if worded in SENSITIVE:
                contained_categories.add(worded)
    if category not in SENSITIVE and contained_categories:
        category = sorted(contained_categories, key=lambda c: c.value)[0]
    identity = _report_identity(resource, full_url)
    seen_identity = _identity(resource, full_url)
    parts = ["Report", kind, identity, system, code, when] if identity else [
        "Report", kind, system, code, when, _normalized_name(name)]
    key = _key(parts)
    aliases = {seen_identity} if seen_identity else set()
    metadata = {"resource_ids": sorted(aliases), "identity_aliases": sorted(aliases), "attachments": attachments,
                "source_ids": sorted({source_id} - {None})}
    if contained_categories:
        metadata["contained_categories"] = sorted(c.value for c in contained_categories)
    if full_url:
        metadata["full_url"] = full_url
    if kind == "DiagnosticReport":
        metadata.update({key: resource[key] for key in ("result", "imagingStudy") if key in resource})
    if kind == "ImagingStudy":
        metadata.update(modalities=resource.get("modality", []),
                        body_sites=[s["bodySite"] for s in _read_objects(resource.get("series")) if "bodySite" in s],
                        **{key: resource[key] for key in ("numberOfSeries", "numberOfInstances") if key in resource})
        document["series"] = [{k: v for k, v in series.items() if k != "instance"}
                              for series in _read_objects(document.get("series"))]
    if len(narrative) > MAX_NARRATIVE:
        narrative = narrative[:MAX_NARRATIVE]
        metadata["narrative_truncated"] = True
    issued = _date_value(resource.get("issued"))[0]
    status = resource.get("status") or resource.get("docStatus")
    previous = vault.execute("SELECT * FROM clinical_report WHERE dedupe_key=?", (key,)).fetchone()
    if previous:
        old = json.loads(previous["raw_json"])
        old_meta = old.get("_caduceus", {})
        metadata["source_ids"] = sorted(report_source_ids(old_meta, previous["source_id"]) | set(metadata["source_ids"]))
        metadata["resource_ids"] = sorted(_resource_ids(old) | aliases)
        metadata["identity_aliases"] = sorted(set(old_meta.get("identity_aliases", [_identity(old, old_meta.get("full_url"))]))
                                              - {None} | aliases)
        if _report_rank(resource) < _report_rank(old) or (
                _report_rank(resource) == _report_rank(old) and previous["issued"] and not issued) or (
                issued and previous["issued"] and _instant(issued) < _instant(previous["issued"])):
            old.setdefault("_caduceus", {}).update({key: metadata[key] for key in ("resource_ids", "identity_aliases", "source_ids")})
            _strip_report_data(old)
            vault.execute("UPDATE clinical_report SET raw_json=? WHERE id=?",
                          (json.dumps(old, sort_keys=True, separators=(",", ":")), previous["id"]))
            if kind == "ImagingStudy":
                resolver.refresh(kind, previous["id"])
            return
        category, conflict = _stricter(category, previous["category"])
        if conflict:
            # Preserve the existing sensitive document; only the status may move across categories.
            document = old
            document["status"] = status
            if "docStatus" in resource:
                document["docStatus"] = resource["docStatus"]
            metadata = {**old_meta, **{key: metadata[key] for key in ("resource_ids", "identity_aliases", "source_ids")}}
            source_id = previous["source_id"]  # retained text/references keep their source-resolution boundary
            name, conclusion, narrative, issued = (previous[k] for k in ("name", "conclusion", "narrative", "issued"))
    # Metadata may reference objects from the original input; sanitize only copies.
    metadata = copy.deepcopy(metadata)
    _strip_report_data(document)
    _strip_report_data(metadata)
    raw = _raw(document, precision, **{k: v for k, v in metadata.items() if k != "date_precision"})
    values = (name, status, issued, conclusion, narrative, category.value, source_id, raw)
    if previous is None:
        cur = vault.execute("INSERT INTO clinical_report(resource_type,code_system,code,effective_at,dedupe_key,"
                            "name,status,issued,conclusion,narrative,category,source_id,raw_json,source) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'fhir')", (kind, system, code, when, key, *values))
        ref = cur.lastrowid
        counts["report"] += 1
    else:
        ref = previous["id"]
        if raw == previous["raw_json"] and category.value == previous["category"]:
            return
        vault.execute("UPDATE clinical_report SET name=?,status=?,issued=?,conclusion=?,narrative=?,category=?,"
                      "source_id=?,raw_json=? WHERE id=?", (*values, ref))
        counts["report_updated"] = counts.get("report_updated", 0) + 1
    if kind == "ImagingStudy":
        resolver.refresh(kind, ref)
    search.index_doc(vault, kind="report", ref=str(ref), title=name,
                     body=" ".join(t for t in (name, conclusion, narrative) if t), category=category, ts=when)


def _retract_report(vault, resource, counts, full_url=None, resolver=None):
    identity = _report_identity(resource, full_url)
    if identity is None:
        return
    concept = _report_concept(resource.get("code") if resource["resourceType"] == "DiagnosticReport" else resource.get("type"))
    codes = [(c.get("system"), c["code"]) for c in concept["coding"] if c.get("code")]
    dates = _report_dates(resource)
    when = next((at for value in dates if (at := _instant(value)) is not None), None)
    if when is None and any(value is not None for value in dates):
        return  # an invalid supplied date must not turn a narrow withdrawal into a broad one
    rows = vault.execute("SELECT * FROM clinical_report WHERE source='fhir' AND resource_type=?",
                         (resource["resourceType"],)).fetchall()
    for row in rows:
        document = json.loads(row["raw_json"])
        metadata = document.get("_caduceus", {})
        identities = _resource_ids(document)
        own_identity = _report_identity(document, metadata.get("full_url"))
        if identity not in identities and identity != own_identity:
            continue
        if codes and not any(row["code"] == code and (not system or row["code_system"] == system)
                             for system, code in codes):
            continue
        if when is not None and _instant(row["effective_at"]) != when:
            continue
        withdrawing = {identity}
        if identity == own_identity:
            withdrawing.update(metadata.get("identity_aliases", [_identity(document, metadata.get("full_url"))]))
        others = identities - withdrawing
        if others:
            metadata["resource_ids"] = sorted(others)
            metadata["identity_aliases"] = sorted(set(metadata.get("identity_aliases", [])) - withdrawing)
            document["_caduceus"] = metadata
            vault.execute("UPDATE clinical_report SET raw_json=? WHERE id=?", (json.dumps(document, sort_keys=True,
                          separators=(",", ":")), row["id"]))
        else:
            search.remove_doc(vault, kind="report", ref=str(row["id"]))
            vault.execute("DELETE FROM clinical_report WHERE id=?", (row["id"],))
            counts["report_retracted"] = counts.get("report_retracted", 0) + 1
        if resource["resourceType"] == "ImagingStudy":
            resolver.refresh("ImagingStudy", row["id"])


def ingest(vault, document, source_id, *, cancel=None, run_id=None):
    """Shared local ingestion; each resource and its index rows commit together."""
    counts = {"observation": 0, "condition": 0, "medication": 0, "report": 0, "ignored": 0,
              "ignored_by_type": {}, "observation_by_category": {}}
    resources, processed = [(document, None)], 0
    resolver = None
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
        if kind in REPORT_TYPES:
            if resolver is None:
                resolver = ReportResolver(vault)
            with write_txn(vault):
                if resource.get("status") == "entered-in-error" or (
                        kind == "DocumentReference" and resource.get("docStatus") == "entered-in-error") or (
                        kind in ("DiagnosticReport", "ImagingStudy") and resource.get("status") == "cancelled"):
                    _skip(counts, kind, "status")
                    _retract_report(vault, resource, counts, full_url, resolver)
                else:
                    _report(vault, resource, source_id, counts, full_url, resolver)
            continue
        if kind not in ("Observation", "Condition", "MedicationRequest", "MedicationStatement"):
            counts["ignored"] += 1
            label = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z]{1,64}", kind) else "other"
            counts["ignored_by_type"][label] = counts["ignored_by_type"].get(label, 0) + 1
            continue
        if (kind == "Observation" and resource.get("status") in ("entered-in-error", "cancelled")) or (
                kind == "Condition" and _status(resource.get("verificationStatus")) in ("entered-in-error", "refuted")) or (
                kind.startswith("Medication") and resource.get("status") in ("entered-in-error", "cancelled")):
            _skip(counts, kind, "status")
            with write_txn(vault):
                linked = resolver.resolve([{"reference": _identity(resource, full_url)}], broad=True) if (
                    resolver is not None and kind == "Observation" and "Observation" in resolver.indexes) else []
                _retract(vault, resource, kind, counts, full_url)
                for row in linked:
                    resolver.refresh("Observation", row["id"])
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
                counts["observation"] += _observations(vault, resource, source_id, when, precision, counts, full_url, resolver)
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


def _read_objects(value):
    """Read a single object or an array of objects, ignoring unreadable entries."""
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _read_label(value):
    for concept in _read_objects(value):
        coding = _read_objects(concept.get("coding"))
        labels = [concept.get("text"), *[c.get("display") for c in coding], *[c.get("code") for c in coding]]
        label = next((s for s in labels if isinstance(s, str) and s), None)
        if label is not None:
            return label
    return None


def _read_quantity(value):
    for quantity in _read_objects(value):
        result = {key: quantity[key] for key in ("unit", "code", "system") if isinstance(quantity.get(key), str)}
        number = quantity.get("value")
        if isinstance(number, (int, float, str)) and not isinstance(number, bool):
            result["value"] = number
        if result:
            return result
    return {}


def _read_bounds(value):
    for bounds in _read_objects(value):
        result = {key: quantity for key in ("low", "high") if (quantity := _read_quantity(bounds.get(key)))}
        if result:
            return result
    return {}


def _stored_part(document):
    """The row's own part, rather than a panel or sibling's clinical content."""
    parts = _read_objects(document.get("component"))
    if len(parts) == 1:
        return parts[0]
    return document


def _stored_quantity(document):
    """The quantity a stored row was read from: its own component's, for a row cut from a panel."""
    return _read_quantity(_stored_part(document).get("valueQuantity"))


def observation_categories(document):
    """Coarse FHIR categories only; local codes must not leak through aggregate counts."""
    standard = {"social-history", "vital-signs", "imaging", "laboratory", "procedure",
                "survey", "exam", "therapy", "activity"}
    categories = set()
    for concept in _read_objects(document.get("category")):
        coding = [c for c in _read_objects(concept.get("coding")) if isinstance(c.get("code"), str) and c["code"]]
        preferred = [c for c in coding if c.get("system") ==
                     "http://terminology.hl7.org/CodeSystem/observation-category" and c.get("code")]
        categories.update(c["code"] if c["code"] in standard else "other" for c in (preferred or coding))
    return sorted(categories or {"unspecified"})


def observation_query(system, code, name):
    """The same series address for discovery and a lab-name fallback."""
    return f"{system}|{code}" if system not in (None, "", LOINC) and code else code or name


def clinical_series(vault, *, allow):
    """Discover visible observations before aggregation; no hidden metadata enters a group."""
    groups, withheld = {}, 0
    for row in vault.execute("SELECT * FROM observation ORDER BY effective_at,id"):
        if not allow(row["category"]):
            withheld += 1
            continue
        code, system = row["code"], row["code_system"]
        key = (None if system in (None, "", LOINC) else system, code) if code else (_normalized_name(row["name"]),)
        if key not in groups:
            groups[key] = {"name": row["name"], "code": code, "code_system": system,
                           "units": set(), "count": 0, "numeric": 0, "first": row["effective_at"],
                           "last": row["effective_at"], "sources": set(), "fhir_categories": set()}
        group = groups[key]
        if code and system == LOINC:
            group["code_system"] = LOINC  # a visible explicit LOINC row labels the merged bare-code series
        group.update(name=row["name"], last=row["effective_at"])
        group["count"] += 1
        group["numeric"] += row["value_num"] is not None
        if row["unit"] is not None:
            group["units"].add(row["unit"])
        group["sources"].add(row["source"])
        group["fhir_categories"].update(observation_categories(json.loads(row["raw_json"] or "{}")))
    entries = sorted(groups.values(), key=lambda g: g["name"])
    for group in entries:
        for field in ("units", "sources", "fhir_categories"):
            group[field] = sorted(group[field])
        group["query"] = observation_query(group["code_system"], group["code"], group["name"])
    return {"clinical_series": entries[:300], "withheld": withheld,
            **({"clinical_series_truncated": len(entries) - 300} if len(entries) > 300 else {})}


def observation_counts(vault):
    """Row totals, counting each category once per row and each observation only once overall."""
    categories, total, other = Counter(), 0, 0
    for row in vault.execute("SELECT raw_json FROM observation"):
        codes = observation_categories(json.loads(row["raw_json"] or "{}"))
        total += 1
        categories.update(codes)
        other += not ({"laboratory", "vital-signs"} & set(codes))
    return {"observations": total, "lab_results": categories["laboratory"],
            "vital_signs": categories["vital-signs"], "observations_by_category": dict(sorted(categories.items())),
            "other_observations": other}


def _reference_ranges(document):
    result = []
    for ref in _read_objects(_stored_part(document).get("referenceRange")):
        entry = _read_bounds(ref)
        if isinstance(ref.get("text"), str):
            entry["text"] = ref["text"]
        if (label := _read_label(ref.get("type"))) is not None:
            entry["type"] = label
        applies = [label for c in _read_objects(ref.get("appliesTo")) if (label := _read_label(c)) is not None]
        if applies or ref.get("appliesTo") == []:
            entry["applies_to"] = applies
        if age := _read_bounds(ref.get("age")):
            entry["age"] = age
        result.append(entry)
    return result


def observation_series(vault, name_or_code, *, start=None, end=None, allow=None) -> dict:
    """Return one compatible unit per series, with explicit conversions/exclusions.

    With *allow* (a category predicate), hidden results are removed before anything else is computed from them, so
    units, coverage and unplotted results describe visible results only; ``withheld`` counts the rest."""
    from .trends import _bound, coverage

    name = str(name_or_code).strip()
    if "|" in name:
        system, code = name.split("|", 1)
        rows = vault.execute("SELECT * FROM observation WHERE code_system=? AND code=? ORDER BY effective_at,id",
                             (system, code)).fetchall()
        if not rows:
            rows = [r for r in vault.execute("SELECT * FROM observation WHERE code IS NULL OR code='' "
                                            "ORDER BY effective_at,id") if _normalized_name(r["name"]) == _normalized_name(name)]
    else:
        codes = tuple(dict.fromkeys((*ALIASES.get(name.casefold(), ()), name)))
        where = "((code IN (" + ",".join("?" for _ in codes) + ") AND (code_system IS NULL OR code_system='' OR code_system=?)) OR (code IS NULL OR code=''))"
        rows = [r for r in vault.execute("SELECT * FROM observation WHERE " + where + " ORDER BY effective_at,id",
                                        [*codes, LOINC]) if r["code"] or _normalized_name(r["name"]) == _normalized_name(name)]
    points = []
    for r in rows:
        if allow is not None and not allow(r["category"]):
            # Retain only what is needed to count hidden rows within the requested date window.
            points.append({"date": r["effective_at"], "category": r["category"], "_hidden": True})
            continue
        document = json.loads(r["raw_json"] or "{}")
        points.append({"date": r["effective_at"], "date_precision": _date_value(r["effective_at"])[1],
               "code": r["code"], "value": r["value_num"], "value_text": r["value_text"], "unit": r["unit"],
               "reference_range": {"low": r["ref_low"], "high": r["ref_high"]},
               "reference_ranges": _reference_ranges(document),
               "interpretation": r["interpretation"], "source": r["source"], "source_id": r["source_id"],
               "category": r["category"], "status": document.get("status"),
               "_quantity": _stored_quantity(document), "_system": r["code_system"]})
    low, high = _bound(start) if start else None, _bound(end, end=True) if end else None
    if (start and low is None) or (end and high is None):
        raise ValueError("invalid series date bound")
    points = [p for p in points if _bound(p["date"]) is not None and
              (low is None or _bound(p["date"], end=True) >= low) and
              (high is None or _bound(p["date"]) <= high)]
    withheld = 0
    if allow is not None:
        shown = [p for p in points if not p.get("_hidden")]
        withheld, points = len(points) - len(shown), shown
    for point in points:
        scale, offset = None, 0
        quantity = point.pop("_quantity")
        system = point.pop("_system")
        original_units = {quantity.get("unit"), quantity.get("code"), point["unit"]} - {None, ""}
        first_range = next(iter(point["reference_ranges"]), {})
        if any(bound.get(key) and bound[key] not in original_units
               for bound in (first_range.get("low", {}), first_range.get("high", {})) for key in ("unit", "code")):
            point["reference_range_mismatch"] = True
            point["reference_range"] = {"low": None, "high": None}
        hba1c = system in (None, "", LOINC) and (point["code"] in ALIASES["hba1c"] or name.casefold() == "hba1c")
        glucose = system in (None, "", LOINC) and (point["code"] in ALIASES["glucose"] or name.casefold() == "glucose")
        if hba1c:
            original_unit = point["unit"]
            unit = (quantity.get("code") if quantity.get("system") == "http://unitsofmeasure.org" else None) or original_unit or ""
            unit = re.sub(r"\{[^}]*\}", "", unit)
            if unit == "%" or unit.casefold() == "% of total hgb":
                point["unit"] = "%"
            elif unit == "mmol/mol":
                point["unit"] = "mmol/mol"
            if point["unit"] != original_unit:
                point["unit_normalized_from"] = original_unit
        if hba1c and point["unit"] == "mmol/mol":
            scale, offset, target = 1/10.929, 2.15, "%"
        elif glucose and (
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
