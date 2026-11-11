"""Reproducible daily summaries and descriptive trends, not health advice."""
from __future__ import annotations

import json
import math
import calendar
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from statistics import mean, median

from ..core.db import write_txn

# 2 adds lean_mass, protein and waist (0.2.0); rollups from an older calculation are rebuilt once.
CALC_VERSION = "trend/2"
ROLLUP_MARK = int(CALC_VERSION.rsplit("/", 1)[1])  # stored as samples.db's user_version by every rollup write
# Friendly name: HealthKit suffix, canonical unit, aggregation policy.
METRICS = {
    "weight": ("BodyMass", "kg", "mean"), "bmi": ("BodyMassIndex", "count", "mean"),
    "body_fat": ("BodyFatPercentage", "%", "mean"),
    "lean_mass": ("LeanBodyMass", "kg", "mean"), "waist": ("WaistCircumference", "cm", "mean"),
    "protein": ("DietaryProtein", "g", "max_source_sum"),
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


def _known_types() -> tuple[str, ...]:
    return tuple("HK" + ("Category" if m == "sleep_hours" else "Quantity") + "TypeIdentifier" + spec[0]
                 for m, spec in METRICS.items())


def rollups_stale(samples) -> bool:
    """True when the stored rollups came from an older calculation, or when samples of a known metric have no rollups
    at all. An export imported before 0.2.0 kept its raw lean mass, protein and waist samples but got no rollups for
    them; one that held only those metrics, or an import cut off before its rollups, left none."""
    if samples.execute("PRAGMA user_version").fetchone()[0] >= ROLLUP_MARK:
        return False  # this calculation already ran on this file, even if the samples gave it nothing to roll up
    if samples.execute("SELECT 1 FROM daily_rollup WHERE calc_version <> ? LIMIT 1", (CALC_VERSION,)).fetchone():
        return True
    if samples.execute("SELECT 1 FROM daily_rollup LIMIT 1").fetchone():
        return False
    types = _known_types()
    return samples.execute(f"SELECT 1 FROM sample WHERE type IN ({','.join('?' * len(types))}) LIMIT 1",
                           types).fetchone() is not None


def _rollup_rows(samples) -> tuple[list[tuple], list[str]]:
    """Every daily rollup computed from the stored samples, and the sleep warnings. Reads only.

    One SQL scan; choose one daily source for sums, all sources for means.

    Unknown units stay in raw samples but do not enter a mixed-unit rollup.
    Sleep unions asleep intervals per night/source, then selects one source.
    """
    cases = " ".join("WHEN 'HK" + ("Category" if m == "sleep_hours" else "Quantity") +
                     "TypeIdentifier" + spec[0] + "' THEN '" + m + "'" for m, spec in METRICS.items() if m != "sleep_hours")
    conversions = {
        "weight": "WHEN unit IN ('kg','g','lb','lbs') THEN value * CASE unit WHEN 'g' THEN 0.001 WHEN 'lb' THEN 0.45359237 WHEN 'lbs' THEN 0.45359237 ELSE 1 END",
        "blood_glucose": "WHEN unit='mg/dL' THEN value WHEN unit='mmol/L' OR unit GLOB 'mmol<*>/L' THEN value*18.016",
        "active_energy": "WHEN unit IN ('kcal','kJ') THEN value * CASE unit WHEN 'kJ' THEN 1.0/4.184 ELSE 1 END",
        "lean_mass": "WHEN unit IN ('kg','g','lb','lbs') THEN value * CASE unit WHEN 'g' THEN 0.001 WHEN 'lb' THEN 0.45359237 WHEN 'lbs' THEN 0.45359237 ELSE 1 END",
        "waist": "WHEN unit IN ('cm','in','m','mm') THEN value * CASE unit WHEN 'in' THEN 2.54 WHEN 'm' THEN 100 WHEN 'mm' THEN 0.1 ELSE 1 END",
        "protein": "WHEN unit IN ('g','mg') THEN value * CASE unit WHEN 'mg' THEN 0.001 ELSE 1 END",
    }
    normal = " ".join("WHEN '" + m + "' THEN CASE " + conversions.get(m, "WHEN unit='" + spec[1] + "' THEN value") + " END" for m, spec in METRICS.items())
    summed = ",".join("'" + m + "'" for m, spec in METRICS.items() if spec[2] == "max_source_sum")
    rows = [tuple(row) for row in samples.execute(
        "WITH mapped AS (SELECT *, CASE type " + cases + " END AS metric FROM sample), "
        "normalized AS (SELECT metric,local_day AS day,source_name,CASE metric " + normal + " END AS v FROM mapped WHERE metric IS NOT NULL), "
        "per_source AS (SELECT metric,day,source_name,SUM(v) AS total,COUNT(v) AS n FROM normalized WHERE v IS NOT NULL GROUP BY metric,day,source_name), "
        "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY metric,day ORDER BY total DESC,source_name) AS rank FROM per_source), "
        "rollups AS (SELECT metric,day,SUM(total)/SUM(n) AS value,SUM(n) AS n,json_group_array(source_name) AS names FROM per_source "
        "WHERE metric NOT IN (" + summed + ") GROUP BY metric,day "
        "UNION ALL SELECT metric,day,total,n,json_array(source_name) FROM ranked WHERE metric IN (" + summed + ") AND rank=1) "
        "SELECT metric,day,value,n,names,? FROM rollups", (CALC_VERSION,))]
    stages = ("1", "3", "4", "5", "HKCategoryValueSleepAnalysisAsleep",
              "HKCategoryValueSleepAnalysisAsleepUnspecified", "HKCategoryValueSleepAnalysisAsleepCore",
              "HKCategoryValueSleepAnalysisAsleepDeep", "HKCategoryValueSleepAnalysisAsleepREM")
    asleep = samples.execute(
        "SELECT tz_offset_min,source_name,start_utc,end_utc FROM sample "
        "WHERE type='HKCategoryTypeIdentifierSleepAnalysis' AND value_text IN ("+
        ",".join("?" for _ in stages)+")", stages)
    grouped = {}
    for row in asleep:
        offset = timezone(timedelta(minutes=row[0]))
        start = datetime.fromisoformat(row[2]).astimezone(offset)
        end = datetime.fromisoformat(row[3]).astimezone(offset)
        while start < end:
            morning = start.date() + timedelta(days=1 if start.hour >= 12 else 0)
            noon = datetime.combine(morning, time(12), tzinfo=offset)
            stop = min(end, noon)
            grouped.setdefault((morning.isoformat(), row[1]), []).append((start, stop))
            start = stop
    nights = {}
    for (night, source), intervals in sorted(grouped.items()):
        left = right = None
        seconds, n = 0, 0
        for start, end in sorted(intervals):
            if end < start:
                continue
            n += 1
            if right is None or start > right:
                if right is not None:
                    seconds += (right-left).total_seconds()
                left, right = start, end
            else:
                right = max(right, end)
        if right is not None:
            seconds += (right-left).total_seconds()
            if night not in nights or seconds > nights[night][0]:
                nights[night] = (seconds, n, source)
    rows += [("sleep_hours", day, min(seconds/3600, 20), n, json.dumps([source]), CALC_VERSION)
             for day, (seconds, n, source) in nights.items()]
    return rows, ["sleep_hours: clipped a night at 20 h" for seconds, _, _ in nights.values() if seconds > 20*3600]


def _replace_rollups(samples, rows) -> None:
    samples.execute("DELETE FROM daily_rollup")
    samples.executemany("INSERT INTO daily_rollup(metric,day,value,n,source_name,calc_version) VALUES (?,?,?,?,?,?)",
                        rows)
    samples.execute(f"PRAGMA user_version = {ROLLUP_MARK}")  # in the same transaction as the rows


def recompute_rollups(samples):
    """Rebuild every daily rollup inside one write transaction (the end of an import). Returns the sleep warnings."""
    with write_txn(samples):
        rows, warnings = _rollup_rows(samples)
        _replace_rollups(samples, rows)
    return warnings


def rebuild_rollups(samples) -> bool:
    """The one-time rebuild of rollups from an older calculation. Every scan runs outside the process's writer lock, so
    other requests carry on; only the swap is a write. If samples arrived during a scan (an import), it scans again,
    still outside the lock; if an import rebuilt the rollups meanwhile, its own result stands. Returns True when it
    replaced the rollups."""
    for _attempt in range(3):
        marker = samples.execute("SELECT max(id) FROM sample").fetchone()[0]
        rows, _warnings = _rollup_rows(samples)
        with write_txn(samples):
            if samples.execute("PRAGMA user_version").fetchone()[0] >= ROLLUP_MARK:
                return False  # an import rebuilt them while this scan ran: this scan is obsolete
            if samples.execute("SELECT max(id) FROM sample").fetchone()[0] == marker:
                _replace_rollups(samples, rows)
                return True
    return False  # an import is still adding samples; it builds the rollups when it ends


def _day(value):
    text = str(value)
    try:
        return date.fromisoformat(text[:10]) if len(text) == 10 or text[10:11] in ("T", " ") else None
    except ValueError:
        return None


def _bound(value, *, end=False):
    """Partial query bounds encompass their calendar period; points stay partial."""
    full = _day(value)
    if full is not None:
        return full
    text = str(value)
    try:
        if len(text) == 4 and text.isdigit():
            return date(int(text), 12 if end else 1, 31 if end else 1)
        if len(text) == 7 and text[4] == "-":
            year, month = int(text[:4]), int(text[5:])
            return date(year, month, calendar.monthrange(year, month)[1] if end else 1)
    except ValueError:
        pass
    return None


def coverage(days, start=None, end=None):
    days = sorted({day.isoformat() for value in days if (day := _day(value)) is not None})
    first, last = (days[0], days[-1]) if days else (None, None)
    low = _bound(start) if start else (_day(first) if first else None)
    high = _bound(end, end=True) if end else (_day(last) if last else None)
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
        if _bound(start) is None:
            raise ValueError("invalid start date")
        args.append(_bound(start).isoformat())
    if end:
        sql += " AND day<=?"
        if _bound(end, end=True) is None:
            raise ValueError("invalid end date")
        args.append(_bound(end, end=True).isoformat())
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
        if when is None:
            continue
        key = when if bucket == "day" else (when - timedelta(days=when.weekday()) if bucket == "week" else when.replace(day=1))
        groups.setdefault(key.isoformat(), []).append(point)
    buckets = [{"day": key, "value": mean(p["value"] for p in group), "n_days": len(group),
                "sources": sorted({s for p in group for s in p["sources"]})} for key, group in groups.items()]
    return {**_metadata(metric, points, start, end), "bucket": bucket, "points": buckets}


def _slope(points):
    points = [p for p in points if _day(p["day"]) is not None]
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
    pivot = _bound(pivot)
    if pivot is None:
        raise ValueError("invalid pivot date")
    low, high = pivot-timedelta(days=before_days), pivot+timedelta(days=after_days-1)
    points = _points(samples, metric, low, high)
    before = [p for p in points if _day(p["day"]) is not None and _day(p["day"]) < pivot]
    after = [p for p in points if _day(p["day"]) is not None and _day(p["day"]) >= pivot]
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
    now = _bound(today, end=True) if today else date.today()
    if now is None:
        raise ValueError("invalid check date")
    signals, checked = [], []
    for metric in limits:
        if metric not in ("resting_heart_rate", "sleep_hours", "weight", "steps"):
            raise ValueError("unknown daily-check threshold: " + metric)
        points = _points(samples, metric, now-timedelta(days=30), now)
        item = _metadata(metric, points, now-timedelta(days=30), now)
        recent = [p for p in points if _day(p["day"]) is not None and _day(p["day"]) >= now-timedelta(days=2)]
        baseline = [p for p in points if _day(p["day"]) is not None and _day(p["day"]) < now-timedelta(days=2)]
        value, reference, triggered = None, None, False
        if metric == "weight":
            earlier = [p for p in baseline if now-timedelta(days=9) <= _day(p["day"]) <= now-timedelta(days=7)]
            value = mean(p["value"] for p in recent) if len(recent) >= 2 else None
            reference = mean(p["value"] for p in earlier) if len(earlier) >= 2 else None
            triggered = value is not None and reference is not None and abs(value-reference) > limits[metric]
        elif len(recent) == 3:
            value = mean(p["value"] for p in recent)
            reference = mean(p["value"] for p in baseline) if baseline else None
            if metric == "sleep_hours":
                triggered = value < limits[metric]
            elif reference is not None and len(baseline) >= 7:
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


def _tick(value: float) -> str:
    """An axis number people can read: 7,970 or 104.3 or 7.19, never 7969.86 or 104.267."""
    size = abs(value)
    text = f"{value:,.0f}" if size >= 1000 else f"{value:.1f}" if size >= 10 else f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _when(ordinal: int) -> str:
    day = date.fromordinal(ordinal)
    return f"{day.day} {day.strftime('%b %Y')}"


def trend_svg(points, *, unit, title, markers=()) -> str:
    """Small self-contained time plot, positioned by date. Labels are text, never markup.

    The vertical range spans at least a tenth of the values' size, so day-to-day noise is not drawn as a swing."""
    values = []
    for point in points:
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError, OverflowError):
            continue
        day = _day(point.get("day") or point.get("date"))
        if day is not None and math.isfinite(value) and abs(value) <= 1e12:  # beyond any health value; no overflow
            values.append((day.toordinal(), value))
    values.sort()
    low = min((v for _, v in values), default=0.0)
    high = max((v for _, v in values), default=0.0)
    span = max(high - low, abs(low + high) / 2 * 0.1, 1e-9)
    middle = (low + high) / 2
    low, high = middle - span * 0.55, middle + span * 0.55
    first = values[0][0] if values else 0
    last = values[-1][0] if values else 0
    left, right, top, bottom = 95.0, 700.0, 45.0, 200.0

    def x(day):
        return left + (right - left) * (day - first) / max(1, last - first)

    def y(value):
        return bottom - (bottom - top) * (value - low) / (high - low)

    label = 'font-size="13" fill="#555"'
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 260" role="img" '
             'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif">',
             '<title>' + _xml_text(title) + '</title>', '<rect width="720" height="260" fill="white"/>',
             '<text x="20" y="24" font-size="15" font-weight="600" fill="#222">' + _xml_text(title) + '</text>']
    if values:
        for value in (high, low):
            parts.append(f'<path d="M{left:.0f},{y(value):.2f} L{right:.0f},{y(value):.2f}" stroke="#e4e4e0"/>')
            parts.append(f'<text x="{left - 8:.0f}" y="{y(value) + 4:.2f}" text-anchor="end" {label}>'
                         + _xml_text(f"{_tick(value)} {unit}".strip()) + '</text>')
        parts.append(f'<text x="{left:.0f}" y="250" {label}>' + _xml_text(_when(first)) + '</text>')
        if last != first:
            parts.append(f'<text x="{right:.0f}" y="250" text-anchor="end" {label}>' + _xml_text(_when(last)) + '</text>')
    path = " ".join(("M" if i == 0 else "L") + f"{x(d):.2f},{y(v):.2f}" for i, (d, v) in enumerate(values))
    parts.append('<path d="' + path + '" fill="none" stroke="#226699" stroke-width="2"/>')
    if len(values) <= 24:  # sparse series (lab results): show each measurement
        parts += [f'<circle cx="{x(d):.2f}" cy="{y(v):.2f}" r="3.5" fill="#226699"/>' for d, v in values]
    for marker in markers:
        parsed = _day(marker.get("day") or marker.get("date"))
        if parsed is None:
            continue
        day = parsed.toordinal()
        if values and first <= day <= last:
            xpos = x(day)
            anchor = ' text-anchor="end"' if xpos > right - 120 else ""
            parts.append(f'<path d="M{xpos:.2f},40 L{xpos:.2f},{bottom + 5:.0f}" stroke="#777" stroke-dasharray="4 3"/>')
            parts.append(f'<text x="{xpos + (-4 if anchor else 4):.2f}" y="222"{anchor} font-size="13" fill="#333">'
                         + _xml_text(marker.get("label", "")) + '</text>')
    parts.append('</svg>')
    return "".join(parts)


def _xml_text(value):
    return escape("".join(c for c in str(value) if c in "\t\n\r" or
                          0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                          0x10000 <= ord(c) <= 0x10FFFF))
