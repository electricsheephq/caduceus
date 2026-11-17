"""U4a session memory and specialty briefs, using generated synthetic records only."""
import json
from datetime import date
from pathlib import Path

import pytest

from caduceus import brief, ops
from caduceus.core import sources
from caduceus.core.db import open_vault, write_txn
from caduceus.core.policy import Policy
from caduceus.genome.dtc import import_dtc
from caduceus.health.fhir import import_fhir
from synth.dtc import make_23andme


@pytest.fixture
def dna(vault, tmp_path):
    path = make_23andme(tmp_path / "synthetic.txt", n=2,
                        inject={"rs429358": "TT", "rs7412": "CC", "rs4149056": "TC"})
    src, _ = sources.store_source(vault, path, "dtc")
    assert import_dtc(vault, src).status == "succeeded"
    return vault


def capture(ctx, **args):
    return ops.capture_tool(ctx, args)


def test_consent_set_read_clear_and_categories(fake_ctx, vault):
    for answer in ("yes", "no", "ask_later"):
        reply = capture(fake_ctx, op="consent", category="alzheimers", answer=answer)
        assert "never changes visibility" in reply["note"].lower()
        saved = capture(fake_ctx, op="consent")
        record = saved["consent"]["alzheimers"]
        assert record["answer"] == answer and record["at"]
        assert json.loads(vault.execute("SELECT value_json FROM settings WHERE key='consent:alzheimers'")
                          .fetchone()[0]) == record
        assert "never changes visibility" in saved["note"].lower()
    cleared = capture(fake_ctx, op="consent", category="alzheimers", answer="clear")
    assert "never changes visibility" in cleared["note"].lower()
    assert capture(fake_ctx, op="consent")["consent"] == {}
    with pytest.raises(ops.ToolError, match="sensitive"):
        capture(fake_ctx, op="consent", category="trait", answer="yes")
    with pytest.raises(ops.ToolError, match="answer"):
        capture(fake_ctx, op="consent", category="carrier", answer="maybe")


def test_consent_yes_never_unlocks_hidden_results(fake_ctx, dna):
    fake_ctx.config["privacy_level"] = "standard"
    capture(fake_ctx, op="medication", drug="Synthetic dementia medicine", date="2026-01-01", confirm=True)
    with write_txn(dna):
        ops.search_index.index_doc(dna, kind="annotation", ref="synthetic", title="Synthetic APOE",
                                   body="Synthetic dementia marker", category="alzheimers")
    before_config = dict(fake_ctx.config)
    before_search = ops.search_tool(fake_ctx, {"kinds": ["annotation"]})
    before_lookup = ops.genome_tool(fake_ctx, {"op": "lookup", "rsids": ["rs429358"]})
    before_brief = Path(capture(fake_ctx, op="brief")["path"]).read_text()
    assert "Synthetic dementia medicine" not in before_brief and "hidden by your privacy settings" in before_brief
    capture(fake_ctx, op="consent", category="alzheimers", answer="yes")
    assert fake_ctx.config == before_config
    assert ops.search_tool(fake_ctx, {"kinds": ["annotation"]}) == before_search
    assert before_search["results"] == [] and before_search["withheld"] == 1
    assert ops.genome_tool(fake_ctx, {"op": "lookup", "rsids": ["rs429358"]}) == before_lookup
    assert before_lookup["results"] == [] and before_lookup["withheld"] == 1
    assert Path(capture(fake_ctx, op="brief")["path"]).read_text() == before_brief
    assert not ops.allowed(ops.policy_for(fake_ctx), "alzheimers")


def test_phenotype_categories_hidden_reply_and_search(fake_ctx, dna):
    fake_ctx.config["privacy_level"] = "standard"
    saved = capture(fake_ctx, op="phenotype", marker="RS429358", stance="unsure", text="Not sure yet")
    assert set(saved) == {"saved", "note"} and saved["saved"]
    row = dna.execute("SELECT * FROM phenotype_note").fetchone()
    assert row["marker"] == "rs429358" and row["category"] == "alzheimers"
    saved = capture(fake_ctx, op="phenotype", marker="RS1", trait="Sleep", stance="confirms",
                    text="My anxiety affects my sleep")
    assert set(saved) == {"saved", "note"}
    assert dna.execute("SELECT category FROM phenotype_note ORDER BY id DESC").fetchone()[0] == "mental_health"
    result = ops.genome_tool(fake_ctx, {"op": "lookup", "rsids": ["rs1"]})
    assert not result["results"][0].get("phenotype_notes") and result["withheld"] == 1
    found = ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})
    assert found["results"] == [] and found["withheld"] == 2
    assert ops.search_tool(fake_ctx, {"ref": f"phenotype:{row['id']}"})["withheld"] is True


