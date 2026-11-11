"""0.2.1: GLP-1 brief pivots, synchronous erase admission and the one-time rollup rebuild."""
import importlib
import json
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import FakeCtx, load_plugin_package
from caduceus import ops, tools
from caduceus.brief import write_brief
from caduceus.core import jobs
from caduceus.core.db import write_txn
from caduceus.core.policy import Policy
from caduceus.health import trends


@pytest.fixture
def call():
    ctx = FakeCtx()
    package = load_plugin_package()
    package.register(ctx)

    def run(tool, **args):
        result = json.loads(ctx.tools[tool]["handler"](args))
        assert "error" not in result, result
        return result

    # Hermes loads a separate package namespace: patch and join the modules its handlers actually use.
    run.ops = importlib.import_module(f"{package.__name__}.caduceus.ops")
    run.trends = importlib.import_module(f"{run.ops.__package__}.health.trends")
    run.command = ctx.commands["caduceus"]["handler"]
    run.ctx = ctx
    return run


@pytest.fixture
def health_samples(samples):
    with write_txn(samples):
        for i in range(21):
            day = (date(2026, 9, 1) + timedelta(days=i)).isoformat()
            for kind, value in (("BodyMass", 100 - i / 10), ("LeanBodyMass", 60 - i / 100)):
                samples.execute("INSERT INTO sample(source_id,type,source_name,unit,start_utc,end_utc,"
                                "tz_offset_min,local_day,value,value_text,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (1, "HKQuantityTypeIdentifier" + kind, "Synthetic scale", "kg", day + "T00:00:00Z",
                                 day + "T00:00:00Z", 0, day, value, str(value), f"{kind}-{i}"))
    trends.recompute_rollups(samples)
    return samples


def test_brief_pivots_on_glp1_or_falls_back_to_latest_start(vault, health_samples, tmp_path):
    with write_txn(vault):
        starts = [("Ozempic 0.5 mg pen", "2026-09-08"), ("Metformin", "2026-09-15")]
        vault.executemany("INSERT INTO medication_event(drug,event,occurred_on,created_at) VALUES (?,'start',?,?)",
                          [(drug, day, day) for drug, day in starts])
    for glp1 in (True, False):
        if not glp1:
            with write_txn(vault):
                vault.execute("DELETE FROM medication_event WHERE drug='Ozempic 0.5 mg pen'")
        page = write_brief(vault, health_samples, tmp_path / "brief", policy=Policy(), today=date(2026, 9, 30)).read_text()
        for label in ("Weight", "Lean mass"):
            panel = page.split(f"<h3>{label}</h3>", 1)[1].split("</svg>", 1)[1].split("</p>", 1)[0]
            drug, day = ("Ozempic", "2026-09-08") if glp1 else ("Metformin", "2026-09-15")
            assert f"before {drug} started ({day})" in panel
            if glp1:
                assert "2026-09-15" not in panel and "Metformin" not in panel
        assert "Metformin start" in page  # all starts remain chart markers


def test_synchronous_request_waits_before_creating_vault_during_erase(data_dir):
    entered, finished = threading.Event(), threading.Event()
    results = []

    def request():
        entered.set()
        try:
            results.append(tools.command(FakeCtx(), "imports"))
        finally:
            finished.set()

    worker = threading.Thread(target=request)
    try:
        with jobs.erasing() as free:
            assert free
            worker.start()
            assert entered.wait(5)
            assert not finished.wait(0.3)
            assert not (data_dir / "vault.db").exists()
    finally:
        if worker.ident is not None:
            worker.join(5)
    assert not worker.is_alive() and results == ["No imports yet."]


def test_erase_refuses_until_synchronous_store_closes(data_dir):
    entered, release = threading.Event(), threading.Event()

    def request():
        with ops._Store():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert entered.wait(5)
        before = {p.name: p.read_bytes() for p in data_dir.iterdir()}
        assert "still running" in tools.command(FakeCtx(), "erase confirm")
        assert {p.name: p.read_bytes() for p in data_dir.iterdir()} == before
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert tools.command(FakeCtx(), "erase confirm").startswith("Erased")
    assert list(data_dir.iterdir()) == []


