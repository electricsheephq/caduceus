"""0.2.2: a repute value from genome results goes straight back into the filter, in any casing (#14)."""
import json

import pytest

from conftest import FakeCtx, load_plugin_package
from synth import persona


@pytest.fixture
def plugin(tmp_path):
    ctx = FakeCtx()
    load_plugin_package().register(ctx)

    def call(tool, **args):
        return json.loads(ctx.tools[tool]["handler"](args))

    files = persona.write_persona(tmp_path / "persona", markers=200)
    result = call("caduceus_capture", op="import", path=str(files["report"]))
    assert result.get("status") == "succeeded", result
    return ctx, call


def _markers(results):
    return sorted(r["marker"] for r in results)


def test_results_use_the_values_the_filter_accepts(plugin):
    ctx, call = plugin
    everything = call("caduceus_genome", op="filter", limit=100)["results"]
    shown = {r["repute"] for r in everything}
    assert {"good", "bad"} <= shown <= {"good", "bad", "neutral"}  # the synthetic report holds both
    summary = call("caduceus_genome", op="summary")["top_findings"]
    assert {r["repute"] for r in summary} <= {"good", "bad", "neutral"}  # the same rows, where any rank high
    for value in sorted(shown):
        back = call("caduceus_genome", op="filter", repute=value, limit=100)
        assert "error" not in back, back
        assert {r["repute"] for r in back["results"]} == {value}
        assert _markers(back["results"]) == _markers(r for r in everything if r["repute"] == value)
    repute_schema = ctx.tools["caduceus_genome"]["schema"]["parameters"]["properties"]["repute"]
    assert "enum" not in repute_schema  # Hermes validates deferred calls against the schema; the handler checks values
    assert all(value in repute_schema["description"] for value in ("good", "bad", "neutral"))


@pytest.mark.parametrize("spelling", ["Bad", "bad", "BAD", " bad "])
def test_any_casing_filters_the_same(plugin, spelling):
    _, call = plugin
    want = call("caduceus_genome", op="filter", repute="bad", limit=100)["results"]
    assert want and call("caduceus_genome", op="filter", repute=spelling, limit=100)["results"] == want


@pytest.mark.parametrize("value", ["terrible", "goodish", "none", 3, ["bad"], True])
def test_an_unknown_repute_is_an_error_not_an_unfiltered_list(plugin, value):
    _, call = plugin
    out = call("caduceus_genome", op="filter", repute=value)
    assert out.get("error", "").startswith("repute must be good, bad or neutral"), out
    assert "results" not in out


def test_an_empty_repute_still_means_no_repute_filter(plugin):
    _, call = plugin
    everything = call("caduceus_genome", op="filter", limit=100)["results"]
    assert call("caduceus_genome", op="filter", repute="", limit=100)["results"] == everything
    assert call("caduceus_genome", op="filter", repute="  ", limit=100)["results"] == everything


def test_help_says_the_commands_run_in_chat(plugin):
    ctx, _ = plugin
    text = ctx.commands["caduceus"]["handler"]("help")
    assert "Hermes chat" in text and "no `hermes caduceus` shell command" in text
