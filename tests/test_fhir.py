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
    doc["entry"][0]["resource"]["valueQuantity"]["value"] = 7.3  # Same resource id, different lab.
    changed = import_fhir(vault, _source(vault, tmp_path / "changed.json", doc))
    assert changed.counts["observation"] == 1
    assert len(observation_series(vault, "HbA1c")["points"]) == 3


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


def _observation(code="4548-4", when="2024-01-01", value=7, unit="%", system="http://loinc.org"):
    return {"resourceType": "Observation", "id": "1", "code": {
        "coding": [{"system": system, "code": code, "display": "Synthetic lab"}]},
        "effectiveDateTime": when, "valueQuantity": {"value": value, "unit": unit}}


def _bundle(*resources):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def test_partial_dates_precision_and_missing_date_warnings(vault, tmp_path):
    resources = [_observation(when=when) for when in ("2023-05", "2024", None, "2024-02-30", "2024-05-02T12:00:00Z")]
    resources += [{"resourceType": "Condition", "code": {"text": "Synthetic condition"}, "onsetDateTime": when}
                  for when in ("2023-05", "2024", None)]
    result = import_fhir(vault, _source(vault, tmp_path / "partial.json", _bundle(*resources)))
    assert result.status == "succeeded"
    series = observation_series(vault, "HbA1c")
    assert [p["date_precision"] for p in series["points"]] == ["month", "year", "timestamp"]
    assert series["coverage"]["days_with_data"] == 1
    assert series["coverage"]["first_day"] == "2024-05-02"
    assert [p["date"] for p in observation_series(vault, "HbA1c", start="2023", end="2023")["points"]] == ["2023-05"]
    stored = vault.execute("SELECT raw_json FROM condition ORDER BY onset").fetchall()
    assert [json.loads(r[0])["_caduceus"]["date_precision"] for r in stored] == ["month", "year"]
    assert vault.execute("SELECT clinical_status FROM condition LIMIT 1").fetchone()[0] is None
    assert result.counts["skipped:Observation:date"] == 2
    assert result.counts["skipped:Condition:date"] == 1
    assert any("Observation: skipped 2" in w for w in result.warnings)
    assert any("Condition: skipped 1" in w for w in result.warnings)
    assert all("2024-02-30" not in w for w in result.warnings)


def test_content_natural_keys_across_hospitals(vault, tmp_path):
    import copy
    lab = _observation()
    condition = {"resourceType": "Condition", "id": "1", "onsetDateTime": "2024-01-01",
                 "code": {"coding": [{"system": "synthetic", "code": "A", "display": "Synthetic A"}]}}
    assert import_fhir(vault, _source(vault, tmp_path / "hospital-a.json", _bundle(lab, condition))).status == "succeeded"
    same_lab, same_condition = copy.deepcopy(lab), copy.deepcopy(condition)
    same_lab["id"], same_condition["id"] = "different-id", "different-id"
    same = import_fhir(vault, _source(vault, tmp_path / "repeat.json", _bundle(same_lab, same_condition)))
    assert same.counts["observation"] == same.counts["condition"] == 0
    lab["valueQuantity"]["value"] = 8
    condition["onsetDateTime"] = "2024-02-01"
    different = import_fhir(vault, _source(vault, tmp_path / "hospital-b.json", _bundle(lab, condition)))
    assert different.counts["observation"] == different.counts["condition"] == 1
    assert [p["value"] for p in observation_series(vault, "HbA1c")["points"]] == [7, 8]
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 2


