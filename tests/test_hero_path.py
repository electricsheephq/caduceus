"""The demo's hero path end to end through the registered tools, on the synthetic persona.

"Why isn't my Ozempic working?" draws on three local sources: the genotype, the weight curve since the
recorded start date, and the lab results. "Would Mounjaro be different?" checks the drug-specific genetics.
"""
import json
import re
from pathlib import Path

import pytest

from conftest import FakeCtx, load_plugin_package
from synth import persona


@pytest.fixture
def plugin(tmp_path):
    ctx = FakeCtx()
    load_plugin_package().register(ctx)

    def call(tool, **args):
        return json.loads(ctx.tools[tool]["handler"](args))

    files = persona.write_persona(tmp_path / "persona", markers=2000)
    for kind in ("dna", "report", "apple"):
        result = call("caduceus_capture", op="import", path=str(files[kind]))
        assert result.get("status") == "succeeded" and not result.get("warnings"), result
    return ctx, call


# A page that loads anything from the network (script, style, image, frame, font) would leak that it was opened.
_EXTERNAL_LOAD = re.compile(r"<(?:script|link|img|iframe|source|video|audio)\b[^>]*\b(?:src|href)\s*=\s*[\"']?\s*(?:https?:)?//"
                            r"|url\(\s*[\"']?\s*(?:https?:)?//|@import", re.I)


def _by_marker(result):
    return {m["rsid"]: entry for entry in result["entries"] for m in entry.get("markers") or ()}


def test_why_isnt_my_ozempic_working(plugin):
    _, call = plugin
    looked = call("caduceus_genome", op="lookup", rsids=["rs6923761", "rs10305420", "rs1800437"])
    assert {r["marker"]: r["genotype"] for r in looked["results"]} == {
        "rs6923761": "AA", "rs10305420": "CC", "rs1800437": "GG"}

    ozempic = call("caduceus_genome", op="pgx", drug="Ozempic")
    entries = _by_marker(ozempic)
    assert entries["rs6923761"]["status"] == "flag" and entries["rs6923761"]["your_genotypes"] == {"rs6923761": "AA"}
    assert entries["rs10305420"]["status"] == "no_flag"
    assert "rs1800437" not in entries                 # the GIPR signal is tirzepatide-only
    assert all(e["evidence"] == "research" for e in ozempic["entries"])
    assert ozempic["disclaimer"]

    weight = call("caduceus_trend", op="compare", metric="weight", drug="Ozempic", after_days=84)
    assert weight["pivot"] == persona.START.isoformat()
    assert weight["after"]["mean"] < weight["before"]["mean"] - 1.5
    since = call("caduceus_trend", op="compare", metric="weight", drug="Ozempic")  # the window runs to the latest data
    assert since["after"]["last"]["day"] == persona.LAST_DAY.isoformat()
    first, second = since["after"]["halves"]
    assert first["weekly_slope"] < -0.2 and abs(second["weekly_slope"]) < 0.1   # the loss, then the plateau
    a1c = call("caduceus_trend", op="labs", lab="A1c")
    assert [p["value"] for p in a1c["points"]] == [8.1, 8.4, 7.9, 7.8] and a1c["unit"] == "%"


def test_would_mounjaro_be_different(plugin):
    _, call = plugin
    entries = _by_marker(call("caduceus_genome", op="pgx", drug="Mounjaro 5 mg/0.5 mL"))
    assert entries["rs1800437"]["status"] == "no_flag"     # no GIPR vomiting flag
    assert entries["rs10305420"]["status"] == "no_flag"
    assert "rs6923761" not in entries


def test_report_and_brief_are_local_pages(plugin):
    _, call = plugin
    report = call("caduceus_genome", op="report")
    page = Path(report["path"]).read_text(encoding="utf-8")
    assert report["file_url"].startswith("file://") and "Two Ser168 copies" in page
    assert not _EXTERNAL_LOAD.search(page)

    brief = call("caduceus_capture", op="brief")
    page = Path(brief["path"]).read_text(encoding="utf-8")
    assert not _EXTERNAL_LOAD.search(page)
    for text in ("Ozempic (semaglutide)", "Weight, weekly average", "HbA1c", "GLP1R", "SLCO1B1",
                 "talk to your prescriber", "stored only here"):
        assert text in page, text
    assert "ozempic (semaglutide) 0.5 mg/dose pen start" not in page  # chart labels use the short name


def test_standard_privacy_hides_apoe_everywhere(plugin):
    ctx, call = plugin
    ctx.config["privacy_level"] = "standard"
    looked = call("caduceus_genome", op="lookup", rsids=["rs429358", "rs7412", "rs6923761"])
    assert [r["marker"] for r in looked["results"]] == ["rs6923761"] and looked["withheld"] == 2
    derived = call("caduceus_genome", op="derived")
    assert all("apoe" not in json.dumps(d).lower() for d in derived["derived"])
    page = Path(call("caduceus_genome", op="report")["path"]).read_text(encoding="utf-8")
    assert "APOE e3/e3" not in page
    found = call("caduceus_search", query="APOE")
    assert all("APOE" not in json.dumps(r) for r in found["results"])
