"""U3b read-side regressions; all records are synthetic and normally imported."""
import json

import pytest

from caduceus import brief, ops
from caduceus.core import sources
from caduceus.core.policy import Policy
from caduceus.health import fhir

CAT_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
LOCAL = "urn:synthetic:hospital"


def _obs(name="Synthetic lab", code="1234-5", system=fhir.LOINC, value=12, unit="mg/dL", **extra):
    coding = [{"code": code, **({"system": system} if system else {})}] if code else []
    return {"resourceType": "Observation", "status": "final", "code": {"text": name, "coding": coding},
            "effectiveDateTime": "2025-01-01", "valueQuantity": {"value": value, "unit": unit}, **extra}


def _cat(*codes):
    return [{"coding": [{"system": CAT_SYSTEM, "code": c} for c in codes]}]


def _import(vault, tmp_path, *records):
    path = tmp_path / "synthetic-bundle.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in records]}))
    src, _ = sources.store_source(vault, path, "fhir")
    result = fhir.import_fhir(vault, src)
    assert result.status == "succeeded"


def _records():
    return [_obs(category=_cat("laboratory")),
            _obs("Synthetic pulse", "8867-4", unit="/min", category=_cat("vital-signs")),
            _obs("Synthetic local", "LOCAL1", LOCAL, category=_cat("imaging")),
            _obs("Synthetic text", None, valueQuantity=None, valueString="Synthetic qualitative")]


def test_fhir_only_discovery_queries_and_normalized_names(vault, samples, tmp_path, fake_ctx):
    records = _records()
    records += [_obs("Synthetic renamed lab", category=_cat("laboratory"), effectiveDateTime="2025-02"),
                _obs("  SYNTHETIC   TEXT  ", None, valueQuantity=None, valueString="Synthetic repeat",
                     effectiveDateTime="2025-03"),
                _obs("Synthetic systemless", "SYSLESS", None)]
    _import(vault, tmp_path, *records)
    result = ops.trend_tool(fake_ctx, {"op": "metrics"})
    assert result["metrics"] == []
    series = result["clinical_series"]
    assert len(series) == 5 and series == sorted(series, key=lambda s: s["name"])
    by_code = {s["code"]: s for s in series}
    assert by_code["1234-5"] == {"name": "Synthetic renamed lab", "code": "1234-5", "code_system": fhir.LOINC,
                                  "units": ["mg/dL"], "count": 2, "numeric": 2, "first": "2025-01-01",
                                  "last": "2025-02", "sources": ["fhir"], "fhir_categories": ["laboratory"],
                                  "query": "1234-5"}
    assert by_code["8867-4"]["fhir_categories"] == ["vital-signs"]
    assert by_code["LOCAL1"]["query"] == LOCAL + "|LOCAL1"
    assert by_code["SYSLESS"]["query"] == "SYSLESS"
    assert by_code[None]["count"] == 2 and by_code[None]["numeric"] == 0
    for s in series:
        reply = ops.trend_tool(fake_ctx, {"op": "labs", "lab": s["query"]})
        assert len(reply["points"]) == s["count"]
        assert {p["code"] for p in reply["points"]} == {s["code"]}
    assert samples.execute("SELECT count(*) FROM daily_rollup").fetchone()[0] == 0


def test_discovery_filters_before_grouping_and_separate_wellness_gate(vault, tmp_path, fake_ctx):
    hidden = _obs("Synthetic depression SECRETNAME", "HIDDENCODE", LOCAL, unit="HIDDENUNIT",
                  effectiveDateTime="2099-12-31", category=_cat("laboratory"))
    _import(vault, tmp_path, *_records(), hidden)
    fake_ctx.config.update(privacy_level="standard", hidden_categories=["wellness"])
    reply = ops.trend_tool(fake_ctx, {"op": "metrics"})
    assert reply["metrics"] == [] and len(reply["clinical_series"]) == 4
    assert reply["withheld"] == 2 and reply["withheld_note"] == "hidden by your privacy settings"
    assert not any(word in json.dumps(reply) for word in ("SECRETNAME", "HIDDENCODE", "HIDDENUNIT", "2099"))
    fake_ctx.config["hidden_categories"] = ["records"]
    reply = ops.trend_tool(fake_ctx, {"op": "metrics"})
    assert reply["clinical_series"] == [] and reply["withheld"] == 5


