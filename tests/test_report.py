import json
import re
from html.parser import HTMLParser

import pytest

from caduceus.core import sources
from caduceus.core.contracts import SourceKind
from caduceus.core.db import write_txn
from caduceus.core.policy import Category, Policy
from caduceus.genome.promethease import import_promethease
from caduceus.genome.report import NoSuccessfulReport, _STYLE, build_report, parity
from synth.promethease import chunk, html_report, records, write_report


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags, self.text, self.data = [], [], []
        self.inside = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script":
            self.inside = attrs.get("id") == "caduceus-data"

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        self.text.append(data)
        if self.inside:
            self.data.append(data)

    def payload(self):
        return json.loads("".join(self.data))


def _import(vault, tmp_path):
    path = write_report(tmp_path / "synthetic-promethease.zip")
    src = sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]
    assert import_promethease(vault, src).status == "succeeded"
    return src


def test_report_is_self_contained_parseable_and_escaped(vault, tmp_path):
    src = _import(vault, tmp_path)
    path = build_report(vault, tmp_path / "out", policy=Policy())
    assert re.fullmatch(rf"variant-report-{src.sha256[:8]}-\d{{4}}-\d{{2}}-\d{{2}}\.html", path.name)
    html = path.read_text(encoding="utf-8")
    page = Page(html)
    payload = page.payload()
    assert payload["records"] == records() and len(payload["genotype_info"]) == 5
    assert payload["withheld"] == 0
    assert len([tag for tag, _ in page.tags if tag == "script"]) == 2
    assert not any(tag in ("img", "iframe", "link", "object", "embed") for tag, _ in page.tags)
    assert not any("src" in attrs or "href" in attrs or any(k.startswith("on") for k in attrs) for _, attrs in page.tags)
    assert "<script>document.write" not in html
    assert "&lt;script&gt;document.write(&#x27;inert-only&#x27;)&lt;/script&gt;" in html
    assert "</" not in "".join(page.data)
    assert "Rebuilt by Caduceus from synthetic-promethease.zip imported " in html
    assert "Annotations: SNPedia (CC BY-NC-SA 3.0) via your Promethease report." in html
    assert "Consumer chips miss rare variants and can be wrong." in html
    for title in ("Your variant report", "Highest magnitude", "Drug response", "Good news", "Worth a closer look"):
        assert title in html
    ids = {attrs.get("id") for _, attrs in page.tags}
    assert {"magnitude", "repute", "text", "theme"} <= ids
    assert "prefers-color-scheme:light" in html and "body.dark" in html


def test_standard_policy_omits_withheld_records_and_maps_entirely(vault, tmp_path):
    src = _import(vault, tmp_path)
    path = build_report(vault, tmp_path / "standard", source_id=src.id, policy=Policy(level="standard"))
    html = path.read_text()
    payload = Page(html).payload()
    assert payload["withheld"] == 1 and len(payload["records"]) == 5
    assert len(payload["genotype_info"]) == 4
    assert "Sealed orchard" not in html and "APOE" not in html and "rs99999002" not in html
    assert "1 records withheld by your privacy settings" in html
    unlocked = Policy(level="standard", unlocked=frozenset({Category.ALZHEIMERS}))
    opened = build_report(vault, tmp_path / "unlocked", policy=unlocked, source_id=src.id)
    assert len(Page(opened.read_text()).payload()["records"]) == 6


def test_explicit_hidden_categories_override_open_policy(vault, tmp_path):
    _import(vault, tmp_path)
    path = build_report(vault, tmp_path / "hidden", policy=Policy(hidden=frozenset({Category.PGX})))
    payload = Page(path.read_text()).payload()
    assert payload["withheld"] == 1
    assert all(r.get("rsnum") != "rs99999003" for r in payload["records"])
    assert all(r["marker"] != "rs99999003" for r in payload["genotype_info"])