def test_visible_phenotypes_lookup_gene_and_browse_newest_first(fake_ctx, dna):
    for text, stance in (("First words", "confirms"), ("Correction", "refutes")):
        capture(fake_ctx, op="phenotype", marker="RS4149056", trait="Synthetic trait", stance=stance, text=text)
    for args in ({"op": "lookup", "rsids": ["rs4149056"]}, {"op": "gene", "gene": "SLCO1B1"}):
        result = ops.genome_tool(fake_ctx, args)
        marker = next(r for r in result["results"] if r["marker"] == "rs4149056")
        notes = marker["phenotype_notes"]
        assert [n["text"] for n in notes] == ["Correction", "First words"]
        assert set(notes[0]) == {"id", "stance", "trait", "text", "date"}
        assert notes[0]["stance"] == "refutes" and notes[0]["date"]
    found = ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})
    assert [r["snippet"] for r in found["results"]] == ["Correction", "First words"]
    capture(fake_ctx, op="phenotype", trait="Taste", stance="unsure", text="Undecided")
    assert dna.execute("SELECT category FROM phenotype_note ORDER BY id DESC").fetchone()[0] == "trait"


@pytest.mark.parametrize("marker,category", [("I4000378", "cancer_risk"), ("GS123", "general"),
                                            ("APOE", "alzheimers"), ("CYP2C19", "pgx")])
def test_supported_marker_forms(fake_ctx, vault, marker, category):
    capture(fake_ctx, op="phenotype", marker=marker, stance="unsure", text="Synthetic words")
    row = vault.execute("SELECT marker,category FROM phenotype_note").fetchone()
    assert tuple(row) == (marker.lower(), category)


@pytest.mark.parametrize("extra", [
    {}, {"marker": "rs1", "stance": "yes"}, {"marker": "rs1", "text": "x" * 2001},
    {"trait": "x" * 201}, {"marker": "rs1", "text": ""}, {"marker": "../bad"},
])
def test_phenotype_validation(fake_ctx, extra):
    args = {"op": "phenotype", "stance": "confirms", "text": "Synthetic words", **extra}
    with pytest.raises(ops.ToolError):
        capture(fake_ctx, **args)
    # Invalid operations do not write a note, and the new table exists.
    with ops._Store() as store:
        assert store.vault.execute("SELECT count(*) FROM phenotype_note").fetchone()[0] == 0


def _labs(vault, tmp_path):
    records = []
    for name, code in (("Synthetic LDL", "13457-7"), ("Synthetic ApoB", "1884-6"),
                       ("Synthetic unrelated TSH", "3016-3")):
        records.append({"resourceType": "Observation", "status": "final",
                        "code": {"text": name, "coding": [{"system": "http://loinc.org", "code": code}]},
                        "effectiveDateTime": "2026-01-01", "valueQuantity": {"value": 80, "unit": "mg/dL"},
                        "referenceRange": [{"low": {"value": 40, "unit": "mg/dL"},
                                            "high": {"value": 100, "unit": "mg/dL"}}]})
    path = tmp_path / "synthetic-labs.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in records]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"


def test_specialty_labs_gene_filter_questions_and_general(fake_ctx, dna, tmp_path):
    _labs(dna, tmp_path)
    for drug in ("simvastatin", "semaglutide"):
        capture(fake_ctx, op="medication", drug=drug, date="2026-01-01", confirm=True)
    result = capture(fake_ctx, op="brief", specialty="cardiology", questions=['Why <b>this</b> & that?'])
    page = Path(result["path"]).read_text()
    assert Path(result["path"]).name == f"health-brief-{date.today()}-cardiology.html"
    assert "Prepared for: Cardiology" in page
    assert "Synthetic LDL" in page and "Synthetic ApoB" in page and "40 to 100" in page
    assert "unrelated TSH" not in page
    assert "SLCO1B1" in page and "GLP1R" not in page
    assert "Semaglutide" in page  # medications stay, even with no relevant gene entry
    assert "Your questions" in page and "Why &lt;b&gt;this&lt;/b&gt; &amp; that?" in page
    assert "<b>this</b>" not in page
    general = Path(capture(fake_ctx, op="brief")["path"])
    assert general.name == f"health-brief-{date.today()}.html"
    assert "GLP1R" in general.read_text() and "Prepared for:" not in general.read_text()


