"""Round-two regressions: synthetic pre-upgrade imports, sleep and Condition dates."""
import json
import zipfile
from datetime import date, timedelta

import pytest

from conftest import FakeCtx
from caduceus import ops
from caduceus.core import db, sources
from caduceus.health import apple, fhir, hae


def _bundle(*resources):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def _obs(rid, name="Synthetic Alpha", value=3):
    return {"resourceType": "Observation", "id": rid, "status": "final", "code": {"text": name},
            "effectiveDateTime": "2026-11-01", "valueQuantity": {"value": value, "unit": "u"}}


def _condition(recorded="2026-11", name="Synthetic condition"):
    return {"resourceType": "Condition", "id": "synthetic-condition", "code": {"text": name},
            "recordedDate": recorded, "clinicalStatus": {"coding": [{"code": "active"}]}}


def _file(tmp_path, document, zipped):
    path = tmp_path / ("export.zip" if zipped else "records.json")
    if zipped:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("apple_health_export/export.xml", '<HealthData><Record '
                             'type="HKQuantityTypeIdentifierStepCount" sourceName="Synthetic Watch" '
                             'value="10" unit="count" startDate="2026-11-01 12:00:00 +0000"/></HealthData>')
            archive.writestr("apple_health_export/clinical-records/records.json", json.dumps(document))
    else:
        path.write_text(json.dumps(document))
    return path


def _snapshot(vault, table):
    return [dict(row) for row in vault.execute(f"SELECT * FROM {table} ORDER BY id")]


