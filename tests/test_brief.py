"""The health brief: an empty vault, escaping, hidden categories, and value formatting."""
import json
from pathlib import Path

import pytest

from conftest import FakeCtx, load_plugin_package


@pytest.fixture
def call():
    ctx = FakeCtx()
    load_plugin_package().register(ctx)

    def run(tool, **args):
        return json.loads(ctx.tools[tool]["handler"](args))

    run.ctx = ctx
    return run


def _brief(call) -> str:
    result = call("caduceus_capture", op="brief")
    path = Path(result["path"])
    assert path.name.startswith("health-brief-") and not path.with_name(path.name + ".part").exists()
    return path.read_text(encoding="utf-8")


def test_an_empty_vault_says_what_to_do(call):
    page = _brief(call)
    assert "Nothing to show yet" in page and "talk to your prescriber" in page and "stored only here" in page


def test_recorded_text_is_escaped(call):
    call("caduceus_capture", op="medication", drug='Tonic <img src=x onerror="alert(1)"> & co', date="2026-05-01",
         confirm=True)
    page = _brief(call)
    assert "<img src=x" not in page and "&lt;img src=x" in page


def test_hidden_categories_are_counted_never_described(call):
    call("caduceus_capture", op="medication", drug="semaglutide", dose="0.5 mg weekly", date="2026-05-04",
         confirm=True)
    assert "Semaglutide" in _brief(call)
    call.ctx.config["hidden_categories"] = ["medication"]
    page = _brief(call)
    assert "semaglutide" not in page.lower() and "1 item hidden by your privacy settings" in page


def test_medication_table_has_no_empty_columns(call):
    call("caduceus_capture", op="medication", drug="metformin", date="2023-09-20", confirm=True)
    page = _brief(call)
    assert "<th>Brand</th>" not in page and "<th>Dose</th>" not in page and "<td>You</td>" in page


def test_a_medication_the_records_say_was_stopped_is_not_current(call, tmp_path):
    import json as _json

    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": {
        "resourceType": "MedicationStatement", "id": "s1", "status": "stopped",
        "medicationCodeableConcept": {"text": "Simvastatin 20 mg tablet"}, "effectivePeriod": {"start": "2024-02-01"}}}]}
    path = tmp_path / "records.json"
    path.write_text(_json.dumps(bundle))
    assert call("caduceus_capture", op="import", path=str(path))["status"] == "succeeded"
    page = _brief(call)
    assert "no longer taken; stop date unknown" in page
    assert "Genes and your medications" not in page  # nothing current to check


def test_a_month_only_start_never_breaks_the_brief(vault, samples, tmp_path):
    from datetime import date

    from caduceus.brief import write_brief
    from caduceus.core import sources
    from caduceus.core.policy import Policy
    from caduceus.health.apple import import_apple_health
    from caduceus.health.fhir import import_fhir
    from synth.apple import make_export

    export = tmp_path / "export.zip"
    make_export(export, days=90)
    assert import_apple_health(samples, vault, sources.store_source(vault, export, "apple_health")[0]).status == "succeeded"
    statement = {"resourceType": "MedicationStatement", "id": "m1", "status": "active",
                 "medicationCodeableConcept": {"text": "Ozempic 0.5 mg pen"}, "effectivePeriod": {"start": "2024-04"}}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(statement))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"
    # The latest start has only a month: it is listed as written, and never used as a chart pivot.
    page = write_brief(vault, samples, tmp_path / "out", policy=Policy(), today=date(2024, 4, 30)).read_text()
    assert "Weight, weekly average" in page and "<td>2024-04</td>" in page
