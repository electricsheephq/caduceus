"""0.2.0: /caduceus erase and the GLP-1 companion metrics (lean mass, protein, waist)."""
import hashlib
import json
import threading
import zipfile

import pytest

from conftest import FakeCtx, load_plugin_package
from caduceus import tools
from caduceus.core import jobs
from caduceus.core.db import write_txn
from caduceus.health.trends import recompute_rollups, trend


def _seed(root):
    for name in ("vault.db", "vault.db-wal", "samples.db"):
        (root / name).write_bytes(b"x" * 10)
    for folder in ("sources", "reports", "demo-files"):
        (root / folder).mkdir()
        (root / folder / "f.bin").write_bytes(b"y" * 10)
    (root / "notes.txt").write_text("mine")


def test_erase_with_nothing_stored(data_dir):
    assert "nothing to erase" in tools.command(FakeCtx(), "erase").lower()


def test_erase_preview_deletes_nothing(data_dir, vault):
    (data_dir / "sources").mkdir(exist_ok=True)
    (data_dir / "sources" / "a.zip").write_bytes(b"z")
    ctx = FakeCtx({"privacy_level": "standard"})
    text = tools.command(ctx, "erase")
    assert "/caduceus erase confirm" in text and "cannot be undone" in text
    assert (data_dir / "vault.db").exists() and (data_dir / "sources" / "a.zip").exists()


def test_erase_confirm_removes_only_caduceus_files(data_dir):
    _seed(data_dir)
    ctx = FakeCtx({"privacy_level": "standard"})
    text = tools.command(ctx, "erase confirm")
    assert text.startswith("Erased the Caduceus vault")
    assert "notes.txt" in text
    assert sorted(p.name for p in data_dir.iterdir()) == ["notes.txt"]
    assert ctx.config == {"privacy_level": "standard"}  # settings stay


def test_erase_never_follows_a_symlinked_folder(data_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (data_dir / "vault.db").write_bytes(b"x")
    (data_dir / "sources").symlink_to(outside, target_is_directory=True)
    text = tools.command(FakeCtx(), "erase confirm")
    assert not (data_dir / "sources").exists() and (outside / "keep.txt").read_text() == "keep"
    assert "sources was a link: only the link was removed" in text


def test_erase_preview_never_opens_a_linked_vault(data_dir, tmp_path):
    import sqlite3

    outside = tmp_path / "elsewhere.db"
    conn = sqlite3.connect(outside)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    before = (outside.read_bytes(), outside.stat().st_mtime_ns)
    (data_dir / "vault.db").symlink_to(outside)
    text = tools.command(FakeCtx(), "erase")
    assert "/caduceus erase confirm" in text and "imported files with their copies" not in text
    assert (outside.read_bytes(), outside.stat().st_mtime_ns) == before
    assert sorted(q.name for q in tmp_path.iterdir() if q.name.startswith("elsewhere")) == ["elsewhere.db"]


def test_erase_preview_with_a_damaged_vault(data_dir):
    (data_dir / "vault.db").write_bytes(b"not a database " * 300)
    text = tools.command(FakeCtx(), "erase")
    assert text.startswith("This deletes everything Caduceus stored") and "error" not in text.lower()


def test_erase_deletes_side_files_before_their_database():
    from caduceus.ops import _ERASE_FILES

    for db in ("vault.db", "samples.db"):
        assert all(_ERASE_FILES.index(db + side) < _ERASE_FILES.index(db) for side in ("-wal", "-shm", "-journal"))


def test_erase_refuses_while_a_job_is_being_prepared(data_dir):
    _seed(data_dir)
    entered, release = threading.Event(), threading.Event()

    def importing():  # an import copying its file before it starts its job
        with jobs.preparing():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=importing)
    worker.start()
    try:
        assert entered.wait(5)
        assert "still running" in tools.command(FakeCtx(), "erase confirm")
        assert (data_dir / "vault.db").exists()
    finally:
        release.set()
        worker.join(5)
    assert tools.command(FakeCtx(), "erase confirm").startswith("Erased")


def test_new_jobs_wait_until_an_erase_finishes(data_dir):
    ran = threading.Event()
    results = []
    with jobs.erasing() as free:
        assert free
        worker = threading.Thread(target=lambda: results.append(jobs.submit("v020-gate", ran.set, wait_s=5)))
        worker.start()
        assert not ran.wait(0.3)  # the job waits while files are being deleted
    worker.join(5)
    assert ran.is_set() and results[0]["state"] == "done"


def test_erase_refuses_while_a_job_runs(data_dir):
    _seed(data_dir)
    release = threading.Event()
    assert jobs.submit("v020-test-job", release.wait, wait_s=0)["state"] == "running"
    try:
        text = tools.command(FakeCtx(), "erase confirm")
        assert "still running" in text
        assert (data_dir / "vault.db").exists() and (data_dir / "sources").exists()
    finally:
        release.set()
        assert jobs.join("v020-test-job", timeout=5)
    assert tools.command(FakeCtx(), "erase confirm").startswith("Erased")


