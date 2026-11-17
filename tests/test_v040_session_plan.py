"""U4b first-session agenda: generated synthetic inputs, no network or real profile."""
import json
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from caduceus import ops
from caduceus.core import sources
from caduceus.core.db import write_txn
from caduceus.genome.dtc import import_dtc
from caduceus.genome.promethease import import_promethease
from caduceus.health.fhir import import_fhir
from synth.dtc import make_23andme
from synth.promethease import write_report

KEYS = ["own_numbers", "actionable", "sensitive", "gene_drug", "carrier_and_screening", "traits"]


def plan(ctx, **args):
    return ops.capture_tool(ctx, {"op": "session_plan", **args})


def areas(reply):
    return {a["key"]: a for a in reply["areas"]}


def report(vault, tmp_path, rows):
    path = write_report(tmp_path / "synthetic-report.zip", rows=rows, maps=({}, {}))
    result = import_promethease(vault, sources.store_source(vault, path, "promethease")[0])
    assert result.status == "succeeded"


def finding(marker, gene, magnitude=3, text="Synthetic finding", **extra):
    return {"rsnum": marker, "genes": [gene], "geno": "(A;G)", "magnitude": magnitude,
            "repute": "Bad", "genosummary": text, **extra}


def labs(vault, tmp_path, mismatch=False):
    records = []
    for name, code, day, value, unit in [
        ("Synthetic LDL", "13457-7", "2026-01-01", 80, "mg/dL"),
        ("Synthetic LDL", "13457-7", "2026-02-01", 120, "mg/dL"),
        ("Synthetic HbA1c", "4548-4", "2026-03-01", 5.2, "%"),
    ]:
        records.append({"resourceType": "Observation", "status": "final",
                        "code": {"text": name, "coding": [{"system": "http://loinc.org", "code": code}]},
                        "effectiveDateTime": day, "valueQuantity": {"value": value + int(mismatch), "unit": unit},
                        "referenceRange": [{"low": {"value": 40, "unit": "wrong" if mismatch else unit},
                                            "high": {"value": 100, "unit": unit}}]})
    path = tmp_path / ("mismatched-labs.json" if mismatch else "synthetic-labs.json")
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in records]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"


@pytest.fixture
def populated(vault, samples, fake_ctx, tmp_path):
    path = make_23andme(tmp_path / "synthetic.txt", n=2,
                        inject={"rs429358": "TT", "rs7412": "CC", "rs4149056": "TC"})
    assert import_dtc(vault, sources.store_source(vault, path, "dtc")[0]).status == "succeeded"
    report(vault, tmp_path, [finding("rs900001", "LDLR", 4),
                             finding("rs900002", "FAKE", 2, "Synthetic eye color trait"),
                             finding("rs900003", "CFTR", 4, "Synthetic carrier finding"),
                             finding("rs429358", "APOE", 5, "Synthetic sealed orchard"),
                             finding("rs4149056", "SLCO1B1", 3)])
    # The report importer currently files nonsensitive traits as general; model a curated trait category.
    with write_txn(vault):
        vault.execute("UPDATE annotation_snapshot SET category='trait' WHERE marker='rs900002'")
    labs(vault, tmp_path)
    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "simvastatin", "date": "2026-01-01", "confirm": True})
    for marker in ("rs900001", "rs900002", "rs4149056"):
        ops.capture_tool(fake_ctx, {"op": "phenotype", "marker": marker, "stance": "confirms",
                                    "text": "Synthetic personal words"})
    with write_txn(samples):
        for i in range(10):
            day = (date.today() - timedelta(days=i)).isoformat()
            samples.execute("INSERT INTO daily_rollup VALUES (?,?,?,?,?,?)",
                            ("weight", day, 80 if i < 3 else 70, 1, '["Synthetic watch"]', "trend/1"))
    return vault


