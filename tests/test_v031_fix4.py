"""Fix round 4: onset-transition privacy and repeated-correction metadata, synthetic only."""
import json

import pytest

from caduceus.core import search, sources
from caduceus.core.policy import Policy
from caduceus.health import fhir


def _condition(display, rid="synthetic-condition", name="Synthetic dx", **extra):
    return {"resourceType": "Condition", "id": rid,
            "code": {"text": name, "coding": [{"system": "urn:synthetic", "code": "X1", "display": display}]},
            "recordedDate": "2025-02", **extra}


def _only_condition(vault):
    rows = vault.execute("SELECT * FROM condition").fetchall()
    assert len(rows) == 1
    row = rows[0]
    indexed = vault.execute("SELECT * FROM search_doc WHERE kind='condition'").fetchall()
    assert len(indexed) == 1
    assert indexed[0]["body"] == row["raw_json"]
    assert indexed[0]["category"] == row["category"]
    assert indexed[0]["ts"] == (row["onset"] or row["recorded"])
    return row, indexed[0]


def test_onset_transition_keeps_sensitive_category_and_withholds(vault):
    fhir.ingest(vault, _condition("Synthetic depression"), None)
    fhir.ingest(vault, _condition("Synthetic finding", onsetDateTime="2025-01-01"), None)
    row, _ = _only_condition(vault)
    assert (row["onset"], row["category"]) == ("2025-01-01", "mental_health")
    result = search.search(vault, "Synthetic", policy=Policy("standard"), kinds=["condition"])
    assert result["results"] == [] and result["withheld"] == 1


@pytest.mark.parametrize("onset", [{"onsetDateTime": "2025-01"},
                                   {"onsetPeriod": {"start": "2025-01", "end": "2025-03"}}],
                         ids=["datetime", "period"])
def test_onset_transition_conflict_keeps_document_and_moves_statuses(vault, onset):
    original = _condition("Synthetic depression", name="Synthetic kept text",
                          clinicalStatus={"coding": [{"code": "active"}]},
                          verificationStatus={"coding": [{"code": "provisional"}]})
    fhir.ingest(vault, original, None)
    incoming = _condition("Synthetic pregnancy", name="Synthetic rejected text", recordedDate="2026-11-14",
                          clinicalStatus={"coding": [{"code": "resolved"}]},
                          verificationStatus={"coding": [{"code": "confirmed"}]}, **onset)
    fhir.ingest(vault, incoming, None)
    row, indexed = _only_condition(vault)
    raw = json.loads(row["raw_json"])
    assert (row["onset"], row["recorded"], row["category"]) == ("2025-01", "2025-02", "mental_health")
    assert raw["code"] == original["code"] and raw["recordedDate"] == original["recordedDate"]
    for key in ("clinicalStatus", "verificationStatus", *onset):
        assert raw[key] == incoming[key]
    assert ("onsetDateTime" in raw) == ("onsetDateTime" in onset)
    assert ("onsetPeriod" in raw) == ("onsetPeriod" in onset)
    assert row["clinical_status"] == "resolved"
    assert raw["_caduceus"]["recorded_precision"] == raw["_caduceus"]["date_precision"] == "month"
    assert raw["_caduceus"]["verification_status"] == "confirmed" and raw["_caduceus"]["current"] is False
    assert indexed["title"] == "Synthetic kept text [verification: confirmed] [not current: resolved]"
    assert "pregnancy" not in row["raw_json"] and "rejected" not in row["raw_json"]
    for query in ("pregnancy", "rejected"):
        assert search.search(vault, query, policy=Policy("standard"))["withheld"] == 0


@pytest.mark.parametrize("display,category", [("Synthetic depression", "mental_health"),
                                            ("Synthetic finding", "records")])
def test_onset_transition_from_ordinary_uses_incoming_category(vault, display, category):
    fhir.ingest(vault, _condition("Synthetic ordinary"), None)
    incoming = _condition(display, onsetDateTime="2025-01-01")
    fhir.ingest(vault, incoming, None)
    row, _ = _only_condition(vault)
    assert row["category"] == category and json.loads(row["raw_json"])["code"] == incoming["code"]


def test_onset_transition_preserves_other_identity_assertion(vault):
    for rid in ("synthetic-A", "synthetic-B"):
        fhir.ingest(vault, _condition("Synthetic depression", rid), None)
    original = dict(vault.execute("SELECT * FROM condition").fetchone())
    fhir.ingest(vault, _condition("Synthetic finding", "synthetic-A", onsetDateTime="2025-01-01"), None)
    rows = vault.execute("SELECT * FROM condition ORDER BY id").fetchall()
    assert len(rows) == 2 and rows[0]["id"] == original["id"]
    assert [(r["onset"], r["category"]) for r in rows] == [(None, "mental_health"), ("2025-01-01", "mental_health")]
    assert [json.loads(r["raw_json"])["_caduceus"]["resource_ids"] for r in rows] == [["synthetic-B"], ["synthetic-A"]]
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='condition'").fetchone()[0] == 2


