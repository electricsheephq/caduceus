"""Synthetic regression cases for the nine U3a fix-round1 findings."""
import base64
import copy
import json

import pytest

from caduceus.core import db, search
from caduceus.core.policy import Policy
from caduceus.tools import TOOLS, make_handler
from tests.test_v040_reports import _attachment, _bundle, _import, _observation, _report


def _row(vault):
    return vault.execute("SELECT * FROM clinical_report ORDER BY id LIMIT 1").fetchone()


def _read(vault, policy=None):
    return search.fetch(vault, f"report:{_row(vault)['id']}", policy=policy or Policy())


def _url_bundle(resource, url):
    return {"resourceType": "Bundle", "entry": [{"resource": resource, "fullUrl": url}]}


@pytest.mark.parametrize("arrival", ["before", "after", "category-change"])
def test_hidden_link_withholds_whole_report_everywhere(vault, tmp_path, fake_ctx, arrival):
    observation = _observation("questionnaire", "44261-6", 987654)
    if arrival == "category-change":
        observation["code"]["coding"][0]["code"] = "2345-7"
    report = _report(conclusion="uniquereportword result 987654", text={"div": "<p>score 987654</p>"},
                     result=[{"reference": "urn:uuid:questionnaire"}],
                     presentedForm=[{"contentType": "application/pdf", "title": "Synthetic attachment"}])
    if arrival in ("before", "category-change"):
        assert _import(vault, tmp_path, _bundle(observation), "observation.json").status == "succeeded"
    assert _import(vault, tmp_path, _bundle(report), "report.json").status == "succeeded"
    assert _row(vault)["category"] == ("mental_health" if arrival == "before" else "records")
    if arrival == "after":
        assert _import(vault, tmp_path, _bundle(observation), "later.json").status == "succeeded"
    elif arrival == "category-change":
        with db.write_txn(vault):
            vault.execute("UPDATE observation SET category='mental_health'")
    fake_ctx.config["privacy_level"] = "standard"
    handler = make_handler("caduceus_search", fake_ctx)
    for arguments in ({"query": "uniquereportword", "kinds": ["report"]}, {"kinds": ["report"]}):
        result = json.loads(handler(arguments))
        assert result["results"] == [] and result["withheld"] == 1
        assert "987654" not in json.dumps(result)
    result = json.loads(handler({"ref": f"report:{_row(vault)['id']}"}))
    assert result["withheld"] is True
    assert set(result) == {"ref", "withheld", "reason"}


def test_visible_links_stay_visible_and_sensitive_tie_keeps_own_category(vault, tmp_path):
    observation = _observation("visible", "2345-7", 90)
    report = _report(result=[{"reference": "urn:uuid:visible"}], conclusion="visibleword")
    assert _import(vault, tmp_path, _bundle(observation, report)).status == "succeeded"
    assert _read(vault, Policy(level="standard"))["results"][0]["value_num"] == 90
    assert search.browse(vault, policy=Policy(level="standard"), kinds="report")["withheld"] == 0
    report.update(conclusion="Synthetic pregnancy", result=[{"reference": "urn:uuid:mental"}])
    assert _import(vault, tmp_path, _bundle(_observation("mental", "44261-6", 5), report), "tie.json").status == "succeeded"
    assert _row(vault)["category"] == "reproductive"
    assert _read(vault, Policy(level="standard", unlocked=frozenset({"reproductive"})))["withheld"] is True


@pytest.mark.parametrize("kind,attachment", [
    ("DocumentReference", {"contentType": "application/pdf", "url": "https://example.invalid/report", "title": "Synthetic depression"}),
    ("DiagnosticReport", _attachment("synthetic pdf", "application/pdf", title="Synthetic depression")),
])
def test_attachment_titles_are_sensitive(vault, tmp_path, kind, attachment):
    fields = {"content": [{"attachment": attachment}]} if kind == "DocumentReference" else {"presentedForm": [attachment]}
    assert _import(vault, tmp_path, _report(kind, **fields)).status == "succeeded"
    assert _row(vault)["category"] == "mental_health"
    assert _read(vault, Policy(level="standard"))["withheld"] is True


def test_description_and_conclusion_codes_are_classified(vault, tmp_path):
    doc = _report("DocumentReference", description="Synthetic depression")
    assert _import(vault, tmp_path, doc).status == "succeeded"
    assert _row(vault)["category"] == "mental_health"
    other = _report(rid="other", description="Synthetic pregnancy", conclusionCode=[{"text": "neutral"}])
    assert _import(vault, tmp_path, other, "other.json").status == "succeeded"
    assert vault.execute("SELECT category FROM clinical_report ORDER BY id DESC").fetchone()[0] == "reproductive"


