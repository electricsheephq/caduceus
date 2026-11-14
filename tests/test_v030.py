"""0.3.0 Health Auto Export: synthetic import, precedence and fake MCP sync."""
import hashlib
import json
import sqlite3
import threading
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from conftest import FakeCtx
from caduceus import ops
from caduceus.core import jobs, sources
from caduceus.core.db import open_samples, open_vault
from caduceus.health import hae, trends
from caduceus.health.apple import import_apple_health
from caduceus.registration import register
from synth.apple import make_export
from synth.apple_rollups_v022 import _rollup_rows as legacy_rollup_rows

DAY = "2026-11-13"
TODAY = date.fromisoformat(DAY)
SOURCE = "Synthetic HAE Watch"


@pytest.fixture
def plugin():
    ctx = FakeCtx({"hae_backfill_days": 1})
    register(ctx)

    def call(tool="caduceus_capture", **args):
        return json.loads(ctx.tools[tool]["handler"](args))

    return ctx, call


def entry(qty=8123, day=DAY, **extra):
    return {"date": day + " 00:00:00 +0900", "qty": qty, "source": SOURCE, **extra}


def metric(name="step_count", qty=8123, units="count", **extra):
    return {"name": name, "units": units, "data": [entry(qty, **extra)]}


def payload(*metrics, **extra):
    return {"data": {"metrics": list(metrics), **extra}}


def write(tmp_path, name="hae.json", data=None):
    path = tmp_path / name
    path.write_text(json.dumps(data if data is not None else payload(metric())))
    return path


def ingest_file(tmp_path, vault, samples, data, name="hae.json"):
    src, _ = sources.store_source(vault, write(tmp_path, name, data), "hae")
    return hae.import_hae(samples, vault, src)


def series(call, name):
    return call("caduceus_trend", op="series", metric=name, bucket="day")["points"]


def rows(samples):
    return [tuple(r) for r in samples.execute("SELECT * FROM daily_rollup ORDER BY metric,day")]


def sync(call, samples, vault, *, days=1, today=TODAY, **kw):
    return hae.sync_hae(call, samples, vault, server="health-auto-export", backfill_days=days,
                        today=today, local_offset="+0900", **kw)


def empty(tool, args):
    return {"ok": True, "result": payload()}


def test_every_metric_file_to_trends_and_local_offsets(plugin, tmp_path, samples):
    _, call = plugin
    quantities = [metric("weight_body_mass", 220, "lbs"), metric("body_mass", 70, "kg", day="2026-11-14"),
                  metric("body_mass_index", 24), metric("body_fat_percentage", 22.5, "%"),
                  metric("lean_body_mass", 60, "kg"), metric("waist_circumference", 32, "in"),
                  metric("protein", 90000, "mg"), metric(), metric("active_energy", 400, "Cal"),
                  metric("resting_heart_rate", 59, "bpm"), metric("heart_rate", 99, "beats/min", Min=45, Avg=64, Max=135),
                  metric("heart_rate_variability", 48, "ms"), metric("vo2_max", 42, "ml/(kg·min)"),
                  metric("blood_glucose", 5, "mmol/l"),
                  metric("blood_pressure", units="mmHg", systolic=119, diastolic=78),
                  metric("nutrition_unknown", 123456)]
    # Same UTC date as the +0900 entry, but the device-local day is the preceding day.
    quantities[8]["data"][0]["date"] = "2026-11-12 23:30:00 -0700"
    quantities.append({"name": "sleep_analysis", "data": [entry(totalSleep=7.25,
                         sleepEnd=DAY + " 07:00:00 +0900")]})
    path = write(tmp_path, data=payload(*quantities, stateOfMind=[{}, {}]))
    out = call(op="import", path=str(path))
    assert out["status"] == "succeeded", out
    assert out["counts"]["metrics_ignored"] == {"nutrition_unknown": 1}
    assert out["counts"]["ignored_arrays"] == {"stateOfMind": 2}
    for name, value in (("weight", 220 * .45359237), ("steps", 8123), ("resting_heart_rate", 59),
                        ("systolic", 119), ("sleep_hours", 7.25), ("heart_rate", 64), ("vo2max", 42)):
        assert series(call, name)[0]["value"] == pytest.approx(value)
    assert len(call("caduceus_trend", op="metrics")["metrics"]) == len(trends.METRICS)
    assert samples.execute("SELECT value FROM sample WHERE type LIKE '%BodyFatPercentage'").fetchone()[0] == .225
    assert samples.execute("SELECT local_day FROM sample WHERE type LIKE '%ActiveEnergyBurned'").fetchone()[0] == "2026-11-12"
    assert samples.execute("SELECT count(*) FROM sample WHERE value=123456").fetchone()[0] == 0