def test_category_counts_status_and_erase_preview(vault, tmp_path, fake_ctx):
    records = _records()
    records += [_obs("Synthetic multi", "MULTI", LOCAL, category=_cat("laboratory", "vital-signs", "laboratory")),
                _obs("Synthetic fallback category", "FALLBACK", LOCAL,
                     category=[{"coding": [{"system": "urn:synthetic:category", "code": "procedure"}]}])]
    records[0]["category"][0]["coding"].insert(0, {"system": "urn:synthetic:category", "code": "ignore"})
    _import(vault, tmp_path, *records)
    counts = ops.capture_tool(fake_ctx, {"op": "settings"})["counts"]
    assert counts["observations"] == 6 and counts["lab_results"] == counts["vital_signs"] == 2
    assert counts["observations_by_category"] == {"laboratory": 2, "vital-signs": 2, "imaging": 1,
                                                  "unspecified": 1, "procedure": 1}
    for cmd in ("status", "erase"):
        assert "6 observations (2 lab results, 2 vital signs, 3 other)" in ops.command(fake_ctx, cmd)


def test_discovery_truncation_and_rebuilding_note(vault, tmp_path, fake_ctx, monkeypatch):
    _import(vault, tmp_path, *[_obs(f"Synthetic {i:03}", f"C{i:03}", LOCAL) for i in range(302)])
    monkeypatch.setattr(ops, "_refresh_rollups", lambda store: "Synthetic rebuilding note")
    reply = ops.trend_tool(fake_ctx, {"op": "metrics"})
    assert len(reply["clinical_series"]) == 300 and reply["clinical_series_truncated"] == 2
    assert reply["clinical_series"][-1]["name"] == "Synthetic 299"
    assert reply["rebuilding"] == "Synthetic rebuilding note"


def _ranges(unit="mg/dL"):
    return [{"low": {"value": 4, "unit": unit, "code": unit, "system": "http://unitsofmeasure.org"},
             "high": {"value": 9, "unit": unit}, "text": "Synthetic recorded range <adult>",
             "type": {"coding": [{"code": "normal", "display": "Normal"}]},
             "appliesTo": [{"coding": [{"code": "synthetic-adult"}]}],
             "age": {"low": {"value": 18, "unit": "years"}, "high": {"value": 90, "code": "a"}}},
            {"text": "Synthetic second range", "type": {"coding": [{"code": "therapeutic"}]}}]


def test_ranges_as_recorded_and_matching_bounds(vault, tmp_path):
    _import(vault, tmp_path, _obs(referenceRange=_ranges()))
    before = vault.execute("SELECT raw_json FROM observation").fetchone()[0]
    p = fhir.observation_series(vault, "1234-5")["points"][0]
    assert p["reference_ranges"] == [
        {"low": {"value": 4, "unit": "mg/dL", "code": "mg/dL", "system": "http://unitsofmeasure.org"},
         "high": {"value": 9, "unit": "mg/dL"}, "text": "Synthetic recorded range <adult>",
         "type": "Normal", "applies_to": ["synthetic-adult"],
         "age": {"low": {"value": 18, "unit": "years"}, "high": {"value": 90, "code": "a"}}},
        {"text": "Synthetic second range", "type": "therapeutic"}]
    assert p["reference_range"] == {"low": 4, "high": 9} and not p.get("reference_range_mismatch")
    assert vault.execute("SELECT raw_json FROM observation").fetchone()[0] == before