def test_source_name_and_table_cells_are_escaped(vault, tmp_path):
    src = _import(vault, tmp_path)
    with write_txn(vault):
        vault.execute("UPDATE source_artifact SET original_name=? WHERE id=?", ('<img src=x onerror="boom">.zip', src.id))
        vault.execute("UPDATE annotation_snapshot SET marker=? WHERE source_id=? AND ordinal=0",
                      ('<script>bad()</script>', src.id))
    html = build_report(vault, tmp_path / "escaped", source_id=src.id, policy=Policy()).read_text()
    page = Page(html)
    assert not any(tag == "img" for tag, _ in page.tags)
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;" in html


def test_parity_decoded_stored_and_rebuilt(vault, tmp_path):
    src = _import(vault, tmp_path)
    checked = parity(vault, src.id)
    assert checked["ok"] and checked["diffs"] == []
    assert checked["decoded"] == checked["stored"] == checked["rebuilt"]
    assert checked["decoded"]["by_kind"] == {"snp": 4, "genoset": 1, "other": 1, "genotype_info": 5}
    assert ("gsSynthetic1", "") in checked["decoded"]["pairs"]
    path = build_report(vault, tmp_path / "out", policy=Policy(), source_id=src.id)
    assert parity(vault, src.id, report_path=path)["ok"]


@pytest.mark.parametrize("column,value,key", [("magnitude", 9, "magnitude_hist"),
                                               ("repute", "Bad", "repute_hist"),
                                               ("marker", "rs99999888", "pairs"),
                                               ("kind", "other", "by_kind")])
def test_parity_detects_tampered_normalized_row(vault, tmp_path, column, value, key):
    src = _import(vault, tmp_path)
    with write_txn(vault):
        vault.execute(f"UPDATE annotation_snapshot SET {column}=? WHERE source_id=? AND ordinal=0", (value, src.id))
    checked = parity(vault, src.id)
    assert not checked["ok"] and f"decoded vs stored: {key}" in checked["diffs"]


def test_parity_detects_tampered_record_json_and_missing_rows(vault, tmp_path):
    src = _import(vault, tmp_path)
    tampered = records()[0]
    tampered["magnitude"] = 9
    with write_txn(vault):
        vault.execute("UPDATE annotation_snapshot SET record_json=? WHERE source_id=? AND ordinal=0",
                      (json.dumps(tampered), src.id))
    checked = parity(vault, src.id)
    assert not checked["ok"] and "decoded vs rebuilt: magnitude_hist" in checked["diffs"]
    with write_txn(vault):
        vault.execute("DELETE FROM annotation_snapshot WHERE source_id=? AND kind='genotype_info'", (src.id,))
    assert "decoded vs stored: by_kind" in parity(vault, src.id)["diffs"]


def test_parity_detects_filtered_or_invalid_report(vault, tmp_path):
    src = _import(vault, tmp_path)
    path = build_report(vault, tmp_path / "standard", policy=Policy(level="standard"), source_id=src.id)
    assert not parity(vault, src.id, report_path=path)["ok"]
    path.write_text("<html>No data block</html>")
    checked = parity(vault, src.id, report_path=path)
    assert not checked["ok"] and checked["diffs"] == ["rebuilt: missing or invalid data block"]


def test_report_requires_a_promethease_source(vault, tmp_path):
    with pytest.raises(NoSuccessfulReport, match="No successfully imported Promethease report"):
        build_report(vault, tmp_path, policy=Policy())


def test_clinvar_privacy_probes_are_omitted_from_standard_report(vault, tmp_path):
    probes = [
        {"rsnum": "rs99999111", "genes": ["SORL1"], "clinvar_diseases": ["Alzheimer disease, late onset"]},
        {"rsnum": "rs99999222", "clinvar_1": "Hereditary breast and ovarian cancer syndrome"},
    ]
    maps = ({row["rsnum"]: {"(A;A)": {"mag": 1}} for row in probes}, {})
    path = write_report(tmp_path / "privacy.html", rows=[*probes, records()[0]], maps=maps)
    src = sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]
    assert import_promethease(vault, src).status == "succeeded"
    rows = vault.execute("SELECT category FROM annotation_snapshot WHERE kind='snp' ORDER BY ordinal").fetchall()
    assert [row[0] for row in rows] == ["alzheimers", "cancer_risk", "general"]
    html = build_report(vault, tmp_path / "out", policy=Policy(level="standard")).read_text()
    assert Page(html).payload()["withheld"] == 2
    for hidden in ("rs99999111", "rs99999222", "SORL1", "Alzheimer", "Hereditary breast"):
        assert hidden not in html
    assert len(Page(html).payload()["records"]) == 1
    assert Page(html).payload()["genotype_info"] == []


