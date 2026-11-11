from datetime import date, timedelta
from xml.etree import ElementTree as ET

import pytest

from caduceus.core import sources
from caduceus.core.db import write_txn
from caduceus.health.apple import import_apple_health
from caduceus.health.trends import (CALC_VERSION, compare_windows, daily_check, metrics, recompute_rollups,
                                    trend, trend_svg)
from tests.synth.apple import PIVOT, make_export


def _rollup(samples, metric, day, value):
    with write_txn(samples):
        samples.execute("INSERT INTO daily_rollup VALUES (?,?,?,?,?,?)",
                        (metric, str(day), value, 1, '["Synthetic"]', CALC_VERSION))


def test_buckets_daily_means_and_gaps(samples):
    for day, value in [("2024-01-01", 10), ("2024-01-02", 20), ("2024-01-15", 30), ("2024-02-01", 40)]:
        _rollup(samples, "weight", day, value)
    weeks = trend(samples, "weight", start="2024-01-01", end="2024-02-10")
    assert [p["value"] for p in weeks["points"]] == [15, 30, 40]
    assert weeks["coverage"]["days_with_data"] == 4
    assert weeks["coverage"]["days_in_range"] == 41
    assert weeks["coverage"]["gaps"] == [
        {"start": "2024-01-03", "end": "2024-01-14", "days": 12},
        {"start": "2024-01-16", "end": "2024-01-31", "days": 16},
        {"start": "2024-02-02", "end": "2024-02-10", "days": 9}]
    assert trend(samples, "weight", bucket="month")["points"][0]["value"] == 20
    available = metrics(samples)
    assert available[0]["metric"] == "weight" and available[0]["unit"] == "kg"
    assert available[0]["sources"] == ["Synthetic"]
    empty = trend(samples, "steps", start="2024-01-01", end="2024-01-08")
    assert empty["coverage"]["gaps"][0]["days"] == 8
    with pytest.raises(ValueError):
        trend(samples, "unknown")
    with pytest.raises(ValueError):
        trend(samples, "weight", bucket="year")
    with pytest.raises(ValueError):
        trend(samples, "weight", start="2025-01-01", end="2024-01-01")


def test_compare_weight_drop_and_plateau(samples, vault, tmp_path):
    src = sources.store_source(vault, make_export(tmp_path / "weight.zip"), "apple_health")[0]
    assert import_apple_health(samples, vault, src).status == "succeeded"
    result = compare_windows(samples, "weight", pivot=PIVOT, before_days=28, after_days=168)
    assert result["before"]["n_days"] == 28
    assert result["before"]["mean"] == pytest.approx(100)
    assert result["before"]["weekly_slope"] == pytest.approx(0, abs=1e-10)
    halves = result["after"]["halves"]
    assert halves[0]["weekly_slope"] == pytest.approx(-1)
    assert halves[1]["weekly_slope"] == pytest.approx(0, abs=1e-10)
    assert result["after"]["last_value"] == pytest.approx(88)
    assert result["unit"] == "kg" and result["calc_version"] == CALC_VERSION
    with pytest.raises(ValueError):
        compare_windows(samples, "weight", pivot=PIVOT, after_days=0)


def test_daily_check_signals_and_thresholds(samples):
    start = date(2024, 1, 1)
    for i in range(31):
        day = start + timedelta(days=i)
        for metric, value in {"resting_heart_rate": 75 if i >= 28 else 60,
                              "sleep_hours": 4 if i >= 28 else 8,
                              "steps": 2000 if i >= 28 else 8000,
                              "weight": 97 if i >= 28 else 100}.items():
            _rollup(samples, metric, day, value)
    result = daily_check(samples, today="2024-01-31")
    assert {p["metric"] for p in result["signals"]} == {"resting_heart_rate", "sleep_hours", "weight", "steps"}
    assert len(result["metrics_checked"]) == 4
    assert daily_check(samples, today="2024-01-31", thresholds={
        "resting_heart_rate": 20, "sleep_hours": 3, "weight": 4, "steps": 0.1})["signals"] == []
    assert daily_check(samples, today="2024-03-01")["signals"] == []


