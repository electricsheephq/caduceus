"""Synthetic regressions for bounded retries of unreadable HAE request windows."""
import json
import threading
from datetime import date, timedelta

import pytest

from caduceus.core import sources
from caduceus.health import hae
from test_v032_hae_shapes import TODAY, _bucket, _metric, _server, _sync, _workout
from test_v033_hae import _quantity_starts, _seed


def _gapped_server(gap, *, truncated=False, metrics=(), seen=None):
    readable = _server(metrics, seen=seen)

    def call(tool, args):
        start, end = (date.fromisoformat(args[key]) for key in ("start", "end"))
        if args.get("aggregate") and start <= gap < end:
            if seen is not None:
                seen.append((tool, dict(args), {}))
            return {"ok": True, "truncated": truncated,
                    "result": "Synthetic Private Metric 876543 Synthetic Private Device"}
        return readable(tool, args)
    return call


def _stored(vault, result):
    return sources.run_info(vault, result.run_id)["counts"]


def test_reread_gap_refills_once_then_returns_to_overlap(samples, vault):
    earliest = TODAY - timedelta(days=40)
    gap = earliest + timedelta(days=14)
    _seed(vault, since=earliest, through=TODAY - timedelta(days=3))
    metrics = [_metric("step_count", [_bucket(earliest, qty=876543)])]
    first = _sync(_gapped_server(gap, metrics=metrics), samples, vault)
    assert first.status == "succeeded" and first.counts["reread"] is True
    assert first.counts["windows_empty"] == 1
    assert first.counts["refill_from"] == str(gap)
    assert _stored(vault, first)["refill_from"] == str(gap)
    assert first.counts["refill_tries"] == 1

    seen = []
    second = _sync(_server(seen=seen), samples, vault)
    assert second.status == "succeeded" and second.counts["refill"] is True
    assert _quantity_starts(seen)[0] == second.counts["since"] == str(gap)
    assert "reread" not in second.counts
    assert "refill_from" not in second.counts and "refill_tries" not in second.counts
    assert _stored(vault, second) == second.counts
    seen.clear()
    third = _sync(_server(seen=seen), samples, vault)
    assert _quantity_starts(seen)[0] == str(TODAY - timedelta(days=2))
    assert all(key not in third.counts for key in ("refill", "refill_from", "refill_tries"))


def test_normal_gap_refills_after_failed_and_cancelled_runs(samples, vault):
    earliest = TODAY - timedelta(days=40)
    gap = earliest + timedelta(days=14)
    _seed(vault, version=hae.PARSER_VERSION, since=earliest,
          through=earliest + timedelta(days=2))
    metrics = [_metric("step_count", [_bucket(earliest, qty=876543)])]
    first = _sync(_gapped_server(gap, metrics=metrics), samples, vault)
    assert first.status == "succeeded" and "reread" not in first.counts
    assert first.counts["refill_from"] == str(gap)
    assert first.counts["refill_tries"] == 1
    for status in ("failed", "cancelled"):
        cancel = threading.Event()
        if status == "cancelled":
            cancel.set()
        interrupted = hae.sync_hae(lambda tool, args: {"ok": False}, samples, vault,
                                   server="synthetic-hae", backfill_days=1, today=TODAY,
                                   cancel=cancel)
        assert interrupted.status == status
        assert "refill_from" not in interrupted.counts and "refill_tries" not in interrupted.counts
    seen = []
    second = _sync(_server(seen=seen), samples, vault)
    assert second.status == "succeeded" and second.counts["refill"] is True
    assert _quantity_starts(seen)[0] == str(gap)
    assert "refill_from" not in second.counts and "refill_tries" not in second.counts


def test_single_day_truncation_succeeds_and_keeps_other_rows(samples, vault):
    earliest = TODAY - timedelta(days=4)
    gap = earliest
    _seed(vault, version=hae.PARSER_VERSION, since=earliest,
          through=earliest + timedelta(days=2))
    days = [earliest + timedelta(days=n) for n in range(5)]
    metrics = [_metric("step_count", [_bucket(day, qty=876543) for day in days])]
    result = _sync(_gapped_server(gap, truncated=True, metrics=metrics), samples, vault)
    assert result.status == "succeeded"
    assert result.counts["windows_skipped"] == 1
    assert result.counts["refill_from"] == str(gap)
    assert result.counts["refill_tries"] == 1
    assert [tuple(row) for row in samples.execute("SELECT local_day,value FROM sample ORDER BY local_day")] == [
        (str(day), 876543) for day in days if day != gap]
    assert _stored(vault, result) == result.counts