def test_agenda_fields_order_and_read_only(populated, samples, fake_ctx, monkeypatch):
    before = list(populated.iterdump()), list(samples.iterdump())
    monkeypatch.setattr(ops, "_refresh_rollups", lambda *a: pytest.fail("read must not rebuild rollups"))
    reply = plan(fake_ctx)
    assert [a["key"] for a in reply["areas"]] == KEYS
    assert reply["limits"] == [ops.CHIP_LIMITS, ops.LIMITS, "Information with limits, not medical advice."]
    a = areas(reply)
    assert all(set(("key", "title", "items", "more")) <= set(area) for area in reply["areas"])
    own = a["own_numbers"]["items"]
    assert [i["code"] for i in own if "code" in i] == ["4548-4", "13457-7"]
    assert next(i for i in own if i.get("metric") == "weight")["daily_check"]
    item = a["actionable"]["items"][0]
    assert item["marker"] == "rs900001" and item["genes"] == ["LDLR"]
    assert set(("summary", "magnitude", "repute", "evidence", "chip_reliability", "your_data", "phenotype_notes")) <= set(item)
    assert item["phenotype_notes"][0]["text"] == "Synthetic personal words"
    pgx = a["gene_drug"]
    assert pgx["ask_current_medications"] is True
    assert pgx["items"][0]["status"] == "flag"
    statin = next(i for i in pgx["items"] if "simvastatin" in i["drugs"])
    assert statin["evidence"] == "CPIC guideline" and statin["on_record"][0]["drug"] == "simvastatin"
    assert statin["phenotype_notes"][0]["text"] == "Synthetic personal words"
    assert a["traits"]["items"][0]["marker"] == "rs900002"
    assert a["traits"]["invite"] == "Ask the user to confirm or refute each one."
    assert (list(populated.iterdump()), list(samples.iterdump())) == before


def test_consent_and_policy_are_separate(populated, fake_ctx):
    for key, category in (("sensitive", "alzheimers"), ("carrier_and_screening", "carrier")):
        item = next(i for i in areas(plan(fake_ctx))[key]["items"] if i["category"] == category)
        assert item["findings"] and item["consent"] is None and item["ask_first"]
        assert "items" not in item
        for answer in ("no", "ask_later", "yes"):
            ops.capture_tool(fake_ctx, {"op": "consent", "category": category, "answer": answer})
            item = next(i for i in areas(plan(fake_ctx))[key]["items"] if i["category"] == category)
            assert item["consent"] == answer and not item["ask_first"]
            assert bool(item.get("items")) == (answer == "yes")
        fake_ctx.config["hidden_categories"] = [category]
        reply = plan(fake_ctx)
        item = next(i for i in areas(reply)[key]["items"] if i["category"] == category)
        assert item["hidden"] is True and not item["ask_first"] and not {"items", "findings", "withheld"} & set(item)
        assert ("Synthetic sealed orchard" if category == "alzheimers" else "Synthetic carrier finding") not in json.dumps(reply)
        fake_ctx.config["hidden_categories"] = []


@pytest.mark.parametrize("mismatch,flag", [(False, "above"), (True, "unknown")])
def test_linked_latest_ldl_and_range(populated, fake_ctx, tmp_path, mismatch, flag):
    if mismatch:
        labs(populated, tmp_path, mismatch=True)
    item = areas(plan(fake_ctx))["actionable"]["items"][0]
    point = next(p for p in item["your_data"] if p.get("code") == "13457-7")
    assert point["date"] == "2026-02-01" and point["value"] == 120 + int(mismatch)
    assert point["range_flag"] == flag and point["reference_range_mismatch"] is mismatch


def test_evidence_order_and_chip_rules(vault, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs900010", "FAKE", 1), finding("rs900011", "FAKE", 4),
                             finding("i900012", "FAKE", 5), finding("rs900013", "FAKE", 2, repute="Good"),
                             finding("rs900014", "CYP2C19", 0.5), finding("rs900015", "GLP1R", 1)])
    items = areas(plan(fake_ctx))["actionable"]["items"]
    assert [i["marker"] for i in items] == ["i900012", "rs900011", "rs900013", "rs900010", "rs900015", "rs900014"]
    assert [i["evidence"] for i in items] == ["research", "research", "weak", "weak", "weak", "weak"]
    assert "internal probe" in items[0]["chip_reliability"]
    assert "clinical test should confirm" in items[1]["chip_reliability"]
    assert "common variants reliably" in items[2]["chip_reliability"]