@pytest.mark.parametrize("code,unit,range_unit,mismatch,expected", [
    ("1234-5", "mg/dL", "mmol/L", True, None),
    ("17856-6", "mmol/mol", "mmol/mol", False, 4 / 10.929 + 2.15),
    ("17856-6", "mmol/mol", "%", True, None),
    ("2345-7", "mmol/L", "mmol/L", False, 4 * 18.016),
    ("2345-7", "mmol/L", "mg/dL", True, None),
])
def test_range_mismatch_and_conversion(vault, tmp_path, code, unit, range_unit, mismatch, expected):
    _import(vault, tmp_path, _obs(code=code, unit=unit, referenceRange=_ranges(range_unit)))
    p = fhir.observation_series(vault, code)["points"][0]
    assert p["reference_ranges"][0]["low"]["unit"] == range_unit
    assert bool(p.get("reference_range_mismatch")) == mismatch
    assert p["reference_range"]["low"] == (pytest.approx(expected) if expected is not None else None)
    if mismatch:
        assert p["reference_range"] == {"low": None, "high": None}


def test_component_ranges_and_quantity_code_match(vault, tmp_path):
    panel = _obs("Synthetic panel", "PANEL", LOCAL, referenceRange=_ranges("wrong-panel-unit"))
    panel["component"] = [
        {"code": {"coding": [{"system": fhir.LOINC, "code": "1234-5"}]},
         "valueQuantity": {"value": 7, "unit": "milligrams per deciliter", "code": "mg/dL"},
         "referenceRange": _ranges()},
        {"code": {"coding": [{"system": fhir.LOINC, "code": "5678-9"}]},
         "valueQuantity": {"value": 2, "unit": "sibling"}, "referenceRange": _ranges("sibling")}]
    _import(vault, tmp_path, panel)
    p = fhir.observation_series(vault, "1234-5")["points"][0]
    assert p["reference_ranges"][0]["low"]["unit"] == "mg/dL"
    assert p["reference_range"] == {"low": 4, "high": 9} and not p.get("reference_range_mismatch")
    assert "sibling" not in json.dumps(p) and "wrong-panel-unit" not in json.dumps(p)


@pytest.mark.parametrize("range_value,expected", [
    (_ranges("mmol/L"), "Synthetic recorded range &lt;adult&gt;"),
    ([{"low": {"value": 9876, "unit": "mmol/L"}}], "not comparable: different units"),
    ([{"text": "Synthetic text only"}], "Synthetic text only"),
])
def test_brief_shows_range_text_without_incomparable_numbers(vault, tmp_path, range_value, expected):
    _import(vault, tmp_path, _obs(code="4548-4", unit="%", referenceRange=range_value))
    html, withheld = brief._labs_section(vault, Policy())
    assert expected in html and withheld == 0
    assert "4 to 9" not in html and "9876" not in html


# Fix round 1: append-only regressions against the previous U3b implementation.
@pytest.mark.parametrize("category", [
    {"coding": [{"code": "laboratory"}]},
    [None, "invalid", {"coding": [None, 42, {"code": "laboratory"}]}],
    [{"coding": {"code": "laboratory"}}],
], ids=["dstu2-object", "skip-invalid-entries", "coding-object"])
def test_fix1_category_shapes_work_on_all_read_surfaces(vault, tmp_path, fake_ctx, category):
    _import(vault, tmp_path, _obs(category=category))
    reply = ops.trend_tool(fake_ctx, {"op": "metrics"})
    assert reply["clinical_series"][0]["fhir_categories"] == ["laboratory"]
    counts = ops.capture_tool(fake_ctx, {"op": "settings"})["counts"]
    assert counts["observations"] == counts["lab_results"] == 1
    assert counts["observations_by_category"] == {"laboratory": 1}
    for cmd in ("status", "erase"):
        assert "1 observations (1 lab results, 0 vital signs, 0 other)" in ops.command(fake_ctx, cmd)


def test_fix1_uncoded_pipe_name_round_trips(vault, tmp_path, fake_ctx):
    name = "Urine colour | visual"
    _import(vault, tmp_path, _obs(name, None, valueQuantity=None, valueString="Synthetic amber"))
    entry = ops.trend_tool(fake_ctx, {"op": "metrics"})["clinical_series"][0]
    assert entry["query"] == name
    points = ops.trend_tool(fake_ctx, {"op": "labs", "lab": entry["query"]})["points"]
    assert len(points) == 1 and points[0]["value_text"] == "Synthetic amber"
    # A hidden exact coded match must not fall through to an uncoded namesake.
    _import(vault, tmp_path, _obs("Synthetic depression", " visual", "Urine colour ", value=99))
    fake_ctx.config["privacy_level"] = "standard"
    reply = ops.trend_tool(fake_ctx, {"op": "labs", "lab": name})
    assert reply["points"] == [] and reply["withheld"] == 1