@pytest.mark.parametrize("truncated", [False, True])
def test_refill_cap_stops_after_three_pending_runs(samples, vault, truncated):
    earliest = TODAY - timedelta(days=40)
    gap = earliest + timedelta(days=14)
    _seed(vault, version=hae.PARSER_VERSION, since=earliest,
          through=earliest + timedelta(days=2))
    for attempt in range(1, 5):
        metrics = [_metric("step_count", [_bucket(earliest, qty=876543)])] if not truncated else []
        result = _sync(_gapped_server(gap, truncated=truncated, metrics=metrics), samples, vault)
        assert result.status == "succeeded"
        if attempt <= 3:
            assert result.counts["refill_from"] == str(gap)
            assert result.counts["refill_tries"] == attempt
        else:
            assert "refill_from" not in result.counts and "refill_tries" not in result.counts
        assert _stored(vault, result) == result.counts
        cap_warnings = [warning for warning in result.warnings if "after 3 tries" in warning]
        if attempt < 4:
            assert cap_warnings == []
        else:
            last = gap if truncated else gap + timedelta(days=13)
            assert cap_warnings == [
                "Health Auto Export gave no readable reply for these days after 3 tries "
                "(it may have no data for them), "
                f"so they are not retried automatically: quantity {gap} to {last}."]
    seen = []
    fifth = _sync(_server(seen=seen), samples, vault)
    assert _quantity_starts(seen)[0] == str(TODAY - timedelta(days=2))
    assert all(key not in fifth.counts for key in ("refill", "refill_from", "refill_tries"))


def test_refill_counts_and_warnings_do_not_leak_payload(samples, vault):
    earliest = TODAY - timedelta(days=40)
    _seed(vault, version=hae.PARSER_VERSION, since=earliest,
          through=earliest + timedelta(days=2))
    gap = earliest + timedelta(days=14)
    metrics = [_metric("step_count", [_bucket(day, qty=876543, source="Synthetic Private Device")
                                      for day in (earliest, TODAY)])]
    for _ in range(4):
        result = _sync(_gapped_server(gap, metrics=metrics), samples, vault)
        assert result.status == "succeeded"
        assert result.counts.get("refill_from") == str(gap) or any(
            "after 3 tries" in warning for warning in result.warnings)
        diagnostic = json.dumps([result.counts, result.warnings, _stored(vault, result),
                                 sources.run_info(vault, result.run_id)["errors"]])
        assert all(text not in diagnostic for text in (
            "876543", "Synthetic Private Metric", "Synthetic Private Device", "step_count"))
        for key in ("since", "through", "refill_from"):
            if key in result.counts:
                assert str(date.fromisoformat(result.counts[key])) == result.counts[key]
        if "refill_tries" in result.counts:
            assert type(result.counts["refill_tries"]) is int
        if "refill" in result.counts:
            assert result.counts["refill"] is True


@pytest.mark.parametrize("version,refill_age,expected_age,lowered", [
    (hae.PARSER_VERSION, 5000, 3650, True),
    ("2", 30, 40, False),
    ("2", 50, 50, True),
    (hae.PARSER_VERSION, 1, 2, False),
])
def test_refill_composes_with_version_start_floor_and_attempts(
        samples, vault, version, refill_age, expected_age, lowered):
    src = _seed(vault, version=version, since=TODAY - timedelta(days=40), through=TODAY)
    run_id = vault.execute("SELECT max(id) FROM import_run WHERE source_id=?", (src.id,)).fetchone()[0]
    sources.finish_import(vault, run_id, status="succeeded", counts={
        "since": str(TODAY - timedelta(days=40)), "through": str(TODAY),
        "refill_from": str(TODAY - timedelta(days=refill_age)), "refill_tries": 2})
    # Refuse later requests after capturing the first: no ten-year data generation.
    seen = []
    def stop(tool, args):
        seen.append(args)
        return {"ok": False}
    result = _sync(stop, samples, vault)
    assert result.counts["since"] == str(TODAY - timedelta(days=expected_age))
    assert result.counts.get("refill", False) is lowered
    if expected_age == 3650:
        return
    # Exercise counter advancement even when overlap/version re-read already covers the gap.
    result = _sync(_gapped_server(TODAY - timedelta(days=expected_age), truncated=True), samples, vault)
    assert result.status == "succeeded" and result.counts["refill_tries"] == 3
    assert result.counts["refill_from"] == str(TODAY - timedelta(days=expected_age))


