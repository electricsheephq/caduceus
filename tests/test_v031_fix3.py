"""Pass-two regressions: synthetic sleep buckets, marker privacy and FHIR records."""
import json
from datetime import date, timedelta

import pytest

from conftest import FakeCtx
from caduceus import ops
from caduceus.core import db, sources
from caduceus.genome.dtc import import_dtc
from caduceus.health import fhir, hae
from caduceus.knowledge import pack as knowledge


TODAY = date(2026, 11, 14)


def _bucket(day=TODAY, **values):
    return {"start": f"{day} 12:00:00 AM +0900", "end": f"{day} 11:59:59 PM +0900",
            "sources": "Synthetic Phone", "asleep": 0, "inBed": 7.5, "core": 0, "deep": 0,
            "rem": 0, "totalSleep": 0, **values}


def _server(entries):
    def call(tool, args):
        if tool == "get_workouts":
            data = {"workouts": []}
        elif args["metrics"] == "sleep_analysis":
            first, last = (date.fromisoformat(args[k]) for k in ("start", "end"))
            data = {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [e for e in entries
                                if first <= date.fromisoformat(e["start"][:10]) < last]}]}
        else:
            data = {"metrics": []}
        return {"ok": True, "result": json.dumps({"data": data})}
    return call


def _sync(vault, samples, entries, today=TODAY):
    return hae.sync_hae(_server(entries), samples, vault, server="synthetic-hae", backfill_days=7, today=today)


def _nights(samples):
    return [tuple(r) for r in samples.execute(
        "SELECT day,value FROM daily_rollup WHERE metric='sleep_hours' ORDER BY day")]


def test_iphone_only_sleep_syncs_parse_and_advance(vault, samples):
    entries = [_bucket()]
    first = _sync(vault, samples, entries)
    second = _sync(vault, samples, entries, TODAY + timedelta(days=1))
    for result in (first, second):
        assert result.status == "succeeded", result
        assert result.counts["entries_by_kind"]["sleep"] == {"parsed": 1, "skipped": 0}
        assert result.counts["samples"] == 0 and result.warnings == []
        assert "unreadable_kinds" not in result.counts
        assert sources.run_info(vault, result.run_id)["status"] == "succeeded"
    assert first.counts["since"] == "2026-11-08"
    assert second.counts["since"] == "2026-11-12"
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
    assert _nights(samples) == []


def test_mixed_sleep_stores_only_watch_nights(vault, samples):
    entries = [_bucket(TODAY - timedelta(days=1)), _bucket(core=4, deep=1, rem=2, totalSleep=7)]
    result = _sync(vault, samples, entries)
    assert result.status == "succeeded", result
    assert result.counts["entries_by_kind"]["sleep"] == {"parsed": 2, "skipped": 0}
    assert result.counts["samples"] == 1
    assert _nights(samples) == [(str(TODAY), 7.0)]


def test_iphone_only_file_sleep_prescan_accepts_empty_rows(vault, samples, tmp_path):
    path = tmp_path / "synthetic-sleep.json"
    path.write_text(json.dumps({"data": {"metrics": [{"name": "sleep_analysis", "data": [_bucket()]}]}}))
    src, _ = sources.store_source(vault, path, "hae")
    result = hae.import_hae(samples, vault, src)
    assert result.status == "succeeded", result
    assert result.counts["samples"] == result.counts["entries_skipped"] == 0
    assert result.warnings == [] and _nights(samples) == []


@pytest.mark.parametrize("values", [
    {"totalSleep": -1}, {"asleep": -1, "totalSleep": 7}, {"inBed": -1},
    {"core": "invalid", "totalSleep": 7}, {"inBed": "invalid"}, {"totalSleep": float("nan")},
    {"rem": float("inf"), "totalSleep": 7}, {"totalSleep": 25}, {"core": 23, "deep": 2},
])
def test_malformed_sleep_summary_still_fails_sync(vault, samples, values):
    result = _sync(vault, samples, [_bucket(**values)])
    assert result.status == "failed", result
    assert result.counts["entries_by_kind"]["sleep"] == {"parsed": 0, "skipped": 1}
    assert result.counts["unreadable_kinds"] == ["sleep"]
    assert result.counts["samples"] == 0 and _nights(samples) == []


def test_zero_total_sleep_falls_back_to_stages(vault, samples):
    result = _sync(vault, samples, [_bucket(asleep=1, core=4, deep=1, rem=1)])
    assert result.status == "succeeded", result
    assert _nights(samples) == [(str(TODAY), 7.0)]


def test_long_sleep_buckets_stay_on_neighbouring_nights(vault, samples):
    result = _sync(vault, samples, [_bucket("2026-11-13", core=9, deep=2, rem=3, totalSleep=14),
                                  _bucket(core=4, deep=1, rem=2, totalSleep=7)])
    assert result.status == "succeeded", result
    assert _nights(samples) == [("2026-11-13", 14.0), ("2026-11-14", 7.0)]
    assert [tuple(r) for r in samples.execute(
        "SELECT start_utc,end_utc,tz_offset_min FROM sample ORDER BY local_day")] == [
            ("2026-11-12T13:00:00Z", "2026-11-13T03:00:00Z", 540),
            ("2026-11-13T20:00:00Z", "2026-11-14T03:00:00Z", 540)]


