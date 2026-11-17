"""Synthetic U3a clinical reports, private attachments and import receipts."""
import base64
import json
import socket
import urllib.request
import zipfile

import pytest

from caduceus.core import db, search, sources
from caduceus.core.policy import Policy
from caduceus.health import apple
from caduceus.health.fhir import import_fhir, ingest, observation_categories


def _attachment(text, content_type="text/plain", **fields):
    return {"contentType": content_type, "data": base64.b64encode(text.encode()).decode(), **fields}


def _report(kind="DiagnosticReport", rid="report-one", **fields):
    resource = {"resourceType": kind, "id": rid, "status": "final"}
    resource.update({"DiagnosticReport": {"code": {"text": "Synthetic report"}, "effectiveDateTime": "2024-01-02"},
                     "DocumentReference": {"type": {"text": "Synthetic document"}, "date": "2024-01-02"},
                     "Composition": {"type": {"text": "Synthetic composition"}, "date": "2024-01-02"},
                     "ImagingStudy": {"description": "Synthetic scan", "started": "2024-01-02"}}[kind])
    return {**resource, **fields}


def _observation(rid, code, value, **fields):
    return {"resourceType": "Observation", "id": rid, "effectiveDateTime": "2024-01-02",
            "code": {"text": "Synthetic measurement", "coding": [{"system": "http://loinc.org", "code": code}]},
            "valueQuantity": {"value": value, "unit": "synthetic"}, **fields}


