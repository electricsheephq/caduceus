"""Synthetic regressions for heart-rate buckets, parser re-reads and distinct tallies."""
import json
from datetime import timedelta

import pytest

from caduceus.core import sources
from caduceus.core.db import open_samples
from caduceus.health import hae
from test_v032_hae_shapes import TODAY, _bucket, _metric, _server, _sync


def _heart(day=TODAY, separator=" ", offset="+0700", **values):
    entry = _bucket(day, separator, **{"context": {}, **values})
    return {key: value.replace("+0900", offset) if key in ("start", "end") else value
            for key, value in entry.items()}


def _rows(samples):
    return [tuple(row) for row in samples.execute(
        "SELECT type,local_day,value FROM sample ORDER BY type,local_day,value")]


def _seed(vault, *, version="2", status="succeeded", since, through, server="synthetic-hae",
          parser="hae_sync"):
    src = sources.virtual_source(vault, "hae_sync", server, "Synthetic HAE")
    run = sources.begin_run(vault, src, parser, version)
    sources.finish_import(vault, run.id, counts={"since": str(since), "through": str(through)}, status=status)
    return src


def _quantity_starts(seen):
    return [args["start"] for tool, args, data in seen if args.get("aggregate")]


@pytest.mark.parametrize("separator", [" ", "\u202f"])
def test_heart_rate_min_avg_max_sync(samples, vault, separator):
    entries = [_heart(day, separator, offset, min=43, avg=83.2461, max=159)
               for day, offset in ((TODAY, "+0900"), (TODAY + timedelta(days=1), "+0700"))]
    result = _sync(_server([_metric("heart_rate", entries, "count/min")]), samples, vault)
    assert result.status == "succeeded", result
    assert result.warnings == []
    assert result.counts["skip_reasons"]["metrics"] == {}
    assert result.counts["samples"] == 2
    assert [tuple(row) for row in samples.execute(
        "SELECT type,local_day,value,unit,tz_offset_min FROM sample ORDER BY local_day")] == [
            ("HKQuantityTypeIdentifierHeartRate", str(day), 83.2461, "count/min", offset)
            for day, offset in ((TODAY, 540), (TODAY + timedelta(days=1), 420))]
    diagnostic = json.dumps([result.counts, result.warnings])
    assert all(text not in diagnostic for text in ("heart_rate", "83.2461", '"min"', '"max"'))


@pytest.mark.parametrize("values,expected", [
    ({"avg": 83.2461, "Avg": 92.531, "qty": 107.981}, 83.2461),
    ({"Avg": 92.531, "qty": 107.981}, 92.531),
    ({"qty": 107.981}, 107.981),
    ({"avg": 0, "Avg": 92.531}, 0),
])
def test_heart_rate_value_precedence(samples, vault, values, expected):
    result = _sync(_server([_metric("heart_rate", [_heart(**values)], "bpm")]), samples, vault)
    assert result.status == "succeeded"
    assert tuple(samples.execute("SELECT value,unit FROM sample").fetchone()) == (expected, "count/min")


def test_heart_rate_missing_value_skips_without_payload_leaks(samples, vault):
    entry = _heart(min=43.753, max=159.827, source="Synthetic Private Device",
                   context={"metric": "Synthetic Private Metric"})
    result = _sync(_server([_metric("heart_rate", [entry], "count/min")]), samples, vault)
    assert result.status == "failed"
    assert result.counts["skip_reasons"]["metrics"] == {"missing": 1}
    assert samples.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
    diagnostic = json.dumps([result.counts, result.warnings])
    assert all(text not in diagnostic for text in (
        "43.753", "159.827", "Synthetic Private Device", "Synthetic Private Metric"))
    # Existing malformed-entry warnings retain their fixed, supported metric label.
    assert "heart_rate" not in json.dumps(result.counts)


def test_json_export_reads_lowercase_heart_rate(samples, vault, tmp_path):
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps({"data": {"metrics": [
        _metric("heart_rate", [_heart(min=43, avg=83.2461, max=159)], "count/min")]}}))
    src, _ = sources.store_source(vault, path, "hae")
    result = hae.import_hae(samples, vault, src)
    assert result.status == "succeeded"
    assert result.counts["samples"] == 1
    assert samples.execute("SELECT value FROM sample").fetchone()[0] == 83.2461