@pytest.mark.parametrize("confidence,phrase", [("called", "called from the chip"),
                                              ("ambiguous", "ambiguous on a chip"),
                                              ("not_assessed", "not assessed")])
def test_derived_reliability(populated, fake_ctx, monkeypatch, confidence, phrase):
    from caduceus.knowledge import pack
    derive = pack.derive
    def synthetic(pack, calls):
        rows = derive(pack, calls)
        for row in rows:
            if row["name"] == "APOE":
                row["confidence"] = confidence
        return rows
    monkeypatch.setattr(pack, "derive", synthetic)
    ops.capture_tool(fake_ctx, {"op": "consent", "category": "alzheimers", "answer": "yes"})
    category = areas(plan(fake_ctx))["sensitive"]["items"][0]
    assert next(i for i in category["items"] if i["marker"] == "APOE")["chip_reliability"] == phrase


def test_hidden_records_and_amendment_marker_alias_checks(populated, samples, fake_ctx):
    with write_txn(populated):
        # Model notes saved before a marker acquired a sensitive classification.
        populated.execute("INSERT INTO marker_alias VALUES ('rs900099','rs900001','synthetic')")
        populated.execute("INSERT INTO marker_alias VALUES ('rs900099','rs80357906','synthetic')")
        populated.execute("INSERT INTO phenotype_note(marker,trait,stance,text,category,created_at) "
                          "VALUES ('rs900099','Synthetic alias','refutes','HIDDENALIAS','trait','2099-01-01')")
        populated.execute("UPDATE phenotype_note SET text='HIDDENNOTE',category='trait' WHERE marker='rs4149056'")
        populated.execute("UPDATE annotation_snapshot SET category='cancer_risk',summary='HIDDENMARKER' WHERE marker='rs4149056'")
        populated.execute("UPDATE observation SET category='mental_health',name='HIDDENLAB',unit='HIDDENUNIT',effective_at='2098-01-01'")
        populated.execute("UPDATE medication_event SET category='mental_health',drug='HIDDENMED',occurred_on='2097-01-01'")
    fake_ctx.config.update(privacy_level="standard", hidden_categories=["wellness"])
    reply = plan(fake_ctx, max_items=25)
    text = json.dumps(reply)
    assert not any(s in text for s in ("HIDDENALIAS", "HIDDENNOTE", "HIDDENMARKER", "HIDDENLAB", "HIDDENUNIT", "HIDDENMED", "2099", "2098", "2097", "Synthetic watch"))
    assert not any(i["gene"] == "SLCO1B1" for i in areas(reply)["gene_drug"]["items"])
    assert reply["withheld"] >= 9


def test_empty_vault_and_schema(fake_ctx, data_dir):
    reply = plan(fake_ctx)
    assert [a["key"] for a in reply["areas"]] == KEYS
    assert all(a["items"] == [] and a["more"] == 0 for a in reply["areas"])
    assert (data_dir / "vault.db").is_file()  # Normal store initializes schema; no captured records.
    from caduceus.tools import TOOLS
    props = next(t for t in TOOLS if t["name"] == "caduceus_capture")["parameters"]["properties"]
    assert "session_plan" in props["op"]["enum"]
    assert props["max_items"]["maximum"] == 25


@pytest.mark.parametrize("limit", [1, 25])
def test_limits_and_more(populated, fake_ctx, limit):
    for category in ("alzheimers", "carrier"):
        ops.capture_tool(fake_ctx, {"op": "consent", "category": category, "answer": "yes"})
    reply = plan(fake_ctx, max_items=limit)
    full = areas(plan(fake_ctx, max_items=25))
    for area in reply["areas"]:
        assert len(area["items"]) <= limit
        assert area["more"] == max(0, len(full[area["key"]]["items"]) - limit) + full[area["key"]]["more"]
        for category in area["items"]:
            if isinstance(category.get("findings"), int) and "items" in category:
                assert len(category["items"]) <= limit
                assert len(category["items"]) + category["more"] == category["findings"]


@pytest.mark.parametrize("limit", [0, 26, True, 1.5])
def test_invalid_max_items(fake_ctx, limit):
    with pytest.raises(ops.ToolError, match="max_items"):
        plan(fake_ctx, max_items=limit)


