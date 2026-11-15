"""Synthetic HAE Mac reply shapes; no personal data or network."""
import json
from datetime import date, timedelta

import pytest

from caduceus.health import hae


TODAY = date(2026, 11, 14)


def _bucket(day=TODAY, separator="\u202f", **values):
    return {"start": f"{day} 12:00:00{separator}AM +0900",
            "end": f"{day} 11:59:59{separator}PM +0900", "sources": "", **values}


def _metric(name, entries, units="count"):
    return {"name": name, "units": units, "data": entries}


def _workout(separator="\u202f"):
    return {"id": "synthetic-workout", "name": "Synthetic Walk", "location": "Synthetic Park",
            "start": f"{TODAY} 1:05:09{separator}PM +0900",
            "end": f"{TODAY} 2:05:09{separator}PM +0900", "isIndoor": False,
            "duration": 3599.25,
            "source": {"name": "Synthetic Private Watch", "identifier": "synthetic-device"},
            "activeEnergyBurned": {"units": "kJ", "qty": 450},
            "distance": {"units": "km", "qty": 4},
            "avgSpeed": {"units": "km/hr", "qty": 4},
            "maxSpeed": {"units": "km/hr", "qty": 5},
            "speed": {"units": "km/hr", "qty": 4},
            "intensity": {"units": "kcal/hr\u00b7kg", "qty": 3},
            "temperature": {"units": "degC", "qty": 15},
            "humidity": {"units": "%", "qty": 50},
            "estimatedEffortScore": {"units": "count", "qty": 2},
            "elevationUp": {"units": "m", "qty": 10},
            "events": [{"event": {"name": "Synthetic Pause"}}],
            "activities": [{"activity": {"name": "Synthetic Walk"}}],
            "segments": [{"segment": {"distance": 1}}]}


def _server(metrics=(), workouts=(), *, seen=None):
    def call(tool, args):
        first, last = (date.fromisoformat(args[key]) for key in ("start", "end"))

        def inside(entry):
            return first <= date.fromisoformat(entry["start"][:10]) <= last

        if tool == "get_workouts":
            data = {"workouts": [entry for entry in workouts if inside(entry)]}
        else:
            requested = args["metrics"].split(",")
            data = {"metrics": [{**metric, "data": [entry for entry in metric["data"] if inside(entry)]}
                                for metric in metrics if metric["name"] in requested]}
        if seen is not None:
            seen.append((tool, dict(args), data))
        return {"ok": True, "result": json.dumps({"data": data})}
    return call


def _sync(call, samples, vault):
    return hae.sync_hae(call, samples, vault, server="synthetic-hae", backfill_days=1, today=TODAY)


