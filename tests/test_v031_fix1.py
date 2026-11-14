"""Round-one review regressions, using only generated synthetic records and calls."""
import json

import pytest

from conftest import FakeCtx
from caduceus import ops
from caduceus.core import db, search, sources
from caduceus.core.policy import Policy
from caduceus.genome.dtc import import_dtc
from caduceus.health import fhir
from caduceus.knowledge import pack as knowledge


def _dna(vault, tmp_path):
    path = tmp_path / "synthetic.txt"
    calls = [("rs429358", "19", "TT"), ("rs7412", "19", "CC"), ("rs4244285", "10", "AA"),
             ("rs4986893", "10", "GG"), ("rs12248560", "10", "CC"), ("rs1", "1", "AA")]
    lines = ["# synthetic data; reference human assembly build 37", "# rsid\tchromosome\tposition\tgenotype"]
    lines += [f"{marker}\t{chrom}\t{i + 1}\t{genotype}" for i, (marker, chrom, genotype) in enumerate(calls)]
    path.write_text("\n".join(lines) + "\n")
    src, _ = sources.store_source(vault, path, "dtc")
    assert import_dtc(vault, src).status == "succeeded"
    return src


def _row(vault, src, marker, gene, category):
    with db.write_txn(vault):
        vault.execute("INSERT INTO annotation_snapshot(source_id,ordinal,kind,marker,genes_json,category,record_json) "
                      "VALUES (?,1,'snp',?,?,?,'{}')", (src.id, marker, json.dumps([gene]), category))


@pytest.mark.parametrize("report", [False, True])
def test_pgx_hidden_gene_and_lookup(vault, tmp_path, report):
    src = _dna(vault, tmp_path)
    if report:
        _row(vault, src, "rs1", "CYP2C19", "general")
    ctx = FakeCtx({"hidden_categories": ["pgx"]})
    gene = ops.genome_tool(ctx, {"op": "gene", "gene": "CYP2C19"})
    assert gene == {"gene": "CYP2C19", "results": [], "withheld": 1,
                    "withheld_note": "hidden by your privacy settings"}
    lookup = ops.genome_tool(ctx, {"op": "lookup", "rsids": ["rs4244285", "rs1"]})
    assert lookup["withheld"] == 1
    assert [row["marker"] for row in lookup["results"]] == ["rs1"]
    assert lookup["results"][0]["genotype"] == "AA"


@pytest.mark.parametrize("settings", [{"privacy_level": "standard"}, {"hidden_categories": ["alzheimers"]}])
def test_apoe_gene_still_withheld_once(vault, tmp_path, settings):
    src = _dna(vault, tmp_path)
    _row(vault, src, "rs7412", "APOE", "alzheimers")
    result = ops.genome_tool(FakeCtx(settings), {"op": "gene", "gene": "APOE"})
    assert result == {"gene": "APOE", "results": [], "withheld": 1,
                      "withheld_note": "hidden by your privacy settings"}


def test_open_gene_and_derived_basis_unchanged(vault, tmp_path):
    src = _dna(vault, tmp_path)
    _row(vault, src, "rs4244285", "CYP2C19", "mental_health")
    ctx = FakeCtx({"privacy_level": "open"})
    result = ops.genome_tool(ctx, {"op": "gene", "gene": "CYP2C19"})
    basis = {"rs4244285": "AA", "rs4986893": "GG", "rs12248560": "CC"}
    assert result["derived"]["basis"] == basis
    assert result["derived"]["value"] == "*2/*2"
    assert list(result).index("derived") < list(result).index("results")
    assert [row["marker"] for row in result["results"]] == list(basis)
    assert {row["marker"]: row["genotype"] for row in result["results"]} == basis
    assert "withheld" not in result
    derived = ops.genome_tool(ctx, {"op": "derived"})
    assert next(row for row in derived["derived"] if row["name"] == "CYP2C19") == result["derived"]
    assert "withheld" not in derived


