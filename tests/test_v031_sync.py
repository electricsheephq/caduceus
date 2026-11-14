"""Real-shaped HAE MCP replies, generated locally; no personal data or network."""
import json
import re
from datetime import date, timedelta

import pytest

from caduceus.core import sources
from caduceus.health import hae


TODAY = date(2026, 11, 14)


def _bucket(day=TODAY, **values):
    return {"start": f"{day} 12:00:00 AM +0900", "end": f"{day} 11:59:59 PM +0900",
            "sources": "Synthetic Watch | Synthetic Watch", **values}


def _metric(name="step_count", entries=None, units="count"):
    return {"name": name, "units": units,
            "data": [_bucket(qty=8123)] if entries is None else entries}


def _sleep(day=TODAY):
    return _bucket(day, asleep=0, inBed=0, core=4, deep=1, rem=2, totalSleep=7.5)


def _workout(day=TODAY):
    return {"name": "Synthetic Walk", "start": f"{day} 12:30:00 PM +0900",
            "end": f"{day} 01:30:00 PM +0900", "sources": "Synthetic Watch"}


def _reply(**data):
    return {"ok": True, "result": json.dumps({"data": data})}


def _server(metrics=(), workouts=(), *, seen=None, inclusive=True):
    def call(tool, args):
        if seen is not None:
            seen.append((tool, dict(args)))
        first, last = (date.fromisoformat(args[key]) for key in ("start", "end"))

        def inside(entry):
            day = date.fromisoformat(entry["start"][:10])
            return first <= day and (day <= last if inclusive else day < last)

        if tool == "get_workouts":
            return _reply(workouts=[entry for entry in workouts if inside(entry)])
        requested = args["metrics"].split(",")
        selected = [{**metric, "data": [entry for entry in metric["data"] if inside(entry)]}
                    for metric in metrics if metric["name"] in requested]
        return _reply(metrics=[metric for metric in selected if metric["data"]])
    return call


def _sync(call, samples, vault, *, days=1, today=TODAY):
    return hae.sync_hae(call, samples, vault, server="synthetic-hae", backfill_days=days,
                        today=today, local_offset="+0700")


@pytest.mark.parametrize("text,hour,minute,second", [
    ("2026-11-14 12:00:00 AM +0900", 0, 0, 0),
    ("2026-11-14 12:30:00 PM +0900", 12, 30, 0),
    ("2026-11-14 01:05:09 PM +0900", 13, 5, 9),
    ("2026-11-14 09:05:00 pm +0900", 21, 5, 0),
    ("2026-11-14 1:05:09P.M. +09:00", 13, 5, 9),
    ("2026-11-14 21:05:00 +0900", 21, 5, 0),
    ("2026-11-14T21:05:00+09:00", 21, 5, 0),
])
def test_date_accepts_locale_independent_times(text, hour, minute, second):
    moment = hae._date(text)
    assert (moment.date(), moment.hour, moment.minute, moment.second) == (TODAY, hour, minute, second)
    assert moment.utcoffset() == timedelta(hours=9)


@pytest.mark.parametrize("text", [
    "2026-11-14 00:10:00 AM +0900", "2026-11-14 13:00:00 PM +0900",
    "2026-11-14 12:00:00 AM", "2026-11-14 21:05:00", "2026-11-14T21:05:00",
])
def test_date_rejects_invalid_hours_and_missing_offsets(text):
    with pytest.raises(ValueError):
        hae._date(text)