def test_brief_hidden_question_and_unknown_specialty(fake_ctx):
    fake_ctx.config["privacy_level"] = "standard"
    page = Path(capture(fake_ctx, op="brief", questions=["Ask about anxiety", "Ask about exercise"])["path"]).read_text()
    assert "anxiety" not in page and "Ask about exercise" in page
    assert "1 item hidden by your privacy settings" in page
    with pytest.raises(ops.ToolError, match="cardiology"):
        capture(fake_ctx, op="brief", specialty="unknown")


@pytest.mark.parametrize("questions", ["not a list", ["x"] * 11, ["x" * 501], [1]])
def test_brief_question_limits(fake_ctx, questions):
    with pytest.raises(ops.ToolError, match="questions"):
        capture(fake_ctx, op="brief", questions=questions)


def test_existing_vault_gains_phenotype_table_on_open(vault, data_dir):
    # Model a vault created before this feature; never touch a real vault.
    with write_txn(vault):
        vault.execute("DROP TABLE IF EXISTS phenotype_note")
    reopened = open_vault(data_dir / "vault.db")
    try:
        assert reopened.execute("SELECT count(*) FROM phenotype_note").fetchone()[0] == 0
    finally:
        reopened.close()


# Fix round 1: append-only regressions against the reviewed U4a candidate.
def _fix1_note_case(ctx, vault, case):
    marker, text, category = {
        "apoe": ("rs429358", "Synthetic anxiety words", "alzheimers"),
        "pgx": ("rs4149056", "Synthetic response words", "pgx"),
        "alias": ("rs900001", "Synthetic alias words", "cancer_risk"),
    }[case]
    if case == "alias":
        with write_txn(vault):
            vault.execute("INSERT INTO marker_alias VALUES ('rs900001','rs80357906','synthetic')")
    ctx.config.update(privacy_level="standard" if case == "alias" else "open",
                      hidden_categories=[category] if case != "alias" else [])
    return marker, text, category


@pytest.mark.parametrize("case", ["apoe", "pgx", "alias"])
def test_fix1_write_category_never_downgrades_marker(fake_ctx, dna, case):
    marker, text, category = _fix1_note_case(fake_ctx, dna, case)
    reply = capture(fake_ctx, op="phenotype", marker=marker, stance="confirms", text=text)
    row = dna.execute("SELECT * FROM phenotype_note").fetchone()
    assert row["category"] == category
    assert dna.execute("SELECT category FROM search_doc WHERE kind='phenotype'").fetchone()[0] == category
    assert set(reply) == {"saved", "note"} and reply["saved"]
    assert marker not in json.dumps(reply) and text not in json.dumps(reply)


@pytest.mark.parametrize("case", ["apoe", "pgx", "alias"])
@pytest.mark.parametrize("surface", ["query", "browse", "fetch", "lookup"])
def test_fix1_marker_gate_on_every_note_read(fake_ctx, dna, case, surface):
    marker, text, _ = _fix1_note_case(fake_ctx, dna, case)
    capture(fake_ctx, op="phenotype", marker=marker, stance="confirms", text=text)
    # A note saved by the reviewed code may have a permissive category. Read-time enforcement must stand alone.
    with write_txn(dna):
        dna.execute("UPDATE phenotype_note SET category='general', created_at='2001-02-03T04:05:06Z'")
        dna.execute("UPDATE search_doc SET category='general', ts='2001-02-03T04:05:06Z' WHERE kind='phenotype'")
    ref = str(dna.execute("SELECT id FROM phenotype_note").fetchone()[0])
    if surface == "lookup":
        reply = ops.genome_tool(fake_ctx, {"op": "lookup", "rsids": [marker]})
        assert reply["results"] == [] and reply["withheld"] == 2  # marker plus its note
    else:
        args = {"ref": "phenotype:" + ref} if surface == "fetch" else {"kinds": ["phenotype"]}
        if surface == "query":
            args["query"] = "Synthetic"
        reply = ops.search_tool(fake_ctx, args)
        assert reply["withheld"] == 1
        assert not reply.get("results")
    assert all(value not in json.dumps(reply) for value in (marker, text, "2001-02-03"))