@pytest.mark.parametrize("html", ["<html>No data</html>", f"decompressString('{chunk(records())}');"])
def test_empty_import_fails_and_parity_never_passes_vacuously(vault, tmp_path, html):
    path = tmp_path / "empty.html"
    path.write_text(html)
    src = sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]
    result = import_promethease(vault, src)
    assert result.status == "failed" and "No records found in the report" in result.warnings
    assert sources.run_info(vault, result.run_id)["status"] == "failed"
    checked = parity(vault, src.id)
    assert not checked["ok"] and "decoded: No records found in the report" in checked["diffs"]
    if "decompressString" in html:
        assert any(diff.startswith("decoded warning: chunk 1:") for diff in checked["diffs"])


def test_parity_rejects_decode_warnings_even_if_surviving_sets_match(vault, tmp_path):
    path = write_report(tmp_path / "partial.html", bad_chunk=True)
    src = sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]
    assert import_promethease(vault, src).status == "succeeded"
    checked = parity(vault, src.id)
    assert checked["decoded"] == checked["stored"] == checked["rebuilt"]
    assert not checked["ok"] and checked["diffs"] == ["decoded warning: chunk 1: error"]


def test_theme_selectors_define_every_used_variable():
    used = set(re.findall(r"var\((--[\w-]+)\)", _STYLE))
    assert used == {"--bg", "--panel", "--ink", "--muted", "--line"}
    assert _STYLE.startswith(":root {")
    for selector in (":root", "body.light", "body.dark"):
        blocks = re.findall(re.escape(selector) + r"\s*\{([^{}]*)\}", _STYLE)
        assert blocks
        for block in blocks:
            assert used <= set(re.findall(r"(--[\w-]+)\s*:", block))


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
def test_default_report_ignores_newer_partial_import(vault, tmp_path, status):
    complete = _import(vault, tmp_path)
    path = write_report(tmp_path / "newer.html", rows=[records()[0]], maps=({}, {}))
    partial = sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]
    run = sources.begin_import(vault, partial, "promethease", "1")
    with write_txn(vault):
        vault.execute(
            "INSERT INTO annotation_snapshot(source_id, ordinal, kind, record_json) VALUES (?,0,'other','{}')", (partial.id,)
        )
    if status != "running":
        sources.finish_import(vault, run.id, counts={}, status=status)
    report = build_report(vault, tmp_path / "default", policy=Policy())
    assert Page(report.read_text()).payload()["source_id"] == complete.id
    # A source with snapshots alone is insufficient when no successful run remains.
    with write_txn(vault):
        vault.execute("UPDATE import_run SET status='failed' WHERE source_id=?", (complete.id,))
    with pytest.raises(NoSuccessfulReport, match="^No successfully imported Promethease report$"):
        build_report(vault, tmp_path / "no-complete", policy=Policy())


@pytest.mark.parametrize("spelling", ["metabolizer", "metaboliser"])
def test_metabolizer_and_pgx_gene_records_reach_drug_response_section(vault, tmp_path, spelling):
    rows = [{"rsnum": "rs99999111", "genosummary": f"Invented ultra-rapid {spelling}"},
            {"rsnum": "rs99999222", "genes": ["CYP2C19"], "genosummary": "Invented neutral text"}]
    path = write_report(tmp_path / "pgx.html", rows=rows, maps=({}, {}))
    src = sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]
    assert import_promethease(vault, src).status == "succeeded"
    html = build_report(vault, tmp_path / "out", policy=Policy()).read_text()
    section = re.search(r"<article><h2>Drug response</h2>(.*?)</article>", html, re.S)[1]
    assert "rs99999111" in section and "rs99999222" in section
