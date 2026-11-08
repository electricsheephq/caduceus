"""Synthetic calls only; recorded public reference metadata verify the curated panel."""
import json
from pathlib import Path

import pytest

from caduceus.core.contracts import Call, CallState
from caduceus.knowledge.pack import PACK_VERSION, derive, load_pack, normalize_drug, pgx_check

FIXTURES = Path(__file__).parent / "fixtures" / "research"


def calls(**values):
    return {rsid: Call(rsid, CallState.CALLED, value) for rsid, value in values.items()}


def test_pack_loads_validates_sources_and_plus_strand():
    pack = load_pack()
    assert pack.version == PACK_VERSION == "2026.11.0"
    assert len(pack.glp1) == 3
    assert {e["gene"] for e in pack.cpic} >= {
        "CYP2C19", "CYP2C9", "VKORC1", "SLCO1B1", "TPMT", "NUDT15", "DPYD", "CYP3A5", "IFNL3",
        "UGT1A1", "MT-RNR1", "CYP2D6", "RYR1", "HLA-B"}
    dbsnp = {r["rsid"]: r for r in json.loads((FIXTURES / "dbsnp_markers.json").read_text())}
    cpic_locations = json.loads((FIXTURES / "cpic_locations.json").read_text())
    locations = {r["dbsnpid"]: r for r in cpic_locations["locations"]}
    for entry in pack.entries:
        assert entry["limits"] and entry["sources"] and entry["phenotype_rules"]
        assert all(s["title"] and s["url"] and s["pmid"] and s["year"] for s in entry["sources"])
        for marker in entry["markers"]:
            alleles = {p["inserted_sequence"] for p in dbsnp[marker["rsid"]]["plus_placements"]}
            assert marker["effect"] in alleles and marker["other"] in alleles
            if marker["rsid"] in locations:
                chrom = locations[marker["rsid"]]["chromosomelocation"]
                assert marker["other"] + ">" + marker["effect"] in chrom
    historical = next(e for e in pack.entries if e["gene"] == "IFNL3")
    assert historical["guideline"]["status"] == "retired"
    assert "retired" in historical["evidence"]


@pytest.mark.parametrize("name,generic", [
    ("  OzEmPiC  ", "semaglutide"), ("Wegovy", "semaglutide"), ("Rybelsus", "semaglutide"),
    ("Mounjaro", "tirzepatide"), ("Zepbound", "tirzepatide"), ("Victoza", "liraglutide"),
    ("Saxenda", "liraglutide"), ("Trulicity", "dulaglutide"), ("Plavix", "clopidogrel"),
    ("Zocor", "simvastatin"), ("Lipitor", "atorvastatin"), ("Crestor", "rosuvastatin"),
    ("Jantoven", "warfarin"), ("Celexa", "citalopram"), ("Lexapro", "escitalopram"),
    ("Zoloft", "sertraline"), ("Prilosec", "omeprazole"), (None, None), ("unknown", None),
])
def test_normalize_drug(name, generic):
    assert normalize_drug(name) == generic


def test_persona_statuses_and_research_limits():
    pack = load_pack()
    persona = calls(rs6923761="AA", rs10305420="CC", rs1800437="GG")
    semaglutide_rows = pgx_check(pack, "Ozempic", persona)["entries"]
    tirzepatide_rows = pgx_check(pack, "Mounjaro", persona)["entries"]
    rows = semaglutide_rows + tirzepatide_rows
    assert {r["id"]: r["status"] for r in rows} == {
        "glp1-rs6923761": "flag", "glp1-rs10305420": "no_flag", "glp1-rs1800437": "no_flag"}
    assert "glp1-rs6923761" not in {r["id"] for r in tirzepatide_rows}
    assert all(r["evidence"] == "research" and r["sources"] and r["limits"] and r["disclaimer"] for r in rows)
    hba1c = next(r for r in rows if r["id"] == "glp1-rs6923761")
    assert "not a weight effect" in hba1c["finding"] and "210" in hba1c["finding"]
    assert "semaglutide-specific" in hba1c["finding"]
    semaglutide = pgx_check(pack, "Ozempic", persona)
    assert {r["gene"] for r in semaglutide["entries"]} == {"GLP1R"}
    assert "no evidence" in pgx_check(pack, "invented drug", persona)["note"].lower()
    glp = json.loads((Path(__file__).parents[1] / "caduceus/knowledge/data/glp1.json").read_text())
    assert "no CPIC guideline" in glp["note"]
    assert {s["pmid"] for e in pack.glp1 for s in e["sources"]} == {"41951734", "36528349", "41307691"}
    assert all(s["doi"] for e in pack.glp1 for s in e["sources"])


