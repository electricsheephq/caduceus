"""Reproducible daily summaries and descriptive trends, not health advice."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from html import escape
from statistics import mean, median

from ..core.db import write_txn

CALC_VERSION = "trend/1"
# Friendly name: HealthKit suffix, canonical unit, aggregation policy.
METRICS = {
    "weight": ("BodyMass", "kg", "mean"), "bmi": ("BodyMassIndex", "count", "mean"),
    "body_fat": ("BodyFatPercentage", "%", "mean"),
    "resting_heart_rate": ("RestingHeartRate", "count/min", "mean"),
    "heart_rate": ("HeartRate", "count/min", "mean"),
    "hrv": ("HeartRateVariabilitySDNN", "ms", "mean"),
    "vo2max": ("VO2Max", "mL/min·kg", "mean"),
    "blood_glucose": ("BloodGlucose", "mg/dL", "mean"),
    "systolic": ("BloodPressureSystolic", "mmHg", "mean"),
    "diastolic": ("BloodPressureDiastolic", "mmHg", "mean"),
    "steps": ("StepCount", "count", "max_source_sum"),
    "active_energy": ("ActiveEnergyBurned", "kcal", "max_source_sum"),
    "sleep_hours": ("SleepAnalysis", "h", "max_source_sum"),
}


def recompute_rollups(samples):
    """One SQL scan; choose one daily source for sums, all sources for means.

    Unknown units stay in raw samples but do not enter a mixed-unit rollup.
    Sleep sums only asleep stages, assigned to the local end date.
    """
    cases = " ".join("WHEN 'HK" + ("Category" if m == "sleep_hours" else "Quantity") +
                     "TypeIdentifier" + spec[0] + "' THEN '" + m + "'" for m, spec in METRICS.items())
    conversions = {
        "weight": "WHEN unit IN ('kg','g','lb','lbs') THEN value * CASE unit WHEN 'g' THEN 0.001 WHEN 'lb' THEN 0.45359237 WHEN 'lbs' THEN 0.45359237 ELSE 1 END",
        "blood_glucose": "WHEN unit IN ('mg/dL','mmol/L') THEN value * CASE unit WHEN 'mmol/L' THEN 18.0 ELSE 1 END",
        "active_energy": "WHEN unit IN ('kcal','kJ') THEN value * CASE unit WHEN 'kJ' THEN 1.0/4.184 ELSE 1 END",
        "sleep_hours": "WHEN value_text IN ('1','HKCategoryValueSleepAnalysisAsleep','HKCategoryValueSleepAnalysisAsleepUnspecified','HKCategoryValueSleepAnalysisAsleepCore','HKCategoryValueSleepAnalysisAsleepDeep','HKCategoryValueSleepAnalysisAsleepREM','3','4','5') AND end_utc >= start_utc THEN (julianday(end_utc)-julianday(start_utc))*24",
    }
    normal = " ".join("WHEN '" + m + "' THEN CASE " + conversions.get(m, "WHEN unit='" + spec[1] + "' THEN value") + " END" for m, spec in METRICS.items())
    summed = ",".join("'" + m + "'" for m, spec in METRICS.items() if spec[2] == "max_source_sum")
    with write_txn(samples):
        samples.execute("DELETE FROM daily_rollup")
        samples.execute(
            "WITH mapped AS (SELECT *, CASE type " + cases + " END AS metric FROM sample), "
            "normalized AS (SELECT metric,local_day AS day,source_name,CASE metric " + normal + " END AS v FROM mapped WHERE metric IS NOT NULL), "
            "per_source AS (SELECT metric,day,source_name,SUM(v) AS total,COUNT(v) AS n FROM normalized WHERE v IS NOT NULL GROUP BY metric,day,source_name), "
            "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY metric,day ORDER BY total DESC,source_name) AS rank FROM per_source), "
            "rollups AS (SELECT metric,day,SUM(total)/SUM(n) AS value,SUM(n) AS n,json_group_array(source_name) AS names FROM per_source "
            "WHERE metric NOT IN (" + summed + ") GROUP BY metric,day "
            "UNION ALL SELECT metric,day,total,n,json_array(source_name) FROM ranked WHERE metric IN (" + summed + ") AND rank=1) "
            "INSERT INTO daily_rollup(metric,day,value,n,source_name,calc_version) SELECT metric,day,value,n,names,? FROM rollups", (CALC_VERSION,))


def _day(value):
    return date.fromisoformat(str(value)[:10])


def coverage(days, start=None, end=None):
    days = sorted(set(days))
    first, last = (days[0], days[-1]) if days else (None, None)
    low = _day(start) if start else (_day(first) if first else None)
    high = _day(end) if end else (_day(last) if last else None)
    if low and high and high < low:
        raise ValueError("end must not precede start")
    gaps = []
    if low and high:
        cursor = low
        for present in [_day(d) for d in days] + [high + timedelta(days=1)]:
            if present < low or present > high + timedelta(days=1):
                continue
            missing = (present - cursor).days
            if missing >= 7:
                gaps.append({"start": cursor.isoformat(), "end": (present - timedelta(days=1)).isoformat(), "days": missing})
            cursor = present + timedelta(days=1)
    return {"first_day": first, "last_day": last, "days_with_data": len(days),
            "days_in_range": (high-low).days + 1 if low and high else 0, "gaps": gaps}


def _metadata(metric, points, start=None, end=None):
    if metric not in METRICS:
        raise ValueError("unknown metric: " + str(metric))
    policy = ("all sources averaged" if METRICS[metric][2] == "mean"
              else "single source with largest daily total")
    return {"metric": metric, "unit": METRICS[metric][1], "source_policy": policy,
            "coverage": coverage([p["day"] for p in points], start, end), "calc_version": CALC_VERSION}


def _points(samples, metric, start=None, end=None):
    if metric not in METRICS:
        raise ValueError("unknown metric: " + str(metric))
    sql, args = "SELECT * FROM daily_rollup WHERE metric=?", [metric]
    if start:
        sql += " AND day>=?"
        args.append(_day(start).isoformat())
    if end:
        sql += " AND day<=?"
        args.append(_day(end).isoformat())
    rows = samples.execute(sql + " ORDER BY day", args)
    return [{"day": r["day"], "value": r["value"], "n": r["n"],
             "sources": json.loads(r["source_name"])} for r in rows]


def metrics(samples) -> list[dict]:
    result = []
    for row in samples.execute("SELECT DISTINCT metric FROM daily_rollup ORDER BY metric"):
        points = _points(samples, row[0])
        result.append({**_metadata(row[0], points), "sources": sorted({s for p in points for s in p["sources"]})})
    return result


def trend(samples, metric, *, start=None, end=None, bucket="week") -> dict:
    if bucket not in ("day", "week", "month"):
        raise ValueError("bucket must be day, week or month")
    points = _points(samples, metric, start, end)
    groups = {}
    for point in points:
        when = _day(point["day"])
        key = when if bucket == "day" else (when - timedelta(days=when.weekday()) if bucket == "week" else when.replace(day=1))
        groups.setdefault(key.isoformat(), []).append(point)
    buckets = [{"day": key, "value": mean(p["value"] for p in group), "n_days": len(group),
                "sources": sorted({s for p in group for s in p["sources"]})} for key, group in groups.items()]
    return {**_metadata(metric, points, start, end), "bucket": bucket, "points": buckets}


def _slope(points):
    if len(points) < 2:
        return None
    xs = [(_day(p["day"]) - _day(points[0]["day"])).days for p in points]
    ys = [p["value"] for p in points]
    mx, my = mean(xs), mean(ys)
    denominator = sum((x-mx)**2 for x in xs)
    return 7 * sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / denominator if denominator else None


def _window(points):
    values = [p["value"] for p in points]
    return {"mean": mean(values) if values else None, "median": median(values) if values else None,
            "n_days": len(values), "first": points[0] if points else None, "last": points[-1] if points else None,
            "first_value": values[0] if values else None, "last_value": values[-1] if values else None,
            "weekly_slope": _slope(points)}


def compare_windows(samples, metric, *, pivot, before_days=28, after_days=28) -> dict:
    if before_days < 1 or after_days < 1:
        raise ValueError("window lengths must be positive")
    pivot = _day(pivot)
    low, high = pivot-timedelta(days=before_days), pivot+timedelta(days=after_days-1)
    points = _points(samples, metric, low, high)
    before = [p for p in points if _day(p["day"]) < pivot]
    after = [p for p in points if _day(p["day"]) >= pivot]
    half = pivot + timedelta(days=(after_days+1)//2)
    after_summary = _window(after)
    after_summary["halves"] = [_window([p for p in after if _day(p["day"]) < half]),
                               _window([p for p in after if _day(p["day"]) >= half])]
    return {**_metadata(metric, points, low, high), "pivot": pivot.isoformat(),
            "before": _window(before), "after": after_summary,
            "limits": "Descriptive association only; slopes do not establish medication effects."}


def daily_check(samples, *, thresholds=None, today=None) -> dict:
    """Only signal when each recent calendar window has all required days.

    Threshold overrides: resting_heart_rate (bpm), sleep_hours (h), weight (kg),
    steps (fraction of baseline). Baselines exclude the recent three days.
    """
    limits = {"resting_heart_rate": 10, "sleep_hours": 5, "weight": 2, "steps": 0.4}
    limits.update(thresholds or {})
    now = _day(today) if today else date.today()
    signals, checked = [], []
    for metric in limits:
        if metric not in ("resting_heart_rate", "sleep_hours", "weight", "steps"):
            raise ValueError("unknown daily-check threshold: " + metric)
        points = _points(samples, metric, now-timedelta(days=30), now)
        item = _metadata(metric, points, now-timedelta(days=30), now)
        recent = [p for p in points if _day(p["day"]) >= now-timedelta(days=2)]
        baseline = [p for p in points if _day(p["day"]) < now-timedelta(days=2)]
        value, reference, triggered = None, None, False
        if metric == "weight":
            by_day = {p["day"]: p["value"] for p in points}
            value, reference = by_day.get(now.isoformat()), by_day.get((now-timedelta(days=7)).isoformat())
            triggered = value is not None and reference is not None and abs(value-reference) > limits[metric]
        elif len(recent) == 3:
            value = mean(p["value"] for p in recent)
            reference = mean(p["value"] for p in baseline) if baseline else None
            if metric == "sleep_hours":
                triggered = value < limits[metric]
            elif reference is not None:
                triggered = (value-reference > limits[metric] if metric == "resting_heart_rate"
                             else value < limits[metric]*reference)
        item.update({"recent": value, "baseline": reference, "threshold": limits[metric]})
        checked.append(item)
        if triggered:
            signals.append({**item, "message": "Outside the configured descriptive threshold; data may be incomplete."})
    return {"today": now.isoformat(), "signals": signals, "checked": checked,
            "metrics_checked": [item["metric"] for item in checked], "unit": "per metric",
            "source_policy": "daily rollups", "coverage": {p["metric"]: p["coverage"] for p in checked},
            "calc_version": CALC_VERSION}


def trend_svg(points, *, unit, title, markers=()) -> str:
    """Small self-contained time plot. Labels are text, never markup."""
    values = []
    for point in points:
        value = point.get("value")
        if value is not None and math.isfinite(float(value)):
            values.append((_day(point.get("day") or point["date"]).toordinal(), float(value)))
    values.sort()
    low = min((v for _, v in values), default=0)
    high = max((v for _, v in values), default=0)
    first = values[0][0] if values else 0
    last = values[-1][0] if values else 0
    def x(day):
        return 65 + 620*(day-first)/max(1, last-first)
    def y(value):
        return 210 - 155*(value-low)/(high-low) if high != low else 132.5
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 260" role="img">',
             '<title>'+escape(str(title))+'</title>', '<rect width="720" height="260" fill="white"/>',
             '<text x="20" y="24">'+escape(str(title))+'</text>',
             '<text x="5" y="52">'+escape(f"{high:g} {unit}")+'</text>',
             '<text x="5" y="225">'+escape(f"{low:g} {unit}")+'</text>']
    path = " ".join(("M" if i == 0 else "L")+f"{x(d):.2f},{y(v):.2f}" for i, (d,v) in enumerate(values))
    parts.append('<path d="'+path+'" fill="none" stroke="#226699" stroke-width="2"/>')
    for marker in markers:
        day = _day(marker.get("day") or marker["date"]).toordinal()
        if values and first <= day <= last:
            xpos = x(day)
            parts.append(f'<path d="M{xpos:.2f},40 L{xpos:.2f},225" stroke="#777" stroke-dasharray="4 3"/>')
            parts.append(f'<text x="{xpos:.2f}" y="245">'+escape(str(marker.get("label", "")))+'</text>')
    parts.append('</svg>')
    return "".join(parts)