@pytest.mark.parametrize("difference", ["code", "system", "date"])
def test_retraction_narrows_code_system_and_date(vault, tmp_path, difference):
    first = _report(code={"coding": [{"system": "urn:synthetic:a", "code": "A"}]})
    second = copy.deepcopy(first)
    if difference == "date":
        second["effectiveDateTime"] = "2025-01-02"
    else:
        second["code"]["coding"][0][difference] = "B" if difference == "code" else "urn:synthetic:b"
    bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in (first, second)]}
    assert _import(vault, tmp_path, bundle).counts["report"] == 2
    first["status"] = "entered-in-error"
    result = _import(vault, tmp_path, first, "retract.json")
    assert result.counts["report_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 1
    remaining = json.loads(_row(vault)["raw_json"])
    assert remaining["effectiveDateTime"] == second["effectiveDateTime"]
    assert remaining["code"] == second["code"]
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='report'").fetchone()[0] == 1


@pytest.mark.parametrize("old_status,new_status,old_issued,new_issued", [
    ("final", "preliminary", "2024-03-01", "2024-04-01"),
    ("corrected", "final", "2024-03-01", "2024-04-01"),
    ("final", "amended", "2024-03-01", "2024-02-01"),
    ("unknown", "partial", None, None),
])
def test_older_report_keeps_content_but_records_new_alias(vault, tmp_path, old_status, new_status, old_issued, new_issued):
    report = _report(status=old_status, issued=old_issued, conclusion="newerword")
    first = _import(vault, tmp_path, _url_bundle(report, "urn:uuid:newer"))
    report.update(status=new_status, issued=new_issued, conclusion="olderword")
    result = _import(vault, tmp_path, _url_bundle(report, "urn:uuid:older"), "older.json")
    assert result.counts["report"] == 0
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 1
    assert (_row(vault)["status"], _row(vault)["conclusion"], _row(vault)["source_id"]) == (old_status, "newerword", first.source_id)
    assert set(json.loads(_row(vault)["raw_json"])["_caduceus"]["resource_ids"]) >= {"urn:uuid:newer", "urn:uuid:older"}
    assert search.search(vault, "newerword", policy=Policy())["results"]
    assert not search.search(vault, "olderword", policy=Policy())["results"]


@pytest.mark.parametrize("status", ["final", "amended", "corrected", "appended", "unknown"])
def test_newer_report_updates_at_equal_or_higher_rank(vault, tmp_path, status):
    report = _report(status="final", issued="2024-03-01", conclusion="firstword")
    _import(vault, tmp_path, report)
    report.update(status=status, issued="2024-04-01", conclusion="secondword")
    result = _import(vault, tmp_path, report, "next.json")
    assert result.counts.get("report_updated") == 1 and _row(vault)["conclusion"] == "secondword"


def test_reexport_aliases_dedupe_and_retract_together(vault, tmp_path):
    report = _report()
    for index, url in enumerate(("urn:uuid:r-1111", "urn:uuid:r-2222", None)):
        result = _import(vault, tmp_path, _url_bundle(report, url) if url else report, f"report-{index}.json")
        assert result.counts["report"] == (1 if index == 0 else 0)
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 1
    assert set(json.loads(_row(vault)["raw_json"])["_caduceus"]["resource_ids"]) == {
        "urn:uuid:r-1111", "urn:uuid:r-2222", "report-one"}
    result = _import(vault, tmp_path, _url_bundle({**report, "status": "entered-in-error"}, "urn:uuid:r-3333"), "retract.json")
    assert result.counts["report_retracted"] == 1


def test_urn_without_resource_id_and_absolute_urls_remain_distinct(vault, tmp_path):
    report = _report(rid=None)
    for index, url in enumerate(("urn:uuid:one", "urn:uuid:two", "https://a.invalid/DiagnosticReport/1", "https://b.invalid/DiagnosticReport/1")):
        result = _import(vault, tmp_path, _url_bundle(report, url), f"report-{index}.json")
        assert result.counts["report"] == 1
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 4


def test_line_wrapped_base64_is_searchable(vault, tmp_path):
    data = base64.encodebytes(("wrappedword " * 50).encode()).decode()
    attachment = {"contentType": "text/plain", "data": "\t\r " + data + "\v\f"}
    result = _import(vault, tmp_path, _report(presentedForm=[attachment]))
    assert result.status == "succeeded" and not result.counts.get("skipped:DiagnosticReport:attachment_unreadable")
    assert search.search(vault, "wrappedword", policy=Policy())["results"]


@pytest.mark.parametrize("reference", ["Observation/1", "urn:uuid:foreign", "https://a.invalid/Observation/1"])
def test_cross_source_links_require_exact_identity(vault, tmp_path, reference):
    url = reference if reference != "Observation/1" else "https://a.invalid/Observation/1"
    observation = _observation("1", "44261-6", 987654)
    assert _import(vault, tmp_path, _url_bundle(observation, url), "foreign.json").status == "succeeded"
    assert _import(vault, tmp_path, _report(result=[{"reference": reference}]), "report.json").status == "succeeded"
    row = _row(vault)
    if reference == "Observation/1":
        assert row["category"] == "mental_health"
        assert _read(vault)["results"] == [] and _read(vault)["withheld"] == 0
        assert _read(vault, Policy(level="standard"))["withheld"] is True
    else:
        assert row["category"] == "mental_health"
        assert _read(vault)["results"][0]["value_num"] == 987654
        assert _read(vault, Policy(level="standard"))["withheld"] is True


@pytest.mark.parametrize("url", ["https://same.invalid/Observation/1", "urn:uuid:same"])
def test_same_source_relative_links_resolve(vault, tmp_path, url):
    observation = _observation("1", "2345-7", 90)
    report = _report(result=[{"reference": "Observation/1"}])
    bundle = {"resourceType": "Bundle", "entry": [{"fullUrl": url, "resource": observation}, {"resource": report}]}
    assert _import(vault, tmp_path, bundle).status == "succeeded"
    assert _read(vault)["results"][0]["value_num"] == 90


@pytest.mark.parametrize("concept,expected", [
    ({"text": 123}, "123"), ({"text": ["ignored"], "coding": [{"display": 42, "code": 7}]}, "42"),
    ({"text": {"bad": "shape"}, "coding": [{"display": [], "code": 7}]}, "7"),
    ({"coding": [{"display": {}, "code": []}]}, "Unknown"),
    ({"text": True, "coding": [{"display": False, "code": {}}]}, "Unknown"),
])
def test_malformed_concept_labels_skip_or_convert_numbers(vault, tmp_path, concept, expected):
    result = _import(vault, tmp_path, _report(code=concept))
    assert result.status == "succeeded", result.warnings
    assert _row(vault)["name"] == expected


@pytest.mark.parametrize("resource,expected_date", [
    (_report(text="not an object"), "2024-01-02"),
    (_report("Composition", section=[{"title": "Section", "text": ["invalid"]}]), "2024-01-02"),
    (_report("DocumentReference", date=None, context=[None, {"period": {"start": "2024-05"}}]), "2024-05"),
    (_report(effectiveDateTime=None, effectivePeriod="invalid", issued="2024-06"), "2024-06"),
    (_report("DocumentReference", context={"period": ["invalid"]}), "2024-01-02"),
])
def test_malformed_optional_shapes_do_not_fail_import(vault, tmp_path, resource, expected_date):
    result = _import(vault, tmp_path, resource)
    assert result.status == "succeeded", result.warnings
    assert _row(vault)["effective_at"] == expected_date


def test_contained_binary_bytes_are_not_stored(vault, tmp_path):
    payload = base64.b64encode(b"syntheticbinarypayload").decode()
    document = _report("DocumentReference", contained=[{"resourceType": "Binary", "id": "binary", "data": payload,
                                                         "contentType": "application/pdf"}],
                       content=[{"attachment": {"url": "#binary", "contentType": "application/pdf"}}])
    assert _import(vault, tmp_path, document).status == "succeeded"
    raw = _row(vault)["raw_json"]
    assert payload not in raw and "data" not in json.loads(raw)["contained"][0]


def test_document_docstatus_entered_in_error_retracts(vault, tmp_path):
    document = _report("DocumentReference", status="current", docStatus="final")
    assert _import(vault, tmp_path, document).counts["report"] == 1
    document["docStatus"] = "entered-in-error"
    result = _import(vault, tmp_path, document, "retract.json")
    assert result.counts.get("report_retracted") == 1
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 0
    assert not search.browse(vault, policy=Policy(), kinds="report")["results"]


def test_report_is_in_search_tool_kind_enum():
    tool = next(tool for tool in TOOLS if tool["name"] == "caduceus_search")
    assert "report" in tool["parameters"]["properties"]["kinds"]["items"]["enum"]