def test_erase_usage_and_help():
    assert tools.command(FakeCtx(), "erase now") == "Usage: /caduceus erase [confirm]"
    assert "erase [confirm]" in tools.command(FakeCtx(), "help")
    ctx = FakeCtx()
    load_plugin_package().register(ctx)
    assert "erase [confirm]" in ctx.commands["caduceus"]["args_hint"]


def test_companion_metrics_convert_units(samples):
    records = [("LeanBodyMass", "kg", 60, "Scale A"), ("LeanBodyMass", "lb", 62 / 0.45359237, "Scale B"),
               ("DietaryProtein", "g", 30, "Food log"), ("DietaryProtein", "g", 40, "Food log"),
               ("DietaryProtein", "mg", 50000, "Other app"),
               ("WaistCircumference", "in", 40, "Phone"), ("WaistCircumference", "furlong", 1, "Odd")]
    with write_txn(samples):
        for i, (kind, unit, value, source) in enumerate(records):
            samples.execute("INSERT INTO sample(source_id,type,source_name,unit,start_utc,end_utc,"
                            "tz_offset_min,local_day,value,value_text,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (1, "HKQuantityTypeIdentifier" + kind, source, unit, "2024-01-01T00:00:00Z",
                             "2024-01-01T00:00:00Z", 0, "2024-01-01", value, str(value), str(i)))
    recompute_rollups(samples)
    assert trend(samples, "lean_mass", bucket="day")["points"][0]["value"] == pytest.approx(61)
    assert trend(samples, "protein", bucket="day")["points"][0]["value"] == pytest.approx(70)  # one source a day
    assert trend(samples, "waist", bucket="day")["points"][0]["value"] == pytest.approx(101.6)  # unknown unit left out


def _apple_rows(files):
    apple = next(p for p in files.values() if "apple" in p.name)
    with zipfile.ZipFile(apple) as z:
        data = z.read(next(n for n in z.namelist() if n.endswith(".xml"))).decode()
    return [line for line in data.splitlines() if line.lstrip().startswith("<Record ")], apple


def test_demo_persona_keeps_every_earlier_series(tmp_path):
    from caduceus.demo.persona import write_persona

    rows, _ = _apple_rows(write_persona(tmp_path / "p", markers=50))
    new = ("LeanBodyMass", "DietaryProtein", "WaistCircumference")
    old = [r for r in rows if not any(k in r for k in new)]
    # The fingerprint of the 0.1.5 demo's records: the film's and README's numbers stay exactly as they were.
    assert (hashlib.sha256("\n".join(old).encode()).hexdigest(), len(old)) == (
        "bae6b7f441b7f252365e8d9e8627a284fba3b0d1968beba7d889ca469887f506", 12241)
    assert all(any(k in r for r in rows) for k in new)


def test_demo_answers_am_i_losing_muscle(tmp_path):
    from caduceus.demo.persona import write_persona

    ctx = FakeCtx()
    load_plugin_package().register(ctx)
    call = lambda tool, **args: json.loads(ctx.tools[tool]["handler"](args))  # noqa: E731
    _, apple = _apple_rows(write_persona(tmp_path / "p", markers=50))
    assert call("caduceus_capture", op="import", path=str(apple))["status"] == "succeeded"
    lean = call("caduceus_trend", op="compare", metric="lean_mass", drug="Ozempic", after_days=84)
    weight = call("caduceus_trend", op="compare", metric="weight", drug="Ozempic", after_days=84)
    lean_drop = lean["before"]["mean"] - lean["after"]["mean"]
    weight_drop = weight["before"]["mean"] - weight["after"]["mean"]
    assert lean["unit"] == "kg" and 0.4 < lean_drop < weight_drop
    protein = call("caduceus_trend", op="compare", metric="protein", drug="Ozempic", after_days=84)
    assert protein["unit"] == "g" and protein["after"]["mean"] < protein["before"]["mean"] - 10


def test_overlapping_erases_keep_the_gate_closed(data_dir):
    ran = threading.Event()
    with jobs.erasing() as first:
        assert first
        with jobs.erasing() as second:
            assert not second  # a second erase is refused
        worker = threading.Thread(target=lambda: jobs.submit("v020-gate2", ran.set, wait_s=5))
        worker.start()
        assert not ran.wait(0.3)  # and leaving it did not reopen the gate for new jobs
    worker.join(5)
    assert ran.is_set()


def test_erase_clears_watch_results_kept_in_memory(data_dir):
    from caduceus import ops

    _seed(data_dir)
    with ops._WATCH_LOCK:
        ops._WATCH_PENDING.append({"result": {"new_items": ["from the old vault"]}})
    try:
        assert tools.command(FakeCtx(), "erase confirm").startswith("Erased")
        with ops._WATCH_LOCK:
            assert ops._WATCH_PENDING == []
    finally:
        with ops._WATCH_LOCK:
            ops._WATCH_PENDING.clear()


def test_erase_preview_counts_a_missing_table_as_none(data_dir, vault, monkeypatch):
    from caduceus import ops

    real = ops._settings
    monkeypatch.setattr(ops, "_settings", lambda ctx: {**real(ctx), "counts": dict.fromkeys(real(ctx)["counts"])})
    text = tools.command(FakeCtx(), "erase")
    assert "None" not in text and "0 imported files" in text