def test_pack_marker_sensitive_category_wins_before_consent(populated, fake_ctx):
    with write_txn(populated):
        populated.execute("UPDATE annotation_snapshot SET category='carrier' WHERE marker='rs4149056'")
    reply = areas(plan(fake_ctx, max_items=25))
    assert not any(i["gene"] == "SLCO1B1" for i in reply["gene_drug"]["items"])
    category = next(i for i in reply["carrier_and_screening"]["items"] if i["category"] == "carrier")
    assert category["findings"] > 1 and "items" not in category


def test_withheld_counts_each_hidden_record_once(vault, samples, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs429358", "APOE", 5, "HIDDENFINDING")])
    labs(vault, tmp_path)
    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "simvastatin", "date": "2026-01-01", "confirm": True})
    ops.capture_tool(fake_ctx, {"op": "phenotype", "marker": "rs429358", "stance": "refutes", "text": "HIDDENNOTE"})
    with write_txn(samples):
        samples.execute("INSERT INTO daily_rollup VALUES ('weight','2026-01-01',80,1,'[]','trend/1')")
    fake_ctx.config.update(privacy_level="standard", hidden_categories=["records", "wellness", "medication"])
    reply = plan(fake_ctx)
    assert reply["withheld"] == 7  # finding + 3 observations + metric + medication + note
    assert "HIDDEN" not in json.dumps(reply)


def test_latest_text_and_one_sided_range(vault, fake_ctx, tmp_path):
    labs(vault, tmp_path)
    with write_txn(vault):
        vault.execute("UPDATE observation SET value_num=NULL,value_text='Synthetic qualitative' WHERE effective_at='2026-02-01'")
        vault.execute("UPDATE observation SET ref_low=NULL WHERE code='4548-4'")
    own = areas(plan(fake_ctx))["own_numbers"]["items"]
    ldl = next(i for i in own if i["code"] == "13457-7")
    assert ldl["value_text"] == "Synthetic qualitative" and ldl["range_flag"] == "unknown"
    assert next(i for i in own if i["code"] == "4548-4")["range_flag"] == "within"


@pytest.mark.parametrize("genotype,summary", [("TT", "lack the listed effect"), ("--", "Not assessed.")])
def test_review_sensitive_pack_uses_assessed_summary(populated, fake_ctx, genotype, summary):
    with write_txn(populated):
        populated.execute("UPDATE annotation_snapshot SET category='carrier' WHERE marker='rs4149056'")
        populated.execute("UPDATE genotype_call SET genotype=? WHERE marker='rs4149056'", (genotype,))
    ops.capture_tool(fake_ctx, {"op": "consent", "category": "carrier", "answer": "yes"})
    category = next(i for i in areas(plan(fake_ctx, max_items=25))["carrier_and_screening"]["items"]
                    if i["category"] == "carrier")
    findings = [i for i in category["items"] if i["marker"] == "SLCO1B1"]
    assert findings and all(summary in i["summary"] for i in findings)