@pytest.mark.parametrize("separator", [" ", "\u202f", "\u00a0"])
def test_full_sync_real_mac_shapes(samples, vault, separator):
    metrics = [_metric("step_count", [_bucket(day, separator, qty=876543)
                                      for day in (TODAY, TODAY + timedelta(days=1))]),
               _metric("sleep_analysis", [_bucket(separator=separator, asleep=0, inBed=8,
                       totalSleep=7.5, core=4, deep=1, rem=2,
                       sources="Synthetic Person\u2019s Watch")], "hr")]
    seen = []
    result = _sync(_server(metrics, [_workout(separator)], seen=seen), samples, vault)
    assert result.status == "succeeded", result
    assert "unreadable_kinds" not in result.counts and result.warnings == []
    assert result.counts["entries_skipped"] == 0
    assert result.counts["entries_by_kind"] == {
        "metrics": {"parsed": 2, "skipped": 0}, "sleep": {"parsed": 1, "skipped": 0},
        "workouts": {"parsed": 1, "skipped": 0}}
    quantity_request = next((args, data) for tool, args, data in seen if args.get("aggregate"))
    assert quantity_request[0]["start"] == str(TODAY)
    assert quantity_request[0]["end"] == str(TODAY + timedelta(days=1))
    assert len(quantity_request[1]["metrics"][0]["data"]) == 2
    assert [tuple(row) for row in samples.execute(
        "SELECT local_day,value,source_name FROM sample WHERE type='HKQuantityTypeIdentifierStepCount' "
        "ORDER BY local_day")] == [(str(day), 876543, "Health Auto Export")
                                  for day in (TODAY, TODAY + timedelta(days=1))]
    sleep = samples.execute("SELECT * FROM sample WHERE type='HKCategoryTypeIdentifierSleepAnalysis'").fetchone()
    assert (sleep["local_day"], sleep["start_utc"], sleep["end_utc"]) == (
        str(TODAY), "2026-11-13T19:30:00Z", "2026-11-14T03:00:00Z")
    assert samples.execute("SELECT value FROM daily_rollup WHERE metric='sleep_hours'").fetchone()[0] == 7.5
    workouts = samples.execute("SELECT * FROM workout").fetchall()
    assert len(workouts) == 1
    workout = workouts[0]
    assert (workout["source_name"], workout["duration"], workout["duration_unit"]) == (
        "Synthetic Private Watch", 3599.25, "s")
    assert (workout["start_utc"], workout["end_utc"]) == (
        "2026-11-14T04:05:09Z", "2026-11-14T05:05:09Z")
    stats = json.loads(workout["statistics_json"])
    assert stats["intensity"] == {"units": "kcal/hr\u00b7kg", "qty": 3}
    assert all(key not in stats for key in ("events", "activities", "segments", "source"))


@pytest.mark.parametrize("text,hour", [
    ("2026-11-14 12:05:09\u202fAM +0900", 0),
    ("2026-11-14 1:05:09\u202fPM +0900", 13),
    ("2026-11-14 01:05:09\u00a0pm +0900", 13),
    ("2026-11-14 01:05:09 a.\u00a0m. +0900", 1),
    ("2026-11-14\u00a001:05:09\u202fPM\u00a0+0900", 13),
    ("2026-11-14\u200901:05:09\u2009p.\u2009M.\u2009+09:00", 13),
    ("2026-11-14 1:05:09PM +0900", 13),
    ("2026-11-14\u00a013:05:09\u2009+0900", 13),
    ("2026-11-14T13:05:09+09:00", 13),
])
def test_date_unicode_separators_and_marks(text, hour):
    moment = hae._date(text)
    assert (moment.date(), moment.hour, moment.minute, moment.second) == (TODAY, hour, 5, 9)
    assert moment.utcoffset() == timedelta(hours=9)


@pytest.mark.parametrize("text", [
    "2026-11-14 01:05:09\u202fXM +0900", "2026-11-14 01:05:09 QQ +0900",
    "2026-11-14 13:00:00\u202fPM +0900", "2026-11-14 00:10:00\u202fAM +0900",
    "2026-11-14 01:05:09\u202fPM", "2026-11-14 13:05:09", None,
])
def test_date_never_drops_marks_or_offset(text):
    with pytest.raises(ValueError):
        hae._date(text)


@pytest.mark.parametrize("text", [
    "2026-11-14 12:00:00.1 AM +0900", "2026-11-14 12:00:00.123 PM +0900", "2026-11-14 1:05:09.123456 PM +0900",
])
def test_date_rejects_fractions_with_marks(text):
    """0.3.1 never read a fraction with AM/PM; it still raises rather than guessing."""
    with pytest.raises(ValueError):
        hae._date(text)


def test_date_rejects_long_text_quickly():
    """A long run of spaces cannot make the parser backtrack: text over 64 characters is refused first."""
    import time
    for text in ("2026-11-14 1:05:09" + " " * 2000 + "PM", "2026-11-14 1:05:09 " + "A" * 70 + " +0900"):
        started = time.perf_counter()
        with pytest.raises(ValueError):
            hae._date(text)
        assert time.perf_counter() - started < 0.5


