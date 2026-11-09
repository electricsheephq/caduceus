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


def test_confirming_a_new_dose_updates_the_record(plugin):
    _, call, _ = plugin
    call("caduceus_capture", op="medication", drug="metformin", dose="10 mg", date="2023-09-20", confirm=True)
    second = call("caduceus_capture", op="medication", drug="metformin", dose="20 mg", date="2023-09-20", confirm=True)
    assert second["updated"] is True and second["dose"] == "20 mg" and second["previous_dose"] == "10 mg"
    found = call("caduceus_search", query="metformin")["results"]
    assert len(found) == 1 and "20 mg" in call("caduceus_search", ref=found[0]["ref"])["body"]
    again = call("caduceus_capture", op="medication", drug="metformin", date="2023-09-20", confirm=True)
    assert again["already_recorded"] is True and again["dose"] == "20 mg"  # the stored dose, not "not given"


@pytest.mark.parametrize("tool,args,needle", [
    ("caduceus_genome", {"op": "lookup", "rsids": 42}, "rsids must be a list"),
    ("caduceus_genome", {"op": "filter", "min_magnitude": "high"}, "min_magnitude must be a number"),
    ("caduceus_genome", {"op": "filter", "limit": "ten"}, "limit must be a whole number"),
    ("caduceus_genome", {"op": "gene", "gene": "GLP1R", "limit": "x"}, "limit must be a whole number"),
    ("caduceus_search", {"query": "weight", "limit": "abc"}, "limit must be a whole number"),
    ("caduceus_trend", {"op": "compare", "metric": "weight", "pivot": "2026-05-01", "before_days": "x"},
     "before_days must be a whole number"),
    ("caduceus_trend", {"op": "compare", "metric": "weight", "pivot": "2026-05-01", "before_days": 10**9},
     "between 1 and 730"),
    ("caduceus_capture", {"op": "status", "run_id": "abc"}, "run_id must be a whole number"),
    ("caduceus_genome", {"op": "report"}, "No successfully imported"),
])
def test_wrong_argument_types_are_clear_errors(plugin, tool, args, needle):
    _, call, _ = plugin
    error = call(tool, **args).get("error", "")
    assert needle in error and "internal error" not in error


def test_unknown_commands_and_extra_words_change_nothing(plugin):
    ctx, _, command = plugin
    assert "Unknown command" in command("frobnicate")
    assert command("hide mental_health please").startswith("Usage")
    assert command("privacy standard extra").startswith("Usage")
    assert not ctx.config.get("hidden_categories") and ctx.config.get("privacy_level") is None


def test_a_month_only_start_cannot_be_a_pivot(plugin, tmp_path):
    _, call, _ = plugin
    statement = {"resourceType": "MedicationStatement", "id": "m1", "status": "active",
                 "medicationCodeableConcept": {"text": "Ozempic 0.5 mg pen"}, "effectivePeriod": {"start": "2024-03"}}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(statement))
    assert call("caduceus_capture", op="import", path=str(path))["status"] == "succeeded"
    error = call("caduceus_trend", op="compare", metric="weight", drug="semaglutide")["error"]
    assert "has no day (only 2024-03)" in error


def test_a_watch_result_is_delivered_once(plugin, monkeypatch):
    import importlib

    _, call, _ = plugin
    package = load_plugin_package()
    ops = importlib.import_module(package.__name__ + ".caduceus.ops")
    watch = importlib.import_module(package.__name__ + ".caduceus.research.watch")
    runs = iter(["first", "second", "third"])
    monkeypatch.setattr(watch, "run_watch", lambda *a, **k: {"new_items": [{"title": next(runs), "category": "research"}]})
    nested, arrived = [], []

    def submit(key, fn, wait_s):
        result = fn()  # the run finishes, and another watch_run arrives before this call has returned
        if not arrived:
            arrived.append(True)
            nested.append(call("caduceus_capture", op="watch_run"))
        return {"state": "done", "result": result}

    monkeypatch.setattr(ops.jobs, "submit", submit)
    outer = call("caduceus_capture", op="watch_run")
    titles = [item["title"] for result in (outer, nested[0]) for item in result.get("new_items", [])]
    assert sorted(titles) == ["first", "second"]


def test_booleans_are_not_numbers(plugin):
    _, call, _ = plugin
    error = call("caduceus_trend", op="compare", metric="weight", pivot="2026-05-01", before_days=True)["error"]
    assert "before_days must be a whole number" in error


def test_watch_results_respect_a_hidden_research_category(plugin):
    import importlib

    from caduceus.core.policy import Policy

    ops = importlib.import_module(load_plugin_package().__name__ + ".caduceus.ops")
    result = {"new_items": [{"title": "Invented APOE paper", "category": "alzheimers"},
                            {"title": "Invented GLP1R paper", "category": "research"}]}
    filtered = ops._filter_watch(result, Policy(hidden=frozenset({"research"})))
    assert filtered["new_items"] == [] and filtered["withheld"] == 2


def test_concurrent_reports_and_briefs_never_share_a_temporary_file(plugin):
    import threading

    _, call, _ = plugin
    errors = []

    def brief():
        result = call("caduceus_capture", op="brief")
        if "error" in result:
            errors.append(result["error"])

    threads = [threading.Thread(target=brief) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_late_watch_failure_is_reported_once_then_retry_uses_fake_transport(plugin, monkeypatch):
    import importlib
    import threading

    from test_watch_pairs import PairHttp

    _, call, _ = plugin
    package = load_plugin_package()
    ops = importlib.import_module(package.__name__ + ".caduceus.ops")
    watch = importlib.import_module(package.__name__ + ".caduceus.research.watch")
    transport = importlib.import_module(package.__name__ + ".caduceus.research.http")
    original = watch.run_watch
    release = threading.Event()
    monkeypatch.setattr(ops, "WATCH_WAIT_S", 0.01)
    monkeypatch.setattr(transport, "UrllibHttp", PairHttp)

    def fail_late(*args, **kwargs):
        assert release.wait(2)
        raise ValueError("synthetic path/URL detail must not escape")

    monkeypatch.setattr(watch, "run_watch", fail_late)
    try:
        first = call("caduceus_capture", op="watch_run")
        assert first["status"] == "running"
    finally:
        release.set()
        assert ops.jobs.join("research-watch", 2)
    second = call("caduceus_capture", op="watch_run")
    assert second == {"status": "failed", "error": "ValueError",
                      "note": "The last research watch run failed in the background after its call returned. "
                              "Nothing new was saved from it. Run the watch again."}
    assert ops._WATCH_PENDING == []
    monkeypatch.setattr(watch, "run_watch", original)
    monkeypatch.setattr(ops, "WATCH_WAIT_S", 10)
    third = call("caduceus_capture", op="watch_run")
    assert third.get("kind") == "seed" and third["errors"] == [], third
    assert ops._WATCH_PENDING == []