def test_sensitive_basis_withholds_derived_in_both_ops(vault, tmp_path):
    src = _dna(vault, tmp_path)
    ctx = FakeCtx({"privacy_level": "standard"})
    before = ops.genome_tool(ctx, {"op": "derived"})
    assert any(row["name"] == "CYP2C19" for row in before["derived"])
    _row(vault, src, "rs4244285", "CYP2C19", "mental_health")
    gene = ops.genome_tool(ctx, {"op": "gene", "gene": "CYP2C19"})
    assert "derived" not in gene and gene["withheld"] == 2
    assert [row["marker"] for row in gene["results"]] == ["rs4986893", "rs12248560"]
    assert "rs4244285" not in json.dumps(gene)
    derived = ops.genome_tool(ctx, {"op": "derived"})
    assert not any(row["name"] == "CYP2C19" for row in derived["derived"])
    assert derived["withheld"] == before["withheld"] + 1
    assert "rs4244285" not in json.dumps(derived)


def test_marker_requires_every_gene_category(vault, tmp_path, monkeypatch):
    _dna(vault, tmp_path)
    monkeypatch.setattr(ops, "_marker_genes", lambda: {"rs1": {"CYP2C19", "APOE"}})
    result = ops.genome_tool(FakeCtx({"privacy_level": "standard"}), {"op": "lookup", "rsids": ["rs1"]})
    assert result["results"] == [] and result["withheld"] == 1


def _condition(*, onset=None, rid="synthetic-condition", coded=False):
    concept = {"text": "Synthetic condition"}
    if coded:
        concept["coding"] = [{"system": "synthetic", "code": "C"}]
    resource = {"resourceType": "Condition", "id": rid, "code": concept, "recordedDate": "2026-01-05"}
    if onset is not None:
        resource["onsetDateTime"] = onset
    return resource


