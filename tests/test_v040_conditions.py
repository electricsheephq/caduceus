"""Condition identity and retraction regressions for #32/#33; synthetic only."""
import json

import pytest

from caduceus.health import fhir


def _condition(name, *, code=None, onset=None, recorded="2024-06-01", clinical="active",
               verification="confirmed"):
    resource = {"resourceType": "Condition", "id": "synthetic-c1", "recordedDate": recorded,
                "clinicalStatus": {"coding": [{"code": clinical}]},
                "verificationStatus": {"coding": [{"code": verification}]}}
    if name is not None:
        resource["code"] = {"text": name}
    if code is not None:
        resource.setdefault("code", {})["coding"] = [
            {"system": "http://example.org/synthetic", "code": code, "display": name}]
    if onset is not None:
        resource["onsetDateTime"] = onset
    return resource


def _ingest(vault, resource):
    counts = fhir.ingest(vault, resource, None)
    public = json.dumps({"counts": counts, "warnings": fhir.warning_messages(counts)})
    concept = resource.get("code", {})
    for text in [concept.get("text"), *[c.get("display") for c in concept.get("coding", [])]]:
        if text:
            assert text not in public
    return counts


def _only_condition(vault):
    rows = vault.execute("SELECT * FROM condition").fetchall()
    assert len(rows) == 1
    indexed = vault.execute("SELECT * FROM search_doc WHERE kind='condition'").fetchall()
    assert len(indexed) == 1
    assert indexed[0]["ref"] == str(rows[0]["id"])
    assert indexed[0]["body"] == rows[0]["raw_json"]
    assert indexed[0]["category"] == rows[0]["category"]
    assert indexed[0]["ts"] == (rows[0]["onset"] or rows[0]["recorded"])
    return rows[0], indexed[0]


def _conflicting_transition(vault):
    original = _condition("Synthetic depression alpha", code="X1")
    _ingest(vault, original)
    row, _ = _only_condition(vault)
    assert row["onset"] is None and row["category"] == "mental_health"
    dated = _condition("Synthetic pregnancy beta", code="X1", onset="2025-01-01", recorded="2024-07-01")
    _ingest(vault, dated)
    row, indexed = _only_condition(vault)
    assert (row["onset"], row["recorded"], row["category"]) == ("2025-01-01", "2024-06-01", "mental_health")
    assert row["name"] == original["code"]["text"]
    assert json.loads(indexed["body"])["code"] == original["code"]
    assert dated["code"]["text"] not in indexed["body"]
    return original, dated, row


def test_conflict_onsetless_status_updates_the_known_row(vault):
    original, dated, before = _conflicting_transition(vault)
    onsetless = _condition(dated["code"]["text"], code="X1", clinical="resolved", verification="provisional")
    counts = _ingest(vault, onsetless)
    row, indexed = _only_condition(vault)
    assert counts["condition"] == 0 and counts["condition_updated"] == 1
    assert row["id"] == before["id"] and row["dedupe_key"] == before["dedupe_key"]
    assert (row["clinical_status"], row["onset"], row["recorded"], row["category"]) == (
        "resolved", "2025-01-01", "2024-06-01", "mental_health")
    raw = json.loads(row["raw_json"])
    assert raw["code"] == original["code"] and raw["onsetDateTime"] == dated["onsetDateTime"]
    assert raw["clinicalStatus"] == onsetless["clinicalStatus"]
    assert raw["verificationStatus"] == onsetless["verificationStatus"]
    assert raw["_caduceus"]["current"] is False
    assert raw["_caduceus"]["date_precision"] == raw["_caduceus"]["recorded_precision"] == "day"
    assert "resolved" in indexed["title"] and "provisional" in indexed["title"]
    assert "pregnancy" not in indexed["body"] and "beta" not in indexed["body"]
    assert _ingest(vault, onsetless).get("condition_updated", 0) == 0
    assert _only_condition(vault)[0]["raw_json"] == row["raw_json"]
    # Coded identity still retracts the retained text despite the incoming name.
    counts = _ingest(vault, {**dated, "verificationStatus": {"coding": [{"code": "entered-in-error"}]}})
    assert counts["condition_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='condition'").fetchone()[0] == 0