@pytest.mark.parametrize("end_field", ["sleepEnd", "inBedEnd"])
def test_file_sleep_summary_keeps_explicit_end(end_field):
    entry = {"date": "2026-11-14 00:00:00 +0900", end_field: "2026-11-14 07:00:00 +0900", "totalSleep": 7}
    row = hae._metric_rows("sleep_analysis", {}, entry, None)[0]
    assert row[4:8] == ("2026-11-13T15:00:00Z", "2026-11-13T22:00:00Z", 540, "2026-11-14")


def _dna(vault, tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("# synthetic\n# rsid\tchromosome\tposition\tgenotype\n"
                    "rs4244285\t10\t1\tAA\nrs4986893\t10\t2\tGG\nrs12248560\t10\t3\tCC\n")
    src, _ = sources.store_source(vault, path, "dtc")
    assert import_dtc(vault, src).status == "succeeded"
    return src


def test_summary_pgx_and_profile_follow_marker_visibility(vault, tmp_path):
    src = _dna(vault, tmp_path)
    ctx = FakeCtx({"privacy_level": "standard"})
    before = ops.genome_tool(ctx, {"op": "summary"})
    pgx_before = ops.genome_tool(ctx, {"op": "pgx", "drug": "clopidogrel"})
    assert any(d["name"] == "CYP2C19" for d in before["gene_drug_profile"]["derived"])
    assert any(e["gene"] == "CYP2C19" for e in before["gene_drug_profile"]["flagged"])
    assert any(e["status"] == "flag" and e["your_genotypes"].get("rs4244285") == "AA"
               for e in pgx_before["entries"])
    with db.write_txn(vault):
        vault.execute("INSERT INTO annotation_snapshot(source_id,ordinal,kind,marker,genes_json,category,record_json) "
                      "VALUES (?,1,'snp','rs4244285','[\"CYP2C19\"]','mental_health','{}')", (src.id,))
    summary = ops.genome_tool(ctx, {"op": "summary"})
    pgx = ops.genome_tool(ctx, {"op": "pgx", "drug": "clopidogrel"})
    hidden_entries = [e for e in knowledge.load_pack().entries if any(m["rsid"] == "rs4244285" for m in e["markers"])]
    profile = summary["gene_drug_profile"]
    assert all(d["name"] != "CYP2C19" for d in profile["derived"])
    assert all(e["gene"] != "CYP2C19" for e in profile["flagged"])
    assert summary["withheld"] == before["withheld"] + len(hidden_entries) + 1
    assert sum(profile["counts"].values()) == len(knowledge.load_pack().entries) - len(hidden_entries)
    hidden_pgx = [e for e in pgx_before["entries"] if any(m["rsid"] == "rs4244285" for m in e["markers"])]
    assert pgx["entries"] == [e for e in pgx_before["entries"] if e not in hidden_pgx]
    assert pgx["withheld"] == len(hidden_pgx)
    assert "rs4244285" not in json.dumps(pgx) and "rs4244285" not in json.dumps(profile)
    # Restoring visibility restores the exact prior profile and complete PGX output.
    with db.write_txn(vault):
        vault.execute("UPDATE annotation_snapshot SET category='general'")
    restored = ops.genome_tool(ctx, {"op": "summary"})
    assert restored["gene_drug_profile"] == before["gene_drug_profile"]
    assert restored["withheld"] == before["withheld"]
    assert ops.genome_tool(ctx, {"op": "pgx", "drug": "clopidogrel"}) == pgx_before


def _condition(rid="synthetic-condition", **extra):
    return {"resourceType": "Condition", "id": rid, "code": {"text": "Synthetic condition"}, **extra}


def test_onset_dropped_later_updates_status_and_keeps_dates(vault):
    original = _condition(onsetDateTime="2025-01", recordedDate="2025-02-01",
                          clinicalStatus={"coding": [{"code": "active"}]},
                          verificationStatus={"coding": [{"code": "provisional"}]})
    assert fhir.ingest(vault, original, None)["condition"] == 1
    stored = dict(vault.execute("SELECT * FROM condition").fetchone())
    incoming = _condition(recordedDate="2026-11-14", clinicalStatus={"coding": [{"code": "resolved"}]},
                          verificationStatus={"coding": [{"code": "confirmed"}]})
    result = fhir.ingest(vault, incoming, None)
    assert result["condition"] == 0 and result["condition_updated"] == 1
    rows = vault.execute("SELECT * FROM condition").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == stored["id"] and row["dedupe_key"] == stored["dedupe_key"]
    assert (row["onset"], row["recorded"], row["clinical_status"]) == ("2025-01", "2025-02-01", "resolved")
    raw = json.loads(row["raw_json"])
    assert raw["onsetDateTime"] == "2025-01" and raw["recordedDate"] == "2025-02-01"
    assert raw["_caduceus"]["date_precision"] == "month" and raw["_caduceus"]["recorded_precision"] == "day"
    assert raw["_caduceus"]["verification_status"] == "confirmed" and raw["_caduceus"]["current"] is False
    indexed = vault.execute("SELECT * FROM search_doc WHERE kind='condition'").fetchone()
    assert indexed["ts"] == "2025-01" and indexed["body"] == row["raw_json"]
    assert "[verification: confirmed]" in indexed["title"] and "[not current: resolved]" in indexed["title"]


def test_onset_dropped_updates_only_remaining_identity(vault):
    for rid in ("A", "B"):
        fhir.ingest(vault, _condition(rid, onsetDateTime="2025-01", recordedDate="2025-02-01"), None)
    fhir.ingest(vault, _condition("A", onsetDateTime="2025-01",
                verificationStatus={"coding": [{"code": "entered-in-error"}]}), None)
    result = fhir.ingest(vault, _condition("B", recordedDate="2026-11-14",
                         clinicalStatus={"coding": [{"code": "resolved"}]}), None)
    assert result["condition"] == 0 and result["condition_updated"] == 1
    rows = vault.execute("SELECT * FROM condition").fetchall()
    assert len(rows) == 1
    assert (rows[0]["onset"], rows[0]["recorded"], rows[0]["clinical_status"]) == ("2025-01", "2025-02-01", "resolved")
    assert json.loads(rows[0]["raw_json"])["_caduceus"]["resource_ids"] == ["B"]


def test_two_condition_identities_keep_their_onsets_and_retraction(vault):
    for rid in ("A", "B"):
        fhir.ingest(vault, _condition(rid, recordedDate="2025-02-01"), None)
    for rid, onset in (("A", "2025-01"), ("B", "2024-06")):
        fhir.ingest(vault, _condition(rid, onsetDateTime=onset, recordedDate="2025-02-01"), None)
    assert [r[0] for r in vault.execute("SELECT onset FROM condition ORDER BY onset")] == ["2024-06", "2025-01"]
    result = fhir.ingest(vault, _condition("A", onsetDateTime="2025-01",
                        verificationStatus={"coding": [{"code": "entered-in-error"}]}), None)
    assert result["condition_retracted"] == 1
    row = vault.execute("SELECT * FROM condition").fetchone()
    assert row["onset"] == "2024-06" and json.loads(row["raw_json"])["_caduceus"]["resource_ids"] == ["B"]


def _obs(rid="synthetic-observation", **extra):
    return {"resourceType": "Observation", "id": rid, "status": "final", "code": {"text": "Synthetic lab"},
            "effectiveDateTime": "2025-01-01", "valueQuantity": {"value": 5, "unit": "u"}, **extra}


def _bundle(*resources, **extra):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources], **extra}