def _bundle(*resources):
    return {"resourceType": "Bundle", "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in resources]}


def _import(vault, tmp_path, document, filename="input.json"):
    path = tmp_path / filename
    path.write_text(json.dumps(document))
    return import_fhir(vault, sources.store_source(vault, path, "fhir")[0])


def _fetch(vault, rid="report-one", policy=None):
    rows = vault.execute("SELECT id,raw_json FROM clinical_report").fetchall()
    row = next(r for r in rows if json.loads(r["raw_json"])["id"] == rid)
    return search.fetch(vault, f"report:{row['id']}", policy=policy or Policy())


def test_report_bundle_search_attachments_and_links(vault, tmp_path):
    pdf = _attachment("syntheticpdfpayload", "application/pdf", title="Scan copy", size=19)
    report = _report(conclusion="Synthetic zebrafinding", conclusionCode=[{"text": "amberfinding"}],
                     result=[{"reference": "urn:uuid:obs-one"}, {"reference": "urn:uuid:obs-two"}],
                     imagingStudy=[{"reference": "urn:uuid:scan-one"}],
                     presentedForm=[_attachment("plainattachmentword"), pdf],
                     text={"div": "<div>fallbacknotneeded</div>"})
    document = _report("DocumentReference", "document-one", description="Synthetic document description",
                       content=[{"attachment": _attachment(
                           "<div>htmlattachmentword &amp; entity<script>scriptsecret</script>"
                           "<style>stylesecret</style><p>paragraph</p></div>", "text/html")}])
    composition = _report("Composition", "composition-one", title="Discharge title", section=[
        {"title": "Section alpha", "text": {"div": "<div>sectionword</div>"}},
        {"title": "Section beta", "section": [{"title": "Nested title", "text": {"div": "<p>nestedword</p>"}}]}])
    imaging = _report("ImagingStudy", "scan-one", modality=[{"code": "MR"}], numberOfSeries=1,
                      numberOfInstances=2, series=[{"bodySite": {"code": "synthetic-site"},
                                                   "instance": [{"uid": "neverstoreinstance"}]}])
    result = _import(vault, tmp_path, _bundle(report, document, composition, imaging,
                                             _observation("obs-one", "2345-7", 90),
                                             _observation("obs-two", "4548-4", 6)))
    assert result.status == "succeeded"
    assert result.counts["report"] == 4
    assert vault.execute("SELECT count(*) FROM clinical_report").fetchone()[0] == 4
    for word, rid in [("zebrafinding", "report-one"), ("amberfinding", "report-one"),
                      ("htmlattachmentword", "document-one"), ("sectionword", "composition-one"),
                      ("nestedword", "composition-one")]:
        hits = search.search(vault, word, policy=Policy(), kinds="report")["results"]
        assert len(hits) == 1 and hits[0]["ref"] == _fetch(vault, rid)["ref"]
    for word in ("scriptsecret", "stylesecret", "fallbacknotneeded", "syntheticpdfpayload", "neverstoreinstance"):
        assert search.search(vault, word, policy=Policy(), kinds="report")["results"] == []
    fetched = _fetch(vault)
    assert fetched["title"] == "Synthetic report" and fetched["status"] == "final"
    assert fetched["date"] == "2024-01-02" and fetched["conclusion"] == "Synthetic zebrafinding"
    assert "plainattachmentword" in fetched["narrative"]
    assert sorted(r["value_num"] for r in fetched["results"]) == [6, 90]
    assert fetched["imaging"][0]["title"] == "Synthetic scan" and fetched["withheld"] == 0
    assert pdf["data"] not in vault.execute("SELECT raw_json FROM clinical_report WHERE resource_type='DiagnosticReport'").fetchone()[0]
    assert {"content_type": "application/pdf", "size": 19, "title": "Scan copy",
            "has_inline_data": True, "has_url": False} in fetched["attachments"]
    raw = json.loads(vault.execute("SELECT raw_json FROM clinical_report WHERE resource_type='DiagnosticReport'").fetchone()[0])
    assert raw["_caduceus"]["result"] == report["result"]
    assert raw["_caduceus"]["imagingStudy"] == report["imagingStudy"]
    assert raw["_caduceus"]["resource_ids"] == ["urn:uuid:report-one"]
    raw_scan = vault.execute("SELECT raw_json FROM clinical_report WHERE resource_type='ImagingStudy'").fetchone()[0]
    assert "neverstoreinstance" not in raw_scan
    assert json.loads(raw_scan)["_caduceus"]["body_sites"] == [{"code": "synthetic-site"}]
    assert "htmlattachmentword & entity" in _fetch(vault, "document-one")["narrative"]
    assert "Nested title" in _fetch(vault, "composition-one")["narrative"]


def test_ignored_type_receipts_are_safe(vault):
    resources = [{"resourceType": name} for name in ("Patient", "Patient", "Encounter", "Private-42", "Évil", "A" * 65)]
    counts = ingest(vault, {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}, None)
    assert counts["ignored"] == 6
    assert counts["ignored_by_type"] == {"Patient": 2, "Encounter": 1, "other": 3}


def test_apple_export_merges_nested_clinical_receipts(samples, vault, tmp_path):
    category = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory"}]}
    first = _bundle(_report(conclusion="exportreportword"),
                    {"resourceType": "Patient", "id": "patient-one"},
                    _observation("lab-one", "2345-7", 90, category=category))
    second = _bundle({"resourceType": "Patient", "id": "patient-two"},
                     {"resourceType": "Encounter", "id": "encounter-one"},
                     _observation("lab-two", "4548-4", 6, category=category),
                     _observation("uncategorized", "99999-9", 1))
    path = tmp_path / "clinical-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("apple_health_export/export.xml", "<HealthData/>")
        for index, document in enumerate((first, second)):
            archive.writestr(f"apple_health_export/clinical-records/{index}.json", json.dumps(document))
    source = sources.store_source(vault, path, "apple_health")[0]
    result = apple.import_apple_health(samples, vault, source)
    assert result.status == "succeeded", result.warnings
    assert result.counts["report"] == 1 and result.counts["observation"] == 3
    assert result.counts["ignored"] == 3
    assert result.counts["ignored_by_type"] == {"Patient": 2, "Encounter": 1}
    assert result.counts["observation_by_category"] == {"laboratory": 2, "unspecified": 1}
    assert search.search(vault, "exportreportword", policy=Policy(), kinds="report")["results"]
    stored = json.loads(vault.execute("SELECT counts_json FROM import_run WHERE id=?", (result.run_id,)).fetchone()[0])
    assert stored == result.counts


def test_report_idempotence_and_status_upsert(vault, tmp_path):
    report = _report(status="preliminary", conclusion="preliminaryword")
    first = _import(vault, tmp_path, report)
    assert first.counts["report"] == 1
    assert _import(vault, tmp_path, report).status == "skipped"
    again = ingest(vault, report, first.source_id)
    assert again["report"] == 0
    row_id = vault.execute("SELECT id FROM clinical_report").fetchone()[0]
    report.update(status="final", conclusion="finalword")
    final = _import(vault, tmp_path, report, "final.json")
    assert final.status == "succeeded" and final.counts["report"] == 0
    row = vault.execute("SELECT id,status,conclusion FROM clinical_report").fetchone()
    assert tuple(row) == (row_id, "final", "finalword")
    assert search.search(vault, "finalword", policy=Policy())["results"]
    assert not search.search(vault, "preliminaryword", policy=Policy())["results"]
    # Unidentified resources dedupe by normalized name; identified resources stay distinct.
    unnamed = _report(rid=None, code={"text": "  Same   title "})
    ingest(vault, unnamed, first.source_id)
    unnamed["code"]["text"] = "same title"
    assert ingest(vault, unnamed, first.source_id)["report"] == 0
    assert ingest(vault, _report(rid="other-report"), first.source_id)["report"] == 1


@pytest.mark.parametrize("kind,status", [(kind, "entered-in-error") for kind in
                         ("DiagnosticReport", "DocumentReference", "Composition", "ImagingStudy")] +
                         [("DiagnosticReport", "cancelled"), ("ImagingStudy", "cancelled")])
def test_report_retraction_is_scoped_and_preserves_other_assertions(vault, kind, status):
    one, two = _report(kind, "one"), _report(kind, "two")
    assert ingest(vault, _bundle(one, two), None)["report"] == 2
    counts = ingest(vault, _bundle({"resourceType": kind, "id": "one", "status": status}), None)
    assert counts["report_retracted"] == 1
    assert [json.loads(r[0])["id"] for r in vault.execute("SELECT raw_json FROM clinical_report")] == ["two"]
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='report'").fetchone()[0] == 1
    # Retraction cannot withdraw an assertion another identity still supports.
    row = vault.execute("SELECT id,raw_json FROM clinical_report").fetchone()
    document = json.loads(row["raw_json"])
    document["_caduceus"]["resource_ids"].append("urn:uuid:other-assertion")
    with db.write_txn(vault):
        vault.execute("UPDATE clinical_report SET raw_json=? WHERE id=?", (json.dumps(document), row["id"]))
    counts = ingest(vault, _bundle({"resourceType": kind, "id": "two", "status": status}), None)
    assert counts.get("report_retracted", 0) == 0
    kept = json.loads(vault.execute("SELECT raw_json FROM clinical_report").fetchone()[0])
    assert kept["_caduceus"]["resource_ids"] == ["urn:uuid:other-assertion"]
    assert not _fetch(vault, "two")["body"].startswith("{")  # index remains prose, never raw JSON


def test_reports_and_linked_rows_obey_privacy(vault, tmp_path):
    # Fix round 1: links resolve within the report's own stored source, and a report that links a hidden
    # result or imaging study is withheld whole (it may repeat the hidden value in its own text).
    report = _report(conclusion="Synthetic depression finding")
    mixed = _report(rid="mixed", result=[{"reference": "Observation/private"},
                                          {"reference": "Observation/public"}])
    scan_report = _report(rid="scan-report", imagingStudy=[{"reference": "ImagingStudy/private-scan"}])
    visible = _report(rid="visible", result=[{"reference": "Observation/public"}])
    resources = [report, mixed, scan_report, visible, _observation("private", "44261-6", 987),
                 _observation("public", "2345-7", 90),
                 _report("ImagingStudy", "private-scan", description="Synthetic dementia scan")]
    first = _import(vault, tmp_path, _bundle(*resources))
    policy = Policy(level="standard")
    assert _fetch(vault, policy=policy)["withheld"] is True
    assert not search.search(vault, "depression", policy=policy)["results"]
    for rid in ("mixed", "scan-report"):
        fetched = _fetch(vault, rid, policy)
        assert fetched["withheld"] is True
        assert "987" not in json.dumps(fetched) and "dementia" not in json.dumps(fetched)
    shown = _fetch(vault, "visible", policy)
    assert [r["value_num"] for r in shown["results"]] == [90]
    assert shown["imaging"] == [] and shown["withheld"] == 0
    # Hiding all records also hides reports with a sensitive category that was unlocked.
    assert _fetch(vault, policy=Policy(hidden=frozenset({"records"})))["withheld"] is True
    report.update(status="final", conclusion="Ordinary wording")
    ingest(vault, _bundle(report), first.source_id)
    assert _fetch(vault, policy=policy)["withheld"] is True


def test_attachment_limits_bad_data_and_never_fetch(vault, monkeypatch):
    calls = []

    def refused(*args, **kwargs):
        calls.append(True)
        raise AssertionError("no attachment URL may be fetched")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    decode = base64.b64decode
    decoded_lengths = []

    def track(data, *args, **kwargs):
        decoded_lengths.append(len(data))
        return decode(data, *args, **kwargs)

    monkeypatch.setattr(base64, "b64decode", track)
    large = _attachment("x" * 200_001)
    oversize = _attachment("z" * ((2 << 20) + 1))
    pdf = _attachment("pdfword", "application/pdf")
    report = _report(presentedForm=[large, oversize, pdf, {"contentType": "text/plain", "data": "%%%"},
                                    {"contentType": "text/plain", "url": "https://example.invalid/private"}])
    counts = ingest(vault, report, None)
    assert counts["report"] == 1 and counts["skipped:DiagnosticReport:attachment_unreadable"] == 1
    fetched = _fetch(vault)
    assert len(fetched["narrative"]) == 200_000 and calls == []
    assert len(oversize["data"]) not in decoded_lengths and len(pdf["data"]) not in decoded_lengths
    raw_text = vault.execute("SELECT raw_json FROM clinical_report").fetchone()[0]
    assert oversize["data"] not in raw_text and pdf["data"] not in raw_text
    assert json.loads(raw_text)["_caduceus"]["narrative_truncated"] is True
    from caduceus.health.fhir import warning_messages
    assert any("unreadable" in w for w in warning_messages(counts))


def test_observation_category_receipt_uses_existing_helper(vault, monkeypatch):
    from caduceus.health import fhir
    category = lambda code: {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": code}]}
    resources = [_observation("one", "2345-7", 1, category=category("laboratory")),
                 _observation("two", "4548-4", 2, category=[category("laboratory"), category("vital-signs"), category("laboratory"), 42]),
                 _observation("three", "99999-9", 3, category={"coding": [{"code": "private-local-code"}]}),
                 _observation("four", "99998-9", 4)]
    calls = []

    def track(document):
        calls.append(document)
        return observation_categories(document)

    monkeypatch.setattr(fhir, "observation_categories", track)
    counts = ingest(vault, _bundle(*resources), None)
    assert counts["observation_by_category"] == {"laboratory": 2, "vital-signs": 1, "other": 1, "unspecified": 1}
    assert len(calls) == 4
    assert ingest(vault, _bundle(*resources), None)["observation_by_category"] == {}


def test_existing_vault_gains_report_table_on_open(tmp_path):
    path = tmp_path / "old-vault.db"
    old = db.open_vault(path)
    with db.write_txn(old):
        old.execute("DROP TABLE IF EXISTS clinical_report")
    old.close()
    reopened = db.open_vault(path)
    try:
        assert reopened.execute("SELECT name FROM sqlite_master WHERE name='clinical_report'").fetchone()
        assert ingest(reopened, _report(), None)["report"] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("kind,date_fields", [
    ("DiagnosticReport", {"effectivePeriod": {"start": "2024-02"}, "issued": "2024-03"}),
    ("DiagnosticReport", {"issued": "2024-03"}),
    ("DocumentReference", {"context": {"period": {"start": "2024"}}}),
    ("Composition", {"date": "2024-03"}), ("ImagingStudy", {"started": "2024-03"})])
def test_report_date_fallbacks_and_tolerant_categories(vault, kind, date_fields):
    resource = _report(kind, category=[None, {"coding": [{"system": "http://snomed.info/sct", "code": "35489007"}]}])
    for field in ("effectiveDateTime", "date", "started"):
        resource.pop(field, None)
    resource.update(date_fields)
    assert ingest(vault, resource, None)["report"] == 1
    row = vault.execute("SELECT effective_at,category FROM clinical_report").fetchone()
    assert row["effective_at"] in ("2024", "2024-02", "2024-03") and row["category"] == "mental_health"
    resource = {**resource, "id": "invalid"}
    for field in ("effectiveDateTime", "effectivePeriod", "issued", "date", "context", "started"):
        resource.pop(field, None)
    assert ingest(vault, resource, None)[f"skipped:{kind}:date"] == 1