@pytest.mark.parametrize("state", [CallState.NOT_ON_CHIP, CallState.NO_CALL, CallState.UNRESOLVED])
def test_not_assessed_never_no_flag(state):
    value = {"rs6923761": Call("rs6923761", state, "--")}
    row = next(r for r in pgx_check(load_pack(), "Ozempic", value)["entries"] if r["id"] == "glp1-rs6923761")
    assert row["status"] == "not_assessed" and str(state) in row["reason"]


@pytest.mark.parametrize("genotype", ["AC", "--", "A", "AAA", "DI"])
def test_outside_marker_alleles_not_assessed(genotype):
    row = next(r for r in pgx_check(load_pack(), "Ozempic", calls(rs6923761=genotype))["entries"]
               if r["id"] == "glp1-rs6923761")
    assert row["status"] == "not_assessed"


def test_missing_one_required_marker_and_unreliable_panels():
    pack = load_pack()
    assert pgx_check(pack, "Plavix", calls(rs4244285="GG", rs12248560="CC"))["entries"][0]["status"] == "not_assessed"
    for drug in ("codeine", "allopurinol", "sevoflurane"):
        assert all(e["status"] == "not_assessed" and e["chip_reliability"] == "unreliable"
                   for e in pgx_check(pack, drug, {})["entries"])
    hla = pgx_check(pack, "abacavir", calls(rs2395029="TG"))["entries"][0]
    assert hla["status"] == "flag" and hla["chip_reliability"] == "limited"
    mt = pgx_check(pack, "gentamicin", calls(rs267606617="G"))["entries"][0]
    assert mt["status"] == "flag"


@pytest.mark.parametrize("first,second,value,confidence", [
    ("TT", "CC", "ε3/ε3", "called"), ("CT", "CC", "ε3/ε4", "called"),
    ("CC", "CC", "ε4/ε4", "called"), ("TT", "TT", "ε2/ε2", "called"),
    ("TT", "CT", "ε2/ε3", "called"), ("CC", "CT", "ε1/ε4", "called"),
    ("TC", "CT", "ε1/ε3 or ε2/ε4", "ambiguous"),
])
def test_apoe_including_unphased_ambiguity(first, second, value, confidence):
    row = derive(load_pack(), calls(rs429358=first, rs7412=second))[0]
    assert (row["value"], row["confidence"], row["category"]) == (value, confidence, "alzheimers")


def test_derived_cyp2c19_and_cyp2c9_vkorc1_slco1b1():
    result = {r["name"]: r for r in derive(load_pack(), calls(
        rs4244285="GA", rs4986893="GG", rs12248560="CC", rs1799853="CC", rs1057910="AA",
        rs9923231="CT", rs4149056="TC"))}
    assert result["CYP2C19"]["value"] == "*1/*2" and result["CYP2C19"]["phenotype"] == "IM"
    assert result["CYP2C9"]["activity_score"] == 2
    assert result["CYP2C9"]["value"] == "*1/*1"
    assert result["SLCO1B1"]["value"] == "decreased function marker pattern"
    assert result["VKORC1"]["value"] == "1 copies of −1639A (plus-strand T)"
    assert all(r["sources"] and r["limits"] for r in result.values())
    ambiguous = {r["name"]: r for r in derive(load_pack(), calls(rs4244285="GA", rs4986893="GG", rs12248560="CT"))}
    assert ambiguous["CYP2C19"]["confidence"] == "ambiguous"
    assert all(r["confidence"] == "not_assessed" for r in derive(load_pack(), {}))
