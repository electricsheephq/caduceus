"""0.3.1 regressions: generated DNA, FHIR and HAE data only; no network."""
import json
import zipfile

import pytest

from conftest import FakeCtx
from caduceus import ops
from caduceus.core import db, search, sources
from caduceus.core.policy import Policy
from caduceus.genome.dtc import import_dtc
from caduceus.health import fhir, hae
from caduceus.knowledge import pack as knowledge


def _bundle(*resources):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def _fhir_file(vault, tmp_path, document, name="records.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document))
    src, _ = sources.store_source(vault, path, "fhir")
    result = fhir.import_fhir(vault, src)
    assert result.status == "succeeded", result
    return result


def _dna(vault, tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("# synthetic data; reference human assembly build 37\n"
                    "# rsid\tchromosome\tposition\tgenotype\n"
                    "rs429358\t19\t1\tTT\nrs7412\t19\t2\tCC\nrs1\t1\t3\tAA\n")
    src, _ = sources.store_source(vault, path, "dtc")
    assert import_dtc(vault, src).status == "succeeded"
    return src


def test_gene_includes_unannotated_derived_markers_first(vault, tmp_path):
    src = _dna(vault, tmp_path)
    with db.write_txn(vault):
        vault.execute("INSERT INTO annotation_snapshot(source_id,ordinal,kind,marker,genes_json,category,record_json) "
                      "VALUES (?,1,'snp','rs7412','[\"APOE\"]','alzheimers','{}')", (src.id,))
    result = ops.genome_tool(FakeCtx({"privacy_level": "open"}), {"op": "gene", "gene": "APOE"})
    assert list(result).index("derived") < list(result).index("results")
    assert result["derived"]["value"] == "ε3/ε3"
    assert [r["marker"] for r in result["results"]] == ["rs429358", "rs7412"]
    assert result["results"][0]["annotations"] == []
    assert result["results"][1]["annotations"][0]["marker"] == "rs7412"
    assert "derived-call definitions" in result["note"]
    hidden = ops.genome_tool(FakeCtx({"privacy_level": "standard"}), {"op": "gene", "gene": "APOE"})
    assert hidden == {"gene": "APOE", "results": [], "withheld": 1,
                      "withheld_note": "hidden by your privacy settings"}


def test_gene_union_order_and_derived_policy(vault, tmp_path, monkeypatch):
    src = _dna(vault, tmp_path)
    with db.write_txn(vault):
        vault.execute("INSERT INTO annotation_snapshot(source_id,ordinal,kind,marker,genes_json,record_json) "
                      "VALUES (?,1,'snp','rs1','[\"CYP2C19\"]','{}')", (src.id,))
    result = ops.genome_tool(FakeCtx(), {"op": "gene", "gene": "CYP2C19"})
    definitions = [m[0] for gene, ms in knowledge.DERIVED_MARKERS if gene == "CYP2C19" for m in ms]
    pack_markers = [m["rsid"] for e in knowledge.load_pack().entries if e["gene"] == "CYP2C19" for m in e["markers"]]
    assert [r["marker"] for r in result["results"]] == list(dict.fromkeys(definitions + pack_markers + ["rs1"]))
    monkeypatch.setattr(knowledge, "derive", lambda *a: [{"name": "CYP2C19", "category": "alzheimers"}])
    hidden = ops.genome_tool(FakeCtx({"privacy_level": "standard"}), {"op": "gene", "gene": "CYP2C19"})
    assert "derived" not in hidden and hidden["withheld"] == 1
    assert hidden["results"] == result["results"]


def test_lookup_always_has_annotations_and_keeps_cap(vault, tmp_path):
    _dna(vault, tmp_path)
    result = ops.genome_tool(FakeCtx(), {"op": "lookup", "rsids": [f"rs{i}" for i in range(1, 61)]})
    assert len(result["results"]) == 50
    assert all(r["annotations"] == [] for r in result["results"])


def _observation(name="Synthetic Alpha", rid="alpha", *, text=False):
    value = {"valueString": "Synthetic finding"} if text else {"valueQuantity": {"value": 3, "unit": "synthetic"}}
    return {"resourceType": "Observation", "id": rid, "status": "final", "code": {"text": name},
            "effectiveDateTime": "2026-11-01", **value}


@pytest.mark.parametrize("text", [False, True])
def test_uncoded_observations_keep_names_and_reimport(vault, tmp_path, text):
    doc = _bundle(_observation(text=text), _observation("Synthetic Beta", "beta", text=text))
    first = _fhir_file(vault, tmp_path, doc)
    assert first.counts["observation"] == 2
    assert [r[0] for r in vault.execute("SELECT name FROM observation ORDER BY name")] == ["Synthetic Alpha", "Synthetic Beta"]
    doc["id"] = "synthetic-second-export"
    assert _fhir_file(vault, tmp_path, doc, "again.json").counts["observation"] == 0
    normalized = _observation("  SYNTHETIC   Alpha  ", "another-id", text=text)
    assert fhir.ingest(vault, normalized, first.source_id)["observation"] == 0


@pytest.mark.parametrize("components", [False, True])
def test_uncoded_correction_matches_own_name_and_retraction(vault, tmp_path, components):
    alpha, beta = _observation(), _observation("Synthetic Beta", "alpha")
    if components:
        original = {"resourceType": "Observation", "id": "alpha", "effectiveDateTime": "2026-11-01",
                    "code": {"text": "Synthetic panel"}, "component": [
                        {k: v for k, v in r.items() if k in ("code", "valueQuantity")} for r in (alpha, beta)]}
        document = _bundle(original)
    else:
        document = _bundle(alpha, beta)
    first = _fhir_file(vault, tmp_path, document)
    correction = {**alpha, "status": "corrected", "valueQuantity": {"value": 4, "unit": "synthetic"}}
    result = fhir.ingest(vault, correction, first.source_id)
    assert result["observation_superseded"] == 1
    assert [tuple(r) for r in vault.execute("SELECT name,value_num FROM observation ORDER BY name")] == [
        ("Synthetic Alpha", 4), ("Synthetic Beta", 3)]
    assert fhir.ingest(vault, alpha, first.source_id)["observation_ignored_after_correction"] == 1
    retracted = fhir.ingest(vault, {**alpha, "status": "entered-in-error"}, first.source_id)
    assert retracted["observation_retracted"] == 2
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 0


def test_coded_observation_key_is_unchanged(vault, tmp_path):
    resource = _observation()
    resource["code"]["coding"] = [{"system": "synthetic", "code": "A"}]
    _fhir_file(vault, tmp_path, resource)
    expected = fhir._key(["Observation", "synthetic", "A", "2026-11-01", [3.0, None], "synthetic", None])
    assert vault.execute("SELECT dedupe_key FROM observation").fetchone()[0] == expected


@pytest.mark.parametrize("text,components,merged", [(False, False, False), (True, False, False),
                                                   (False, True, False), (False, False, True)])
def test_uncoded_migration_reimport_and_idempotence(vault, tmp_path, monkeypatch, text, components, merged):
    alpha, beta = _observation(text=text), _observation("Synthetic Beta", "beta", text=text)
    resources = [alpha, beta] if merged else [alpha]
    if components:
        resources = [{"resourceType": "Observation", "id": "panel", "effectiveDateTime": "2026-11-01",
                      "code": {"text": "Synthetic panel"}, "component": [{"code": alpha["code"],
                          "valueQuantity": alpha["valueQuantity"]}]}]
    document = _bundle(*resources)
    real_key = fhir._key
    with monkeypatch.context() as patch:
        patch.setattr(fhir, "_key", lambda parts: real_key(parts[:-1] if parts[0] == "Observation" and len(parts) == 8 else parts))
        first = _fhir_file(vault, tmp_path, document)
    assert first.counts["observation"] == 1
    old = dict(vault.execute("SELECT * FROM observation").fetchone())
    with db.write_txn(vault):
        vault.execute("DELETE FROM settings WHERE key='migration:fhir_uncoded_names_v1'")
    db._migrate_vault(vault)
    row = dict(vault.execute("SELECT * FROM observation").fetchone())
    assert row["dedupe_key"] != old["dedupe_key"]
    assert {k: v for k, v in row.items() if k != "dedupe_key"} == {k: v for k, v in old.items() if k != "dedupe_key"}
    db._migrate_vault(vault)
    assert dict(vault.execute("SELECT * FROM observation").fetchone()) == row
    document["id"] = "synthetic-new-export"
    again = _fhir_file(vault, tmp_path, document, "again.json")
    assert again.counts["observation"] == (1 if merged else 0)
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == (2 if merged else 1)
    assert fhir.ingest(vault, document, first.source_id)["observation"] == 0


def test_uncoded_migration_skips_collisions_and_runs_once(vault, tmp_path):
    _fhir_file(vault, tmp_path, _observation())
    with db.write_txn(vault):
        vault.execute("INSERT INTO observation(name,value_num,unit,effective_at,source,dedupe_key,raw_json) "
                      "SELECT name,value_num,unit,effective_at,source,'synthetic-old-collision',raw_json FROM observation")
        vault.execute("DELETE FROM settings WHERE key='migration:fhir_uncoded_names_v1'")
    db._migrate_vault(vault)
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 2
    assert vault.execute("SELECT count(*) FROM observation WHERE dedupe_key='synthetic-old-collision'").fetchone()[0] == 1
    with db.write_txn(vault):
        vault.execute("UPDATE observation SET dedupe_key='synthetic-after-migration' WHERE id=1")
    db._migrate_vault(vault)
    assert vault.execute("SELECT dedupe_key FROM observation WHERE id=1").fetchone()[0] == "synthetic-after-migration"


def _condition(recorded="2026-11-01", onset=None):
    resource = {"resourceType": "Condition", "id": "synthetic-condition", "code": {"text": "Synthetic condition"}}
    if recorded is not None:
        resource["recordedDate"] = recorded
    if onset is not None:
        resource["onsetDateTime"] = onset
    return resource


@pytest.mark.parametrize("recorded,precision", [("2026-11-01", "day"), ("2026", "year"), ("2026-11", "month")])
def test_condition_recorded_only_search_reimport_and_retraction(vault, tmp_path, recorded, precision):
    resource = _condition(recorded)
    first = _fhir_file(vault, tmp_path, resource)
    assert first.counts["condition"] == 1
    row = vault.execute("SELECT * FROM condition").fetchone()
    assert row["onset"] is None and row["recorded"] == recorded
    meta = json.loads(row["raw_json"])["_caduceus"]
    assert meta["date_precision"] is None and meta["recorded_precision"] == precision
    assert row["dedupe_key"] == fhir._key([None, None, None, "Synthetic condition"])
    hit = search.search(vault, "Synthetic", policy=Policy())["results"][0]
    assert hit["title"] == f"Synthetic condition [onset unknown; recorded {recorded}]"
    assert vault.execute("SELECT ts FROM search_doc WHERE kind='condition'").fetchone()[0] == recorded
    again = {**resource, "meta": {"versionId": "synthetic-reexport"}}
    assert _fhir_file(vault, tmp_path, again, "again.json").counts["condition"] == 0
    withdrawn = {**resource, "verificationStatus": {"coding": [{"code": "entered-in-error"}]}}
    assert fhir.ingest(vault, withdrawn, first.source_id)["condition_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert search.search(vault, "Synthetic", policy=Policy())["results"] == []


def test_condition_both_dates_and_neither_date(vault, tmp_path):
    first = _fhir_file(vault, tmp_path, _condition(onset="2025-06"))
    row = vault.execute("SELECT * FROM condition").fetchone()
    assert (row["onset"], row["recorded"]) == ("2025-06", "2026-11-01")
    assert row["dedupe_key"] == fhir._key([None, None, "2025-06", "Synthetic condition"])
    assert json.loads(row["raw_json"])["_caduceus"]["recorded_precision"] == "day"
    assert vault.execute("SELECT ts FROM search_doc WHERE kind='condition'").fetchone()[0] == "2025-06"
    assert "onset unknown" not in vault.execute("SELECT title FROM search_doc WHERE kind='condition'").fetchone()[0]
    assert fhir.ingest(vault, _condition(None), first.source_id)["skipped:Condition:date"] == 1
    assert fhir.ingest(vault, _condition(onset="2025-06"), first.source_id)["condition"] == 0


def test_old_vault_adds_recorded_column_and_preserves_condition(vault, tmp_path, data_dir):
    first = _fhir_file(vault, tmp_path, _condition(None, onset="2025-06"))
    old = dict(vault.execute("SELECT * FROM condition").fetchone())
    with db.write_txn(vault):
        vault.execute("ALTER TABLE condition DROP COLUMN recorded")
    reopened = db.open_vault(data_dir / "vault.db")
    try:
        assert dict(reopened.execute("SELECT * FROM condition").fetchone()) == old
        result = fhir.ingest(reopened, _condition(onset="2025-06"), first.source_id)
        assert result["condition"] == 0 and result["condition_updated"] == 1
        assert reopened.execute("SELECT recorded FROM condition").fetchone()[0] == "2026-11-01"
    finally:
        reopened.close()


@pytest.mark.parametrize("days", [None, 14])
def test_erase_preview_and_confirmation_name_all_schedules(data_dir, days):
    (data_dir / "vault.db").write_bytes(b"synthetic damaged vault")
    ctx = FakeCtx({} if days is None else {"hae_backfill_days": days})
    preview = ops.command(ctx, "erase")
    confirm = ops.command(ctx, "erase confirm")
    for text in (preview, confirm):
        assert "Schedules armed with /blueprint keep running" in text
        assert "next research watch starts a new vault" in text
        assert f"next Health Auto Export sync imports the last {days or 90} days again" in text
        assert "daily check finds nothing until data returns" in text
    assert "Your Caduceus settings stay" in preview
    assert "Your settings are unchanged" in confirm


NIGHT = "2026-11-13"


def _segment(start=0, end=1, source="Synthetic Watch"):
    return {"date": f"{NIGHT} 00:00:00 +0900", "start": f"{NIGHT} {start:02d}:00:00 +0900",
            "end": f"{NIGHT} {end:02d}:00:00 +0900", "value": "Core", "source": source}


def _summary(source="Synthetic Watch"):
    return {"date": f"{NIGHT} 00:00:00 +0900", "sleepEnd": f"{NIGHT} 07:00:00 +0900",
            "totalSleep": 7, "source": source}


def _hae_file(vault, samples, tmp_path, entries, name="sleep.json"):
    path = tmp_path / name
    path.write_text(json.dumps(_sleep_payload(entries)))
    src, _ = sources.store_source(vault, path, "hae")
    result = hae.import_hae(samples, vault, src)
    assert result.status == "succeeded", result
    return result


def _sleep_payload(entries):
    return {"data": {"metrics": [{"name": "sleep_analysis", "data": entries}]}}


def _hours(samples):
    return samples.execute("SELECT value FROM daily_rollup WHERE metric='sleep_hours' AND day=?", (NIGHT,)).fetchone()[0]


def test_other_source_summary_preserves_partial_edge_six_hours(vault, samples, tmp_path):
    _hae_file(vault, samples, tmp_path, [_segment(0, 4), _segment(4, 6)])
    assert _hours(samples) == 6
    summary = {**_summary("Synthetic Other"), "totalSleep": 2}
    result = _hae_file(vault, samples, tmp_path, [_segment(4, 6), summary], "partial.json")
    assert result.counts["samples_replaced"] == 0
    assert _hours(samples) == 6
    assert result.counts.get("sleep_segments_omitted", 0) == 0


@pytest.mark.parametrize("layout", ["json", "zip-summary-first", "zip-segments-first"])
def test_same_source_summary_omits_segments_seven_hours(vault, samples, tmp_path, layout):
    segment, summary = _segment(7, 8), _summary()
    if layout == "json":
        result = _hae_file(vault, samples, tmp_path, [segment, summary])
    else:
        entries = [[summary], [segment]] if layout == "zip-summary-first" else [[segment], [summary]]
        path = tmp_path / "sleep.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for i, member in enumerate(entries):
                archive.writestr(f"{i}.json", json.dumps(_sleep_payload(member)))
        src, _ = sources.store_source(vault, path, "hae")
        result = hae.import_hae(samples, vault, src)
        assert result.status == "succeeded"
    assert _hours(samples) == 7
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 1
    assert result.counts["sleep_segments_omitted"] == 1
    assert result.warnings == [f"Sleep summaries used instead of same-source segments for nights: {NIGHT}"]
    assert "Synthetic Watch" not in json.dumps(result.warnings)


def test_edge_replacement_requires_summary_for_every_segment_source(vault, samples, tmp_path):
    segments = [_segment(0, 4), _segment(4, 6), _segment(0, 4, "Synthetic Other"),
                _segment(4, 6, "Synthetic Other")]
    _hae_file(vault, samples, tmp_path, segments)
    result = _hae_file(vault, samples, tmp_path, [_segment(4, 6), _segment(4, 6, "Synthetic Other"),
                                                _summary()], "mixed.json")
    assert result.counts["samples_replaced"] == 2  # only the summary source's old segments are replaced
    assert result.counts["sleep_segments_omitted"] == 1
    assert samples.execute("SELECT count(*) FROM sample WHERE source_name='Synthetic Other'").fetchone()[0] == 2