def test_components_coded_values_and_censored_quantities(vault, tmp_path):
    panel = _observation("85354-9")
    del panel["valueQuantity"]
    panel["component"] = [{"code": {"coding": [{"system": "http://loinc.org", "code": code}]},
                           "valueQuantity": {"value": value, "unit": "mmHg"}}
                          for code, value in (("8480-6", 120), ("8462-4", 80))]
    coded = _observation("synthetic-coded")
    del coded["valueQuantity"]
    coded["valueCodeableConcept"] = {"coding": [{"code": "NEG", "display": "Negative"}]}
    censored = _observation("2345-7", value=0.5, unit="mg/dL")
    censored["valueQuantity"]["comparator"] = "<"
    result = import_fhir(vault, _source(vault, tmp_path / "values.json", _bundle(panel, coded, censored)))
    assert result.counts["observation"] == 4
    assert observation_series(vault, "8480-6")["points"][0]["value"] == 120
    assert observation_series(vault, "8462-4")["points"][0]["value"] == 80
    assert observation_series(vault, "85354-9")["points"] == []
    assert observation_series(vault, "synthetic-coded")["points"][0]["value_text"] == "Negative"
    point = observation_series(vault, "glucose")["points"][0]
    assert point["value"] is None and point["value_text"] == "<0.5"


@pytest.mark.parametrize("kind", ["MedicationStatement", "MedicationRequest"])
@pytest.mark.parametrize("status", ["active", "completed", "on-hold", "intended", "stopped", "entered-in-error", "not-taken", "cancelled", "draft", "unknown", None])
def test_medication_status_and_date_only(vault, tmp_path, kind, status):
    resource = {"resourceType": kind, "id": "1", "status": status,
                "medicationCodeableConcept": {"coding": [{"display": "Semaglutide"}]},
                "effectivePeriod": {"start": "2024-01-02T12:00:00-08:00", "end": "2024-03-04T12:00:00Z"}}
    result = import_fhir(vault, _source(vault, tmp_path / "status.json", resource))
    expected = status in {"active", "completed", "on-hold", "stopped"} or (kind == "MedicationStatement" and status == "intended")
    assert result.status == "succeeded"
    rows = vault.execute("SELECT drug,event,occurred_on FROM medication_event").fetchall()
    if expected:
        assert rows[0][:] == ("semaglutide", "stop" if status == "stopped" else "start",
                              "2024-03-04" if status == "stopped" else "2024-01-02")
    else:
        assert not rows
        assert result.counts[f"skipped:{kind}:status"] == 1
        assert any(kind in w for w in result.warnings)


def test_invalid_medication_cannot_swallow_valid_row(vault, tmp_path):
    import copy
    valid = {"resourceType": "MedicationStatement", "status": "active",
             "medicationCodeableConcept": {"text": "Semaglutide"}, "effectiveDateTime": "2024-05"}
    invalid = {**valid, "status": "not-taken"}
    invalid_request = {**valid, "resourceType": "MedicationRequest", "status": "entered-in-error"}
    code_only = {**valid, "medicationCodeableConcept": {"coding": [{"code": "12345"}]}}
    no_date = copy.deepcopy(valid)
    del no_date["effectiveDateTime"]
    result = import_fhir(vault, _source(vault, tmp_path / "meds.json", _bundle(invalid, invalid_request, code_only, no_date, valid)))
    assert result.counts["medication"] == 1
    assert result.counts["skipped:MedicationStatement:name"] == 1
    assert result.counts["skipped:MedicationStatement:date"] == 1
    assert result.counts["skipped:MedicationRequest:status"] == 1
    row = vault.execute("SELECT drug,occurred_on,note FROM medication_event").fetchone()
    assert row[:2] == ("semaglutide", "2024-05")
    assert json.loads(row["note"])["date_precision"] == "month"


@pytest.mark.parametrize("code,display,category", [
    ("F43.1", "PTSD", "mental_health"), ("F42.9", "OCD", "mental_health"),
    ("F50.0", "Anorexia nervosa", "mental_health"), ("F10.2", "Alcohol use disorder", "mental_health"),
    ("N80", "Endometriosis", "reproductive"), ("E28.2", "PCOS", "reproductive"),
    ("N52.9", "Erectile dysfunction", "reproductive"), ("F32.9", None, "mental_health")])
