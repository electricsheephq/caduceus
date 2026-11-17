"""Round-2 source-independent privacy and bounded link-resolution cost."""
import json

import pytest

from caduceus.core import search
from caduceus.core.policy import Policy
from caduceus.health.fhir import ingest
from tests.test_v040_reports import _report, _observation, _bundle, _import, _fetch


def _urls(*pairs):
    return {"resourceType": "Bundle", "entry": [{"resource": r, "fullUrl": url} for r, url in pairs]}


def _hidden(vault):
    assert _fetch(vault, policy=Policy(level="standard"))["withheld"] is True
    assert search.browse(vault, policy=Policy(level="standard"), kinds="report")["results"] == []
    assert search.search(vault, "copyword", policy=Policy(level="standard"), kinds="report")["results"] == []


def test_s1_late_hidden_result_stays_hidden_after_new_export(vault, tmp_path):
    report = _report(status="preliminary", conclusion="copyword", result=[{"reference": "Observation/q"}])
    first = _import(vault, tmp_path, _bundle(report, _observation("q", "44261-6", 12)), "a.json")
    assert first.status == "succeeded"
    _hidden(vault)
    second = _import(vault, tmp_path, {**report, "status": "final"}, "b.json")
    assert second.status == "succeeded"
    _hidden(vault)
    meta = json.loads(vault.execute("SELECT raw_json FROM clinical_report").fetchone()[0])["_caduceus"]
    assert meta["source_ids"] == sorted([first.source_id, second.source_id])


@pytest.mark.parametrize("kind", ["Observation", "ImagingStudy"])
def test_s2_separate_files_use_broad_privacy(vault, tmp_path, kind):
    linked = _observation("q", "44261-6", 12) if kind == "Observation" else _report(
        "ImagingStudy", "q", description="depression scan")
    field = "result" if kind == "Observation" else "imagingStudy"
    _import(vault, tmp_path, _urls((linked, f"https://portal.invalid/fhir/{kind}/q")), "labs.json")
    report = _report(conclusion="copyword", **{field: [{"reference": f"{kind}/q"}]})
    _import(vault, tmp_path, _urls((report, "https://portal.invalid/fhir/DiagnosticReport/r")), "report.json")
    row = vault.execute("SELECT category FROM clinical_report WHERE resource_type='DiagnosticReport'").fetchone()
    assert row[0] == "mental_health"
    assert _fetch(vault, policy=Policy(level="standard"))["withheld"] is True
    assert not search.search(vault, "copyword", policy=Policy(level="standard"), kinds="report")["results"]


@pytest.mark.parametrize("kind", ["Observation", "ImagingStudy"])
def test_absolute_report_base_displays_only_matching_provider(vault, tmp_path, kind):
    def linked(value):
        return _observation("q", "2345-7", value) if kind == "Observation" else _report(
            "ImagingStudy", "q", description=f"Scan {value}")
    for host, value in [("portal", 90), ("foreign", 99)]:
        _import(vault, tmp_path, _urls((linked(value), f"https://{host}.invalid/fhir/{kind}/q")), f"{host}.json")
    field = "result" if kind == "Observation" else "imagingStudy"
    report = _report(**{field: [{"reference": f"{kind}/q"}]})
    _import(vault, tmp_path, _urls((report, "https://portal.invalid/fhir/DiagnosticReport/r")), "report.json")
    rows = _fetch(vault)["results" if kind == "Observation" else "imaging"]
    assert len(rows) == 1
    assert rows[0]["value_num"] == 90 if kind == "Observation" else rows[0]["title"] == "Scan 90"


@pytest.mark.parametrize("status", ["final", "preliminary"])
def test_all_report_sources_survive_reexport_including_stale_copy(vault, tmp_path, status):
    report = _report(result=[{"reference": "Observation/q"}])
    first = _import(vault, tmp_path, _bundle(report, _observation("q", "2345-7", 90)), "a.json")
    second = _import(vault, tmp_path, {**report, "status": status}, "b.json")
    assert _fetch(vault)["results"][0]["value_num"] == 90
    meta = json.loads(vault.execute("SELECT raw_json FROM clinical_report").fetchone()[0])["_caduceus"]
    assert meta["source_ids"] == sorted([first.source_id, second.source_id])


def test_legacy_report_source_fallback(vault, tmp_path):
    _import(vault, tmp_path, _bundle(_report(result=[{"reference": "Observation/q"}]),
                                    _observation("q", "2345-7", 90)))
    vault.execute("UPDATE clinical_report SET raw_json=json_remove(raw_json,'$._caduceus.source_ids')")
    vault.commit()
    assert _fetch(vault)["results"][0]["value_num"] == 90


def test_same_rank_missing_issued_keeps_dated_content(vault, tmp_path):
    report = _report(issued="2024-01-03T10:00:00Z", conclusion="datedword")
    first = _import(vault, tmp_path, report, "dated.json")
    second = _import(vault, tmp_path, _report(conclusion="undatedword"), "undated.json")
    row = vault.execute("SELECT * FROM clinical_report").fetchone()
    assert row["conclusion"] == "datedword" and row["issued"] == report["issued"]
    assert row["source_id"] == first.source_id
    assert not search.search(vault, "undatedword", policy=Policy())["results"]
    assert json.loads(row["raw_json"])["_caduceus"]["source_ids"] == sorted([first.source_id, second.source_id])


