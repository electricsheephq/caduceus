"""Privacy through the paths the own-code reviews probed: hidden items are counted, never described, and a hidden
item's name never leaves the machine as a search term."""
import importlib
import json
from pathlib import Path

import pytest

from conftest import FakeCtx, load_plugin_package


def _plugin(config):
    ctx = FakeCtx(config)
    load_plugin_package().register(ctx)

    def run(tool, **args):
        return json.loads(ctx.tools[tool]["handler"](args))

    return run


@pytest.fixture
def standard():
    return _plugin({"privacy_level": "standard"})


def _import(call, tmp_path, name, *resources):
    path = tmp_path / name
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}))
    result = call("caduceus_capture", op="import", path=str(path))
    assert result["status"] == "succeeded", result


def _statement(rid, text, start="2024-02-01", status="active"):
    return {"resourceType": "MedicationStatement", "id": rid, "status": status,
            "medicationCodeableConcept": {"text": text}, "effectivePeriod": {"start": start}}


def _symptom(rid, when, **value):
    return {"resourceType": "Observation", "id": rid, "status": "final", "effectiveDateTime": when,
            "code": {"coding": [{"system": "http://loinc.org", "code": "75325-1", "display": "Symptom"}]}, **value}


def test_hidden_lab_results_leave_nothing_beside_the_count(standard, tmp_path):
    _import(standard, tmp_path, "labs.json",
            _symptom("o1", "2024-03-01", valueQuantity={"value": 3, "unit": "{score}"}),
            _symptom("o2", "2024-04-02", valueCodeableConcept={"text": "ZZSECRETX major depression episode"}),
            {**_symptom("o3", "2024-05-03", valueQuantity={"value": 2, "unit": "zzunitx"}),
             "code": {"coding": [{"system": "http://loinc.org", "code": "75325-1", "display": "PHQ-9 item"}]}})
    result = standard("caduceus_trend", op="labs", lab="75325-1")
    text = json.dumps(result)
    assert [p["value"] for p in result["points"]] == [3] and result["withheld"] == 2
    for leak in ("ZZSECRETX", "zzunitx", "2024-04-02", "2024-05-03", "mental_health"):
        assert leak not in text


def test_a_panel_row_carries_only_its_own_component(standard, tmp_path):
    panel = {"resourceType": "Observation", "id": "p1", "status": "final", "effectiveDateTime": "2024-03-01",
             "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8", "display": "Synthetic panel"}]},
             "component": [
                 {"code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
                  "valueQuantity": {"value": 99, "unit": "mg/dL"}},
                 {"code": {"coding": [{"system": "http://loinc.org", "code": "44249-1"}]},
                  "valueQuantity": {"value": 23, "unit": "{score}"}}]}
    _import(standard, tmp_path, "panel.json", panel)
    assert standard("caduceus_search", query="44249-1")["results"] == []
    found = standard("caduceus_search", query="Glucose")["results"]
    assert len(found) == 1
    body = standard("caduceus_search", ref=found[0]["ref"])["body"]
    assert "44249-1" not in body and '"value":23' not in body and '"value":99' in body
    assert [p["value"] for p in standard("caduceus_trend", op="labs", lab="glucose")["points"]] == [99]


def test_a_sensitive_medication_stays_out_of_the_brief_and_compare(standard, tmp_path):
    _import(standard, tmp_path, "meds.json", _statement("m1", "Levonorgestrel oral contraceptive"))
    found = standard("caduceus_search", query="levonorgestrel")
    assert found["results"] == [] and found["withheld"] == 1
    page = Path(standard("caduceus_capture", op="brief")["path"]).read_text(encoding="utf-8")
    assert "evonorgestrel" not in page and "1 item hidden by your privacy settings" in page
    compare = standard("caduceus_trend", op="compare", metric="weight", drug="levonorgestrel")
    assert compare["withheld"] == 1 and "pivot" not in compare and "2024-02-01" not in json.dumps(compare)


def test_hiding_medications_hides_every_medication(tmp_path):
    call = _plugin({"hidden_categories": ["medication"]})  # the open level
    _import(call, tmp_path, "meds.json", _statement("m1", "Levonorgestrel oral contraceptive"))
    found = call("caduceus_search", query="levonorgestrel")
    assert found["results"] == [] and found["withheld"] == 1
    assert call("caduceus_trend", op="compare", metric="weight", drug="levonorgestrel")["withheld"] == 1


def test_a_recorded_medication_gets_its_own_category(standard):
    saved = standard("caduceus_capture", op="medication", drug="sertraline", date="2026-01-05", confirm=True)
    assert saved["saved"] is True and "mental_health" in saved["note"]
    assert standard("caduceus_search", query="sertraline")["results"] == []


def test_hidden_medications_never_become_research_search_terms(standard, tmp_path, monkeypatch):
    package = load_plugin_package()
    watch = importlib.import_module(package.__name__ + ".caduceus.research.watch")
    _import(standard, tmp_path, "meds.json", _statement("m1", "Sertraline 50 mg tablet"),
            _statement("m2", "Ozempic 0.5 mg pen", start="2024-03-01"))
    seen = {}

    def fake_run(conn, stored_watch, **kwargs):
        seen["drugs"] = json.loads(stored_watch["terms_json"])["drugs"]
        return {"new_items": []}

    monkeypatch.setattr(watch, "run_watch", fake_run)
    standard("caduceus_capture", op="watch_run")
    assert "semaglutide" in seen["drugs"] and "sertraline" not in seen["drugs"]