def test_full_sync_real_shapes_keep_phone_days_and_sleep_nights(samples, vault):
    quantities = [("step_count", "StepCount", 8123, "count", "count"),
                  ("weight_body_mass", "BodyMass", 72.5, "kg", "kg"),
                  ("resting_heart_rate", "RestingHeartRate", 58, "count/min", "count/min"),
                  ("heart_rate_variability", "HeartRateVariabilitySDNN", 45, "ms", "ms"),
                  ("vo2_max", "VO2Max", 42, "ml/(kg·min)", "mL/min·kg")]
    metrics = [_metric(name, [_bucket(qty=value)], unit) for name, _, value, unit, _ in quantities]
    times = ["12:10:00 AM", "12:30:00 PM", "09:05:00 pm"]
    metrics.append(_metric("heart_rate", [{"start": f"{TODAY} {t} +0900",
        "end": f"{TODAY} {t} +0900", "qty": 65, "sources": "Synthetic Watch"} for t in times], "count/min"))
    metrics.append(_metric("sleep_analysis", [_sleep()], "hr"))
    result = _sync(_server(metrics, [_workout()]), samples, vault)
    assert result.status == "succeeded", result
    assert result.warnings == [] and "unreadable_kinds" not in result.counts
    for _, kind, value, _, unit in quantities:
        row = samples.execute("SELECT * FROM sample WHERE type=?", ("HKQuantityTypeIdentifier" + kind,)).fetchone()
        assert (row["local_day"], row["value"], row["unit"], row["tz_offset_min"]) == (str(TODAY), value, unit, 540)
        assert row["start_utc"] == "2026-11-13T15:00:00Z"
        assert row["end_utc"] == "2026-11-14T14:59:59Z"
        assert row["source_name"] == "Health Auto Export"
    assert [tuple(row) for row in samples.execute(
        "SELECT start_utc,local_day FROM sample WHERE type='HKQuantityTypeIdentifierHeartRate' ORDER BY start_utc")] == [
            ("2026-11-13T15:10:00Z", str(TODAY)), ("2026-11-14T03:30:00Z", str(TODAY)),
            ("2026-11-14T12:05:00Z", str(TODAY))]
    sleep = samples.execute("SELECT * FROM sample WHERE type='HKCategoryTypeIdentifierSleepAnalysis'").fetchone()
    assert (sleep["start_utc"], sleep["end_utc"], sleep["local_day"], sleep["tz_offset_min"]) == (
        "2026-11-13T15:00:00Z", "2026-11-13T22:30:00Z", str(TODAY), 540)
    export_summary = {"date": f"{TODAY} 00:00:00 +0900", "sleepEnd": f"{TODAY} 07:30:00 +0900", "totalSleep": 7.5}
    assert hae._metric_rows("sleep_analysis", {}, export_summary, None)[0][7] == sleep["local_day"]
    assert tuple(samples.execute("SELECT day,value FROM daily_rollup WHERE metric='sleep_hours'").fetchone()) == (
        str(TODAY), 7.5)
    assert samples.execute("SELECT duration FROM workout").fetchone()[0] == 3600
    assert result.counts["entries_by_kind"] == {
        "metrics": {"parsed": 8, "skipped": 0}, "sleep": {"parsed": 1, "skipped": 0},
        "workouts": {"parsed": 1, "skipped": 0}}


@pytest.mark.parametrize("inclusive", [True, False])
def test_neighbouring_windows_keep_one_bucket_without_doubling_steps(samples, vault, inclusive):
    boundary = TODAY - timedelta(days=2)
    result = _sync(_server([_metric(entries=[_bucket(boundary, qty=8123)])], inclusive=inclusive),
                   samples, vault, days=17)
    assert result.status == "succeeded", result
    assert result.counts["entries_by_kind"]["metrics"]["parsed"] == (2 if inclusive else 1)
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 1
    assert tuple(samples.execute("SELECT day,value FROM daily_rollup WHERE metric='steps'").fetchone()) == (
        str(boundary), 8123)


def test_empty_ranges_succeed_without_unreadable_warning(samples, vault):
    result = _sync(lambda tool, args: _reply(metrics=[]), samples, vault, days=40)
    assert result.status == "succeeded" and result.warnings == []
    assert result.counts["samples"] == result.counts["workouts"] == result.counts["entries_skipped"] == 0
    assert "unreadable_kinds" not in result.counts
    assert all(tally == {"parsed": 0, "skipped": 0} for tally in result.counts["entries_by_kind"].values())


def test_requests_use_plain_phone_dates_weight_name_and_overlap(samples, vault):
    seen = []
    result = _sync(_server(seen=seen), samples, vault, days=40)
    assert result.status == "succeeded"
    since = date.fromisoformat(result.counts["since"])
    for kind, size in (("metrics", 14), ("sleep", 7), ("workouts", 31)):
        actual = [(args["start"], args["end"]) for tool, args in seen if
                  ("workouts" if tool == "get_workouts" else
                   "sleep" if args["metrics"] == "sleep_analysis" else "metrics") == kind]
        start = since - timedelta(days=kind == "sleep")
        expected, covered = [], set()
        while start <= TODAY:
            last = min(TODAY, start + timedelta(days=size - 1))
            expected.append((str(start), str(last + timedelta(days=1))))
            covered.update(start + timedelta(days=k) for k in range((last - start).days + 1))
            start = last + timedelta(days=1)
        assert actual == expected
        assert all(since + timedelta(days=k) in covered for k in range(40))
        if kind == "sleep":
            assert since - timedelta(days=1) in covered
    for tool, args in seen:
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[key]) for key in ("start", "end"))
        if tool == "get_health_metrics":
            if args["metrics"] == "sleep_analysis":
                assert args["aggregate"] is False
            else:
                assert args["metrics"].split(",") == [name for name in hae.MAPPING if name not in ("body_mass", "sleep_analysis")]
                assert "weight_body_mass" in args["metrics"].split(",")


