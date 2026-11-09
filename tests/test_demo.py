"""Demo command acceptance through the command wrapper, using only invented data."""
import zipfile

import pytest

from caduceus import ops, tools
from caduceus.core import jobs, sources
from caduceus.core.db import open_samples, open_vault
from conftest import FakeCtx, load_plugin_package
from synth import persona

LOADING = ('Loading the demo persona (an invented person, not real data) in the background. '
           'It takes about a minute. Check with /caduceus imports. Then ask your agent: '
           '“Why isn\'t my Ozempic working?” or “Would Mounjaro be different for me?”')
ALREADY = 'The demo persona is already in this vault. Ask your agent: “Why isn\'t my Ozempic working?”'
NAMES = {"persona-23andme-v5.zip", "persona-variant-report.zip", "persona-apple-health.zip"}


def test_demo_empty_and_repeat(data_dir, monkeypatch):
    monkeypatch.setattr(ops, "DEMO_MARKERS", 300, raising=False)
    submitted, results = [], []
    submit = jobs.submit

    def tracked(key, fn, *, wait_s):
        assert wait_s <= 3
        submitted.append(key)

        def run():
            result = fn()
            results.append(result)
            return result

        return submit(key, run, wait_s=wait_s)

    monkeypatch.setattr(jobs, "submit", tracked)
    monkeypatch.setattr(ops, "check_user_path", lambda *a: pytest.fail("plugin-owned files need no user-path check"))
    try:
        assert tools.command(FakeCtx(), "demo") == LOADING
        assert len(submitted) == 1
    finally:
        for key in submitted:
            assert jobs.join(key, timeout=30), "demo background job did not finish"
    assert results and all(r["status"] == "succeeded" for r in results[0])
    assert {p.name for p in (data_dir / "demo-files").iterdir()} == NAMES
    vault, samples = open_vault(), open_samples()
    try:
        assert {r[0] for r in vault.execute("SELECT original_name FROM source_artifact")} == NAMES
        runs = sources.recent_runs(vault)
        assert len(runs) == 3 and all(r["status"] == "succeeded" for r in runs)
        counts = {r["kind"]: r["counts"] for r in runs}
        expected_markers = len(list(persona._filler(300, 7))) + len(persona.GENOTYPES)
        assert counts["dtc"]["markers"] == expected_markers
        assert vault.execute("SELECT count(*) FROM annotation_snapshot WHERE kind!='genotype_info'").fetchone()[0] == (
            len(persona.REPORT) + len(persona.GENOSETS))
        assert vault.execute("SELECT count(*) FROM condition").fetchone()[0] == len(persona.CONDITIONS)
        assert vault.execute("SELECT count(*) FROM medication_event").fetchone()[0] == len(persona.MEDICATIONS)
        assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == sum(len(lab[-1]) for lab in persona.LABS)
        with zipfile.ZipFile(data_dir / "demo-files/persona-apple-health.zip") as archive:
            records = archive.read("apple_health_export/export.xml").count(b"<Record ")
        assert counts["apple_health"]["records"] == counts["apple_health"]["samples"] == records
        assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == records
        before = {p: p.read_bytes() for p in (data_dir / "demo-files").iterdir()}
        assert tools.command(FakeCtx(), "demo") == ALREADY
        assert len(submitted) == 1
        assert {p: p.read_bytes() for p in before} == before
        assert vault.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == 3
    finally:
        samples.close()
        vault.close()


def test_demo_refuses_other_sources(data_dir, vault, tmp_path, monkeypatch):
    path = tmp_path / "invented-other.txt"
    path.write_text("invented unrelated source")
    sources.store_source(vault, path, "other")
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: pytest.fail("refusal must not submit a job"))
    before = {p: p.read_bytes() for p in data_dir.rglob("*") if p.is_file() and p.suffix not in (".db", "-wal", "-shm")}
    assert tools.command(FakeCtx(), "demo") == (
        "Your vault already holds 1 imported files, so the demo persona would mix with your data. "
        "To try the demo, create a separate Hermes profile (`hermes profile create caduceus-try`), "
        "install Caduceus in it, and run /caduceus demo there.")
    assert not (data_dir / "demo-files").exists()
    assert vault.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == 1
    assert all(p.read_bytes() == content for p, content in before.items())


def test_demo_help_and_hint():
    ctx = FakeCtx()
    load_plugin_package().register(ctx)
    assert "demo" in ctx.commands["caduceus"]["args_hint"]
    assert "demo" in tools.command(ctx, "help")


def test_demo_errors_are_wrapped(monkeypatch):
    def fail():
        raise ValueError("invented file contents must not be in the reply")

    monkeypatch.setattr(ops, "_demo", fail, raising=False)
    assert tools.command(FakeCtx(), "demo") == "Caduceus error: ValueError. See the gateway log."