@pytest.mark.parametrize("surface", ["query", "browse", "fetch", "lookup", "gene"])
def test_fix1_marker_becoming_sensitive_hides_existing_note(fake_ctx, dna, surface):
    capture(fake_ctx, op="phenotype", marker="rs1", stance="confirms", text="Synthetic later-hidden words")
    assert ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})["results"]
    with write_txn(dna):
        dna.execute("INSERT INTO annotation_snapshot(source_id,ordinal,kind,marker,genes_json,category,record_json) "
                    "SELECT source_id,1,'snp','rs1','[\"SYNTH1\"]','cancer_risk','{}' FROM callset LIMIT 1")
    fake_ctx.config["privacy_level"] = "standard"
    ref = dna.execute("SELECT id FROM phenotype_note").fetchone()[0]
    if surface in ("lookup", "gene"):
        args = {"op": "lookup", "rsids": ["rs1"]} if surface == "lookup" else {"op": "gene", "gene": "SYNTH1"}
        reply = ops.genome_tool(fake_ctx, args)
        assert reply["withheld"] == 2
    else:
        args = {"ref": f"phenotype:{ref}"} if surface == "fetch" else {"kinds": ["phenotype"]}
        if surface == "query":
            args["query"] = "Synthetic"
        reply = ops.search_tool(fake_ctx, args)
        assert reply["withheld"] == 1
    assert not reply.get("results") and "later-hidden" not in json.dumps(reply)


def test_fix1_gene_name_note_and_current_marker_policy(fake_ctx, dna):
    capture(fake_ctx, op="phenotype", marker="SLCO1B1", stance="confirms", text="Synthetic gene note")
    reply = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "SLCO1B1"})
    assert reply["phenotype_notes"][0]["text"] == "Synthetic gene note"
    with write_txn(dna):
        dna.execute("INSERT INTO annotation_snapshot(source_id,ordinal,kind,marker,category,record_json) "
                    "SELECT source_id,1,'genoset','slco1b1','cancer_risk','{}' FROM callset LIMIT 1")
    fake_ctx.config["privacy_level"] = "standard"
    hidden = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "SLCO1B1"})
    assert not hidden.get("phenotype_notes") and hidden["withheld"] == 1
    assert "Synthetic gene note" not in json.dumps(hidden)


def test_fix1_same_day_duplicate_is_already_saved(fake_ctx, vault, monkeypatch):
    monkeypatch.setattr(sources, "now_iso", lambda: "2026-11-17T10:00:00Z")
    args = {"op": "phenotype", "marker": "RS1", "trait": "Synthetic taste", "stance": "confirms", "text": "My words"}
    first = capture(fake_ctx, **args)
    second = capture(fake_ctx, **args)
    assert first["id"] == second["id"] and "already saved" in second["note"].lower()
    assert vault.execute("SELECT count(*) FROM phenotype_note").fetchone()[0] == 1
    assert vault.execute("SELECT count(*) FROM search_doc WHERE kind='phenotype'").fetchone()[0] == 1
    # Every natural-key component and the day matter; a correction or a later confirmation is retained.
    for field, value in (("marker", "rs2"), ("trait", "Synthetic smell"), ("stance", "refutes"), ("text", "New words")):
        capture(fake_ctx, **{**args, field: value})
    monkeypatch.setattr(sources, "now_iso", lambda: "2026-11-18T10:00:00Z")
    capture(fake_ctx, **args)
    assert vault.execute("SELECT count(*) FROM phenotype_note").fetchone()[0] == 6


def test_fix1_specialty_includes_text_labs_without_other_units(fake_ctx, vault, tmp_path):
    _labs(vault, tmp_path)
    records = []
    for i, (name, value) in enumerate([
        ("Synthetic LDL", {"valueString": "Synthetic <trace>"}),
        ("Synthetic LDL", {"valueQuantity": {"value": 999, "unit": "mmol/L"}}),
        ("Synthetic anxiety LDL", {"valueString": "Hidden text result"}),
    ], 2):
        records.append({"resourceType": "Observation", "status": "final",
                        "code": {"text": name, "coding": [{"system": "http://loinc.org", "code": "13457-7"}]},
                        "effectiveDateTime": f"2026-01-0{i}", **value})
    path = tmp_path / "synthetic-text-labs.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in records]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"
    fake_ctx.config["privacy_level"] = "standard"
    page = Path(capture(fake_ctx, op="brief", specialty="cardiology")["path"]).read_text()
    assert page.count("Synthetic &lt;trace&gt;") == 1
    assert "<td>999 mmol/L</td>" not in page and "Hidden text result" not in page
    assert "1 item hidden by your privacy settings" in page