def test_mixed_units_and_source_average(samples):
    records = [("BodyMass", "kg", 100, "Scale A"), ("BodyMass", "lb", 100/0.45359237, "Scale B"),
               ("BloodGlucose", "mg/dL", 90.08, "Meter A"), ("BloodGlucose", "mmol/L", 5, "Meter B"),
               ("BodyMass", "unknown", 9999, "Unknown scale"),
               ("StepCount", "count", 3000, "Phone"), ("StepCount", "count", 3000, "Phone"),
               ("StepCount", "count", 8000, "Watch"), ("ActiveEnergyBurned", "kJ", 418.4, "Watch")]
    with write_txn(samples):
        for i, (kind, unit, value, source) in enumerate(records):
            samples.execute("INSERT INTO sample(source_id,type,source_name,unit,start_utc,end_utc,"
                            "tz_offset_min,local_day,value,value_text,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (1, "HKQuantityTypeIdentifier"+kind, source, unit, "2024-01-01T00:00:00Z",
                             "2024-01-01T00:00:00Z", 0, "2024-01-01", value, str(value), str(i)))
    recompute_rollups(samples)
    assert trend(samples, "weight", bucket="day")["points"][0]["value"] == pytest.approx(100)
    assert trend(samples, "weight")["points"][0]["sources"] == ["Scale A", "Scale B"]
    assert trend(samples, "blood_glucose")["points"][0]["value"] == pytest.approx(90.08)
    assert trend(samples, "steps")["points"][0]["value"] == 8000
    assert trend(samples, "active_energy")["points"][0]["value"] == pytest.approx(100)
    recompute_rollups(samples)
    assert samples.execute("SELECT count(*) FROM daily_rollup").fetchone()[0] == 4


def test_svg_xml_escape_and_marker_position():
    points = [{"day": "2024-01-01", "value": 10}, {"day": "2024-01-11", "value": 5}]
    svg = trend_svg(points, unit="kg <unit>", title='Weight <script>& "x"', markers=[
        {"date": "2024-01-06", "label": 'start <medicine>&'},
        {"date": "2025-01-01", "label": "outside"}])
    root = ET.fromstring(svg)
    assert root.attrib["viewBox"] == "0 0 720 260"
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "&lt;medicine&gt;&amp;" in svg
    assert "M397.50,40" in svg and "outside" not in svg
    assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")
    ET.fromstring(trend_svg([], unit="kg", title="No samples"))
    ET.fromstring(trend_svg([points[0]], unit="kg", title="One sample"))


def test_sleep_interval_union_and_source_selection(samples, vault, tmp_path):
    import zipfile
    from datetime import datetime, timezone
    from tests.synth.apple import record
    def interval(start, end, stage="HKCategoryValueSleepAnalysisAsleepUnspecified", source="Watch"):
        return record("SleepAnalysis", stage, datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                      datetime.fromisoformat(end).replace(tzinfo=timezone.utc), source=source, unit="")
    watch = interval("2024-01-01T22:00", "2024-01-02T06:00")
    watch += interval("2024-01-01T22:00", "2024-01-02T02:00", "HKCategoryValueSleepAnalysisAsleepCore")
    watch += interval("2024-01-02T02:00", "2024-01-02T06:00", "HKCategoryValueSleepAnalysisAsleepREM")
    watch += interval("2024-01-01T20:00", "2024-01-02T08:00", "HKCategoryValueSleepAnalysisInBed")
    def load(name, xml):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("export.xml", "<HealthData>"+xml+"</HealthData>")
        src = sources.store_source(vault, path, "apple_health")[0]
        assert import_apple_health(samples, vault, src).status == "succeeded"
    load("watch.zip", watch)
    assert trend(samples, "sleep_hours", bucket="day")["points"][0]["value"] == 8
    phone = interval("2024-01-01T20:00", "2024-01-02T05:00", source="Phone")
    phone += interval("2024-01-01T20:00", "2024-01-02T01:00", "HKCategoryValueSleepAnalysisAsleepCore", "Phone")
    load("two-sources.zip", watch+phone)
    point = trend(samples, "sleep_hours", bucket="day")["points"][0]
    assert point["day"] == "2024-01-02" and point["value"] == 9
    assert point["sources"] == ["Phone"]