def test_sensitive_condition_codes(vault, tmp_path, code, display, category):
    coding = {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code}
    if display:
        coding["display"] = display
    resource = {"resourceType": "Condition", "code": {"coding": [coding]}, "onsetDateTime": "2024"}
    assert import_fhir(vault, _source(vault, tmp_path / "condition.json", resource)).status == "succeeded"
    assert vault.execute("SELECT category FROM condition").fetchone()[0] == category
    assert search.fetch(vault, "condition:1", policy=Policy(level="standard"))["withheld"]


@pytest.mark.parametrize("code", ["44249-1", "44261-6", "70274-6"])
def test_sensitive_observation_codes(vault, tmp_path, code):
    resource = _observation(code)
    assert import_fhir(vault, _source(vault, tmp_path / "screen.json", resource)).status == "succeeded"
    assert observation_series(vault, code)["points"][0]["category"] == "mental_health"
    assert search.fetch(vault, "observation:1", policy=Policy(level="standard"))["withheld"]


def test_all_coding_displays_participate_in_category(vault, tmp_path):
    resource = _observation()
    resource["code"]["text"] = "Neutral text"
    resource["code"]["coding"] += [{"system": "synthetic", "code": "X", "display": "Depression screening"}]
    assert import_fhir(vault, _source(vault, tmp_path / "displays.json", resource)).status == "succeeded"
    assert vault.execute("SELECT category FROM observation").fetchone()[0] == "mental_health"


@pytest.mark.parametrize("alias,code", [(alias, code) for alias, codes in {
    "HbA1c": ("4548-4", "17856-6", "59261-8"), "LDL": ("13457-7", "18262-6", "2089-1"),
    "glucose": ("2345-7", "2339-0", "1558-6")}.items() for code in codes])
def test_aliases_require_loinc(vault, tmp_path, alias, code):
    correct = _observation(code)
    wrong = _observation(code, value=999, system="http://synthetic.invalid/codes")
    wrong["code"]["text"] = alias  # A matching name must not bypass the coding-system check.
    assert import_fhir(vault, _source(vault, tmp_path / "aliases.json", _bundle(correct, wrong))).status == "succeeded"
    assert [p["value"] for p in observation_series(vault, alias)["points"]] == [7]
    assert [p["value"] for p in observation_series(vault, code)["points"]] == [7]


def test_hba1c_conversion_and_other_mixed_units(vault, tmp_path):
    ngsp = _observation(value=7)
    ifcc = _observation("59261-8", when="2024-02-01", value=53, unit="mmol/mol")
    ifcc["referenceRange"] = [{"low": {"value": 20}, "high": {"value": 42}}]
    ldl = [_observation("2089-1", when=f"2024-03-0{i+1}", value=value, unit=unit)
           for i, (value, unit) in enumerate([(100, "mg/dL"), (110, "mg/dL"), (3, "mmol/L")])]
    assert import_fhir(vault, _source(vault, tmp_path / "units.json", _bundle(ngsp, ifcc, *ldl))).status == "succeeded"
    series = observation_series(vault, "HbA1c")
    assert series["unit"] == "%" and series["other_units"] == {}
    point = series["points"][1]
    assert point["value"] == pytest.approx(53/10.929+2.15)
    assert point["reference_range"]["high"] == pytest.approx(42/10.929+2.15)
    assert point["converted_from"] == "mmol/mol" and point["unit"] == "%"
    ldl_series = observation_series(vault, "LDL")
    assert [p["value"] for p in ldl_series["points"]] == [100, 110]
    assert ldl_series["unit"] == "mg/dL" and ldl_series["other_units"] == {"mmol/L": 1}


def test_clinical_glucose_known_unit_conversion(vault, tmp_path):
    resources = [_observation("2345-7", value=90.08, unit="mg/dL"),
                 _observation("2339-0", value=5, unit="mmol/L"),
                 _observation("1558-6", value=5, unit="mmol<180.15588>/L")]
    assert import_fhir(vault, _source(vault, tmp_path / "glucose-units.json", _bundle(*resources))).status == "succeeded"
    series = observation_series(vault, "glucose")
    assert series["unit"] == "mg/dL" and series["other_units"] == {}
    assert [p["value"] for p in series["points"]] == pytest.approx([90.08]*3)