def test_segment_sleep_separate_file(plugin, tmp_path, samples):
    _, call = plugin
    entries = [entry(value=phase, start=DAY + f" {hour:02}:00:00 +0900",
                     end=DAY + f" {hour+1:02}:00:00 +0900")
               for phase, hour in (("Core", 0), ("Deep", 1), ("REM", 2), ("Asleep", 3),
                                   ("Unspecified", 4), ("In Bed", 5), ("Awake", 6))]
    out = call(op="import", path=str(write(tmp_path, data=payload({"name": "sleep_analysis", "data": entries}))))
    assert out["status"] == "succeeded"
    assert series(call, "sleep_hours")[0]["value"] == 5
    assert samples.execute("SELECT count(DISTINCT value_text) FROM sample").fetchone()[0] == 6
    assert set(r[0] for r in samples.execute("SELECT local_day FROM sample")) == {DAY}


def test_same_file_is_skipped(plugin, tmp_path):
    _, call = plugin
    path = write(tmp_path)
    assert call(op="import", path=str(path))["status"] == "succeeded"
    assert call(op="import", path=str(path))["status"] == "skipped"


def test_growing_day_replaces(plugin, tmp_path):
    _, call = plugin
    call(op="import", path=str(write(tmp_path, "a.json", payload(metric(qty=5000)))))
    out = call(op="import", path=str(write(tmp_path, "b.json", payload(metric(qty=12000)))))
    assert series(call, "steps")[0]["value"] == 12000
    assert out["counts"]["samples_replaced"] >= 1


def test_hourly_then_daily_replaces(plugin, tmp_path):
    _, call = plugin
    hourly = metric()
    hourly["data"] = [entry(2000, date=DAY + f" {h:02}:00:00 +0900") for h in range(3)]
    call(op="import", path=str(write(tmp_path, "hours.json", payload(hourly))))
    assert series(call, "steps")[0]["value"] == 6000
    call(op="import", path=str(write(tmp_path, "day.json", payload(metric(qty=7000)))))
    assert series(call, "steps")[0]["value"] == 7000


def test_apple_precedes_hae_for_quantity_means_sums_and_sleep(tmp_path, samples, vault):
    src, _ = sources.store_source(vault, make_export(tmp_path / "apple.zip", days=1), "apple_health")
    assert import_apple_health(samples, vault, src).status == "succeeded"
    data = payload(metric(qty=25000, day="2024-02-01"), metric(qty=9000, day="2024-02-02"),
                   metric("body_mass", 10, "kg", day="2024-02-01"),
                   {"name": "sleep_analysis", "data": [entry(day="2024-02-01", totalSleep=10,
                      sleepEnd="2024-02-01 06:00:00 -0800") ]})
    assert ingest_file(tmp_path, vault, samples, data).status == "succeeded"
    assert [p["value"] for p in trends.trend(samples, "steps", bucket="day")["points"]] == [8000, 9000]
    assert trends.trend(samples, "weight", bucket="day")["points"][0]["value"] == 100
    assert trends.trend(samples, "sleep_hours", bucket="day")["points"][0]["value"] == 8
    assert samples.execute("SELECT count(*) FROM sample WHERE provider='apple'").fetchone()[0] > 0


def test_apple_only_rollups_byte_identical_to_pre_hae(tmp_path, vault, samples):
    src, _ = sources.store_source(vault, make_export(tmp_path / "apple.zip", days=45), "apple_health")
    import_apple_health(samples, vault, src)
    before, old_warnings = legacy_rollup_rows(samples)
    after, new_warnings = trends._rollup_rows(samples)
    assert json.dumps(before).encode() == json.dumps(after).encode()
    assert old_warnings == new_warnings
    assert trends.CALC_VERSION == "trend/2"


