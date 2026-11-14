"""Demo command acceptance through the command wrapper, using only invented data."""
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from caduceus import ops, tools
from caduceus.core import jobs, sources
from caduceus.core.db import open_samples, open_vault, write_txn
from caduceus.genome import dtc
from conftest import FakeCtx, load_plugin_package
from synth import persona

LOADING = ('Loading the demo persona (an invented person, not real data) in the background. '
           'It takes about a minute. Check with /caduceus imports. Then ask your agent: '
           '“Why isn\'t my Ozempic working?” or “Would Mounjaro be different for me?”')
ALREADY = 'The demo persona is already in this vault. Ask your agent: “Why isn\'t my Ozempic working?”'
FINISHING = ('Finishing the demo persona\'s import in the background. Check with /caduceus imports, '
             'then ask your agent: “Why isn\'t my Ozempic working?”')
BUSY = 'The demo persona is still importing. Check with /caduceus imports.'
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
        assert len(runs) == 4 and all(r["status"] == "succeeded" for r in runs)  # Apple samples and clinical run separately
        counts = {r["parser"]: r["counts"] for r in runs}
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
        _add_records(vault, 1, 1)
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
        "Your vault already holds 1 imported file, so the demo persona would mix with your data. "
        "To try the demo, create a separate Hermes profile (`hermes profile create caduceus-try`), "
        "install Caduceus in it, and run /caduceus demo there.")
    assert not (data_dir / "demo-files").exists()
    assert vault.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == 1
    assert all(p.read_bytes() == content for p, content in before.items())


def _add_records(vault, medications, notes):
    with write_txn(vault):
        for i in range(medications):
            vault.execute("INSERT INTO medication_event(drug, event, occurred_on, created_at) "
                          "VALUES (?, 'start', '2026-01-01', '2026-01-01')", (f'invented-{i}',))
        for i in range(notes):
            vault.execute("INSERT INTO journal(ts, text, created_at) VALUES (?, ?, ?)",
                          ('2026-01-01', f'invented note {i}', '2026-01-01'))


@pytest.mark.parametrize("medications,notes", [(1, 0), (0, 1), (2, 3), (1, 1)])
def test_demo_refuses_hand_entered_records(data_dir, vault, monkeypatch, medications, notes):
    monkeypatch.setattr(ops, 'DEMO_MARKERS', 30)
    _add_records(vault, medications, notes)
    before = list(vault.iterdump())
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: pytest.fail("refusal must not submit a job"))
    med_word = 'medication' if medications == 1 else 'medications'
    note_word = 'note' if notes == 1 else 'notes'
    assert tools.command(FakeCtx(), "demo") == (
        f"Your vault already holds records you added ({medications} {med_word}, {notes} {note_word}), "
        "so the demo persona would mix with your data. To try the demo, create a separate Hermes profile "
        "(`hermes profile create caduceus-try`), install Caduceus in it, and run /caduceus demo there.")
    assert not (data_dir / 'demo-files').exists()
    assert list(vault.iterdump()) == before


def test_demo_serializes_generation_and_storage(data_dir, vault, monkeypatch):
    entered, contender, release = threading.Event(), threading.Event(), threading.Event()
    generations, submissions = [], []

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if entered.is_set():
                contender.set()
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)

    def generate(out, **kwargs):
        generations.append(1)
        if len(generations) == 1:
            entered.set()
            assert release.wait(5), 'first generator was not released'
        else:
            contender.set()  # baseline reaches generation instead of waiting on the lock
        out.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name in sorted(NAMES):
            path = out / name
            path.write_text('invented placeholder ' + name)
            paths[name] = path
        return paths

    def submit(key, fn, *, wait_s):
        submissions.append(key)
        return {'state': 'running' if len(submissions) == 1 else 'busy'}

    from caduceus.demo import persona as generator
    monkeypatch.setattr(ops, '_DEMO_LOCK', ObservedLock(), raising=False)
    monkeypatch.setattr(generator, 'write_persona', generate)
    monkeypatch.setattr(jobs, 'submit', submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(tools.command, FakeCtx(), 'demo')
        try:
            assert entered.wait(5)
            second = pool.submit(tools.command, FakeCtx(), 'demo')
            assert contender.wait(5)
        finally:
            release.set()
        replies = [first.result(timeout=5), second.result(timeout=5)]
    assert len(generations) == 1
    # Whichever call submits first starts the job; the other is told it is busy. If the second call
    # submits first, the first one sees the stored persona and reports that it is finishing the import.
    assert set(replies) in ({LOADING, BUSY}, {BUSY, FINISHING})
    assert len(submissions) == 2 and submissions[0] == submissions[1]
    assert vault.execute('SELECT count(*) FROM source_artifact').fetchone()[0] == 3


@pytest.mark.parametrize('status', [None, 'failed', 'interrupted', 'running'])
def test_demo_resumes_partial_import(data_dir, vault, monkeypatch, status):
    files = persona.write_persona(data_dir / 'demo-files', markers=30)
    artifacts = [sources.store_source(vault, p, sources.sniff_kind(p))[0] for p in files.values()]
    assert dtc.import_dtc(vault, artifacts[0]).status == 'succeeded'
    if status is not None:
        run = sources.begin_import(vault, artifacts[1], 'promethease', '1')
        with write_txn(vault):
            vault.execute('UPDATE import_run SET status=? WHERE id=?', (status, run.id))
    before = vault.execute('SELECT count(*) FROM genotype_call').fetchone()[0]
    submitted, results = [], []
    submit = jobs.submit

    def tracked(key, fn, *, wait_s):
        submitted.append(key)

        def run():
            # The import job must be able to take the lock from its own thread.
            lock = getattr(ops, '_DEMO_LOCK', threading.Lock())
            assert lock.acquire(timeout=2), 'demo lock held during imports'
            lock.release()
            result = fn()
            results.append(result)
            return result

        return submit(key, run, wait_s=wait_s)

    from caduceus.demo import persona as generator
    monkeypatch.setattr(generator, 'write_persona', lambda *a, **k: pytest.fail('must reuse stored sources'))
    monkeypatch.setattr(jobs, 'submit', tracked)
    try:
        assert tools.command(FakeCtx(), 'demo') == FINISHING
    finally:
        for key in submitted:
            assert jobs.join(key, timeout=30)
    assert submitted == ['demo:' + artifacts[0].sha256]
    assert [r['status'] for r in results[0]] == ['skipped', 'succeeded', 'succeeded']
    assert vault.execute('SELECT count(*) FROM genotype_call').fetchone()[0] == before
    assert vault.execute('SELECT count(*) FROM source_artifact').fetchone()[0] == 3
    assert vault.execute("SELECT count(DISTINCT source_id) FROM import_run WHERE status='succeeded'").fetchone()[0] == 3
    assert tools.command(FakeCtx(), 'demo') == ALREADY


def test_demo_reports_busy_for_running_job(data_dir, vault):
    files = persona.write_persona(data_dir / 'demo-files', markers=30)
    artifacts = [sources.store_source(vault, p, sources.sniff_kind(p))[0] for p in files.values()]
    key = 'demo:' + artifacts[0].sha256
    release = threading.Event()
    try:
        assert jobs.submit(key, lambda: release.wait(10), wait_s=0)['state'] == 'running'
        assert tools.command(FakeCtx(), 'demo') == BUSY
    finally:
        release.set()
        assert jobs.join(key, timeout=5)


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