def test_onset_transition_uses_lowest_sensitive_row_id(vault):
    for display, name in (("Synthetic ordinary", "Synthetic first"),
                          ("Synthetic depression", "Synthetic second"),
                          ("Synthetic pregnancy", "Synthetic third")):
        fhir.ingest(vault, _condition(display, name=name), None)
    fhir.ingest(vault, _condition("Synthetic pregnancy", onsetDateTime="2025-01-01"), None)
    row, indexed = _only_condition(vault)
    assert row["category"] == "mental_health"
    assert json.loads(row["raw_json"])["code"]["text"] == indexed["title"] == "Synthetic second"


@pytest.mark.parametrize("known_display,expected", [("Synthetic finding", "mental_health"),
                                                  ("Synthetic pregnancy", "reproductive")])
def test_onset_transition_applies_existing_known_row_category(vault, known_display, expected):
    known = _condition(known_display, "synthetic-known", onsetDateTime="2025-01-01")
    fhir.ingest(vault, known, None)
    known_id = vault.execute("SELECT id FROM condition").fetchone()[0]
    fhir.ingest(vault, _condition("Synthetic depression", name="Synthetic unknown text"), None)
    # All statuses and the recorded date are unchanged: inherited privacy still applies.
    fhir.ingest(vault, _condition(known_display, onsetDateTime="2025-01-01"), None)
    row, indexed = _only_condition(vault)
    assert row["id"] == known_id and row["category"] == expected
    assert indexed["title"] == known["code"]["text"]
    raw = json.loads(row["raw_json"])
    assert raw["_caduceus"]["resource_ids"] == ["synthetic-condition", "synthetic-known"]
    if expected == "reproductive":
        assert raw["code"] == known["code"] and "depression" not in row["raw_json"]


def _observation(status, value, low=3, high=10, **extra):
    return {"resourceType": "Observation", "id": "synthetic-observation", "status": status,
            "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
            "effectiveDateTime": "2025-01-01", "valueQuantity": {"value": value, "unit": "mmol/L"},
            "referenceRange": [{"low": {"value": low}, "high": {"value": high}}], **extra}


@pytest.mark.parametrize("status", ["corrected", "amended"])
def test_repeated_correction_refreshes_range_and_interpretation(vault, tmp_path, status):
    def ingest(document, filename):
        path = tmp_path / filename
        path.write_text(json.dumps(document))
        src, _ = sources.store_source(vault, path, "fhir")
        result = fhir.import_fhir(vault, src)
        assert result.status == "succeeded"
        return result

    original = _observation("final", 5)
    copy = {**original, "id": "synthetic-copy"}
    ingest({"resourceType": "Bundle", "entry": [{"resource": r} for r in (original, copy)]}, "original.json")
    old_key = vault.execute("SELECT dedupe_key FROM observation").fetchone()[0]
    ingest(_observation(status, 6), "correction.json")
    before = dict(vault.execute("SELECT * FROM observation").fetchone())
    metadata = json.loads(before["raw_json"])["_caduceus"]
    assert metadata["superseded"] == {"synthetic-copy": old_key}
    result = ingest(_observation(status, 6, low=2, high=4, interpretation=[{"text": "High"}]), "again.json")
    assert result.counts["observation"] == result.counts.get("observation_superseded", 0) == 0
    rows = vault.execute("SELECT * FROM observation").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == before["id"] and row["dedupe_key"] == before["dedupe_key"]
    assert (row["ref_low"], row["ref_high"], row["interpretation"]) == (2, 4, "High")
    assert json.loads(row["raw_json"])["_caduceus"] == metadata
    point = fhir.observation_series(vault, "glucose")["points"][0]
    assert point["reference_range"] == {"low": 36.032, "high": 72.064}
    assert point["unit"] == "mg/dL" and point["interpretation"] == "High"
    assert vault.execute("SELECT body FROM search_doc WHERE kind='observation'").fetchone()[0] == row["raw_json"]


def test_non_corrected_update_keeps_range_and_interpretation(vault):
    fhir.ingest(vault, _observation("final", 5), None)
    fhir.ingest(vault, _observation("final", 5, low=2, high=4, interpretation=[{"text": "High"}]), None)
    row = vault.execute("SELECT * FROM observation").fetchone()
    assert (row["ref_low"], row["ref_high"], row["interpretation"]) == (3, 10, None)