@pytest.mark.parametrize("zipped", [False, True], ids=["fhir", "apple_clinical"])
def test_pre031_reimport_recovers_only_new_records(vault, samples, tmp_path, monkeypatch, zipped):
    alpha, beta = _obs("alpha"), _obs("beta", "Synthetic Beta")
    stale = _obs("stale", "Synthetic corrected", 5)
    correction = {**stale, "id": "corrector", "status": "corrected",
                  "valueQuantity": {"value": 6, "unit": "u"}}
    withdrawn = _obs("withdrawn", "Synthetic withdrawn", 8)
    retraction = {**withdrawn, "status": "entered-in-error"}
    document = _bundle(alpha, beta, {**stale, "id": "corrector"}, stale, correction, retraction, _condition())
    path = _file(tmp_path, document, zipped)
    src, _ = sources.store_source(vault, path, "apple_health" if zipped else "fhir")

    def import_file(conn):
        return apple.import_apple_health(samples, conn, src) if zipped else fhir.import_fhir(conn, src)

    real_key, real_ingest = fhir._key, fhir.ingest

    def old_ingest(conn, doc, source_id, **kwargs):
        # Old ingestion merged the uncoded pair and skipped recorded-only Conditions.
        doc = {**doc, "entry": [e for e in doc["entry"] if e["resource"]["resourceType"] != "Condition"]}
        return real_ingest(conn, doc, source_id, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fhir, "PARSER_VERSION", "1")
        patch.setattr(fhir, "_key", lambda parts: real_key(
            parts[:-1] if parts[0] == "Observation" and len(parts) == 8 else parts))
        patch.setattr(fhir, "ingest", old_ingest)
        fhir.ingest(vault, _bundle(withdrawn), src.id)
        first = import_file(vault)
        assert first.status == "succeeded"
    with db.write_txn(vault):
        # Pre-0.3.1 Apple had only the combined apple_health run.
        if zipped:
            vault.execute("DELETE FROM import_run WHERE parser='apple_clinical'")
            vault.execute("UPDATE import_run SET counts_json=? WHERE source_id=?",
                          (json.dumps({k: v for k, v in first.counts.items() if k != "parts"}), src.id))
        vault.execute("DELETE FROM settings WHERE key='migration:fhir_uncoded_names_v1'")
    before = _snapshot(vault, "observation")
    assert len(before) == 2
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    sample_rows = _snapshot(samples, "sample")
    sample_runs = [row for row in sources.recent_runs(vault) if row["parser"] == apple.PARSER]

    # Public tool entry opens/migrates the vault before invoking either importer.
    upgraded = db.open_vault(vault.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert upgraded.execute("SELECT value_json FROM settings WHERE key='migration:fhir_uncoded_names_v1'").fetchone()
        migrated = _snapshot(upgraded, "observation")
        assert migrated[0]["dedupe_key"] != before[0]["dedupe_key"]
        assert migrated[1]["dedupe_key"] != before[1]["dedupe_key"]
        if zipped:
            def no_samples(*args, **kwargs):
                pytest.fail("clinical recovery must not read sample XML or recompute sample rollups")
            monkeypatch.setattr(apple, "_xml", no_samples)
            monkeypatch.setattr(apple, "recompute_rollups", no_samples)
            archive_open = zipfile.ZipFile.open

            def clinical_only(archive, member, *args, **kwargs):
                name = member.filename if isinstance(member, zipfile.ZipInfo) else str(member)
                assert "clinical-records/" in name
                return archive_open(archive, member, *args, **kwargs)
            monkeypatch.setattr(zipfile.ZipFile, "open", clinical_only)
        second = import_file(upgraded)
        assert second.status == "succeeded", second
        assert second.counts["condition"] == second.counts["observation"] == 1
        assert second.counts.get("observation_superseded", 0) == 0
        assert second.counts.get("observation_retracted", 0) == 0
        assert second.counts["observation_ignored_after_correction"] == 2
        assert _snapshot(upgraded, "observation")[:2] == migrated
        if zipped:
            assert second.counts["parts"][apple.PARSER] == {"status": "skipped", "run_id": None}
            assert second.counts["parts"][apple.CLINICAL_PARSER] == {"status": "succeeded", "run_id": second.run_id}
            assert second.counts.get("samples", 0) == 0
            assert _snapshot(samples, "sample") == sample_rows
            assert [row for row in sources.recent_runs(upgraded) if row["parser"] == apple.PARSER] == sample_runs
        reported = ops.capture_tool(FakeCtx(), {"op": "status", "run_id": second.run_id})
        assert reported["parser"] == (apple.CLINICAL_PARSER if zipped else fhir.PARSER)
        assert reported["parser_version"] == "2"
        assert reported["counts"]["condition"] == reported["counts"]["observation"] == 1
        runs = _snapshot(upgraded, "import_run")
        third = import_file(upgraded)
        assert third.status == "skipped"
        assert _snapshot(upgraded, "import_run") == runs
    finally:
        upgraded.close()


def test_first_apple_import_reports_samples_and_clinical(vault, samples, tmp_path):
    path = _file(tmp_path, _bundle(_obs("alpha"), _condition()), True)
    result = ops.capture_tool(FakeCtx(), {"op": "import", "path": str(path)})
    assert result["status"] == "succeeded", result
    assert result["counts"]["samples"] == result["counts"]["observation"] == result["counts"]["condition"] == 1
    assert {part["status"] for part in result["counts"]["parts"].values()} == {"succeeded"}
    runs = ops.capture_tool(FakeCtx(), {"op": "status"})["recent_imports"]
    assert {run["parser"] for run in runs} == {apple.PARSER, apple.CLINICAL_PARSER}
    assert next(run for run in runs if run["parser"] == apple.PARSER)["counts"]["samples"] == 1
    assert next(run for run in runs if run["parser"] == apple.CLINICAL_PARSER)["counts"]["observation"] == 1
    again = ops.capture_tool(FakeCtx(), {"op": "import", "path": str(path)})
    assert again["status"] == "skipped"


def _seg(night, start, end, source="A"):
    previous = (date.fromisoformat(night) - timedelta(days=1)).isoformat()
    return {"date": f"{night} 00:00:00 +0900", "start": f"{previous if start >= 12 else night} {start:02d}:00:00 +0900",
            "end": f"{night} {end:02d}:00:00 +0900", "value": "Core", "source": source}


def _summary(night, hours=7, source="A"):
    return {"date": f"{night} 00:00:00 +0900", "sleepEnd": f"{night} 07:00:00 +0900",
            "totalSleep": hours, "source": source}


def _sleep_file(vault, samples, tmp_path, groups, name):
    def payload(entries):
        return {"data": {"metrics": [{"name": "sleep_analysis", "data": entries}]}}
    path = tmp_path / name
    if name.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as archive:
            for i, entries in enumerate(groups):
                archive.writestr(f"{i}.json", json.dumps(payload(entries)))
    else:
        path.write_text(json.dumps(payload(groups[0])))
    src, _ = sources.store_source(vault, path, "hae")
    result = hae.import_hae(samples, vault, src)
    assert result.status == "succeeded", result
    return result


def _hours(samples, night):
    return samples.execute("SELECT value FROM daily_rollup WHERE metric='sleep_hours' AND day=?", (night,)).fetchone()[0]


@pytest.mark.parametrize("layout", ["json", "summary-first", "summary-last"])
def test_add_only_night_replaces_summary_source_once(vault, samples, tmp_path, monkeypatch, layout):
    night = "2026-11-13"
    _sleep_file(vault, samples, tmp_path, [[_seg(night, 7, 8), _seg(night, 1, 2, "B")]], "old.json")
    b = dict(samples.execute("SELECT * FROM sample WHERE source_name='B'").fetchone())
    groups = [[_summary(night), _summary(night, 6)], [_seg(night, 6, 7), _seg(night, 2, 3, "B")]]
    if layout == "json":
        groups = [sum(groups, [])]
    elif layout == "summary-last":
        groups.reverse()
    monkeypatch.setattr(hae, "BATCH", 1)  # repeated summaries/zip members must not clear the source twice
    result = _sleep_file(vault, samples, tmp_path, groups, "new.json" if layout == "json" else "new.zip")
    assert _hours(samples, night) == 7
    assert result.counts["samples_replaced"] == 1
    assert result.counts["sleep_segments_omitted"] == 1
    assert dict(samples.execute("SELECT * FROM sample WHERE id=?", (b["id"],)).fetchone()) == b
    assert samples.execute("SELECT count(*) FROM sample WHERE source_name='A'").fetchone()[0] == 2


def test_review1_sleep_probe_gives_seven_hours(vault, samples, tmp_path):
    n0, n1, n2 = "2026-11-12", "2026-11-13", "2026-11-14"
    middle = [_seg(n1, 23, 3), _seg(n1, 4, 7)]
    _sleep_file(vault, samples, tmp_path, [[_seg(n0, 23, 6), *middle, _seg(n2, 23, 6)]], "old.json")
    result = _sleep_file(vault, samples, tmp_path,
                        [[_summary(n1), *middle, _seg(n1, 1, 3, "B"), _seg(n2, 23, 6)]], "new.json")
    assert _hours(samples, n1) == 7
    assert result.counts["samples_replaced"] == 2
    assert samples.execute("SELECT count(*) FROM sample WHERE local_day=? AND source_name='A'", (n1,)).fetchone()[0] == 1


def test_issue18_other_source_keeps_six_hours(vault, samples, tmp_path):
    night = "2026-11-13"
    _sleep_file(vault, samples, tmp_path, [[_seg(night, 0, 4), _seg(night, 4, 6)]], "old.json")
    result = _sleep_file(vault, samples, tmp_path, [[_seg(night, 4, 6), _summary(night, 2, "B")]], "new.json")
    assert _hours(samples, night) == 6
    assert result.counts["samples_replaced"] == 0


@pytest.mark.parametrize("conflict", [False, True], ids=["incoming-document", "kept-document"])
def test_condition_recorded_dates_follow_kept_document(vault, conflict):
    original = _condition("2026-10")
    original["code"]["coding"] = [{"display": "Synthetic depression"}]
    # Same key, different category: text remains unchanged, while the coding display changes category.
    incoming = {**original, "recordedDate": "2026-11-02T08:00:00Z",
                "clinicalStatus": {"coding": [{"code": "resolved"}]}}
    if conflict:
        incoming["code"] = {"text": original["code"]["text"], "coding": [{"display": "Synthetic pregnancy"}]}
    assert fhir.ingest(vault, original, None)["condition"] == 1
    assert fhir.ingest(vault, incoming, None)["condition_updated"] == 1
    row = vault.execute("SELECT * FROM condition").fetchone()
    raw = json.loads(row["raw_json"])
    expected = "2026-10" if conflict else "2026-11-02T08:00:00Z"
    assert row["recorded"] == raw["recordedDate"] == expected
    assert raw["_caduceus"]["recorded_precision"] == ("month" if conflict else "timestamp")
    assert row["clinical_status"] == "resolved"
    indexed = vault.execute("SELECT * FROM search_doc WHERE kind='condition'").fetchone()
    assert indexed["ts"] == expected
    assert f"recorded {expected}" in indexed["title"]
    assert indexed["body"] == row["raw_json"]