def test_review_report_only_pack_findings_survive(vault, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs4149056", "SLCO1B1", 3)])
    reply = areas(plan(fake_ctx, max_items=25))
    assert not reply["actionable"]["items"]
    assert reply["gene_drug"]["items"]
    assert all(i["gene"] == "SLCO1B1" and i["status"] == "not_assessed" for i in reply["gene_drug"]["items"])


def test_review_empty_read_initializes_normal_store(fake_ctx, data_dir, monkeypatch):
    missing = data_dir / "missing-parent" / "missing-vault"
    monkeypatch.setenv("CADUCEUS_DATA_DIR", str(missing))
    reply = plan(fake_ctx)
    assert all(a["items"] == [] for a in reply["areas"])
    assert (missing / "vault.db").is_file()
    with ops._Store() as store:
        assert store.vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 0
        assert store.vault.execute("SELECT count(*) FROM phenotype_note").fetchone()[0] == 0


def test_review_hermes_profile_resolution_uses_normal_store(fake_ctx, data_dir, monkeypatch):
    monkeypatch.delenv("CADUCEUS_DATA_DIR")
    home = data_dir / "synthetic-profile"
    def plugin_data_dir(name):
        path = home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setitem(sys.modules, "plugins", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", SimpleNamespace(plugin_data_dir=plugin_data_dir))
    reply = plan(fake_ctx)
    assert all(a["items"] == [] for a in reply["areas"])
    assert (home / "plugin-data" / "caduceus" / "vault.db").is_file()


# Orchestrator U4b-fix-round1: regressions against the prior candidate.
def test_fix_round1_stored_category_cannot_be_overridden_by_carrier(vault, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs900070", "FAKE", 4, "Synthetic visible carrier"),
                             finding("rs900070", "FAKE", 3, "HIDDEN_PGX_ROW")])
    with write_txn(vault):
        vault.execute("UPDATE annotation_snapshot SET category='carrier' WHERE summary='Synthetic visible carrier'")
        vault.execute("UPDATE annotation_snapshot SET category='pgx' WHERE summary='HIDDEN_PGX_ROW'")
    ops.capture_tool(fake_ctx, {"op": "consent", "category": "carrier", "answer": "yes"})
    fake_ctx.config["hidden_categories"] = ["pgx"]
    reply = plan(fake_ctx)
    assert "Synthetic visible carrier" in json.dumps(reply)
    assert "HIDDEN_PGX_ROW" not in json.dumps(reply)
    assert reply["withheld"] == 1


def test_fix_round1_old_vault_migrates_on_first_plan(vault, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs900071", "FAKE", 3)])
    before = list(vault.execute("SELECT * FROM annotation_snapshot"))
    with write_txn(vault):
        vault.execute("DROP TABLE phenotype_note")  # Model the pre-U4a / 0.3.3 schema.
    reply = plan(fake_ctx)
    assert [a["key"] for a in reply["areas"]] == KEYS
    assert vault.execute("SELECT count(*) FROM phenotype_note").fetchone()[0] == 0
    assert list(vault.execute("SELECT * FROM annotation_snapshot")) == before


@pytest.mark.parametrize("range_unit,flag", [("mg/dL", "unknown"), ("mmol/L", "within")])
def test_fix_round1_latest_mixed_unit_point_and_link(vault, fake_ctx, tmp_path, range_unit, flag):
    report(vault, tmp_path, [finding("rs900072", "LDLR", 3)])
    records = []
    for day, value, unit in [("2025-01-01", 80, "mg/dL"), ("2025-02-01", 90, "mg/dL"),
                              ("2026-06-01", 4.5, "mmol/L")]:
        records.append({"resourceType": "Observation", "status": "final",
                        "code": {"text": "Synthetic mixed LDL", "coding": [{"system": "http://loinc.org", "code": "13457-7"}]},
                        "effectiveDateTime": day, "valueQuantity": {"value": value, "unit": unit},
                        "referenceRange": [{"low": {"value": 3, "unit": range_unit},
                                            "high": {"value": 5, "unit": range_unit}}]})
    path = tmp_path / "synthetic-mixed-units.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in records]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"
    reply = areas(plan(fake_ctx))
    for item in (reply["own_numbers"]["items"][0], reply["actionable"]["items"][0]["your_data"][0]):
        assert item["date"] == "2026-06-01"
        assert item["value"] == 4.5 and item["unit"] == "mmol/L"
        assert item["range_flag"] == flag
    # Existing trend callers retain their dominant-unit plotting behavior.
    usual = ops.lab_series(vault, "13457-7", ops.policy_for(fake_ctx))
    assert all(p["unit"] == "mg/dL" for p in usual["points"])
    assert not usual["unplotted_points"]


def test_fix_round1_evidence_is_marker_specific(vault, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs900073", "CYP2C19", 0.5)])
    item = areas(plan(fake_ctx))["actionable"]["items"][0]
    assert item["evidence"] == "weak"


@pytest.mark.parametrize("marker,target,tier", [("rs4244285", None, "guideline"),
                                               ("rs900074", "rs4244285", "guideline"),
                                               ("rs6923761", None, "research"),
                                               ("rs900075", "rs6923761", "research")])
def test_fix_round1_evidence_pack_markers_and_aliases(vault, fake_ctx, tmp_path, marker, target, tier):
    report(vault, tmp_path, [finding(marker, "FAKE", 0.5)])
    with write_txn(vault):
        vault.execute("UPDATE annotation_snapshot SET category='carrier'")
        if target:
            vault.execute("INSERT INTO marker_alias VALUES (?,?,'synthetic')", (marker, target))
    ops.capture_tool(fake_ctx, {"op": "consent", "category": "carrier", "answer": "yes"})
    category = areas(plan(fake_ctx, max_items=25))["carrier_and_screening"]["items"][0]
    item = next(i for i in category["items"] if i["marker"] == marker)
    assert item["evidence"] == tier


def test_fix_round1_sensitive_pack_keeps_own_pgx_gate(populated, fake_ctx):
    with write_txn(populated):
        populated.execute("UPDATE annotation_snapshot SET category='carrier' WHERE marker='rs4149056'")
    ops.capture_tool(fake_ctx, {"op": "consent", "category": "carrier", "answer": "yes"})
    fake_ctx.config["hidden_categories"] = ["pgx"]
    reply = plan(fake_ctx, max_items=25)
    assert not any(i.get("marker") == "SLCO1B1" for c in areas(reply)["carrier_and_screening"]["items"]
                   for i in c.get("items", []))
    assert not areas(reply)["gene_drug"]["items"]
    assert reply["withheld"] > 0


# PR #45 / U4b-fix-round2: append-only regressions against a237ddc.
def test_fix_round2_different_person_warning_matches_genome(vault, fake_ctx, tmp_path):
    for seed in (101, 202):
        path = make_23andme(tmp_path / f"synthetic-person-{seed}.txt", n=1200, seed=seed,
                            inject={"rs4149056": "TC"})
        assert import_dtc(vault, sources.store_source(vault, path, "dtc")[0]).status == "succeeded"
    active = ops._need("genome.calls", "callsets")(vault)[0]
    assert any(c["same_person"] is False for c in json.loads(active["comparison_json"]))
    expected = ops.genome_tool(fake_ctx, {"op": "summary"})["warning"]
    reply = plan(fake_ctx)
    assert reply["warning"] == expected
    assert list(reply).index("warning") < list(reply).index("areas")
    assert "synthetic-person-202.txt" in reply["warning"]


def test_fix_round2_latest_tie_uses_observation_id(vault, fake_ctx, tmp_path):
    report(vault, tmp_path, [finding("rs900081", "LDLR", 3)])
    records = []
    for day, value, unit in [("2025-01-01", 90, "mg/dL"), ("2026-06-01", 80, "mmol/L"),
                              ("2026-06-01", 120, "mg/dL")]:
        records.append({"resourceType": "Observation", "status": "final",
                        "code": {"text": "Synthetic tied LDL", "coding": [{"system": "http://loinc.org", "code": "13457-7"}]},
                        "effectiveDateTime": day, "valueQuantity": {"value": value, "unit": unit},
                        "referenceRange": [{"high": {"value": 100, "unit": "mg/dL"}}]})
    path = tmp_path / "synthetic-tied-labs.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": r} for r in records]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"
    reply = areas(plan(fake_ctx))
    for item in (reply["own_numbers"]["items"][0], reply["actionable"]["items"][0]["your_data"][0]):
        assert item["date"] == "2026-06-01"
        assert item["value"] == 120 and item["unit"] == "mg/dL"
        assert item["range_flag"] == "above"


def test_fix_round2_category_queries_are_batched(vault, fake_ctx, tmp_path, monkeypatch):
    report(vault, tmp_path, [finding(f"rs{910000+i}", "FAKE", 1) for i in range(128)])
    queries = []
    open_vault = ops.open_vault
    def traced_open():
        conn = open_vault()
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(ops, "open_vault", traced_open)
    reply = plan(fake_ctx)
    assert areas(reply)["actionable"]["more"] == 120
    scans = [q for q in queries if "annotation_snapshot" in q.lower() and q.lstrip().upper().startswith("SELECT")]
    assert len(scans) <= 3, scans[:5]
    assert not any("where marker=" in q.lower() for q in scans)


@pytest.mark.parametrize("retirement_signal", ["evidence", "status", "both"])
def test_fix_round2_retired_cpic_label_limit_and_marker_tier(vault, fake_ctx, tmp_path, monkeypatch, retirement_signal):
    import copy
    pack = copy.deepcopy(ops._pack())
    entry = next(e for e in pack.cpic if e["gene"] == "IFNL3")
    if retirement_signal == "evidence":
        entry["guideline"]["status"] = "active"
    if retirement_signal == "status":
        entry["evidence"] = "CPIC A"
    monkeypatch.setattr(ops, "_pack", lambda: pack)
    path = make_23andme(tmp_path / "synthetic-ifnl3.txt", n=2, inject={"rs12979860": "TT"})
    assert import_dtc(vault, sources.store_source(vault, path, "dtc")[0]).status == "succeeded"
    # Alias keeps this finding in actionable while tying its evidence to the pack marker.
    report(vault, tmp_path, [finding("rs900082", "IFNL3", 0.5)])
    with write_txn(vault):
        vault.execute("INSERT INTO marker_alias VALUES ('rs900082','rs12979860','synthetic')")
    reply = areas(plan(fake_ctx, max_items=25))
    item = next(i for i in reply["gene_drug"]["items"] if i["gene"] == "IFNL3")
    assert item["status"] == "flag" and item["evidence"] == "CPIC retired"
    assert item["limits"] == entry["limits"]
    annotation = next(i for i in reply["actionable"]["items"] if i["marker"] == "rs900082")
    assert annotation["evidence"] == "research"


@pytest.mark.parametrize("origin", ["user", "note"])
def test_u6_medication_words_naming_hidden_topic_stay_off_record(populated, fake_ctx, origin):
    words = "My anxiety got worse during pregnancy"
    if origin == "user":
        ops.capture_tool(fake_ctx, {"op": "medication", "drug": "simvastatin", "text": words,
                                    "date": "2026-02-07", "confirm": True})
    else:
        # FHIR note text is kept only in the search index, never in the medication row.
        from caduceus.health.fhir import ingest
        resource = {"resourceType": "MedicationRequest", "id": "synthetic-statin", "status": "active",
                    "medicationCodeableConcept": {"text": "simvastatin"}, "authoredOn": "2026-02-07",
                    "note": [{"text": words}]}
        with write_txn(populated):
            assert ingest(populated, resource, None)["medication"] == 1

    def statin(reply):
        return next(i for i in areas(reply)["gene_drug"]["items"] if "simvastatin" in i["drugs"])

    shown = plan(fake_ctx)
    assert sorted(m["date"] for m in statin(shown)["on_record"]) == ["2026-01-01", "2026-02-07"]
    fake_ctx.config.update(privacy_level="open", hidden_categories=["reproductive"])
    hidden = plan(fake_ctx)
    assert [m["date"] for m in statin(hidden)["on_record"]] == ["2026-01-01"]
    assert hidden.get("withheld", 0) == shown.get("withheld", 0) + 1
    assert "2026-02-07" not in json.dumps(hidden) and "anxiety" not in json.dumps(hidden).lower()


def test_session_plan_lists_hidden_areas_without_counts(populated, fake_ctx):
    """A hidden area looks the same whether or not the vault holds findings in it; the total still counts them."""
    sensitive = {i["category"]: i for i in areas(plan(fake_ctx))["sensitive"]["items"]}
    assert "alzheimers" in sensitive and "mental_health" not in sensitive
    fake_ctx.config["hidden_categories"] = ["alzheimers", "mental_health"]
    reply = plan(fake_ctx)
    hidden = {i["category"]: i for i in areas(reply)["sensitive"]["items"]}
    expected = {"consent": None, "ask_first": False, "hidden": True}
    assert hidden["alzheimers"] == {"category": "alzheimers", **expected}
    assert hidden["mental_health"] == {"category": "mental_health", **expected}
    assert reply["withheld"] >= sensitive["alzheimers"]["findings"]


def test_session_plan_lists_no_hidden_areas_without_dna(vault, samples, fake_ctx):
    fake_ctx.config["privacy_level"] = "standard"
    reply = areas(plan(fake_ctx))
    assert reply["sensitive"]["items"] == [] and reply["carrier_and_screening"]["items"] == []