def test_old_samples_database_migration(tmp_path):
    path = tmp_path / "old.db"
    schema = (Path(__file__).resolve().parents[1] / "caduceus/core/schema/samples/20_samples.sql").read_text()
    with sqlite3.connect(path) as conn:
        conn.executescript(schema.replace("    provider TEXT NOT NULL DEFAULT 'apple',\n", ""))
        conn.execute("INSERT INTO sample(source_id,type,source_name,unit,start_utc,end_utc,tz_offset_min,local_day,"
                     "value,dedupe_key) VALUES (1,'HKQuantityTypeIdentifierStepCount','Watch','count',"
                     "'2026-11-13T00:00:00Z','2026-11-13T00:00:00Z',0,'2026-11-13',123,'old')")
    conn = open_samples(path)
    try:
        assert conn.execute("SELECT provider FROM sample").fetchone()[0] == "apple"
        trends.recompute_rollups(conn)
        assert trends.trend(conn, "steps", bucket="day")["points"][0]["value"] == 123
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sample_hae_type_day'").fetchone()
    finally:
        conn.close()


def test_zip_two_jsons_share_run_state(tmp_path, vault, samples):
    path = tmp_path / "hae.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.json", json.dumps(payload(metric(qty=2000))))
        archive.writestr("two.json", json.dumps(payload(metric(qty=3000, date=DAY + " 01:00:00 +0900"))))
        archive.writestr("../escape.json", json.dumps(payload(metric(qty=999999))))
    assert sources.sniff_kind(path) == "hae"
    src, _ = sources.store_source(vault, path, "hae")
    out = hae.import_hae(samples, vault, src)
    assert out.status == "succeeded"
    assert out.counts["samples"] == 2 and out.counts["samples_replaced"] == 0
    assert trends.trend(samples, "steps", bucket="day")["points"][0]["value"] == 5000


def test_zip_member_size_cap_fails_cleanly(tmp_path, vault, samples, monkeypatch):
    path = tmp_path / "large.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("big.json", json.dumps(payload(metric())))
    src, _ = sources.store_source(vault, path, "hae")
    monkeypatch.setattr(hae, "MAX_JSON", 20)
    out = hae.import_hae(samples, vault, src)
    assert out.status == "failed" and out.warnings == ["ValueError: Health Auto Export import failed"]
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0


def test_sniff_hae_fhir_and_unrecognised(plugin, tmp_path):
    _, call = plugin
    assert sources.sniff_kind(write(tmp_path)) == "hae"
    assert sources.sniff_kind(write(tmp_path, "fhir.json", {"resourceType": "Bundle", **payload()})) == "fhir"
    other = write(tmp_path, "other.json", {"data": {"other": []}})
    assert sources.sniff_kind(other) == "other"
    assert call(op="import", path=str(other))["error"].startswith("not a recognised file")


def test_workouts_keep_only_safe_statistics(tmp_path, samples, vault):
    workout = {"name": "Walking", "source": SOURCE, "start": DAY + " 10:00:00 -0700",
               "end": DAY + " 11:00:00 -0700", "duration": 3599, "distance": {"qty": 5, "units": "km"},
               "energy": 200, "route": [{"lat": 43.1, "lon": 141.1}], "heartRateData": [65, 80],
               "metadata": {"location": "synthetic"}}
    out = ingest_file(tmp_path, vault, samples, payload(workouts=[workout]))
    assert out.status == "succeeded"
    row = dict(samples.execute("SELECT * FROM workout").fetchone())
    assert row["type"] == "Walking" and row["duration"] == 3599 and row["duration_unit"] == "s"
    assert json.loads(row["statistics_json"]) == {"duration": 3599, "energy": 200, "distance": {"qty": 5, "units": "km"}}
    stored = json.dumps(row)
    assert all(text not in stored for text in ("lat", "lon", "heartRate", "metadata"))
    # A different source file still deduplicates the workout.
    out = ingest_file(tmp_path, vault, samples, payload(workouts=[workout], stateOfMind=[]), "again.json")
    assert out.counts["workouts"] == 0


def test_sync_missing_hermes_api(plugin):
    _, call = plugin
    assert call(op="sync") == {"error": "This Hermes cannot give plugins MCP access; Health Auto Export sync needs Hermes 0.23.0 or newer."}