def test_partial_date_coverage_bounds_and_markers(samples):
    from caduceus.health.trends import coverage
    result = coverage(["2023-05", "2024", None, "invalid", "2024-05-03", "2024-05-03T12:00:00Z"])
    assert result["days_with_data"] == result["days_in_range"] == 1
    assert result["first_day"] == result["last_day"] == "2024-05-03"
    assert coverage(["2023-05", "2024"])["days_with_data"] == 0
    _rollup(samples, "weight", "2024-05-03", 80)
    assert trend(samples, "weight", start="2024", end="2024-05")["points"][0]["value"] == 80
    assert compare_windows(samples, "weight", pivot="2024-05")["pivot"] == "2024-05-01"
    svg = trend_svg([{"day": "2024-05-03", "value": 80}], unit="kg", title="Partial markers",
                    markers=[{"date": "2024", "label": "year-only"}, {"date": "2023-05", "label": "month-only"},
                             {"date": "invalid", "label": "invalid-date"}, {"label": "missing"}])
    ET.fromstring(svg)
    assert all(label not in svg for label in ("year-only", "month-only", "invalid-date", "missing"))


def test_sparse_baselines_and_weight_means(samples):
    today = date(2024, 1, 31)
    for metric, base, recent in [("resting_heart_rate", 60, 90), ("steps", 8000, 100)]:
        _rollup(samples, metric, today-timedelta(days=26), base)
        for i in range(3):
            _rollup(samples, metric, today-timedelta(days=i), recent)
    # The reviewer's single old baseline point must not trigger either signal.
    assert daily_check(samples, today=today)["signals"] == []
    for metric, base in [("resting_heart_rate", 60), ("steps", 8000)]:
        for i in range(6):
            _rollup(samples, metric, today-timedelta(days=3+i), base)
    assert {s["metric"] for s in daily_check(samples, today=today)["signals"]} == {"resting_heart_rate", "steps"}
    # One value per window is insufficient even for a large weight difference.
    _rollup(samples, "weight", today, 90)
    _rollup(samples, "weight", today-timedelta(days=7), 100)
    assert "weight" not in {s["metric"] for s in daily_check(samples, today=today)["signals"]}
    # Means differ by only one kg, despite the ten-kg endpoint difference.
    _rollup(samples, "weight", today-timedelta(days=1), 108)
    _rollup(samples, "weight", today-timedelta(days=8), 100)
    result = daily_check(samples, today=today)
    weight = next(m for m in result["checked"] if m["metric"] == "weight")
    assert weight["recent"] == 99 and weight["baseline"] == 100
    assert "weight" not in {s["metric"] for s in result["signals"]}


def test_svg_skips_bad_values_and_strips_invalid_xml_text():
    points = [{"day": "2024-01-01", "value": "not-a-number"}, {"day": "2024-01-01", "value": None},
              {"day": "2024-01-01", "value": float("nan")}, {"day": "2024-01", "value": 99},
              {"day": "2024-01-01", "value": 5}, {"day": "2024-01-03", "value": "6"}]
    svg = trend_svg(points, unit="kg\x01", title="Title\x00<&\ud800", markers=[
        {"date": "2024-01-02", "label": "Medicine\x02\ufffe"}])
    root = ET.fromstring(svg)
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert root.find("s:path", ns).attrib["d"].count("L") == 1
    assert "Medicine" in "".join(root.itertext())
    assert all(c not in svg for c in ("\x00", "\x01", "\x02", "\ud800", "\ufffe"))


