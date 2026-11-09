"""Lane B regressions: generated DNA only, no network or external profile access."""
import copy
import hashlib
import json
import random

import pytest

from caduceus import ops
from caduceus.core import sources
from caduceus.core.contracts import Call, CallState, SourceKind
from caduceus.core.db import open_vault, write_txn
from caduceus.genome.dtc import import_dtc
from caduceus.knowledge import pack as knowledge


OPS = ({"op": "lookup", "rsids": ["rs1"]}, {"op": "gene", "gene": "GLP1R"},
       {"op": "pgx", "drug": "Ozempic"}, {"op": "derived"}, {"op": "summary"})
VALUES = {"rs6923761": "AA", "rs10305420": "CC", "rs4244285": "GA", "rs4986893": "GG",
          "rs12248560": "CC", "rs2395029": "TG", "rs267606617": "G"}


def dna(vault, tmp_path, name, *, n=1100, seed=1, ancestry=False, reverse=False, inject=None):
    """The layouts share chromosomes and positions; all values come from a seeded generator."""
    rng = random.Random(seed)
    values = {f"rs{i}": rng.choice("ACGT") + rng.choice("ACGT") for i in range(1, n + 1)}
    values.update(inject or {})
    path = tmp_path / name
    with path.open("w") as out:
        out.write("# synthetic data; reference human assembly build 37\n")
        out.write("rsid\tchromosome\tposition\tallele1\tallele2\n" if ancestry else
                  "# rsid\tchromosome\tposition\tgenotype\n")
        for i, (marker, value) in enumerate(values.items(), 1):
            value = value[::-1] if reverse else value
            if ancestry:
                first, second = ("0", "0") if value == "--" else (value[0], value[-1])
                out.write(f"{marker}\t{1 + i % 22}\t{i}\t{first}\t{second}\n")
            else:
                out.write(f"{marker}\t{1 + i % 22}\t{i}\t{value}\n")
    src, _ = sources.store_source(vault, path, SourceKind.DTC)
    result = import_dtc(vault, src)
    assert result.status == "succeeded", result
    cid = vault.execute("SELECT id FROM callset WHERE source_id=?", (src.id,)).fetchone()[0]
    return cid, result, src


def comparisons(vault, cid):
    return json.loads(vault.execute("SELECT comparison_json FROM callset WHERE id=?", (cid,)).fetchone()[0])


def test_same_person_across_layouts_and_skipped_reimport(vault, tmp_path, fake_ctx):
    first, _, _ = dna(vault, tmp_path, "first.txt", inject={"rs1": "--", "rs2": "AG"})
    second, result, src = dna(vault, tmp_path, "second.txt", ancestry=True, reverse=True,
                              inject={"rs1": "--", "rs2": "AG", "rs3": "--"})
    assert comparisons(vault, second) == [{"other_callset_id": first, "other_file": "first.txt",
        "compared": 1098, "matching": 1098, "concordance": 1.0, "same_person": True}]
    assert result.warnings == ["Same person as first.txt (100.0% of 1098 shared markers match). "
                               "Answers use the newest file."]
    assert comparisons(vault, first) == []
    for args in OPS:
        assert "warning" not in ops.genome_tool(fake_ctx, args)
    before = vault.total_changes
    skipped = import_dtc(vault, src)
    assert skipped.status == "skipped" and skipped.warnings == []
    assert vault.total_changes == before


@pytest.mark.parametrize("args", OPS, ids=[a["op"] for a in OPS])
def test_different_person_warns_on_every_answer(vault, tmp_path, fake_ctx, args):
    first, _, _ = dna(vault, tmp_path, "first.txt")
    second, imported, _ = dna(vault, tmp_path, "second.txt", seed=2)
    comparison = comparisons(vault, second)[0]
    assert comparison["same_person"] is False and comparison["other_callset_id"] == first
    assert comparison["compared"] == 1100 and comparison["concordance"] < 0.95
    pct = f"{100 * comparison['matching'] / comparison['compared']:.1f}"
    assert imported.warnings == [f"This DNA file looks like a different person from first.txt: {pct}% of "
        "1100 shared markers match (files from the same person match over 95%). Caduceus answers from the "
        "newest DNA file, so answers now come from this file. Keep each person's DNA in their own Hermes "
        "profile (`hermes profile create <name>`)."]
    answer = ops.genome_tool(fake_ctx, args)
    assert answer["warning"] == ("Your vault holds DNA files from more than one person. "
                                  "These results come from the newest file, second.txt.")
    warnings = " ".join(imported.warnings + [answer["warning"]])
    assert all(genotype not in warnings for genotype in ("AA", "AC", "AG", "AT", "CC", "CG", "CT", "GG", "GT", "TT"))
    if args["op"] == "summary":
        assert answer["callsets"][0]["comparisons"] == [comparison]
        assert answer["callsets"][1]["comparisons"] == []