@pytest.mark.parametrize("reply", ["truncated", "empty", "kind"])
def test_pending_refill_does_not_mask_unreadable_failures(samples, vault, reply):
    first = _sync(_gapped_server(TODAY, truncated=True), samples, vault)
    assert first.status == "succeeded" and first.counts["refill_from"] == str(TODAY)
    def unreadable(tool, args):
        if reply == "kind":
            data = {"metrics": [_metric("step_count", [_bucket(qty="not a number")])]} if args.get("aggregate") else {}
            return {"ok": True, "result": {"data": data}}
        return {"ok": True, "truncated": reply == "truncated", "result": "No data"}
    failed = _sync(unreadable, samples, vault)
    assert failed.status == "failed"
    assert "refill_from" not in failed.counts and "refill_tries" not in failed.counts
    recovered = _sync(_server(), samples, vault)
    assert recovered.status == "succeeded" and "refill_from" not in recovered.counts


@pytest.mark.parametrize("stored_kind", ["sample", "workout", "none"])
def test_first_sync_does_not_refill_days_before_source_data(samples, vault, stored_kind):
    earliest = TODAY - timedelta(days=40)
    first_data = TODAY - timedelta(days=5)
    other = sources.virtual_source(vault, "hae_sync", "synthetic-other", "Synthetic other")
    # Older rows in both tables belong to another source and cannot qualify these windows.
    workout = _workout()
    workout.update(start=f"{earliest}T00:30:00Z", end=f"{earliest}T01:30:00Z")
    hae.ingest_payload(samples, {"metrics": [_metric("step_count", [_bucket(earliest, qty=123)])],
                                 "workouts": [workout]}, other.id,
                       run_state={}, counts=hae._counts(), warnings=[])
    workout = _workout()
    workout.update(start=f"{first_data}T00:30:00Z", end=f"{first_data}T01:30:00Z")
    metrics = [_metric("step_count", [_bucket(first_data, qty=876543)])] if stored_kind == "sample" else []
    readable = _server(metrics, [workout] if stored_kind == "workout" else [])
    def call(tool, args):
        end = date.fromisoformat(args["end"]) - timedelta(days=1)
        if end < first_data:
            return {"ok": True, "result": "No data"}
        return readable(tool, args)
    first = hae.sync_hae(call, samples, vault, server="synthetic-hae", backfill_days=41, today=TODAY)
    assert first.status == "succeeded" and first.counts["windows_empty"] > 0
    assert first.counts["samples"] == (1 if stored_kind == "sample" else 0)
    assert first.counts["workouts"] == (1 if stored_kind == "workout" else 0)
    assert "refill_from" not in first.counts and "refill_tries" not in first.counts
    assert not any("after 3 tries" in warning for warning in first.warnings)
    assert _stored(vault, first) == first.counts
    seen = []
    second = _sync(_server(seen=seen), samples, vault)
    assert second.status == "succeeded"
    assert _quantity_starts(seen)[0] == second.counts["since"] == str(TODAY - timedelta(days=2))
    assert "refill" not in second.counts


@pytest.mark.parametrize("stored_kind", ["sample", "workout"])
@pytest.mark.parametrize("after_end", [0, 1])
def test_empty_window_cutoff_uses_source_rows_from_later_replies(samples, vault, stored_kind, after_end):
    earliest = TODAY - timedelta(days=20)
    window_end = earliest + timedelta(days=13)
    first_data = window_end + timedelta(days=after_end)
    # Sleep is a sample whose local day is later than the UTC date of its start.
    metrics = [_metric("sleep_analysis", [_bucket(first_data, totalSleep=7)], "hr")] if stored_kind == "sample" else []
    workout = _workout()
    workout.update(start=f"{first_data}T00:30:00Z", end=f"{first_data}T01:30:00Z")
    readable = _server(metrics, [workout] if stored_kind == "workout" else [])
    def call(tool, args):
        if args.get("aggregate") and args["start"] == str(earliest):
            assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
            assert samples.execute("SELECT count(*) FROM workout").fetchone()[0] == 0
            return {"ok": True, "result": "No data"}
        return readable(tool, args)
    result = hae.sync_hae(call, samples, vault, server="synthetic-hae", backfill_days=21, today=TODAY)
    assert result.status == "succeeded" and result.counts["windows_empty"] == 1
    assert result.counts["samples"] + result.counts["workouts"] == 1
    if after_end == 0:
        assert result.counts["refill_from"] == str(earliest)
        assert result.counts["refill_tries"] == 1
    else:
        assert "refill_from" not in result.counts and "refill_tries" not in result.counts
    assert _stored(vault, result) == result.counts
