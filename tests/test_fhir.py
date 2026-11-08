import json
import threading

import pytest

from caduceus.core import search, sources
from caduceus.core.policy import Policy
from caduceus.health.fhir import import_fhir, observation_series
from tests.synth.apple import clinical_bundle


def _source(vault, path, document):
    path.write_text(json.dumps(document))
    return sources.store_source(vault, path, "fhir")[0]


def test_bundle_series_index_and_idempotence(vault, tmp_path):
    src = _source(vault, tmp_path / "bundle.json", clinical_bundle())
    result = import_fhir(vault, src)
    assert result.status == "succeeded"
    assert result.counts == {"observation": 2, "condition": 1, "medication": 1, "ignored": 0}
    series = observation_series(vault, "HbA1c")
    assert [p["value"] for p in series["points"]] == [8.2, 6.5]
    assert series["unit"] == "%" and series["calc_version"] == "trend/1"
    assert series["points"][0]["reference_range"] == {"low": 4, "high": 5.6}
    assert series["points"][0]["interpretation"] == "High"
    assert len(observation_series(vault, "4548-4")["points"]) == 1
    assert len(observation_series(vault, "HbA1c", start="2024-03-01", end="2024-07-01")["points"]) == 1
    assert vault.execute("SELECT drug,source FROM medication_event").fetchone()[:] == ("semaglutide", "fhir")
    assert search.search(vault, "diabetes", policy=Policy())["results"][0]["kind"] == "condition"
    assert search.search(vault, "semaglutide", policy=Policy(hidden=frozenset({"medication"})))["withheld"] == 1
    assert import_fhir(vault, src).status == "skipped"
    # A different source with the same resource ids is still idempotent.
    doc = clinical_bundle()
    doc["id"] = "other-bundle"
    again = import_fhir(vault, _source(vault, tmp_path / "again.json", doc))
    assert again.counts["observation"] == again.counts["medication"] == 0
    doc["entry"][2]["resource"]["effectivePeriod"]["start"] = "2024-03-02"
    changed = import_fhir(vault, _source(vault, tmp_path / "changed.json", doc))
    assert changed.counts["medication"] == 0  # Resource id, even when fields change.
    assert vault.execute("SELECT count(*) FROM medication_event").fetchone()[0] == 1


def test_single_resource_hash_key_cancel_and_failure(vault, tmp_path):
    resource = clinical_bundle()["entry"][0]["resource"]
    del resource["id"]
    src = _source(vault, tmp_path / "single.json", resource)
    cancel = threading.Event()
    cancel.set()
    assert import_fhir(vault, src, cancel=cancel).status == "cancelled"
    crashed = sources.begin_import(vault, src, "fhir", "1")
    assert import_fhir(vault, src).status == "succeeded"
    assert sources.run_info(vault, crashed.id)["status"] == "interrupted"
    assert observation_series(vault, "HbA1c")["points"][0]["value"] == pytest.approx(8.2)
    path = tmp_path / "malformed.json"
    path.write_text("{synthetic invalid JSON")
    bad = sources.store_source(vault, path, "fhir")[0]
    assert import_fhir(vault, bad).status == "failed"
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 1


def test_ldl_glucose_alias_and_missing_quantity(vault, tmp_path):
    resource = {"resourceType": "Observation", "id": "synthetic-glucose", "code": {
        "coding": [{"system": "synthetic", "code": "other"},
                   {"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
        "effectiveDateTime": "2024-01-01", "valueString": "not measured"}
    assert import_fhir(vault, _source(vault, tmp_path / "glucose.json", resource)).status == "succeeded"
    point = observation_series(vault, "glucose")["points"][0]
    assert point["value"] is None and point["value_text"] == "not measured"
    assert observation_series(vault, "LDL")["points"] == []