def test_a_hidden_medication_reply_never_shows_what_was_stored(standard):
    standard("caduceus_capture", op="medication", drug="sertraline", date="2026-01-05", dose="50 mg",
             brand="ZZSTOREDBRANDX", confirm=True)
    replay = standard("caduceus_capture", op="medication", drug="sertraline", date="2026-01-05", confirm=True)
    update = standard("caduceus_capture", op="medication", drug="sertraline", date="2026-01-05", dose="100 mg",
                      confirm=True)
    for reply in (replay, update):
        text = json.dumps(reply)
        assert reply["saved"] is True and "50 mg" not in text and "ZZSTOREDBRANDX" not in text
        assert not {"recorded", "already_recorded", "updated", "previous_dose"} & set(reply)
    unlocked = _plugin({})  # the same vault at the open level: the update was stored
    found = unlocked("caduceus_search", query="sertraline")["results"]
    assert len(found) == 1 and "100 mg" in unlocked("caduceus_search", ref=found[0]["ref"])["body"]


def test_a_panel_component_keeps_none_of_the_panels_own_content(standard, tmp_path):
    panel = {"resourceType": "Observation", "id": "p2", "status": "final", "effectiveDateTime": "2024-03-01",
             "text": {"status": "generated", "div": "<div>ZZNARRATIVEX glucose 99</div>"},
             "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8", "display": "Synthetic panel"}]},
             "valueCodeableConcept": {"text": "ZZPARENTX major depression"},
             "interpretation": [{"text": "ZZINTERPX"}], "note": [{"text": "ZZNOTEX"}],
             "component": [{"code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
                            "valueQuantity": {"value": 99, "unit": "mg/dL"}}]}
    _import(standard, tmp_path, "panel.json", panel)
    found = standard("caduceus_search", query="Glucose")["results"]
    assert len(found) == 1
    body = standard("caduceus_search", ref=found[0]["ref"])["body"]
    assert '"value":99' in body
    for leak in ("ZZPARENTX", "ZZNARRATIVEX", "ZZINTERPX", "ZZNOTEX"):
        assert leak not in body
    parent = standard("caduceus_search", query="ZZPARENTX")
    assert parent["results"] == [] and parent["withheld"] == 1
    assert standard("caduceus_search", query="ZZNARRATIVEX")["results"] == []


@pytest.fixture
def reproductive_unlocked():
    return _plugin({"privacy_level": "standard", "unlocked_categories": ["reproductive"]})


def test_a_sensitive_record_never_moves_to_another_sensitive_category(reproductive_unlocked, tmp_path):
    call = reproductive_unlocked
    medication = _statement("m1", "Synthdrug 5 mg tablet")
    medication["medicationCodeableConcept"]["coding"] = [{"display": "Synthdrug for major depression"}]
    condition = {"resourceType": "Condition", "id": "c1", "onsetDateTime": "2024-01-01",
                 "clinicalStatus": {"coding": [{"code": "active"}]},
                 "code": {"text": "Synthetic condition", "coding": [{"display": "major depression"}]}}
    _import(call, tmp_path, "first.json", medication, condition)
    medication = json.loads(json.dumps(medication))
    medication["status"] = "stopped"
    medication["medicationCodeableConcept"]["coding"] = [{"display": "Synthdrug in pregnancy"}]
    condition = json.loads(json.dumps(condition))
    condition["clinicalStatus"] = {"coding": [{"code": "resolved"}]}
    condition["code"]["coding"] = [{"display": "pregnancy related"}]
    _import(call, tmp_path, "second.json", medication, condition)
    for query in ("synthdrug", "Synthetic condition", "pregnancy"):
        assert call("caduceus_search", query=query)["results"] == [], query
    unlocked = _plugin({})
    for query, status in (("synthdrug", '"status":"stopped"'), ("Synthetic condition", '"resolved"')):
        found = unlocked("caduceus_search", query=query)["results"]
        assert len(found) == 1 and found[0]["category"] == "mental_health"
        body = unlocked("caduceus_search", ref=found[0]["ref"])["body"]
        assert "pregnancy" not in body and status in body  # the status moved; the other category's text did not


def test_a_recorded_medication_never_moves_to_another_sensitive_category(reproductive_unlocked):
    call = reproductive_unlocked
    call("caduceus_capture", op="medication", drug="sertraline", date="2026-01-05", brand="ZZBRANDX", confirm=True)
    reply = call("caduceus_capture", op="medication", drug="sertraline", date="2026-01-05",
                 brand="ZZBRANDX pregnancy pack", confirm=True)
    assert "mental_health" in reply["note"]
    assert call("caduceus_search", query="sertraline")["results"] == []
    found = _plugin({})("caduceus_search", query="sertraline")["results"]
    assert len(found) == 1 and found[0]["category"] == "mental_health" and "pregnancy" not in found[0]["title"]