def _file(vault, tmp_path, document, name):
    path = tmp_path / name
    path.write_text(json.dumps(document))
    src, _ = sources.store_source(vault, path, "fhir")
    return fhir.import_fhir(vault, src)


@pytest.mark.parametrize("status", ["corrected", "amended"])
def test_repeated_correction_keeps_row_and_superseded_map(vault, tmp_path, status):
    original = _obs()
    assert _file(vault, tmp_path, _bundle(original, _obs("synthetic-copy")), "original.json").counts["observation"] == 1
    old_key = vault.execute("SELECT dedupe_key FROM observation").fetchone()[0]
    correction = _obs(status=status, valueQuantity={"value": 6, "unit": "u"})
    first = _file(vault, tmp_path, _bundle(correction, id="first-correction"), "correction.json")
    assert first.counts["observation_superseded"] == 1
    stored = dict(vault.execute("SELECT * FROM observation").fetchone())
    superseded = json.loads(stored["raw_json"])["_caduceus"]["superseded"]
    assert superseded == {"synthetic-copy": old_key}
    again = _file(vault, tmp_path, _bundle(correction, id="second-correction"), "again.json")
    assert again.status == "succeeded" and again.counts["observation"] == 0
    assert again.counts.get("observation_superseded", 0) == 0
    rows = vault.execute("SELECT * FROM observation").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == stored["id"] and rows[0]["dedupe_key"] == stored["dedupe_key"]
    assert rows[0]["value_num"] == 6
    assert json.loads(rows[0]["raw_json"])["_caduceus"]["superseded"] == superseded
    assert vault.execute("SELECT body FROM search_doc WHERE kind='observation'").fetchone()[0] == rows[0]["raw_json"]


def test_reimport_after_upgrade_skips_retracted_records(vault, tmp_path, monkeypatch):
    condition = _condition(onsetDateTime="2025-01-01")
    observation = _obs()
    original = _bundle(condition, observation, id="one")
    withdrawn = _bundle({**condition, "verificationStatus": {"coding": [{"code": "entered-in-error"}]}},
                        {**observation, "status": "entered-in-error"}, id="two")
    with monkeypatch.context() as patch:
        patch.setattr(fhir, "PARSER_VERSION", "1")
        assert _file(vault, tmp_path, original, "one.json").status == "succeeded"
        assert _file(vault, tmp_path, withdrawn, "two.json").status == "succeeded"
    again = _file(vault, tmp_path, original, "one.json")
    assert again.status == "skipped"
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 0