def test_too_few_and_no_shared_markers(vault, tmp_path):
    from caduceus.genome.calls import compare_callsets

    first, _, _ = dna(vault, tmp_path, "one.txt", n=999)
    second, result, _ = dna(vault, tmp_path, "two.txt", n=999, ancestry=True)
    assert comparisons(vault, second)[0]["same_person"] is None
    assert result.warnings == ["Could not compare with one.txt: too few shared markers."]
    with write_txn(vault):
        vault.execute("UPDATE genotype_call SET chrom='X' WHERE callset_id=?", (second,))
    result = compare_callsets(vault, second, first)
    assert (result["compared"], result["matching"], result["concordance"], result["same_person"]) == (0, 0, None, None)


@pytest.mark.parametrize("matching,same", [(950, True), (949, False)])
def test_comparison_filters_and_threshold(vault, tmp_path, matching, same):
    from caduceus.genome.calls import compare_callsets

    first, _, _ = dna(vault, tmp_path, "one.txt", n=1000)
    second, _, _ = dna(vault, tmp_path, "two.txt", n=1000, ancestry=True)
    with write_txn(vault):
        vault.execute("UPDATE genotype_call SET genotype='AG' WHERE callset_id=?", (first,))
        vault.execute("UPDATE genotype_call SET genotype='CC' WHERE callset_id=?", (second,))
        vault.execute("UPDATE genotype_call SET genotype='GA' WHERE callset_id=? AND pos<=?", (second, matching))
        excluded = [("X", "AA", "AA"), ("Y", "AA", "AA"), ("MT", "AA", "AA"),
                    ("1", "--", "AA"), ("1", "AA", "--"), ("1", "DI", "AA"),
                    ("1", "AA", "DI"), ("1", "A", "AA"), ("1", "AA", "A"), ("1", "", "AA")]
        for i, (chrom, a, b) in enumerate(excluded, 2000):
            vault.execute("INSERT INTO genotype_call VALUES (?,?,?,?,?)", (first, f"rs{i}", chrom, i, a))
            vault.execute("INSERT INTO genotype_call VALUES (?,?,?,?,?)", (second, f"rs{i}", chrom, i, b))
        vault.execute("INSERT INTO genotype_call VALUES (?, 'rs3000', '1', 3000, 'AA')", (first,))
        vault.execute("INSERT INTO genotype_call VALUES (?, 'rs3000', 'X', 3000, 'AA')", (second,))
    result = compare_callsets(vault, second, first)
    assert (result["compared"], result["matching"], result["concordance"], result["same_person"]) == (
        1000, matching, round(matching / 1000, 3), same)


def test_only_complete_callsets_are_compared_and_active(vault, tmp_path, fake_ctx):
    first, _, _ = dna(vault, tmp_path, "one.txt")
    failed, _, src = dna(vault, tmp_path, "failed.txt", seed=2)
    with write_txn(vault):
        vault.execute("UPDATE import_run SET status='failed' WHERE source_id=?", (src.id,))
    third, result, _ = dna(vault, tmp_path, "three.txt", ancestry=True)
    assert [c["other_callset_id"] for c in comparisons(vault, third)] == [first]
    assert len(result.warnings) == 1 and result.warnings[0].startswith("Same person")
    # A partial newer import (even with a stored different-person comparison) must not control the warning.
    with write_txn(vault):
        vault.execute("UPDATE callset SET created_at='2099-01-01' WHERE id=?", (failed,))
    assert "warning" not in ops.genome_tool(fake_ctx, {"op": "summary"})
    assert ops.genome_tool(fake_ctx, {"op": "lookup", "rsids": ["rs1"]})["results"][0]["callset_id"] == third


def test_older_vault_adds_comparisons_on_reopen(vault, tmp_path):
    cid, _, _ = dna(vault, tmp_path, "one.txt", n=1)
    with write_txn(vault):
        if "comparison_json" in {r[1] for r in vault.execute("PRAGMA table_info(callset)")}:
            vault.execute("ALTER TABLE callset DROP COLUMN comparison_json")
    reopened = open_vault(tmp_path / "plugin-data" / "vault.db")
    try:
        assert "comparison_json" in {r[1] for r in reopened.execute("PRAGMA table_info(callset)")}
        assert comparisons(reopened, cid) == []
    finally:
        reopened.close()


def test_raw_only_profile_privacy_and_gene_fallback(vault, tmp_path, fake_ctx):
    dna(vault, tmp_path, "one.txt", n=2, inject={**VALUES, "rs429358": "TT", "rs7412": "CC"})
    fake_ctx.config["privacy_level"] = "standard"
    summary = ops.genome_tool(fake_ctx, {"op": "summary"})
    result = summary["gene_drug_profile"]
    flagged = {r["gene"]: r for r in result["flagged"]}
    assert flagged["GLP1R"] == {"gene": "GLP1R", "drugs": ["dulaglutide", "exenatide", "liraglutide",
        "lixisenatide", "semaglutide"], "evidence": ["research"]}
    assert "clopidogrel" in flagged["CYP2C19"]["drugs"]
    assert result["counts"]["flag"] >= 2
    assert sum(result["counts"].values()) == len(knowledge.load_pack().entries)
    assert summary["variant_report_records"] == 0 and summary["top_findings"] == []
    assert summary["withheld"] == 1 and "APOE" not in json.dumps(summary)
    assert result["derived"] == ops.genome_tool(fake_ctx, {"op": "derived"})["derived"]
    assert "not a diagnosis" in result["note"] and "'no flag' does not mean normal" in result["note"]
    gene = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "GLP1R"})
    assert gene["note"] == "From the built-in knowledge pack; your variant report has no rows for this gene."
    assert {r["marker"]: r["genotype"] for r in gene["results"]} == {"rs6923761": "AA", "rs10305420": "CC"}
    with write_txn(vault):
        vault.execute("INSERT INTO annotation_snapshot(source_id, ordinal, kind, marker, genes_json, record_json) "
                      "SELECT source_id, 1, 'snp', 'rs1', '[\"GLP1R\"]', '{}' FROM callset LIMIT 1")
    report_gene = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "GLP1R"})
    assert "note" not in report_gene and [r["marker"] for r in report_gene["results"]] == ["rs1"]