def test_sync_permission_failure_is_recorded(plugin, vault):
    ctx, call = plugin

    def denied(*args, **kw):
        raise PermissionError("not echoed")

    ctx.call_mcp = denied
    out = call(op="sync")
    message = ("Caduceus is not allowed to read the MCP server 'health-auto-export'. To allow it, add "
               "'health-auto-export' to plugins.entries.caduceus.mcp_allowlist in config.yaml, then run op=sync again.")
    assert out["status"] == "failed" and out["warnings"] == [message]
    assert sources.run_info(vault, out["run_id"])["errors"] == [message]


@pytest.mark.parametrize("shape", range(6))
def test_sync_unwraps_envelopes(shape, samples, vault):
    data = payload(metric())
    wrappers = [data, json.dumps(data), {"result": data},
                {"result": {"content": [{"type": "text", "text": json.dumps(data)}]}},
                {"content": [{"type": "text", "text": json.dumps(data)}]}, None]

    def call(tool, args):
        if args.get("metrics") == "sleep_analysis" or tool == "get_workouts":
            return empty(tool, args)
        if shape == 5:
            return {"ok": True, "result": "not JSON", "structuredContent": {"result": data}}
        return {"ok": True, "result": wrappers[shape]}

    out = sync(call, samples, vault)
    assert out.status == "succeeded", out
    assert out.counts["samples"] == 1
    assert trends.trend(samples, "steps", bucket="day")["points"][0]["value"] == 8123


def test_sync_truncated_window_exact_splits(samples, vault):
    seen = []

    def call(tool, args):
        if args.get("aggregate") is True:
            seen.append((args["start"], args["end"]))
            if len(seen) == 1:
                return {"ok": True, "truncated": True, "result": "cut"}
        return empty(tool, args)

    out = sync(call, samples, vault, days=4)
    assert out.status == "succeeded"
    assert seen == [("2026-11-10 00:00:00 +0900", "2026-11-13 23:59:59 +0900"),
                    ("2026-11-10 00:00:00 +0900", "2026-11-11 23:59:59 +0900"),
                    ("2026-11-12 00:00:00 +0900", "2026-11-13 23:59:59 +0900")]
    progress = sources.run_info(vault, out.run_id)["progress"]
    assert progress["windows_done"] == progress["windows_total"] == 5


def test_second_sync_overlaps_two_days_and_reuses_virtual_source(samples, vault):
    first = sync(empty, samples, vault, days=30)
    second = sync(empty, samples, vault, today=TODAY + timedelta(days=1))
    assert second.counts["since"] == (TODAY - timedelta(days=2)).isoformat()
    assert second.run_id != first.run_id and second.source_id == first.source_id
    src = sources.get_source(vault, first.source_id)
    assert src.stored_path == "" and src.size_bytes == 0 and src.kind == "hae_sync"
    assert src.sha256 == hashlib.sha256(b"hae-sync:health-auto-export").hexdigest()
    with pytest.raises(ValueError, match="virtual source"):
        sources.source_file(vault, src)


def test_sync_error_preserves_pre_sync_rollups(tmp_path, samples, vault):
    ingest_file(tmp_path, vault, samples, payload(metric(qty=5000)))
    before = rows(samples)
    count = 0

    def call(tool, args):
        nonlocal count
        count += 1
        return ({"ok": True, "result": payload(metric(qty=12000))} if count == 1 else
                {"ok": False, "error": "server not connected"})

    out = sync(call, samples, vault)
    assert out.status == "failed"
    assert out.warnings == ["Health Auto Export did not answer: server not connected"]
    assert rows(samples) == before


def test_sync_tool_counts_only_and_call_arguments(plugin):
    ctx, call = plugin
    seen = []

    def mcp(server, tool, args, timeout):
        seen.append((server, tool, args, timeout))
        return {"ok": True, "result": payload(metric(qty=876543.25))} if args.get("aggregate") else empty(tool, args)

    ctx.call_mcp = mcp
    out = call(op="sync")
    assert out["status"] == "succeeded"
    assert SOURCE not in json.dumps(out) and "876543.25" not in json.dumps(out)
    assert all(server == "health-auto-export" and timeout == 120 for server, _, _, timeout in seen)
    quantity = next(args for _, _, args, _ in seen if args.get("aggregate"))
    assert quantity["metrics"] == ",".join(n for n in hae.MAPPING if n not in ("weight_body_mass", "sleep_analysis"))
    assert quantity["interval"] == "days" and "T" not in quantity["start"]
    sleep = next(args for _, _, args, _ in seen if args.get("metrics") == "sleep_analysis")
    assert sleep["aggregate"] is False
    workout = next(args for _, tool, args, _ in seen if tool == "get_workouts")
    assert workout["includeMetadata"] is False and workout["includeRoutes"] is False