def test_encoded_mmol_glucose_conversion(samples, vault, tmp_path):
    import zipfile
    from datetime import datetime, timezone
    from tests.synth.apple import record
    path = tmp_path / "encoded-glucose.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("export.xml", "<HealthData>"+record("BloodGlucose", 5,
                         datetime(2024, 1, 1, tzinfo=timezone.utc), unit="mmol<180.15588>/L")+"</HealthData>")
    src = sources.store_source(vault, path, "apple_health")[0]
    assert import_apple_health(samples, vault, src).status == "succeeded"
    assert trend(samples, "blood_glucose")["points"][0]["value"] == pytest.approx(90.08)


def _round2_sleep(samples, vault, tmp_path, intervals):
    import zipfile
    from datetime import datetime
    from tests.synth.apple import record
    body = "".join(record("SleepAnalysis", "HKCategoryValueSleepAnalysisAsleepCore",
                          datetime.fromisoformat(start), datetime.fromisoformat(end), source="Watch", unit="")
                   for start, end in intervals)
    path = tmp_path / "round2-sleep.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("export.xml", "<HealthData>"+body+"</HealthData>")
    src = sources.store_source(vault, path, "apple_health")[0]
    result = import_apple_health(samples, vault, src)
    assert result.status == "succeeded", result.warnings
    return result, trend(samples, "sleep_hours", bucket="day")["points"]


@pytest.mark.parametrize("offset", ["-07:00", "+09:00"])
def test_round2_noon_nights_join_midnight_fragments(samples, vault, tmp_path, offset):
    _, points = _round2_sleep(samples, vault, tmp_path, [
        ("2024-05-01T21:00:00"+offset, "2024-05-01T23:50:00"+offset),
        ("2024-05-02T00:10:00"+offset, "2024-05-02T07:00:00"+offset)])
    assert [p["day"] for p in points] == ["2024-05-02"]
    assert points[0]["value"] == pytest.approx(9+2/3)


def test_round2_sleep_noon_partition_and_clip_warning(samples, vault, tmp_path):
    result, points = _round2_sleep(samples, vault, tmp_path, [
        ("2024-04-29T22:00:00-07:00", "2024-05-02T06:00:00-07:00")])
    assert [(p["day"], p["value"]) for p in points] == [
        ("2024-04-30", 14), ("2024-05-01", 20), ("2024-05-02", 18)]
    assert any("clipped" in w and "20" in w for w in result.warnings)
    assert result.counts["sleep_nights_clipped"] == 1


def test_axis_numbers_are_readable_and_noise_is_not_magnified():
    from caduceus.health.trends import _tick

    assert [_tick(v) for v in (7969.86, 104.267, 99.78, 68.5714, 7.19048, 8.4, 168.0, 0.95)] == [
        "7,970", "104.3", "99.8", "68.6", "7.19", "8.4", "168", "0.95"]
    svg = trend_svg([{"day": "2024-01-01", "value": 7.2}, {"day": "2024-01-08", "value": 7.8}], unit="h", title="Sleep")
    ticks = [float(t.text.split()[0]) for t in ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}text")
             if t.text and t.text.endswith(" h")]
    assert max(ticks) - min(ticks) >= 0.75 - 0.01      # at least a tenth of the values' size
    assert "1 Jan 2024" in svg and "8 Jan 2024" in svg and svg.count("<circle") == 2


@pytest.mark.parametrize("values", [[1e308], [1e308, 5.0], [-1e308, 1e308]])
def test_huge_values_never_make_nan_coordinates(values):
    from caduceus.health.trends import trend_svg

    points = [{"date": f"2026-01-0{i + 1}", "value": v} for i, v in enumerate(values)]
    assert "nan" not in trend_svg(points, unit="kg", title="Synthetic").lower()