def test_failed_store_open_releases_gate(data_dir, monkeypatch):
    def failed_open():
        raise OSError("synthetic open failure")

    monkeypatch.setattr(ops, "open_vault", failed_open)
    store = ops._Store()  # constructing a store must not open anything
    with pytest.raises(OSError, match="synthetic open failure"):
        with store:
            pytest.fail("the vault should not open")
    with jobs.erasing() as free:
        assert free


@pytest.mark.parametrize("failing", ["samples", "vault"])
def test_failed_close_still_closes_other_connections_and_releases_gate(monkeypatch, failing):
    closed = []

    class Connection:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)
            if self.name == failing:
                raise OSError("synthetic close failure")

    monkeypatch.setattr(ops, "open_vault", lambda: Connection("vault"))
    monkeypatch.setattr(ops, "open_samples", lambda: Connection("samples"))
    with pytest.raises(OSError, match="synthetic close failure"):
        with ops._Store() as store:
            store.samples
    assert closed == ["samples", "vault"]
    with jobs.erasing() as free:
        assert free


@pytest.mark.parametrize("failed_delete", [False, True])
def test_erase_clears_pending_watch_on_early_return(data_dir, monkeypatch, failed_delete):
    if failed_delete:
        reports = data_dir / "reports"
        reports.mkdir()
        (reports / "synthetic.txt").write_text("synthetic report")

        def refused(path):
            raise OSError("synthetic delete failure")

        monkeypatch.setattr(ops.shutil, "rmtree", refused)
    with ops._WATCH_LOCK:
        ops._WATCH_PENDING.append({"result": {"new_items": ["synthetic old result"]}})
    try:
        text = tools.command(FakeCtx(), "erase confirm")
        assert ("Could not delete" if failed_delete else "nothing to erase") in text
        with ops._WATCH_LOCK:
            assert ops._WATCH_PENDING == []
    finally:
        with ops._WATCH_LOCK:
            ops._WATCH_PENDING.clear()


@pytest.fixture
def aged_rollups(health_samples):
    with write_txn(health_samples):
        health_samples.execute("UPDATE daily_rollup SET calc_version='trend/1'")
        health_samples.execute("DELETE FROM daily_rollup WHERE metric='lean_mass'")
        health_samples.execute("PRAGMA user_version = 0")  # vaults from before 0.2.1 never carried the mark
    return health_samples


def test_series_rebuilds_missing_lean_mass_from_stored_samples(call, aged_rollups):
    try:
        result = call("caduceus_trend", op="series", metric="lean_mass", bucket="day")
        assert len(result["points"]) == 21 and result["points"][0]["value"] == 60
        assert "rebuilding" not in result
        assert {r[0] for r in aged_rollups.execute("SELECT calc_version FROM daily_rollup")} == {trends.CALC_VERSION}
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)


def test_current_rollups_do_not_submit_a_job(call, health_samples, monkeypatch):
    assert not trends.rollups_stale(health_samples)
    submit = call.ops.jobs.submit

    def checked(key, fn, **kwargs):
        assert key != "rollup-rebuild", "current rollups must not be rebuilt"
        return submit(key, fn, **kwargs)

    monkeypatch.setattr(call.ops.jobs, "submit", checked)
    assert call("caduceus_trend", op="series", metric="lean_mass")["points"]


@pytest.fixture
def slow_rebuild(aged_rollups, monkeypatch, call):
    entered, release = threading.Event(), threading.Event()
    scan = call.trends._rollup_rows

    def slow(samples):  # the rebuild's scan, outside the writer lock
        entered.set()
        if not release.wait(10):
            raise TimeoutError("synthetic rebuild was not released")
        return scan(samples)

    monkeypatch.setattr(call.trends, "_rollup_rows", slow)
    monkeypatch.setattr(call.ops, "ROLLUP_WAIT_S", 0.2)
    try:
        yield entered, release
    finally:
        release.set()
        assert call.ops.jobs.join("rollup-rebuild", 5)