def test_concurrent_sync_is_busy(plugin, monkeypatch):
    ctx, call = plugin
    entered, release = threading.Event(), threading.Event()

    def mcp(server, tool, args, timeout):
        entered.set()
        assert release.wait(5)
        return empty(tool, args)

    ctx.call_mcp = mcp
    monkeypatch.setattr(ops, "IMPORT_WAIT_S", 0)
    try:
        out = call(op="sync")
        assert out["status"] == "running"
        assert entered.wait(5)
        assert call(op="sync")["status"] == "busy"
    finally:
        release.set()
        assert jobs.join("hae-sync:health-auto-export", 5)


def test_erase_includes_hae_and_virtual_sources(plugin, data_dir, tmp_path):
    ctx, call = plugin
    call(op="import", path=str(write(tmp_path)))
    ctx.call_mcp = lambda server, tool, args, timeout: empty(tool, args)
    call(op="sync")
    assert len(call(op="sources")["files"]) == 2
    assert "2 files" in ctx.commands["caduceus"]["handler"]("status")
    assert call("caduceus_genome", op="summary")["callsets"] == []
    assert "no report" in call("caduceus_genome", op="report")["error"].lower() or "import" in call("caduceus_genome", op="report")["error"].lower()
    assert "separate Hermes profile" in ctx.commands["caduceus"]["handler"]("demo")
    assert ctx.commands["caduceus"]["handler"]("erase confirm").startswith("Erased")
    assert not (data_dir / "vault.db").exists() and not (data_dir / "samples.db").exists()
    with open_vault() as vault:
        assert vault.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == 0


def test_registration_blueprint_schema_and_settings(plugin):
    ctx, _ = plugin
    blueprint = ctx.blueprints["hae-sync"]
    assert blueprint["title"] == "Health Auto Export sync"
    assert blueprint["slots"][0]["default"] == "06:30"
    assert blueprint["schedule_template"] == "{minute} {hour} * * *"
    assert "[SILENT]" in blueprint["prompt_template"]
    assert "sync" in ctx.tools["caduceus_capture"]["schema"]["parameters"]["properties"]["op"]["enum"]
    manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    assert "  hae_mcp_server:\n    type: str\n    default: \"health-auto-export\"" in manifest
    assert "  hae_backfill_days:\n    type: int\n    default: 90" in manifest


def test_invalid_entries_are_counted_without_input_in_warnings(tmp_path, samples, vault):
    m = metric()
    m["data"] = [entry(v) for v in (True, "bad private value", float("nan"), float("inf"))]
    m["data"].append(entry(1, date="2026-11-13 00:00:00"))
    out = ingest_file(tmp_path, vault, samples, payload(m))
    assert out.status == "succeeded" and out.counts["entries_skipped"] == 5
    assert out.warnings == ["step_count: skipped 5 malformed entries"]
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0


def test_batch_boundary_never_deletes_same_run_rows(tmp_path, samples, vault, monkeypatch):
    monkeypatch.setattr(hae, "BATCH", 2)
    m = metric()
    m["data"] = [entry(100, date=DAY + f" {h:02}:00:00 +0900") for h in range(5)]
    out = ingest_file(tmp_path, vault, samples, payload(m))
    assert out.counts["samples"] == 5 and out.counts["samples_replaced"] == 0
    assert trends.trend(samples, "steps", bucket="day")["points"][0]["value"] == 500


def test_malformed_single_day_protocol_is_skipped(samples, vault):
    out = sync(lambda tool, args: {"ok": True, "result": "not JSON"}, samples, vault)
    assert out.status == "succeeded" and out.counts["windows_skipped"] == 3
    assert all("request window " + DAY in w for w in out.warnings)


def test_begin_run_interrupts_stale_runs(vault):
    src = sources.virtual_source(vault, "hae_sync", "synthetic", "Synthetic sync")
    first = sources.begin_run(vault, src, "hae_sync", "1")
    second = sources.begin_run(vault, src, "hae_sync", "1")
    assert sources.run_info(vault, first.id)["status"] == "interrupted"
    assert sources.run_info(vault, second.id)["status"] == "running"
