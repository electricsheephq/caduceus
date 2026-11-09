"""Tool replies remain explicit without loading the guide; all records here are synthetic."""
import json
from datetime import date

import pytest

from conftest import FakeCtx, load_plugin_package
from caduceus.core import search
from caduceus.core.db import write_txn
from caduceus.core.policy import Policy
from caduceus.health.trends import compare_windows, trend
from synth import persona


GUIDANCE = ("Information with limits, not medical advice. Never suggest starting, stopping or changing a medication "
            "or its dose; for any medication decision, say to talk to the prescriber.")
QUERY_SCOPE = ('These results match your query; they are not a complete list. To list every medication, '
               'call caduceus_search with kinds=["medication"] and no query.')
DAYS = ["2026-02-02", "2026-04-30", "2026-05-04", "2026-06-01", "2026-07-29", "2026-11-07"]
DATA_RANGE = {"first_day": DAYS[0], "last_day": DAYS[-1]}


@pytest.fixture
def plugin():
    ctx = FakeCtx()
    load_plugin_package().register(ctx)

    def call(tool, **args):
        result = json.loads(ctx.tools[tool]["handler"](args))
        assert "error" not in result, result
        return result

    return ctx, call


@pytest.fixture
def series(samples):
    with write_txn(samples):
        samples.executemany("INSERT INTO daily_rollup VALUES (?,?,?,?,?,?)",
                            [("weight", day, 98.123, 1, '["Synthetic"]', "trend/1") for day in DAYS]
                            + [("steps", "2026-12-01", 4321, 1, '["Synthetic"]', "trend/1")])
    return samples


@pytest.fixture
def medications(plugin, tmp_path):
    _, call = plugin
    resources = [r for r in persona.clinical_resources() if r["resourceType"] == "MedicationStatement"]
    path = tmp_path / "synthetic-medications.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "type": "collection",
                                "entry": [{"resource": r} for r in resources]}))
    assert call("caduceus_capture", op="import", path=str(path))["status"] == "succeeded"
    return plugin


def test_compare_explains_actual_window_end_without_changing_coverage(plugin, series):
    _, call = plugin
    result = call("caduceus_trend", op="compare", metric="weight", pivot="2026-05-04", after_days=90)
    original = compare_windows(series, "weight", pivot="2026-05-04", before_days=28, after_days=90)
    assert {key: result[key] for key in original} == original
    assert result["coverage"]["last_day"] == "2026-07-29"  # the window ends after the last sample in it
    assert result["data_range"] == DATA_RANGE
    assert result["window_note"] == (
        "This window ends on 2026-08-01; your data continues to 2026-11-07. "
        "Leave out after_days to cover the data after the start (up to 365 days).")
    assert result["guidance"] == GUIDANCE
    assert "next_step" not in result  # a date-only pivot gives no drug-specific next step


def test_default_compare_reaches_latest_data_without_a_window_note(plugin, series):
    _, call = plugin
    result = call("caduceus_trend", op="compare", metric="weight", pivot="2026-05-04")
    assert result["data_range"] == DATA_RANGE
    assert result["coverage"]["last_day"] == DAYS[-1]
    assert result["before"]["n_days"] == 1  # the existing 28-day default stays in place
    assert "window_note" not in result


def test_default_compare_reports_the_365_day_cap(plugin, series):
    _, call = plugin
    result = call("caduceus_trend", op="compare", metric="weight", pivot="2025-01-01")
    assert result["data_range"] == DATA_RANGE
    assert result["window_note"] == (
        "This window ends on 2025-12-31; your data continues to 2026-11-07. "
        "A compare covers at most 365 days after the start by default; use op=series for the later data.")