def test_fix1_name_fallback_includes_systemless_loinc_rows(vault, tmp_path, fake_ctx):
    _import(vault, tmp_path,
            _obs("Synth creatinine", "2160-0", value=1),
            _obs("Synth creatinine", "2160-0", value=2, effectiveDateTime="2025-02-01"),
            _obs("Synth creatinine", "2160-0", None, value=3, effectiveDateTime="2025-03-01"),
            _obs("Synth creatinine", "2160-0", LOCAL, value=99))
    points = ops.trend_tool(fake_ctx, {"op": "labs", "lab": "creatinine"})["points"]
    assert [p["value"] for p in points] == [1, 2, 3]


@pytest.mark.parametrize("unit,ranges,expected", [
    ("mmol/mol", [{"text": "31 to 42"}], "31 to 42 (in mmol/mol)"),
    ("%", [{"low": {"value": 31, "unit": "mmol/mol"}, "text": "31 to 42"}],
     "31 to 42 (different units)"),
    ("mmol/mol", [{"low": {"value": 4, "unit": "%"}, "text": "4 to 5.6"}],
     "4 to 5.6 (different units)"),
], ids=["converted-text", "mismatched-text", "converted-mismatched-text"])
def test_fix1_brief_qualifies_recorded_range_text(vault, tmp_path, unit, ranges, expected):
    _import(vault, tmp_path, _obs(code="17856-6", value=45, unit=unit, referenceRange=ranges))
    html, withheld = brief._labs_section(vault, Policy())
    assert f"<td>{expected}</td>" in html and withheld == 0
    assert f"<td>{ranges[0]['text']}</td>" not in html


def test_fix1_category_outputs_never_expose_local_codes(vault, tmp_path, fake_ctx):
    hidden = _obs("Synthetic depression", "HIDDEN", LOCAL,
                  category=[{"coding": [{"system": "urn:x", "code": "zzcatx"}]}])
    visible = _obs("Synthetic visible", "VISIBLE", LOCAL,
                   category=_cat("private-local-category", "another-local-category", "activity"))
    _import(vault, tmp_path, hidden, visible, _obs(code="NOCAT"))
    fake_ctx.config["privacy_level"] = "standard"
    settings = ops.capture_tool(fake_ctx, {"op": "settings"})
    assert "zzcatx" not in json.dumps(settings)
    assert settings["counts"]["observations_by_category"] == {"other": 2, "activity": 1, "unspecified": 1}
    reply = ops.trend_tool(fake_ctx, {"op": "metrics"})
    assert reply["withheld"] == 1
    assert not any(s in json.dumps(reply) for s in ("zzcatx", "private-local-category", "another-local-category"))
    entries = {s["code"]: s for s in reply["clinical_series"]}
    assert entries["VISIBLE"]["fhir_categories"] == ["activity", "other"]
    assert entries["NOCAT"]["fhir_categories"] == ["unspecified"]


def test_fix1_literal_systemless_alias_code_round_trips(vault, tmp_path, fake_ctx):
    _import(vault, tmp_path, _obs("Synthetic literal code", "LDL", None))
    entry = ops.trend_tool(fake_ctx, {"op": "metrics"})["clinical_series"][0]
    assert entry["query"] == "LDL"
    points = ops.trend_tool(fake_ctx, {"op": "labs", "lab": entry["query"]})["points"]
    assert len(points) == 1 and points[0]["code"] == "LDL"
    # Literal support must preserve the existing alias expansion too.
    _import(vault, tmp_path, _obs("Synthetic standard LDL", "13457-7", value=13))
    points = ops.trend_tool(fake_ctx, {"op": "labs", "lab": "LDL"})["points"]
    assert {p["code"] for p in points} == {"LDL", "13457-7"}