@pytest.mark.parametrize("op", ["metrics", "series", "compare", "check"])
def test_slow_rebuild_reports_progress_and_blocks_erase(call, slow_rebuild, op):
    entered, release = slow_rebuild
    result = call("caduceus_trend", op=op, metric="lean_mass", pivot="2026-09-08")
    assert entered.is_set()
    if op == "check":
        assert "rebuilding" not in result and result["signals"] == []
    else:
        assert result["rebuilding"] == ops._REBUILDING
    # Another read shares the running rebuild instead of starting a second one.
    assert call("caduceus_trend", op="series", metric="weight")["rebuilding"] == ops._REBUILDING
    if op == "compare":
        call("caduceus_capture", op="medication", drug="Ozempic", date="2026-09-08", confirm=True)
        call.ctx.config["hidden_categories"] = ["medication"]
        hidden = call("caduceus_trend", op="compare", metric="weight", drug="Ozempic")
        assert hidden["withheld"] == 1 and "pivot" not in hidden
        assert hidden["rebuilding"] == ops._REBUILDING
    assert "still running" in call.command("erase confirm")
    release.set()
    assert call.ops.jobs.join("rollup-rebuild", 5)
    result = call("caduceus_trend", op="series", metric="lean_mass")
    assert result["points"] and "rebuilding" not in result


def _sample(samples, kind, unit, value, key):
    samples.execute("INSERT INTO sample(source_id,type,source_name,unit,start_utc,end_utc,tz_offset_min,local_day,"
                    "value,value_text,dedupe_key) VALUES (1,?,?,?,?,?,0,?,?,?,?)",
                    ("HKQuantityTypeIdentifier" + kind, "Synthetic", unit, "2026-09-01T00:00:00Z",
                     "2026-09-01T00:00:00Z", "2026-09-01", value, str(value), key))


def test_rollups_stale_with_old_current_and_missing_rollups(samples):
    assert not trends.rollups_stale(samples)  # a new, empty vault
    with write_txn(samples):
        samples.execute("INSERT INTO daily_rollup VALUES ('weight','2026-09-01',100,1,'[]','trend/1')")
    assert trends.rollups_stale(samples)
    with write_txn(samples):
        samples.execute("UPDATE daily_rollup SET calc_version=?", (trends.CALC_VERSION,))
    assert not trends.rollups_stale(samples)
    with write_txn(samples):
        samples.execute("DELETE FROM daily_rollup")
        _sample(samples, "SomethingNew", "x", 1, "unknown")
    assert not trends.rollups_stale(samples)  # nothing Caduceus rolls up
    with write_txn(samples):
        _sample(samples, "LeanBodyMass", "kg", 60, "lean")
    assert trends.rollups_stale(samples)  # samples of a known metric, no rollups at all
    with write_txn(samples):
        samples.execute(f"PRAGMA user_version = {trends.ROLLUP_MARK}")
    assert not trends.rollups_stale(samples)  # this calculation already ran on the file


def test_brief_warns_while_rollups_are_rebuilding(call, slow_rebuild):
    entered, release = slow_rebuild
    result = call("caduceus_capture", op="brief")
    assert entered.is_set() and Path(result["path"]).is_file()
    assert result["rebuilding"] == (
        "The daily health summaries were still being rebuilt after an update, so this brief may lack lean mass, "
        "protein and waist. Write it again in a minute.")
    release.set()
    assert call.ops.jobs.join("rollup-rebuild", 5)
    assert "rebuilding" not in call("caduceus_capture", op="brief")


def test_failed_rebuild_keeps_stored_rollups_and_waits_before_retrying(call, aged_rollups, monkeypatch):
    attempts = []

    def failed(samples):
        attempts.append(True)
        raise OSError("synthetic rebuild failure")

    monkeypatch.setattr(call.trends, "rebuild_rollups", failed)
    try:
        for _ in range(2):
            result = call("caduceus_trend", op="series", metric="weight")
            assert result["points"] and result["rebuilding"] == ops._REBUILD_FAILED  # stored points, honest note
            assert call.ops.jobs.join("rollup-rebuild", 5)
        assert len(attempts) == 1 and trends.rollups_stale(aged_rollups)  # the second call did not rescan
        brief = call("caduceus_capture", op="brief")
        assert brief["rebuilding"] == ops._BRIEF_NOTES[ops._REBUILD_FAILED] and len(attempts) == 1
        assert "rebuilding" not in call("caduceus_trend", op="check")  # the daily check stays quiet
        monkeypatch.setattr(call.ops, "REBUILD_RETRY_S", 0.0)
        call("caduceus_trend", op="series", metric="weight")
        assert call.ops.jobs.join("rollup-rebuild", 5) and len(attempts) == 2
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)
        call.ops._rebuild_failed_at = None