def test_parser_upgrade_rereads_all_history_once(samples, vault, tmp_path):
    earliest, d0, d1 = TODAY - timedelta(days=30), TODAY - timedelta(days=25), TODAY - timedelta(days=3)
    _seed(vault, status="failed", since=earliest, through=d1)
    src = _seed(vault, since=d0, through=d1)
    # Unrelated sources/parsers must not broaden this source's re-read.
    _seed(vault, since=earliest - timedelta(days=10), through=d1, server="synthetic-other")
    _seed(vault, since=earliest - timedelta(days=10), through=d1, parser="hae_json")
    days = [earliest + timedelta(days=n) for n in range(31)]
    metrics = [
        _metric("step_count", [_bucket(day, qty=876543) for day in days]),
        _metric("heart_rate", [_heart(day, avg=83.2461) for day in days], "count/min"),
        _metric("sleep_analysis", [_bucket(day, totalSleep=7.25) for day in days], "hr"),
    ]
    # Simulate stored partial history, including a value that the re-read must replace.
    hae.ingest_payload(samples, {"metrics": [
        _metric("step_count", [_bucket(d0, qty=765432)]), metrics[-1]]}, src.id,
        run_state={}, counts=hae._counts(), warnings=[])
    seen = []
    result = _sync(_server(metrics, seen=seen), samples, vault)
    assert result.status == "succeeded", result
    assert _quantity_starts(seen)[0] == str(earliest)
    assert result.counts["since"] == str(earliest)
    assert result.counts["reread"] is True
    assert vault.execute("SELECT parser_version FROM import_run WHERE id=?", (result.run_id,)).fetchone()[0] == "3"
    assert result.counts["samples_replaced"] == len(days) + 1

    path = tmp_path / "synthetic-full.json"
    path.write_text(json.dumps({"data": {"metrics": metrics}}))
    fresh_src, _ = sources.store_source(vault, path, "hae")
    fresh = open_samples(tmp_path / "fresh-samples.db")
    try:
        assert hae.import_hae(fresh, vault, fresh_src).status == "succeeded"
        assert _rows(samples) == _rows(fresh)
    finally:
        fresh.close()
    stored = _rows(samples)
    seen.clear()
    second = _sync(_server(metrics, seen=seen), samples, vault)
    assert second.status == "succeeded"
    assert _quantity_starts(seen)[0] == str(TODAY - timedelta(days=2))
    assert "reread" not in second.counts
    assert _rows(samples) == stored


def test_failed_reread_retries_from_history(samples, vault):
    earliest, d1 = TODAY - timedelta(days=20), TODAY - timedelta(days=3)
    _seed(vault, since=earliest, through=d1)
    seen = []
    def fail(tool, args):
        seen.append((tool, args, {}))
        return {"ok": False}
    failed = _sync(fail, samples, vault)
    assert failed.status == "failed"
    assert failed.counts.get("reread") is True
    assert failed.counts["since"] == str(earliest)
    seen.clear()
    retry = _sync(_server(seen=seen), samples, vault)
    assert retry.status == "succeeded"
    assert retry.counts.get("reread") is True
    assert _quantity_starts(seen)[0] == str(earliest)


@pytest.mark.parametrize("old_since", [TODAY - timedelta(days=5000), TODAY])
def test_reread_start_is_bounded_by_floor_and_previous_overlap(samples, vault, old_since):
    d1 = TODAY - timedelta(days=3)
    _seed(vault, since=old_since, through=d1)
    # Stop at the first request; this checks a ten-year floor without a large sync.
    result = _sync(lambda tool, args: {"ok": False}, samples, vault)
    expected = max(min(old_since, d1 - timedelta(days=2)), TODAY - timedelta(days=3650))
    assert result.counts["since"] == str(expected)
    assert result.counts.get("reread") is True


def test_adjacent_windows_tally_distinct_sample_keys(samples, vault):
    first = TODAY - timedelta(days=14)
    metrics = [_metric("step_count", [_bucket(first + timedelta(days=n), qty=876543) for n in range(15)])]
    seen = []
    result = hae.sync_hae(_server(metrics, seen=seen), samples, vault,
                          server="synthetic-hae", backfill_days=15, today=TODAY)
    assert result.status == "succeeded"
    assert len(_quantity_starts(seen)) == 2
    assert result.counts["entries_by_kind"]["metrics"]["parsed"] == 16  # inclusive boundary twice
    assert result.counts["samples"] == samples.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 15