def test_conflict_dated_resend_writes_nothing(vault):
    _, dated, before = _conflicting_transition(vault)
    changes = vault.total_changes
    counts = _ingest(vault, dated)
    assert counts.get("condition_updated", 0) == 0
    assert vault.total_changes == changes
    row, _ = _only_condition(vault)
    assert dict(row) == dict(before)
    assert row["raw_json"].encode() == before["raw_json"].encode()
    # Real status changes still move across the conflict; their re-send is also a no-op.
    resolved = {**dated, "clinicalStatus": {"coding": [{"code": "resolved"}]}}
    assert _ingest(vault, resolved)["condition_updated"] == 1
    row, indexed = _only_condition(vault)
    assert row["clinical_status"] == "resolved" and "resolved" in indexed["body"]
    assert "pregnancy" not in indexed["body"]
    assert _ingest(vault, resolved).get("condition_updated", 0) == 0
    assert _only_condition(vault)[0]["raw_json"] == row["raw_json"]


@pytest.mark.parametrize("first_onset,other_onset,retraction_onset", [
    ("2025-01-01", "2025-02-01", None),
    ("2025-01-01", None, "2025-01-01"),
    (None, None, None),
    (None, None, "2025-01-01"),
    ("2025-01-01", "2025-01-01", "2025-01-01"),
], ids=["undated-retraction", "dated-with-recorded-only-other", "both-recorded-only",
        "both-recorded-only-dated-retraction", "same-onset"])
@pytest.mark.parametrize("first_name", ["Synthetic finding alpha", None], ids=["named", "unknown"])
def test_uncoded_retraction_requires_its_normalized_name(vault, first_name, first_onset, other_onset,
                                                       retraction_onset):
    first = _condition(first_name, onset=first_onset)
    other = _condition("Synthetic finding beta", onset=other_onset)
    for resource in (first, other):
        _ingest(vault, resource)
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 2
    other_before = dict(vault.execute("SELECT * FROM condition WHERE name=?", (other["code"]["text"],)).fetchone())
    normalized = "  SYNTHETIC   finding ALPHA  " if first_name else None
    counts = _ingest(vault, _condition(normalized, onset=retraction_onset, verification="entered-in-error"))
    assert counts["condition_retracted"] == 1
    survivor, _ = _only_condition(vault)
    assert dict(survivor) == other_before
    # The surviving uncoded Condition can still withdraw its own normalized name.
    counts = _ingest(vault, _condition("  synthetic FINDING   BETA ", onset=other_onset,
                                     verification="entered-in-error"))
    assert counts["condition_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='condition'").fetchone()[0] == 0
    # Coded rows still narrow by code, without requiring matching text.
    for code in ("X1", "X2"):
        _ingest(vault, _condition("Synthetic coded finding", code=code, onset="2025-01-01"))
    counts = _ingest(vault, _condition("Synthetic renamed finding", code="X1", onset="2025-01-01",
                                     verification="entered-in-error"))
    assert counts["condition_retracted"] == 1
    assert _only_condition(vault)[0]["code"] == "X2"


def _system_only(name, **kwargs):
    """A concept with a code system but no code: coded for identity, so retraction must not ask for its name."""
    resource = _condition(name, **kwargs)
    resource["code"] = {"text": name, "coding": [{"system": "http://example.org/synthetic", "display": name}]}
    return resource


def test_system_only_condition_retracts_by_identity_after_its_text_changed(vault):
    _ingest(vault, _system_only("Synthetic finding gamma", onset="2025-01-01"))
    # An onset-less copy with other text updates the same row (same system, no code: no name comparison).
    _ingest(vault, _system_only("Synthetic finding delta", clinical="resolved"))
    row, _ = _only_condition(vault)
    assert row["code_system"] == "http://example.org/synthetic" and row["code"] is None
    counts = _ingest(vault, _system_only("Synthetic finding delta", verification="entered-in-error"))
    assert counts["condition_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='condition'").fetchone()[0] == 0


@pytest.mark.parametrize("concept", [{"coding": ["x"]}, {"coding": "x"}, {"coding": None}, "bad", [], ["x"]],
                         ids=["non-dict-entry", "string-coding", "null-coding", "string-code", "empty-list-code",
                              "list-code"])
@pytest.mark.parametrize("stored", ["Synthetic finding epsilon", None], ids=["named", "unknown"])
def test_malformed_retraction_concept_keeps_uncoded_rows(vault, concept, stored):
    _ingest(vault, _condition(stored, onset="2025-01-01"))
    retraction = _condition(None, onset="2025-01-01", verification="entered-in-error")
    retraction["code"] = concept
    later = _condition("Synthetic finding zeta", onset="2025-02-01")
    later["id"] = "synthetic-c2"
    # The malformed retraction neither raises nor stops the rest of the Bundle.
    counts = fhir.ingest(vault, {"resourceType": "Bundle", "entry": [{"resource": retraction}, {"resource": later}]},
                         None)
    assert counts.get("condition_retracted", 0) == 0 and counts["condition"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 2