def test_drug_compare_guidance_uses_only_the_trimmed_argument_and_hidden_start_stays_hidden(medications, series):
    ctx, call = medications
    result = call("caduceus_trend", op="compare", metric="weight", drug="  Ozempic  ", after_days=90)
    assert result["guidance"] == GUIDANCE
    assert result["next_step"] == "For gene–drug evidence about Ozempic, call caduceus_genome op=pgx drug=Ozempic."
    text = result["guidance"] + result["next_step"]
    for stored in ("98.123", "0.5 mg", "2026-05-04", "rs6923761", "AA"):
        assert stored not in text
    ctx.config["hidden_categories"] = ["medication"]
    assert call("caduceus_trend", op="compare", metric="weight", drug="Ozempic") == {
        "metric": "weight", "withheld": 1, "withheld_note": "hidden by your privacy settings"}


@pytest.mark.parametrize("end", [None, "2026-07-31", "2026-11-07", "2026-12-31"])
def test_series_range_is_separate_from_coverage_and_only_explicit_earlier_end_gets_a_note(plugin, series, end):
    _, call = plugin
    args = {"end": end} if end else {}
    result = call("caduceus_trend", op="series", metric="weight", bucket="day", **args)
    original = trend(series, "weight", bucket="day", end=date.fromisoformat(end) if end else None)
    assert {key: result[key] for key in original} == original
    assert result["data_range"] == DATA_RANGE
    if end == "2026-07-31":
        assert result["window_note"] == "This series ends on 2026-07-31; your data continues to 2026-11-07."
    else:
        assert "window_note" not in result
    assert "guidance" not in result and "next_step" not in result


def test_series_involving_a_drug_carries_guidance_and_hidden_wellness_leaks_no_range(plugin, series):
    ctx, call = plugin
    result = call("caduceus_trend", op="series", metric="weight", drug="Ozempic")
    assert result["guidance"] == GUIDANCE
    ctx.config["hidden_categories"] = ["wellness"]
    for op in ("series", "compare"):
        assert call("caduceus_trend", op=op, metric="weight", drug="Ozempic") == {
            "withheld": 1, "withheld_note": "hidden by your privacy settings"}


def test_empty_metric_has_no_data_range_but_compare_still_has_guidance(plugin):
    _, call = plugin
    result = call("caduceus_trend", op="compare", metric="weight", pivot="2026-05-04")
    assert result["guidance"] == GUIDANCE
    assert "data_range" not in result and "window_note" not in result
    series = call("caduceus_trend", op="series", metric="weight", end="2026-07-31")
    assert "data_range" not in series and "window_note" not in series


def test_medication_query_and_fetch_carry_guidance_and_query_is_not_a_full_list(medications):
    _, call = medications
    result = call("caduceus_search", query="Ozempic")
    assert len(result["results"]) == 1 and result["results"][0]["kind"] == "medication"
    assert result["guidance"] == GUIDANCE
    assert result["scope_note"] == QUERY_SCOPE
    fetched = call("caduceus_search", ref=result["results"][0]["ref"])
    assert fetched["guidance"] == GUIDANCE
    assert "scope_note" not in fetched


def test_nonmedication_query_has_scope_note_and_empty_query_match_keeps_existing_note(plugin):
    _, call = plugin
    call("caduceus_capture", op="journal", text="Synthetic walking journal", date="2026-05-01")
    found = call("caduceus_search", query="walking")
    assert found["scope_note"] == QUERY_SCOPE and "guidance" not in found
    empty = call("caduceus_search", query="unmatchedword")
    assert empty["note"] == "Nothing matched. Try fewer or different words, or import more data."
    assert "scope_note" not in empty and "guidance" not in empty


def test_browse_lists_all_three_persona_medications_newest_first_and_honors_limit(medications):
    _, call = medications
    result = call("caduceus_search", query="  ", kinds=["medication"])
    assert [r["title"] for r in result["results"]] == [persona.MEDICATIONS[i][0].lower() for i in (0, 2, 1)]
    assert result["guidance"] == GUIDANCE
    assert result["scope_note"] == "This lists your recorded medication, newest first, up to 8."
    limited = call("caduceus_search", kinds=["medication"], limit=1)
    assert limited["results"] == result["results"][:1]
    assert limited["scope_note"] == "This lists your recorded medication, newest first, up to 1."