@pytest.mark.parametrize("known_first", [False, True])
@pytest.mark.parametrize("retraction_onset", [None, "2025-12"])
@pytest.mark.parametrize("coded", [False, True])
def test_condition_onset_transition_and_retraction(vault, known_first, retraction_onset, coded):
    unknown, known = _condition(coded=coded), _condition(onset="2025-12", coded=coded)
    first, second = (known, unknown) if known_first else (unknown, known)
    assert fhir.ingest(vault, first, None)["condition"] == 1
    counts = fhir.ingest(vault, second, None)
    assert counts["condition"] == (0 if known_first else 1)
    assert [tuple(row) for row in vault.execute("SELECT onset,recorded FROM condition")] == [("2025-12", "2026-01-05")]
    hits = search.search(vault, "Synthetic", policy=Policy())["results"]
    assert len(hits) == 1 and "onset unknown" not in hits[0]["title"]
    assert vault.execute("SELECT ts FROM search_doc WHERE kind='condition'").fetchone()[0] == "2025-12"
    retract = {**_condition(onset=retraction_onset, coded=coded),
               "verificationStatus": {"coding": [{"code": "entered-in-error"}]}}
    assert fhir.ingest(vault, retract, None)["condition_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert search.search(vault, "Synthetic", policy=Policy())["results"] == []


@pytest.mark.parametrize("retraction_onset", [None, "2025-12"])
def test_unknown_onset_retracted_with_either_date(vault, retraction_onset):
    fhir.ingest(vault, _condition(), None)
    result = fhir.ingest(vault, {**_condition(onset=retraction_onset),
        "verificationStatus": {"coding": [{"code": "entered-in-error"}]}}, None)
    assert result["condition_retracted"] == 1
    assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == 0
    assert search.search(vault, "Synthetic", policy=Policy())["results"] == []


def test_condition_onset_preserves_other_identities(vault):
    fhir.ingest(vault, _condition(rid="A"), None)
    fhir.ingest(vault, _condition(rid="B"), None)
    fhir.ingest(vault, _condition(rid="A", onset="2025-12"), None)
    rows = vault.execute("SELECT onset,raw_json FROM condition ORDER BY onset").fetchall()
    assert [(row["onset"], fhir._resource_ids(json.loads(row["raw_json"]))) for row in rows] == [
        (None, {"B"}), ("2025-12", {"A"})]
    hits = search.search(vault, "Synthetic", policy=Policy())["results"]
    assert len(hits) == 2
    assert sum("onset unknown" in hit["title"] for hit in hits) == 1


def _obs(rid="A", concept=None, **value):
    return {"resourceType": "Observation", "id": rid, "status": "final",
            "code": {"text": "Synthetic Alpha"} if concept is None else concept,
            "effectiveDateTime": "2026-11-01", **value}


def _old_keys(patch):
    original = fhir._key
    patch.setattr(fhir, "_key", lambda parts: original(
        parts[:-1] if parts[0] == "Observation" and len(parts) == 8 else parts))


def _migrate(vault):
    with db.write_txn(vault):
        vault.execute("DELETE FROM settings WHERE key='migration:fhir_uncoded_names_v1'")
    db._migrate_vault(vault)


def test_superseded_reference_survives_migration(vault, monkeypatch):
    a = _obs(valueQuantity={"value": 5, "unit": "u"})
    b = {**a, "id": "B"}
    with monkeypatch.context() as patch:
        _old_keys(patch)
        fhir.ingest(vault, {"resourceType": "Bundle", "entry": [{"resource": a}, {"resource": b}]}, None)
        fhir.ingest(vault, {**a, "status": "corrected", "valueQuantity": {"value": 6, "unit": "u"}}, None)
        assert fhir.ingest(vault, b, None)["observation_ignored_after_correction"] == 1
    _migrate(vault)
    assert fhir.ingest(vault, b, None)["observation_ignored_after_correction"] == 1
    assert [tuple(row) for row in vault.execute("SELECT name,value_num FROM observation")] == [("Synthetic Alpha", 6.0)]
    assert fhir.ingest(vault, {**b, "valueQuantity": {"value": 7, "unit": "u"}}, None)["observation"] == 1


@pytest.mark.parametrize("concept", [{"text": 123}, {"coding": [{"display": 123}]}], ids=["text", "display"])
@pytest.mark.parametrize("component", [False, True])
def test_numeric_names_import_whole_file(vault, tmp_path, concept, component):
    resource = _obs(concept=concept, valueQuantity={"value": 3})
    if component:
        resource = _obs(component=[{"code": concept, "valueQuantity": {"value": 3}}])
    path = tmp_path / "synthetic-records.json"
    path.write_text(json.dumps(resource))
    src, _ = sources.store_source(vault, path, "fhir")
    result = fhir.import_fhir(vault, src)
    assert result.status == "succeeded" and result.counts["observation"] == 1
    assert vault.execute("SELECT name FROM observation").fetchone()[0] == "123"
    assert fhir.ingest(vault, resource, src.id)["observation"] == 0
    corrected = {**resource, "status": "corrected"}
    assert fhir.ingest(vault, corrected, src.id)["observation_superseded"] == 1


@pytest.mark.parametrize("value", [{"valueQuantity": {"value": -0.0, "unit": "u"}}, {"valueString": 7},
                                  {"valueQuantity": {"value": 3, "unit": 5}}],
                         ids=["neg_zero", "numeric_valueString", "numeric_unit"])
@pytest.mark.parametrize("component", [False, True])
def test_migration_key_parity_for_raw_shapes(vault, monkeypatch, value, component):
    resource = _obs(**value)
    if component:
        resource = _obs(component=[{"code": resource["code"], **value}])
    with monkeypatch.context() as patch:
        _old_keys(patch)
        assert fhir.ingest(vault, resource, None)["observation"] == 1
    _migrate(vault)
    migrated = dict(vault.execute("SELECT * FROM observation").fetchone())
    assert fhir.ingest(vault, json.loads(json.dumps(resource)), None)["observation"] == 0
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    assert dict(vault.execute("SELECT * FROM observation").fetchone()) == migrated


@pytest.mark.parametrize("days,expected", [("abc", 90), (True, 90), (0, 90), (3651, 90), (7.0, 90),
                                          (None, 90), (1, 1), (3650, 3650)])
def test_erase_backfill_matches_sync_validation(data_dir, days, expected):
    (data_dir / "vault.db").write_bytes(b"synthetic damaged vault")
    ctx = FakeCtx({"hae_backfill_days": days})
    assert ops._hae_backfill_days(ctx) == expected
    preview = ops.command(ctx, "erase")
    confirmation = ops.command(ctx, "erase confirm")
    for text in (preview, confirmation):
        assert f"next Health Auto Export sync imports the last {expected} days again" in text
        assert "last abc days" not in text