@pytest.mark.parametrize("kind,label", [("metrics", "health metric"), ("sleep", "sleep"), ("workouts", "workout")])
def test_unreadable_kind_fails_and_retry_keeps_start_without_duplicates(samples, vault, kind, label):
    first_day = TODAY - timedelta(days=34)
    metrics = [_metric(entries=[_bucket(first_day, qty=8123)]), _metric("sleep_analysis", [_sleep(first_day)], "hr")]
    workouts = [_workout(first_day)]
    target = workouts[0] if kind == "workouts" else metrics[1 if kind == "sleep" else 0]["data"][0]
    original = dict(target)
    target.clear()
    target.update(start=original["start"], unknown_shape="untrusted synthetic server text")
    failed = _sync(_server(metrics, workouts), samples, vault, days=35)
    warning = (f"Health Auto Export sent {label} data that Caduceus cannot read, so none was imported. "
               "The next sync reads these days again.")
    assert failed.status == "failed" and failed.warnings[-1] == warning
    assert failed.counts["unreadable_kinds"] == [kind]
    assert failed.counts["entries_by_kind"][kind] == {"parsed": 0, "skipped": 1}
    assert sources.run_info(vault, failed.run_id)["status"] == "failed"
    assert "untrusted synthetic server text" not in json.dumps(sources.run_info(vault, failed.run_id))
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] > 0
    target.clear()
    target.update(original)
    recovered = _sync(_server(metrics, workouts), samples, vault, days=1, today=TODAY + timedelta(days=5))
    assert recovered.status == "succeeded", recovered
    assert recovered.counts["since"] == failed.counts["since"] == str(first_day)
    assert "unreadable_kinds" not in recovered.counts
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 2
    assert samples.execute("SELECT count(*) FROM workout").fetchone()[0] == 1
    assert samples.execute("SELECT value FROM daily_rollup WHERE metric='steps'").fetchone()[0] == 8123
    assert samples.execute("SELECT value FROM daily_rollup WHERE metric='sleep_hours'").fetchone()[0] == 7.5


@pytest.mark.parametrize("kind", ["metrics", "sleep", "workouts"])
def test_partial_skips_across_windows_do_not_fail_kind(samples, vault, kind):
    bad = {"start": f"{TODAY - timedelta(days=34)} 12:00:00 AM +0900"}
    metrics = [_metric(entries=[bad, _bucket(qty=8123)])] if kind == "metrics" else []
    if kind == "sleep":
        metrics = [_metric("sleep_analysis", [bad, _sleep()], "hr")]
    result = _sync(_server(metrics, [bad, _workout()] if kind == "workouts" else []), samples, vault, days=35)
    assert result.status == "succeeded", result
    # The last sleep bucket appears in both inclusive neighbouring requests.
    assert result.counts["entries_by_kind"][kind] == {"parsed": 2 if kind == "sleep" else 1, "skipped": 1}
    assert "unreadable_kinds" not in result.counts
    if kind != "workouts":
        assert result.warnings == [f"{'step_count' if kind == 'metrics' else 'sleep_analysis'}: skipped 1 malformed entries"]


def test_unknown_metrics_do_not_count_as_unreadable_entries(samples, vault):
    result = _sync(lambda tool, args: _reply(metrics=[_metric("synthetic_unknown", [{}])])
                   if args.get("aggregate") else _reply(metrics=[]), samples, vault)
    assert result.status == "succeeded" and result.warnings == []
    assert result.counts["metrics_ignored"] == {"synthetic_unknown": 1}
    assert result.counts["entries_by_kind"]["metrics"] == {"parsed": 0, "skipped": 0}


@pytest.mark.parametrize("entry", [{"qty": 1}, {**_bucket(qty=1), "date": "invalid"}])
def test_missing_or_invalid_primary_date_is_skipped(samples, vault, entry):
    result = _sync(lambda tool, args: _reply(metrics=[_metric(entries=[entry])])
                   if args.get("aggregate") else _reply(metrics=[]), samples, vault)
    assert result.status == "failed" and result.counts["unreadable_kinds"] == ["metrics"]
    assert result.counts["entries_skipped"] == 1


def test_unreadable_kind_failure_does_not_apply_to_files(samples, vault, tmp_path):
    path = tmp_path / "synthetic-unreadable.json"
    path.write_text(json.dumps({"data": {"metrics": [_metric("sleep_analysis", [{}], "hr")]}}))
    src, _ = sources.store_source(vault, path, "hae")
    result = hae.import_hae(samples, vault, src)
    assert result.status == "succeeded" and result.counts["entries_skipped"] == 1
    assert "entries_by_kind" not in result.counts and "unreadable_kinds" not in result.counts