def test_request_arriving_mid_erase_sees_only_a_new_empty_vault(call, data_dir, monkeypatch):
    """Once an erase ends, nothing from the erased vault comes back: a request that arrived during it waits, then
    opens a new, empty vault."""
    call("caduceus_capture", op="medication", drug="Ozempic", date="2026-09-08", confirm=True)
    for folder in ("sources", "reports"):
        (data_dir / folder).mkdir(exist_ok=True)
        (data_dir / folder / "old.bin").write_bytes(b"old")
    deleting, proceed = threading.Event(), threading.Event()
    rmtree = call.ops.shutil.rmtree

    def slow_rmtree(path, *args, **kwargs):  # the databases are already gone when the first folder is deleted
        deleting.set()
        assert proceed.wait(5)
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(call.ops.shutil, "rmtree", slow_rmtree)
    erased, briefs = [], []
    eraser = threading.Thread(target=lambda: erased.append(call.command("erase confirm")))
    request = threading.Thread(target=lambda: briefs.append(call("caduceus_capture", op="brief")))
    eraser.start()
    try:
        assert deleting.wait(5)
        request.start()
        request.join(0.3)
        assert request.is_alive()  # it waits for the erase instead of reopening the vault mid-delete
        assert not (data_dir / "vault.db").exists()
    finally:
        proceed.set()
        eraser.join(5)
        if request.ident is not None:
            request.join(5)
    assert erased and erased[0].startswith("Erased") and "Left in place" not in erased[0]
    assert briefs, "the waiting brief should finish after the erase"
    brief = Path(briefs[0]["path"])
    assert "Ozempic" not in brief.read_text()
    assert call("caduceus_capture", op="settings")["counts"]["medications"] == 0
    assert not (data_dir / "sources").exists()
    assert [p.name for p in (data_dir / "reports").iterdir()] == [brief.name]


def test_rebuild_keeps_samples_and_is_idempotent(call, aged_rollups):
    import hashlib

    def digest(table, order):
        rows = [tuple(row) for row in aged_rollups.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        assert rows
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    samples_before = digest("sample", "id")
    try:
        assert call("caduceus_trend", op="series", metric="lean_mass")["points"]
        first = digest("daily_rollup", "metric, day")
        with write_txn(aged_rollups):  # mark the rebuilt rollups old again: a second rebuild must change nothing
            aged_rollups.execute("UPDATE daily_rollup SET calc_version='trend/1'")
            aged_rollups.execute("PRAGMA user_version = 0")
        assert call("caduceus_trend", op="series", metric="lean_mass")["points"]
        assert digest("daily_rollup", "metric, day") == first
        assert digest("sample", "id") == samples_before
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)


def test_requests_carry_on_while_the_rebuild_scans(call, slow_rebuild):
    entered, release = slow_rebuild
    assert call("caduceus_trend", op="series", metric="weight")["rebuilding"] == ops._REBUILDING
    assert entered.is_set()
    done = []
    worker = threading.Thread(target=lambda: done.append(call.command("imports")))
    worker.start()
    worker.join(3)  # every request opens the vault, which takes the writer lock the scan must not hold
    assert done == ["No imports yet."]


def test_rebuild_recomputes_inside_the_write_when_samples_arrive(call, aged_rollups, monkeypatch):
    scan, scans = call.trends._rollup_rows, []

    def scan_then_import(samples):
        rows = scan(samples)
        scans.append(len(rows[0]))
        if len(scans) == 1:  # an import adds a sample after the scan, before the swap
            with write_txn(aged_rollups):
                _sample(aged_rollups, "LeanBodyMass", "kg", 59, "late-lean")
                aged_rollups.execute("UPDATE sample SET local_day='2026-09-30' WHERE dedupe_key='late-lean'")
        return rows

    monkeypatch.setattr(call.trends, "_rollup_rows", scan_then_import)
    try:
        call("caduceus_trend", op="series", metric="lean_mass", bucket="day")
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)
    assert len(scans) == 2 and scans[1] == scans[0] + 1
    assert aged_rollups.execute("SELECT value FROM daily_rollup WHERE metric='lean_mass' AND day='2026-09-30'"
                                ).fetchone()[0] == 59


