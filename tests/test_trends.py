from datetime import date, timedelta
from xml.etree import ElementTree as ET

import pytest

from caduceus.core import sources
from caduceus.core.db import write_txn
from caduceus.health.apple import import_apple_health
from caduceus.health.trends import (compare_windows, daily_check, metrics, recompute_rollups,
                                    trend, trend_svg)
from tests.synth.apple import PIVOT, make_export


def _rollup(samples, metric, day, value):
    with write_txn(samples):
        samples.execute("INSERT INTO daily_rollup VALUES (?,?,?,?,?,?)",
                        (metric, str(day), value, 1, '["Synthetic"]', "trend/1"))


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
    assert result["unit"] == "kg" and result["calc_version"] == "trend/1"
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
               ("BloodGlucose", "mg/dL", 90, "Meter A"), ("BloodGlucose", "mmol/L", 5, "Meter B"),
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
    assert trend(samples, "blood_glucose")["points"][0]["value"] == 90
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
    assert "M375.00,40" in svg and "outside" not in svg
    assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")
    ET.fromstring(trend_svg([], unit="kg", title="No samples"))
    ET.fromstring(trend_svg([points[0]], unit="kg", title="One sample"))
