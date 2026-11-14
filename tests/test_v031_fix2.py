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
