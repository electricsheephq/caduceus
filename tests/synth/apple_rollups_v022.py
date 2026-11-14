"""Frozen pre-HAE rollup oracle, from the synthetic-only 0.2.2 baseline."""
import json
from datetime import datetime, time, timedelta, timezone
from caduceus.health.trends import METRICS, CALC_VERSION

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