@pytest.mark.parametrize("hidden,visible_titles,withheld", [
    (["mental_health"], ["Visible synthetic medication"], 1),
    (["medication"], [], 2),
])
def test_browse_filters_both_kind_and_item_categories_and_counts_hidden_rows(plugin, vault, hidden,
                                                                          visible_titles, withheld):
    ctx, call = plugin
    with write_txn(vault):
        search.index_doc(vault, kind="medication", ref="visible", title="Visible synthetic medication",
                         body="Synthetic body", category="medication", ts="2026-05-01")
        search.index_doc(vault, kind="medication", ref="hidden", title="Hidden synthetic medication",
                         body="Private synthetic body", category="mental_health", ts="2026-06-01")
        search.index_doc(vault, kind="journal", ref="unrelated", title="Unrelated synthetic note",
                         body="Synthetic body", category="wellness", ts="2026-07-01")
    ctx.config["hidden_categories"] = hidden
    result = call("caduceus_search", kinds=["medication"], limit=1)
    assert [r["title"] for r in result["results"]] == visible_titles
    assert result["withheld"] == withheld
    assert result["scope_note"] == "This lists your recorded medication, newest first, up to 1."
    assert "Hidden synthetic medication" not in json.dumps(result)
    assert ("guidance" in result) == bool(visible_titles)


def test_browse_multiple_kinds_and_unknown_kind_do_not_widen_listing(vault):
    with write_txn(vault):
        for kind, category, day in [("journal", "wellness", "2026-05-01"),
                                     ("medication", "medication", "2026-05-02"),
                                     ("condition", "records", "2026-05-03")]:
            search.index_doc(vault, kind=kind, ref=kind, title=kind, body="Synthetic record", category=category, ts=day)
    result = search.browse(vault, policy=Policy(), kinds=["journal", "medication"], limit=25)
    assert [r["kind"] for r in result["results"]] == ["medication", "journal"]
    assert all(set(r) == {"ref", "kind", "title", "snippet", "category", "date"} for r in result["results"])
    assert search.browse(vault, policy=Policy(), kinds=["invalid"])["results"] == []


def test_empty_query_still_requires_kinds_or_ref(plugin):
    ctx, call = plugin
    # An explicit kind listing is supported even when the vault is empty.
    result = call("caduceus_search", kinds=["medication"])
    assert result["results"] == []
    assert result["scope_note"] == "This lists your recorded medication, newest first, up to 8."
    for args in ({}, {"query": "  "}, {"query": "", "kinds": []}):
        error = json.loads(ctx.tools["caduceus_search"]["handler"](args))
        assert error == {"error": "give a query, or a ref to fetch"}
    # A misspelt kind is an error, never an empty listing that reads as "you have none".
    error = json.loads(ctx.tools["caduceus_search"]["handler"]({"kinds": ["medications"]}))
    assert error["error"].startswith("unknown kinds; use one of: annotation, ")
    mixed = call("caduceus_search", kinds=["medication", "medications"])
    assert mixed["scope_note"] == "This lists your recorded medication, newest first, up to 8."


def test_registered_descriptions_explain_medication_workflow_without_changing_schemas(plugin):
    from caduceus.tools import TOOLS

    ctx, _ = plugin
    descriptions = {name: tool["schema"]["description"] for name, tool in ctx.tools.items()}
    assert "For medication questions, op=pgx gives the user's own gene–drug evidence." in descriptions["caduceus_genome"]
    assert ("For a question about how a medication is working, also call caduceus_genome op=pgx for that drug."
            in descriptions["caduceus_trend"])
    assert ("Leave out after_days to cover the data after the start, up to a year; before_days defaults to 28 days."
            in descriptions["caduceus_trend"])
    for name in ("caduceus_trend", "caduceus_genome"):
        assert descriptions[name].count(GUIDANCE) == 1
    for tool in TOOLS:
        schema = ctx.tools[tool["name"]]["schema"]
        assert schema == tool
        params = schema["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False
        assert set(params["required"]) <= params["properties"].keys()
    assert set(ctx.tools) == {"caduceus_search", "caduceus_genome", "caduceus_trend", "caduceus_capture"}
