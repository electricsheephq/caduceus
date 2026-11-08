"""The demo persona imports cleanly through the real importers and tells its story."""
import pytest

from caduceus.core import sources
from caduceus.core.contracts import SourceKind
from caduceus.core.policy import Policy
from caduceus.genome.calls import get_calls
from caduceus.genome.dtc import import_dtc
from caduceus.genome.promethease import import_promethease
from caduceus.genome.report import build_report
from caduceus.health.apple import import_apple_health
from caduceus.health.fhir import observation_series
from caduceus.health.trends import compare_windows, trend
from synth import persona


@pytest.fixture
def files(tmp_path):
    return persona.write_persona(tmp_path / "persona", markers=3000)


def _import(fn, vault, path, kind, *args):
    src = sources.store_source(vault, path, kind)[0]
    result = fn(*args, src) if args else fn(vault, src)
    assert result.status == "succeeded" and not result.warnings, result
    return result


def test_dna_has_every_demo_genotype_at_its_real_position(vault, files):
    result = _import(import_dtc, vault, files["dna"], SourceKind.DTC)
    assert result.counts["markers"] >= 3000 + len(persona.GENOTYPES)
    calls = get_calls(vault, list(persona.GENOTYPES))
    for rsid, (chrom, pos, genotype) in persona.GENOTYPES.items():
        call = calls[rsid]
        assert (call.state, call.genotype, call.chrom, call.pos) == ("called", genotype, chrom, pos), rsid


def test_filler_never_lands_on_a_pack_marker():
    skip = persona._skip_markers()
    filler = {rsid for _, _, rsid, _ in persona._filler(3000, 7)}
    assert not filler & skip


def test_variant_report_imports_and_standard_hides_apoe(vault, files, tmp_path):
    _import(import_promethease, vault, files["report"], SourceKind.PROMETHEASE)
    stored = dict(vault.execute("SELECT coalesce(marker, title), category FROM annotation_snapshot WHERE kind != 'genotype_info'"))
    assert len(stored) == len(persona.REPORT) + len(persona.GENOSETS)
    assert stored["rs429358"] == stored["APOE e3/e3"] == "alzheimers"
    assert stored["rs4244285"] == "pgx"
    page = build_report(vault, tmp_path / "standard", policy=Policy(level="standard")).read_text(encoding="utf-8")
    assert "Two Ser168 copies" in page and "APOE e3/e3" not in page
    page = build_report(vault, tmp_path / "open", policy=Policy()).read_text(encoding="utf-8")
    assert "APOE e3/e3" in page


def test_apple_export_shows_the_plateau_and_the_records(vault, samples, files):
    result = _import(import_apple_health, vault, files["apple"], SourceKind.APPLE_HEALTH, samples, vault)
    assert result.counts
    early = compare_windows(samples, "weight", pivot=persona.START.isoformat(), before_days=28, after_days=84)
    assert early["after"]["mean"] < early["before"]["mean"] - 1.5          # it worked at first
    late = compare_windows(samples, "weight", pivot="2026-08-10", before_days=28, after_days=84)
    assert abs(late["after"]["mean"] - late["before"]["mean"]) < 0.8      # then it levelled off
    weekly = trend(samples, "weight", start="2026-02-02", end="2026-11-07")
    assert weekly["unit"] == "kg" and len(weekly["points"]) >= 35
    a1c = observation_series(vault, "4548-4")
    assert [p["value"] for p in a1c["points"]] == [8.1, 8.4, 7.9, 7.8] and a1c["unit"] == "%"
    meds = {row[0]: row[1] for row in vault.execute(
        "SELECT lower(drug || ' ' || coalesce(brand, '')), min(occurred_on) FROM medication_event "
        "WHERE event='start' GROUP BY 1")}
    assert any("semaglutide" in name and day == persona.START.isoformat() for name, day in meds.items()), meds