@pytest.mark.parametrize("loinc_newest", [False, True])
def test_fix1_loinc_and_systemless_share_discovery_entry(vault, tmp_path, fake_ctx, loinc_newest):
    _import(vault, tmp_path,
            _obs("Synthetic LOINC", "2160-0", effectiveDateTime="2025-02" if loinc_newest else "2025-01"),
            _obs("Synthetic systemless", "2160-0", None, value=13,
                 effectiveDateTime="2025-01" if loinc_newest else "2025-02"))
    entries = ops.trend_tool(fake_ctx, {"op": "metrics"})["clinical_series"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["count"] == entry["numeric"] == 2 and entry["query"] == "2160-0"
    assert entry["code_system"] == fhir.LOINC
    assert entry["name"] == ("Synthetic LOINC" if loinc_newest else "Synthetic systemless")
    assert entry["first"] == "2025-01" and entry["last"] == "2025-02"
    assert len(ops.trend_tool(fake_ctx, {"op": "labs", "lab": entry["query"]})["points"]) == 2


@pytest.mark.parametrize("extra,expected", [
    ({"appliesTo": 7}, {}),
    ({"type": [None, {"coding": [False, {"code": "normal"}]}]}, {"type": "normal"}),
    ({"age": [False, {"low": [None, {"value": 18, "unit": "years"}], "high": "invalid"}]},
     {"age": {"low": {"value": 18, "unit": "years"}}}),
    ({"appliesTo": {"coding": {"code": "adult"}}}, {"applies_to": ["adult"]}),
    ({"type": 7, "age": 7}, {}),
    ({"appliesTo": [False, {"coding": [None, {"display": "Synthetic adult"}]}, "invalid"]},
     {"applies_to": ["Synthetic adult"]}),
], ids=["bad-applies", "type-list", "age-list", "applies-object", "bad-type-age", "mixed-applies"])
def test_fix1_second_range_skips_unreadable_metadata(vault, tmp_path, fake_ctx, extra, expected):
    _import(vault, tmp_path, _obs(referenceRange=[{"low": {"value": 4}},
                                                {"text": "Synthetic second", **extra}]))
    before = vault.execute("SELECT raw_json FROM observation").fetchone()[0]
    point = ops.trend_tool(fake_ctx, {"op": "labs", "lab": "1234-5"})["points"][0]
    assert point["reference_ranges"] == [{"low": {"value": 4}}, {"text": "Synthetic second", **expected}]
    assert point["reference_range"] == {"low": 4, "high": None}
    assert vault.execute("SELECT raw_json FROM observation").fetchone()[0] == before


@pytest.mark.parametrize("as_list", [False, True], ids=["range-object", "range-mixed-list"])
def test_fix1_stored_range_shapes_are_read_without_ingest_changes(vault, tmp_path, fake_ctx, as_list):
    from caduceus.core.db import write_txn

    _import(vault, tmp_path, _obs(referenceRange=[{"low": {"value": 4}}]))
    # Simulate legacy stored metadata; current ingest is outside this read-side lane.
    document = json.loads(vault.execute("SELECT raw_json FROM observation").fetchone()[0])
    ref = {"low": [{"value": 4, "unit": "mg/dL"}], "text": "Synthetic stored",
           "type": {"coding": {"code": "normal"}}, "age": {"low": None}}
    document["referenceRange"] = [None, ref, 7] if as_list else ref
    with write_txn(vault):
        vault.execute("UPDATE observation SET raw_json=?", (json.dumps(document),))
    before = vault.execute("SELECT raw_json FROM observation").fetchone()[0]
    point = ops.trend_tool(fake_ctx, {"op": "labs", "lab": "1234-5"})["points"][0]
    assert point["reference_ranges"] == [{"low": {"value": 4, "unit": "mg/dL"},
                                           "text": "Synthetic stored", "type": "normal"}]
    assert vault.execute("SELECT raw_json FROM observation").fetchone()[0] == before
