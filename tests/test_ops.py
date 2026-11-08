"""The tool glue through the real registration path: JSON in and out, privacy, confirmations, errors."""
import json

import pytest

from conftest import FakeCtx, load_plugin_package


@pytest.fixture
def plugin():
    ctx = FakeCtx()
    load_plugin_package().register(ctx)

    def call(tool, **args):
        return json.loads(ctx.tools[tool]["handler"](args))

    def command(raw=""):
        return ctx.commands["caduceus"]["handler"](raw)

    return ctx, call, command


def test_journal_is_saved_and_searchable(plugin):
    _, call, _ = plugin
    saved = call("caduceus_capture", op="journal", text="Felt nauseous after the second injection", date="2026-05-03")
    assert saved == {"saved": True, "date": "2026-05-03", "category": "wellness"}
    found = call("caduceus_search", query="nauseous injection")
    assert [r["kind"] for r in found["results"]] == ["journal"]
    full = call("caduceus_search", ref=found["results"][0]["ref"])
    assert "second injection" in full["body"]


def test_medication_needs_confirmation_then_records_once(plugin):
    _, call, _ = plugin
    preview = call("caduceus_capture", op="medication", drug="semaglutide", dose="0.25 mg weekly", date="2026-05-01")
    assert preview["needs_confirmation"] is True and preview["preview"]["date"] == "2026-05-01"
    assert call("caduceus_capture", op="settings")["counts"]["medications"] == 0
    first = call("caduceus_capture", op="medication", drug="semaglutide", dose="0.25 mg weekly", date="2026-05-01",
                 confirm=True)
    again = call("caduceus_capture", op="medication", drug="semaglutide", date="2026-05-01", confirm=True)
    assert first["recorded"] is True and again["already_recorded"] is True
    assert call("caduceus_capture", op="settings")["counts"]["medications"] == 1
    assert call("caduceus_search", query="semaglutide", kinds=["medication"])["results"]


def test_bad_inputs_are_clear_errors_not_internal_errors(plugin, tmp_path):
    _, call, _ = plugin
    assert "absolute path" in call("caduceus_capture", op="import", path="relative/file.zip")["error"]
    assert "no file" in call("caduceus_capture", op="import", path=str(tmp_path / "missing.zip"))["error"]
    odd = tmp_path / "notes.txt"
    odd.write_text("just some notes\n")
    assert "not a recognised file" in call("caduceus_capture", op="import", path=str(odd))["error"]
    for tool in ("caduceus_genome", "caduceus_trend", "caduceus_capture"):
        assert call(tool, op="nope")["error"].startswith("op must be one of")
    assert "ISO date" in call("caduceus_capture", op="medication", drug="x", date="May 1")["error"]
    assert "give a query" in call("caduceus_search")["error"]


def test_privacy_is_changed_only_by_the_user_command(plugin):
    ctx, call, command = plugin
    call("caduceus_capture", op="journal", text="Read about Alzheimer risk and APOE today")
    assert call("caduceus_search", query="APOE")["results"]          # open (the default) shows everything
    assert "standard" in command("privacy standard")
    hidden = call("caduceus_search", query="APOE")
    assert hidden["results"] == [] and hidden["withheld"] == 1
    assert "now shown" in command("unlock alzheimers")
    assert call("caduceus_search", query="APOE")["results"]
    assert "now hidden" in command("hide alzheimers")
    assert call("caduceus_search", query="APOE")["withheld"] == 1
    assert "Unknown category" in command("hide cats")
    settings = call("caduceus_capture", op="settings")
    assert settings["privacy_level"] == "standard" and settings["hidden_categories"] == ["alzheimers"]
    assert ctx.config["hidden_categories"] == ["alzheimers"]


def test_status_command_and_help(plugin):
    _, _, command = plugin
    assert command("").startswith("Caduceus: 0 files")
    assert "unlock <category>" in command("help")
    assert command("imports") == "No imports yet."
