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
        wanted = [("semaglutide", "start", "2024-01-02")]
        if status in ("completed", "stopped"):
            wanted.append(("semaglutide", "stop", "2024-03-04"))
        assert [row[:] for row in rows] == wanted
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


@pytest.mark.parametrize("status", ["entered-in-error", "cancelled"])
def test_round2_observation_invalid_status(vault, tmp_path, status):
    resource = {**_observation("2345-7", value=400, unit="mg/dL"), "status": status}
    result = import_fhir(vault, _source(vault, tmp_path / "invalid-observation.json", resource))
    assert result.status == "succeeded"
    assert observation_series(vault, "glucose")["points"] == []
    assert result.counts["skipped:Observation:status"] == 1 and result.warnings


def test_round2_corrections_supersede_across_imports_and_keep_other_identity(vault, tmp_path):
    def load(name, *resources):
        result = import_fhir(vault, _source(vault, tmp_path / name, _bundle(*resources)))
        assert result.status == "succeeded", result.warnings
    original = {**_observation("2345-7", value=120, unit="mg/dL"), "status": "final"}
    load("original.json", original)
    corrected = {**original, "status": "corrected", "valueQuantity": {"value": 102, "unit": "mg/dL"}}
    load("corrected.json", corrected)
    assert [p["value"] for p in observation_series(vault, "glucose")["points"]] == [102]
    amended = {**corrected, "status": "amended", "valueQuantity": {"value": 100, "unit": "mg/dL"}}
    other_time = {**original, "effectiveDateTime": "2024-02-01"}
    other_code = {**original, "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]}}
    load("amended.json", amended, other_time, other_code)
    # Later non-corrections at the same identity cannot resurrect the old value.
    load("stale.json", *[{**original, "status": s} for s in ("preliminary", "final", "registered")])
    points = observation_series(vault, "glucose")["points"]
    assert [p["value"] for p in points] == [100, 120]
    assert points[0]["status"] == "amended"
    assert len(observation_series(vault, "HbA1c")["points"]) == 1
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 3
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='observation'").fetchone()[0] == 3
    assert not search.search(vault, "102", policy=Policy())["results"]


def test_round2_correction_within_bundle(vault, tmp_path):
    original = {**_observation(value=9), "status": "final"}
    corrected = {**_observation(value=7), "status": "corrected"}
    result = import_fhir(vault, _source(vault, tmp_path / "same-bundle.json", _bundle(original, corrected, original)))
    assert result.status == "succeeded"
    assert [p["value"] for p in observation_series(vault, "HbA1c")["points"]] == [7]


@pytest.mark.parametrize("verification", ["refuted", "entered-in-error"])
def test_round2_condition_rejected_verification(vault, tmp_path, verification):
    resource = {"resourceType": "Condition", "code": {"text": "Synthetic condition"},
                "onsetDateTime": "2024", "verificationStatus": {"coding": [{"code": verification}]}}
    result = import_fhir(vault, _source(vault, tmp_path / "refuted.json", resource))
    assert result.status == "succeeded"
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert result.counts["skipped:Condition:status"] == 1 and result.warnings


@pytest.mark.parametrize("verification", ["unconfirmed", "provisional", "differential"])
@pytest.mark.parametrize("clinical", ["resolved", "inactive", "remission"])
def test_round2_condition_uncertainty_and_not_current(vault, tmp_path, verification, clinical):
    resource = {"resourceType": "Condition", "code": {"text": "Synthetic condition"}, "onsetDateTime": "2024",
                "verificationStatus": {"coding": [{"code": verification}]},
                "clinicalStatus": {"coding": [{"code": clinical}]}}
    assert import_fhir(vault, _source(vault, tmp_path / "uncertain.json", resource)).status == "succeeded"
    metadata = json.loads(vault.execute("SELECT raw_json FROM condition").fetchone()[0])["_caduceus"]
    assert metadata["verification_status"] == verification and metadata["current"] is False
    title = vault.execute("SELECT title FROM search_doc WHERE kind='condition'").fetchone()[0]
    assert verification in title and "not current" in title


@pytest.mark.parametrize("kind", ["MedicationStatement", "MedicationRequest"])
@pytest.mark.parametrize("status,end", [("completed", "2023-03-01"), ("completed", None), ("stopped", "2023-03-01")])
def test_round2_medication_known_timeline(vault, tmp_path, kind, status, end):
    period = {"start": "2023-01-01"}
    if end:
        period["end"] = end
    resource = {"resourceType": kind, "id": "timeline", "status": status,
                "medicationCodeableConcept": {"text": "Synthetic drug"}, "effectivePeriod": period}
    result = import_fhir(vault, _source(vault, tmp_path / "timeline.json", resource))
    assert result.status == "succeeded", result.warnings
    rows = vault.execute("SELECT event,occurred_on,note FROM medication_event ORDER BY id").fetchall()
    expected = [("start", "2023-01-01")] + ([("stop", end)] if end else [])
    assert [(r[0], r[1]) for r in rows] == expected
    assert all(json.loads(r[2])["current"] is False for r in rows)


@pytest.mark.parametrize("system", ["http://hl7.org/fhir/sid/icd-10", "urn:oid:2.16.840.1.113883.6.3", None])
@pytest.mark.parametrize("code,category", [("F32.9", "mental_health"), ("O80", "reproductive"), ("N97.9", "reproductive")])
def test_round2_sensitive_icd_systems(vault, tmp_path, system, code, category):
    coding = {"code": code}
    if system:
        coding["system"] = system
    resource = {"resourceType": "Condition", "code": {"coding": [coding]}, "onsetDateTime": "2024"}
    assert import_fhir(vault, _source(vault, tmp_path / "icd.json", resource)).status == "succeeded"
    assert vault.execute("SELECT category FROM condition").fetchone()[0] == category
    assert search.fetch(vault, "condition:1", policy=Policy(level="standard"))["withheld"]


@pytest.mark.parametrize("code,category", [
    ("69737-5", "mental_health"), ("44260-8", "mental_health"), ("55757-9", "mental_health"),
    ("55758-7", "mental_health"), ("71354-5", "mental_health"), ("99046-5", "mental_health"),
    ("72109-2", "mental_health"), ("75626-2", "mental_health"), ("82810-3", "reproductive"),
    ("11449-6", "reproductive"), ("77996-7", "reproductive"), ("2106-3", "reproductive"),
    ("2118-8", "reproductive"), ("80384-1", "reproductive"), ("2119-6", "reproductive"),
    ("45194-8", "reproductive"), ("19080-1", "reproductive"), ("2107-1", "reproductive")])
def test_round2_verified_sensitive_loinc(vault, tmp_path, code, category):
    resource = _observation(code)
    assert import_fhir(vault, _source(vault, tmp_path / "loinc.json", resource)).status == "succeeded"
    assert observation_series(vault, code)["points"][0]["category"] == category
    assert search.fetch(vault, "observation:1", policy=Policy(level="standard"))["withheld"]


def test_round2_numeric_known_unit_wins(vault, tmp_path):
    resources = [_observation("2345-7", when=f"2024-01-0{i+1}", value=90+i, unit="mg/dL") for i in range(3)]
    for i in range(4):
        text = _observation("2345-7", when=f"2024-02-0{i+1}", value=None, unit=None)
        text["valueString"] = "See synthetic note"
        resources.append(text)
    assert import_fhir(vault, _source(vault, tmp_path / "unit-vote.json", _bundle(*resources))).status == "succeeded"
    series = observation_series(vault, "glucose")
    assert series["unit"] == "mg/dL" and [p["value"] for p in series["points"]] == [90, 91, 92]
    assert len(series["unplotted_points"]) == 4


def test_round2_known_unit_beats_numeric_none(vault, tmp_path):
    resources = [_observation("2345-7", when=f"2024-01-0{i+1}", value=90+i, unit=None) for i in range(4)]
    resources.append(_observation("2345-7", when="2024-02-01", value=95, unit="mg/dL"))
    assert import_fhir(vault, _source(vault, tmp_path / "none-unit.json", _bundle(*resources))).status == "succeeded"
    assert observation_series(vault, "glucose")["unit"] == "mg/dL"


@pytest.mark.parametrize("unit,ucum", [("% of total Hgb", None), ("%{HbA1c}", None),
                                     ("Percent of hemoglobin", "%"), ("mmol{HbA1c}/mol{Hb}", None)])
def test_round2_hba1c_unit_forms(vault, tmp_path, unit, ucum):
    resource = _observation(value=53 if unit.startswith("mmol") else 7, unit=unit)
    if ucum:
        resource["valueQuantity"].update({"system": "http://unitsofmeasure.org", "code": ucum})
    assert import_fhir(vault, _source(vault, tmp_path / "hgb-unit.json", resource)).status == "succeeded"
    series = observation_series(vault, "HbA1c")
    assert series["unit"] == "%" and len(series["points"]) == 1
    assert series["points"][0]["value"] == pytest.approx(53/10.929+2.15 if unit.startswith("mmol") else 7)


@pytest.mark.parametrize("code,category", [(c, "mental_health") for c in (
    "35489007", "370143000", "197480006", "21897009", "371631005", "13746004", "58214004",
    "47505003", "191736004", "72366004", "56882008", "78004001", "7200002", "66590003",
    "11061003", "75544000", "6471006", "248062006")] + [(c, "reproductive") for c in (
    "77386006", "17369002", "34801009", "129103003", "237055002", "8619003", "860914002", "397803000")])
def test_round2_verified_sensitive_snomed(vault, tmp_path, code, category):
    resource = {"resourceType": "Condition", "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}]},
                "onsetDateTime": "2024"}
    assert import_fhir(vault, _source(vault, tmp_path / "snomed.json", resource)).status == "succeeded"
    assert vault.execute("SELECT category FROM condition").fetchone()[0] == category
    assert search.fetch(vault, "condition:1", policy=Policy(level="standard"))["withheld"]


def test_round2_condition_resolution_updates_existing(vault, tmp_path):
    resource = {"resourceType": "Condition", "id": "same-condition", "onsetDateTime": "2024",
                "code": {"text": "Synthetic condition"}, "clinicalStatus": {"coding": [{"code": "active"}]}}
    assert import_fhir(vault, _source(vault, tmp_path / "active-condition.json", resource)).status == "succeeded"
    resource["clinicalStatus"]["coding"][0]["code"] = "resolved"
    assert import_fhir(vault, _source(vault, tmp_path / "resolved-condition.json", resource)).status == "succeeded"
    rows = vault.execute("SELECT clinical_status,raw_json FROM condition").fetchall()
    assert len(rows) == 1 and rows[0]["clinical_status"] == "resolved"
    assert json.loads(rows[0]["raw_json"])["_caduceus"]["current"] is False
    assert "not current" in vault.execute("SELECT title FROM search_doc WHERE kind='condition'").fetchone()[0]


def test_round2_correction_preserves_deduplicated_resource_identity(vault, tmp_path):
    original = {**_observation(value=9), "status": "final"}
    equivalent = {**original, "id": "second-id"}
    assert import_fhir(vault, _source(vault, tmp_path / "equivalent.json", _bundle(original, equivalent))).status == "succeeded"
    corrected = {**_observation(value=7), "id": "second-id", "status": "corrected"}
    assert import_fhir(vault, _source(vault, tmp_path / "second-correction.json", corrected)).status == "succeeded"
    assert [p["value"] for p in observation_series(vault, "HbA1c")["points"]] == [7]
    # Correction to already-stored content also retains corrected precedence.
    another = {**_observation(value=6), "id": "third-id", "status": "final"}
    assert import_fhir(vault, _source(vault, tmp_path / "third.json", another)).status == "succeeded"
    corrected["valueQuantity"]["value"] = 6
    assert import_fhir(vault, _source(vault, tmp_path / "equal-correction.json", corrected)).status == "succeeded"
    original["id"] = "second-id"
    assert import_fhir(vault, _source(vault, tmp_path / "old-again.json", original)).status == "succeeded"
    assert [p["value"] for p in observation_series(vault, "HbA1c")["points"]] == [6]


def test_round2_undated_stop_fails_closed_under_frozen_schema(vault, tmp_path):
    assert next(r for r in vault.execute('PRAGMA table_info(medication_event)') if r['name']=='occurred_on')['notnull'] == 1
    resource = {"resourceType": "MedicationStatement", "status": "stopped",
                "medicationCodeableConcept": {"text": "Synthetic drug"}, "effectivePeriod": {"start": "2024-02-01"}}
    result = import_fhir(vault, _source(vault, tmp_path / "undated-stop.json", resource))
    assert result.status == "failed"  # Owner gate, not proof of C's requested NULL row.
    assert vault.execute("SELECT count(*) FROM medication_event").fetchone()[0] == 0
    assert "nullable medication_event.occurred_on" in result.warnings[0]
