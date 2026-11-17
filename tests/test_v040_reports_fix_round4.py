"""Round 4 (PR #44 bot review): contained linked results, and search/browse read one snapshot."""
import json

import pytest

from caduceus.core import db, search
from caduceus.core.policy import Policy
from caduceus.health import fhir
from tests.test_v040_reports import _fetch, _observation, _report


def _hidden(vault, policy):
    assert _fetch(vault, policy=policy)["withheld"] is True
    assert search.search(vault, "repeatedvalue", kinds="report", policy=policy)["results"] == []
    assert search.browse(vault, kinds="report", policy=policy)["results"] == []


def test_contained_result_category_withholds_report(vault):
    report = _report(conclusion="repeatedvalue 987654", result=[{"reference": "#private"}],
                     contained=[_observation("private", "44261-6", 987654)])
    fhir.ingest(vault, report, None)
    _hidden(vault, Policy(level="standard"))


def test_contained_result_second_sensitive_category_is_checked(vault):
    # The report's own words are filed under one sensitive category; its contained result is another one.
    report = _report(conclusion="repeatedvalue 987654 dementia", result=[{"reference": "#private"}],
                     contained=[_observation("private", "44261-6", 987654)])
    fhir.ingest(vault, report, None)
    _hidden(vault, Policy(hidden=frozenset({"mental_health"})))
    assert _fetch(vault, policy=Policy())["conclusion"].startswith("repeatedvalue")


@pytest.mark.parametrize("call", ["search", "browse"])
def test_search_and_browse_check_visibility_inside_one_snapshot(vault, monkeypatch, call):
    fhir.ingest(vault, _report(conclusion="repeatedvalue"), None)
    visible = search._visible

    def check(*args, **kwargs):
        assert vault.in_transaction
        return visible(*args, **kwargs)

    monkeypatch.setattr(search, "_visible", check)
    if call == "search":
        assert search.search(vault, "repeatedvalue", kinds="report", policy=Policy())["results"]
    else:
        assert search.browse(vault, kinds="report", policy=Policy())["results"]
    assert not vault.in_transaction


def test_search_snippet_and_visibility_share_a_snapshot(vault, monkeypatch, data_dir):
    fhir.ingest(vault, {"resourceType": "Bundle", "entry": [
        {"resource": _report(conclusion="repeatedvalue 987654", result=[{"reference": "Observation/q"}])},
        {"resource": _observation("q", "44261-6", 987654)}]}, None)
    policy = Policy(level="standard")
    assert search.search(vault, "repeatedvalue", kinds="report", policy=policy)["results"] == []
    writer = db.open_vault(data_dir / "vault.db")
    query = search._query

    def replace_after_query(*args, **kwargs):
        rows = query(*args, **kwargs)
        raw = json.loads(writer.execute("SELECT raw_json FROM clinical_report").fetchone()[0])
        raw.pop("result", None)
        raw["_caduceus"].pop("result", None)
        writer.execute("UPDATE clinical_report SET raw_json=?, conclusion='safe', category='records'",
                       (json.dumps(raw),))
        return rows

    monkeypatch.setattr(search, "_query", replace_after_query)
    try:
        found = search.search(vault, "repeatedvalue", kinds="report", policy=policy)
        assert found["results"] == [] and "987654" not in json.dumps(found)
        assert not vault.in_transaction
    finally:
        writer.close()


@pytest.mark.parametrize("shape", [
    {"code": [{"text": "x"}]}, {"code": "plain"}, {"code": {"coding": ["text"]}},
    {"valueCodeableConcept": "plain"}, {"component": "plain"}, {"component": [{"code": "plain"}]},
])
def test_malformed_contained_target_does_not_fail_import(vault, tmp_path, shape):
    from tests.test_v040_reports import _bundle, _import
    target = {"resourceType": "Observation", "id": "private", **shape}
    result = _import(vault, tmp_path, _bundle(_report(result=[{"reference": "#private"}], contained=[target])))
    assert result.status == "succeeded"
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 1


def test_contained_panel_component_category_withholds_report(vault):
    panel = {"resourceType": "Observation", "id": "panel", "code": {"text": "Panel"},
             "component": [{"code": {"coding": [{"system": "http://loinc.org", "code": "44261-6"}]},
                            "valueQuantity": {"value": 987654}}]}
    fhir.ingest(vault, _report(conclusion="repeatedvalue 987654", result=[{"reference": "#panel"}],
                               contained=[panel]), None)
    _hidden(vault, Policy(level="standard"))