def test_hidden_pgx_omits_profile_and_counts_it(vault, tmp_path, fake_ctx):
    dna(vault, tmp_path, "one.txt", n=1, inject=VALUES)
    fake_ctx.config["hidden_categories"] = ["pgx"]
    result = ops.genome_tool(fake_ctx, {"op": "summary"})
    assert "gene_drug_profile" not in result and result["withheld"] == 1
    gene = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "GLP1R"})
    assert gene["results"] == [] and gene["withheld"] == 1


def test_sensitive_pack_genes_are_filtered_before_aggregation(vault, tmp_path, fake_ctx, monkeypatch):
    dna(vault, tmp_path, "one.txt", n=1, inject=VALUES)
    pack = knowledge.load_pack()
    hidden = copy.deepcopy(pack.glp1[0])
    hidden.update(gene="APOE", drugs=["invented-sensitive-drug"])
    pack.glp1.append(hidden)
    monkeypatch.setattr(ops, "_pack", lambda: pack)
    fake_ctx.config["privacy_level"] = "standard"
    result = ops.genome_tool(fake_ctx, {"op": "summary"})
    assert "gene_drug_profile" in result
    assert "APOE" not in json.dumps(result) and "invented-sensitive-drug" not in json.dumps(result)
    assert result["withheld"] == 2  # one pack entry and one derived item
    assert sum(result["gene_drug_profile"]["counts"].values()) == len(pack.entries) - 1


@pytest.mark.parametrize("drug,digest", [
    ("Ozempic", "a9422b6217d99c652c865db6fec1c49c4dcf6c51719132e7c6c692c9a74c7363"),
    ("Plavix", "fc4b129e55230619fc391f6252bf9bb544549915631c40e2428f23ec3fa267a4"),
    ("abacavir", "83a9ee42944bb831b5bdcfdb5877c9d3560cb0c10b90198b483ce79fa627b2e2"),
    ("gentamicin", "6c01531441ae37ac23f63d75d57c79e2d24f4d7f0da1de3025b984ad8eb0e4c9"),
    ("invented drug", "11fdc10475da951b9b5bc2ea0a031c359db587aa972a7273ddb549d268546ffa"),
])
def test_profile_shares_statuses_without_changing_base_pgx(drug, digest):
    # Hashes cover entire pgx_check results captured from 41f6b55, including nested metadata.
    calls = {m: Call(m, CallState.CALLED, g) for m, g in VALUES.items()}
    pack = knowledge.load_pack()
    result = knowledge.profile(pack, calls)
    pgx = knowledge.pgx_check(pack, drug, calls)
    assert hashlib.sha256(json.dumps(pgx, sort_keys=True).encode()).hexdigest() == digest
    statuses = {"flag": 0, "no_flag": 0, "not_assessed": 0}
    for entry in pack.entries:
        assessed = next(r for r in knowledge.pgx_check(pack, entry["drugs"][0], calls)["entries"]
                        if r["id"] == entry["id"])
        statuses[assessed["status"]] += 1
    assert result["counts"] == statuses
    assert result["derived"] == knowledge.derive(pack, calls)


def test_profile_groups_caps_and_sorts_genes_and_drugs():
    pack = knowledge.load_pack()
    template = next(e for e in pack.glp1 if e["id"] == "glp1-rs6923761")
    entries = []
    for gene in reversed(range(25)):
        for evidence, drugs in (("CPIC A", [f"drug-{n:02}" for n in reversed(range(20))]),
                                ("research", ["drug-00", "drug-01"])):
            entry = copy.deepcopy(template)
            entry.update(gene=f"GENE{gene:02}", drugs=drugs, evidence=evidence)
            entries.append(entry)
    custom = knowledge.Pack(pack.version, pack.drugs, entries, [], pack.watch)
    result = knowledge.profile(custom, {"rs6923761": Call("rs6923761", CallState.CALLED, "AA")})
    assert result["counts"] == {"flag": 50, "no_flag": 0, "not_assessed": 0}
    assert [r["gene"] for r in result["flagged"]] == [f"GENE{i:02}" for i in range(20)]
    assert all(r["drugs"] == [f"drug-{i:02}" for i in range(15)] and r["evidence"] == ["CPIC A", "research"]
               for r in result["flagged"])