def test_unknown_metric_marks_count_only_time_reasons(samples, vault):
    entries = [_bucket(qty=876543) for _ in range(3)]
    for entry in entries:
        entry["start"] = f"{TODAY} 01:05:09\u202fXM +0900"
        entry["source"] = "Synthetic Private Watch"
    result = _sync(_server([_metric("step_count", entries)]), samples, vault)
    assert result.status == "failed"
    assert result.counts["skip_reasons"]["metrics"] == {"time": len(entries)}
    assert "times not recognised" in result.warnings[-1]
    assert result.counts["entries_skipped"] == len(entries)
    assert "876543" not in json.dumps([result.counts, result.warnings])
    assert "Synthetic Private Watch" not in json.dumps([result.counts, result.warnings])


def test_workout_source_without_name_counts_only_source_reason(samples, vault):
    workout = _workout()
    workout["source"] = {"identifier": "synthetic-private-device"}
    result = _sync(_server(workouts=[workout]), samples, vault)
    assert result.status == "failed"
    assert result.counts["skip_reasons"]["workouts"] == {"source": 1}
    assert "source not recognised" in result.warnings[-1]
    assert "synthetic-private-device" not in json.dumps([result.counts, result.warnings])
    assert "3599.25" not in json.dumps([result.counts, result.warnings])


@pytest.mark.parametrize("kind", ["metrics", "sleep", "workouts"])
@pytest.mark.parametrize("code,words", [
    ("time", "times not recognised"), ("source", "source not recognised"),
    ("number", "values not numbers"), ("missing", "fields missing"),
    ("interval", "end before start"), ("other", "unexpected format"),
])
def test_skip_reason_codes_and_warnings(samples, vault, kind, code, words):
    entry = _workout() if kind == "workouts" else _bucket(qty=876543)
    if kind == "sleep":
        entry.update(value="Core")
    if code == "time":
        entry["end"] = "unrecognised synthetic date"
    elif code == "source":
        entry["source"] = {"identifier": "synthetic-private-device"}
    elif code == "number":
        if kind == "sleep":
            del entry["value"]
        entry["duration" if kind == "workouts" else "totalSleep" if kind == "sleep" else "qty"] = float("nan")
    elif code == "missing":
        del entry["end" if kind == "workouts" else "start"]
    elif code == "interval":
        entry["end"] = f"{TODAY - timedelta(days=1)} 11:59:59\u202fPM +0900"
    elif kind == "workouts":
        entry["name"] = 123
    elif kind == "sleep":
        del entry["value"]
        entry["totalSleep"] = 25
    metric = _metric("sleep_analysis" if kind == "sleep" else "step_count", [entry])
    if code == "other" and kind == "metrics":
        metric["units"] = 123
    # A fixed reply also exercises missing starts without the fake server inspecting them.
    data = {"workouts": [entry]} if kind == "workouts" else {"metrics": [metric]}
    def call(tool, args):
        requested_kind = "workouts" if tool == "get_workouts" else (
            "sleep" if args["metrics"] == "sleep_analysis" else "metrics")
        return {"ok": True, "result": json.dumps({"data": data if requested_kind == kind else {}})}
    result = _sync(call, samples, vault)
    assert result.status == "failed"
    assert result.counts["skip_reasons"][kind] == {code: 1}
    assert words in result.warnings[-1]
    assert result.counts["entries_skipped"] == 1


@pytest.mark.parametrize("source,expected", [
    ({}, "Health Auto Export"), ({"source": "Synthetic Watch"}, "Synthetic Watch"),
    ({"source": ""}, ""),
    ({"source": {"name": "Synthetic Watch", "identifier": "synthetic"}}, "Synthetic Watch"),
])
def test_source_accepts_text_or_named_object(source, expected):
    assert hae._source(source) == expected


@pytest.mark.parametrize("source", [None, 123, [], {}, {"identifier": "synthetic"},
                                    {"name": ""}, {"name": 123}])
def test_source_rejects_invalid_objects(source):
    with pytest.raises(ValueError):
        hae._source({"source": source})