def test_fix1_pack_gene_name_notes(fake_ctx, dna):
    saved = capture(fake_ctx, op="phenotype", marker="GLP1R", stance="unsure", text="Synthetic gene words")
    assert saved["category"] == "pgx"
    reply = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "GLP1R"})
    assert reply["phenotype_notes"][0]["text"] == "Synthetic gene words"
    fake_ctx.config["hidden_categories"] = ["pgx"]
    hidden = ops.genome_tool(fake_ctx, {"op": "gene", "gene": "GLP1R"})
    assert hidden["withheld"] == 2 and not hidden.get("phenotype_notes")
    assert ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})["withheld"] == 1


def test_fix1_alias_privacy_without_dna_and_trait_only_duplicate(fake_ctx, vault):
    marker, text, category = _fix1_note_case(fake_ctx, vault, "alias")
    capture(fake_ctx, op="phenotype", marker=marker, stance="confirms", text=text)
    assert vault.execute("SELECT category FROM phenotype_note").fetchone()[0] == category
    assert ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})["withheld"] == 1
    args = {"op": "phenotype", "trait": "Synthetic taste", "stance": "unsure", "text": "Synthetic taste words"}
    first = capture(fake_ctx, **args)
    again = capture(fake_ctx, **args)
    assert again["id"] == first["id"] and "already saved" in again["note"].lower()
    found = ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})
    assert len(found["results"]) == found["withheld"] == 1
    assert found["results"][0]["category"] == "trait"


# Fix round 2 (PR review): a note's own sensitive text, alias notes on the canonical marker, stance in search.

def test_fix2_text_category_hidden_even_when_marker_sensitive(fake_ctx, dna):
    fake_ctx.config.update(privacy_level="open", hidden_categories=["mental_health"])
    saved = capture(fake_ctx, op="phenotype", marker="rs429358", stance="confirms", text="Synthetic anxiety note")
    assert set(saved) == {"saved", "note"}
    row = dna.execute("SELECT id FROM phenotype_note").fetchone()
    found = ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})
    assert found["results"] == [] and found["withheld"] == 1
    assert "anxiety note" not in json.dumps(ops.search_tool(fake_ctx, {"query": "anxiety"}))
    assert ops.search_tool(fake_ctx, {"ref": f"phenotype:{row['id']}"})["withheld"] is True
    for args in ({"op": "lookup", "rsids": ["rs429358"]}, {"op": "gene", "gene": "APOE"}):
        assert "anxiety note" not in json.dumps(ops.genome_tool(fake_ctx, args))


def test_fix2_alias_note_attaches_to_canonical_marker(fake_ctx, dna):
    with write_txn(dna):
        dna.execute("INSERT INTO marker_alias VALUES ('rs900001','rs4149056','synthetic')")
    capture(fake_ctx, op="phenotype", marker="rs900001", stance="confirms", text="Synthetic alias words")
    for args in ({"op": "lookup", "rsids": ["rs4149056"]}, {"op": "gene", "gene": "SLCO1B1"}):
        result = ops.genome_tool(fake_ctx, args)
        marker = next(r for r in result["results"] if r["marker"] == "rs4149056")
        assert [n["text"] for n in marker.get("phenotype_notes", [])] == ["Synthetic alias words"]


def test_fix2_stance_in_search_and_fetch(fake_ctx, vault):
    for stance in ("confirms", "refutes"):
        capture(fake_ctx, op="phenotype", trait="Synthetic taste", stance=stance, text="Same words")
    found = ops.search_tool(fake_ctx, {"kinds": ["phenotype"]})
    assert [r["title"] for r in found["results"]] == ["Refutes: Synthetic taste", "Confirms: Synthetic taste"]
    fetched = ops.search_tool(fake_ctx, {"ref": found["results"][0]["ref"]})
    assert fetched["title"].startswith("Refutes:")