def test_samples_without_any_rollups_are_rebuilt_once(call, samples, monkeypatch):
    with write_txn(samples):
        for i in range(3):
            _sample(samples, "LeanBodyMass", "kg", 60 - i, f"lean-{i}")
            samples.execute("UPDATE sample SET local_day=? WHERE dedupe_key=?", (f"2026-09-0{i + 1}", f"lean-{i}"))
        _sample(samples, "WaistCircumference", "furlong", 1, "odd-unit")  # a unit no rollup takes
    try:
        assert len(call("caduceus_trend", op="series", metric="lean_mass", bucket="day")["points"]) == 3
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)
    assert samples.execute("PRAGMA user_version").fetchone()[0] == trends.ROLLUP_MARK
    submit = call.ops.jobs.submit

    def checked(key, fn, **kwargs):
        assert key != "rollup-rebuild", "a file this calculation already ran on is not rebuilt again"
        return submit(key, fn, **kwargs)

    monkeypatch.setattr(call.ops.jobs, "submit", checked)
    call("caduceus_trend", op="series", metric="waist")


def test_no_rebuild_while_another_job_runs(call, aged_rollups):
    release = threading.Event()
    assert call.ops.jobs.submit("v021-import", release.wait, wait_s=0)["state"] == "running"
    try:
        result = call("caduceus_trend", op="series", metric="weight")
        assert "rebuilding" not in result and not call.ops.jobs.is_active("rollup-rebuild")
        assert trends.rollups_stale(aged_rollups)
    finally:
        release.set()
        assert call.ops.jobs.join("v021-import", 5)
    try:
        assert call("caduceus_trend", op="series", metric="lean_mass")["points"]
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)
    assert not trends.rollups_stale(aged_rollups)


def test_brief_is_written_without_a_trends_lane(call, aged_rollups, monkeypatch):
    need = call.ops._need

    def missing_trends(module, name):
        if module == "health.trends":
            raise call.ops.ToolError("the trends lane is missing")
        return need(module, name)

    monkeypatch.setattr(call.ops, "_need", missing_trends)
    result = call("caduceus_capture", op="brief")
    assert Path(result["path"]).is_file() and "rebuilding" not in result


def test_samples_that_roll_up_to_nothing_are_rebuilt_only_once(call, samples, monkeypatch):
    with write_txn(samples):
        _sample(samples, "WaistCircumference", "furlong", 1, "odd-unit")  # a known metric in a unit no rollup takes
    assert trends.rollups_stale(samples)
    try:
        assert call("caduceus_trend", op="series", metric="waist")["points"] == []
    finally:
        assert call.ops.jobs.join("rollup-rebuild", 5)
    assert samples.execute("SELECT count(*) FROM daily_rollup").fetchone()[0] == 0
    assert not trends.rollups_stale(samples)  # the mark says this calculation already ran
    submit = call.ops.jobs.submit

    def checked(key, fn, **kwargs):
        assert key != "rollup-rebuild", "a file whose samples give no rollups is not rebuilt on every call"
        return submit(key, fn, **kwargs)

    monkeypatch.setattr(call.ops.jobs, "submit", checked)
    call("caduceus_trend", op="series", metric="waist")


def test_erase_refuses_while_a_held_watch_result_is_handed_over(call, data_dir, monkeypatch):
    (data_dir / "vault.db").write_bytes(b"x")
    entered, release = threading.Event(), threading.Event()
    deliver = call.ops._filter_watch

    def slow_deliver(result, policy):
        entered.set()
        assert release.wait(5)
        return deliver(result, policy)

    monkeypatch.setattr(call.ops, "_filter_watch", slow_deliver)
    with call.ops._WATCH_LOCK:
        call.ops._WATCH_PENDING.append({"result": {"new_items": [{"title": "synthetic study"}]}})
    results = []
    worker = threading.Thread(target=lambda: results.append(call("caduceus_capture", op="watch_run")))
    worker.start()
    try:
        assert entered.wait(5)
        assert "still running" in call.command("erase confirm")  # the result taken from memory is still in flight
        assert (data_dir / "vault.db").exists()
    finally:
        release.set()
        worker.join(5)
        with call.ops._WATCH_LOCK:
            call.ops._WATCH_PENDING.clear()
    assert results and results[0]["new_items"] == [{"title": "synthetic study"}]
    assert call.command("erase confirm").startswith("Erased")