def _assert_one_pass(statements):
    for table in ("observation", "clinical_report"):
        # Identity lookups and row hydration are point reads, not a resolver table pass.
        scans = [s.lower() for s in statements if s.lower().startswith("select ") and
                 f" from {table}" in s.lower() and " where id=" not in s.lower() and
                 " where dedupe_key=" not in s.lower() and "source='fhir'" not in s.lower()]
        assert len(scans) == 1, scans
        projection = scans[0].split(" from ")[0]
        assert "select *" not in projection
        assert "json_extract(raw_json,'$.id')" in projection
        assert "json_extract(raw_json,'$._caduceus.resource_ids')" in projection
        assert "effective_at" not in projection and "name" not in projection


def _many_reports():
    return [_report(rid=f"r{i}", result=[{"reference": "Observation/q"}],
                    imagingStudy=[{"reference": "ImagingStudy/s"}]) for i in range(30)]


@pytest.mark.parametrize("operation", ["browse", "search", "fetch", "ingest"])
def test_link_tables_scanned_once_per_call(vault, operation):
    ingest(vault, _bundle(_observation("q", "2345-7", 90), _report("ImagingStudy", "s")), None)
    if operation != "ingest":
        ingest(vault, _bundle(*_many_reports()), None)
    statements = []
    vault.set_trace_callback(statements.append)
    try:
        if operation == "ingest":
            ingest(vault, _bundle(*_many_reports()), None)
        elif operation == "browse":
            search.browse(vault, policy=Policy(), kinds="report")
        elif operation == "search":
            search.search(vault, "Synthetic", policy=Policy(), kinds="report")
        else:
            search.fetch(vault, "report:2", policy=Policy())
    finally:
        vault.set_trace_callback(None)
    _assert_one_pass(statements)


@pytest.mark.parametrize("kind", ["Observation", "ImagingStudy"])
def test_ingest_resolver_sees_later_rows_without_rescan(vault, kind):
    field = "result" if kind == "Observation" else "imagingStudy"
    reports = [_report(rid=f"r{i}", **{field: [{"reference": f"{kind}/q"}]}) for i in range(2)]
    linked = _observation("q", "44261-6", 12) if kind == "Observation" else _report(
        "ImagingStudy", "q", description="depression scan")
    ingest(vault, _bundle(reports[0], linked, reports[1]), None)
    row = vault.execute("SELECT category FROM clinical_report WHERE json_extract(raw_json,'$.id')='r1'").fetchone()
    assert row[0] == "mental_health"
    assert search.fetch(vault, "report:1", policy=Policy(level="standard"))["withheld"] is True


@pytest.mark.parametrize("kind", ["Observation", "ImagingStudy"])
def test_ingest_resolver_removes_retracted_rows(vault, kind):
    field = "result" if kind == "Observation" else "imagingStudy"
    reference = [{"reference": f"urn:uuid:q"}]
    linked = _observation("q", "44261-6", 12) if kind == "Observation" else _report(
        "ImagingStudy", "q", description="depression scan")
    ingest(vault, _bundle(_report(rid="seed", **{field: reference}), linked,
                          _report(rid="hidden", **{field: reference}),
                          {**linked, "status": "entered-in-error"},
                          _report(**{field: reference})), None)
    assert _fetch(vault, policy=Policy(level="standard"))["withheld"] == 0
    assert vault.execute("SELECT category FROM clinical_report WHERE json_extract(raw_json,'$.id')='hidden'").fetchone()[0] == "mental_health"


def test_ingest_resolver_replaces_corrected_observation(vault):
    report = _report(result=[{"reference": "urn:uuid:q"}])
    old = _observation("q", "2345-7", 90, valueCodeableConcept={"text": "depression"})
    ingest(vault, _bundle({**report, "id": "seed"}, old,
                          _observation("q", "2345-7", 91, status="corrected"), report), None)
    fetched = _fetch(vault, policy=Policy(level="standard"))
    assert fetched["withheld"] == 0 and fetched["results"][0]["value_num"] == 91


# Round 3 (orchestrator, from the reviewer's last delta check): broad privacy matching does not depend on how a
# reference is written (absolute URL, another base, a version suffix), for Observations and ImagingStudies.
@pytest.mark.parametrize("kind", ["Observation", "ImagingStudy"])
@pytest.mark.parametrize("stored,reference", [
    (None, "https://h.invalid/fhir/{kind}/q"),                                   # V1: absolute ref, bare-id row
    ("https://h.invalid/fhir/{kind}/q", "https://h.invalid:443/fhir/r4/{kind}/q"),  # V2: another base
    ("urn:uuid:5e1f0000-0000-4000-8000-000000000001", "{kind}/q"),               # S4: urn-stored row
    (None, "{kind}/q/_history/2"),                                               # S5: version suffix
    ("https://h.invalid/fhir/{kind}/q", "https://h.invalid/fhir/{kind}/q/_history/7"),
])
def test_r3_reference_form_never_bypasses_privacy(vault, tmp_path, kind, stored, reference):
    linked = _observation("q", "44261-6", 12) if kind == "Observation" else _report(
        "ImagingStudy", "q", description="depression scan")
    field = "result" if kind == "Observation" else "imagingStudy"
    report = _report(conclusion="copyword", **{field: [{"reference": reference.format(kind=kind)}]})
    entries = [{"resource": report}, {"resource": linked}]
    if stored:
        entries[1]["fullUrl"] = stored.format(kind=kind)
    _import(vault, tmp_path, {"resourceType": "Bundle", "entry": entries})
    _hidden(vault)


def test_r3_trailing_reference_of_another_type_does_not_match(vault, tmp_path):
    report = _report(conclusion="copyword", result=[{"reference": "https://h.invalid/fhir/Patient/q"}])
    _import(vault, tmp_path, _bundle(report, _observation("q", "44261-6", 12)))
    assert _fetch(vault, policy=Policy(level="standard"))["withheld"] == 0
