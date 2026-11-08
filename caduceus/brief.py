"""The health brief: one self-contained local HTML page to read, or to show a clinician (orchestrator-owned).

It puts the user's medications, Apple Health trends (charted against medication dates), lab results and the
gene–drug evidence for their current medications on one page. Everything passes through the privacy policy;
hidden sections say only how many items were left out. No external resources; all text is escaped.
"""
from __future__ import annotations

import html
import json
import math
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from .core.policy import Category, allowed

BRIEF_VERSION = "brief/1"
TREND_METRICS = (
    ("weight", "Weight"), ("resting_heart_rate", "Resting heart rate"), ("steps", "Steps per day"),
    ("sleep_hours", "Sleep per night"), ("blood_glucose", "Blood glucose (Apple Health)"),
)
LABS = (("HbA1c", "HbA1c"), ("glucose", "Glucose"), ("LDL", "LDL cholesterol"))
_EVENT = {"start": "start", "dose_change": "dose change", "stop": "stop"}
_STATUS = {"flag": "You carry the effect allele", "no_flag": "Tested: effect allele not found",
           "not_assessed": "Not assessed"}

_STYLE = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#5f5f5a;--line:#deded8;--accent:#1f5f8b;--flag:#8a4b00;
--flagbg:#fff4e5;--ok:#1e6b3a;--okbg:#e9f6ee;--na:#5f5f5a;--nabg:#f0f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecece8;--muted:#a3a39c;
--line:#34342f;--accent:#7cb7e0;--flag:#ffc27a;--flagbg:#3a2a12;--ok:#8fd6a8;--okbg:#14301f;--na:#b5b5ae;
--nabg:#2a2a26}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 48px}h1{font-size:28px;margin:0 0 4px}
h2{font-size:20px;margin:32px 0 8px}h3{font-size:16px;margin:16px 0 6px}
.lede,.note,footer{color:var(--muted)}.note{font-size:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.chart{background:#fff;border-radius:8px;padding:6px;overflow-x:auto}.chart svg{width:100%;height:auto;min-width:480px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.badge{display:inline-block;border-radius:999px;padding:1px 9px;font-size:13px;font-weight:600}
.flag{color:var(--flag);background:var(--flagbg)}.no_flag{color:var(--ok);background:var(--okbg)}
.not_assessed{color:var(--na);background:var(--nabg)}.warn{border-left:4px solid var(--accent)}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value, digits=1) -> str:
    if value is None:
        return "–"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return _e(value)


_SOURCE = {"fhir": "Health records", "apple_health": "Apple Health", "user": "You"}
_UNIT = {"count/min": "bpm", "count": "steps"}


def _value(value) -> str:
    """A result as it was recorded: 8.1, 168 or 0.95, never 8.10 or 168.00."""
    if value is None:
        return "–"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _e(value)
    if not math.isfinite(number):
        return "–"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _medications(vault) -> list[dict]:
    return [dict(r) for r in vault.execute(
        "SELECT drug, brand, event, dose, occurred_on, source, note, category FROM medication_event "
        "ORDER BY occurred_on, id")]


def _is_day(value) -> bool:
    """A full date: records may give only a month or a year, and no day is ever invented."""
    try:
        date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return False
    return len(str(value)) >= 10


def _not_current(m) -> bool:
    """A record that said the medication is not current (stopped or completed with no stop date)."""
    try:
        note = json.loads(m.get("note") or "{}")
    except ValueError:  # a note the user typed
        return False
    return isinstance(note, dict) and note.get("current") is False and m["event"] == "start"


def _current(meds) -> list[str]:
    last = {}
    for m in meds:
        last[m["drug"]] = m
    return [drug for drug, m in last.items() if m["event"] != "stop" and not _not_current(m)]


def _markers(meds):
    from .ops import short_drug

    return [{"day": m["occurred_on"][:10], "label": f"{short_drug(m['drug'])} {_EVENT.get(m['event'], m['event'])}"}
            for m in meds if m.get("occurred_on")]


def _try(fn, *args, **kwargs):
    from .tools import ToolError

    try:
        return fn(*args, **kwargs), None
    except ToolError as exc:
        return None, str(exc)


def _meds_section(meds, hidden) -> tuple[str, int]:
    """*meds* are the medications the policy shows; *hidden* counts the rest."""
    if not meds:
        if hidden:
            return "", hidden
        return ("<h2>Medications</h2><p class='note'>No medications recorded. Ask your agent to record one "
                "(it will show you the details to confirm).</p>"), 0
    from .ops import display_drug

    brand, dose = any(m["brand"] for m in meds), any(m["dose"] for m in meds)  # no empty columns

    def row(m):
        event = _EVENT.get(m["event"], m["event"]) + (" (no longer taken; stop date unknown)" if _not_current(m) else "")
        cells = [display_drug(m["drug"])] + ([m["brand"] or ""] if brand else []) + [event]
        cells += ([m["dose"] or ""] if dose else []) + [(m["occurred_on"] or "")[:10] or "date unknown",
                                                        _SOURCE.get(m["source"], m["source"])]
        return "<tr>" + "".join(f"<td>{_e(c)}</td>" for c in cells) + "</tr>"

    heads = ["Drug"] + (["Brand"] if brand else []) + ["Event"] + (["Dose"] if dose else []) + ["Date", "Source"]
    return ("<h2>Medications</h2><div class='panel'><table><tr>" + "".join(f"<th>{h}</th>" for h in heads) +
            "</tr>" + "".join(row(m) for m in meds) + "</table></div>"), hidden


def _compare_text(cmp: dict, unit: str, drug: str) -> str:
    before, after = cmp.get("before") or {}, cmp.get("after") or {}
    halves = after.get("halves") or [{}, {}]
    parts = []
    if before.get("mean") is not None:
        parts.append(f"In the {_e(before.get('n_days'))} days with data in the 4 weeks before {_e(drug)} started "
                     f"({_e(cmp.get('pivot'))}), the average was {_num(before['mean'])} {_e(unit)}.")
    if after.get("mean") is not None:
        parts.append(f"After it started, the average was {_num(after['mean'])} {_e(unit)} "
                     f"({_e(after.get('n_days'))} days with data).")
    first, second = halves[0].get("weekly_slope"), halves[1].get("weekly_slope") if len(halves) > 1 else None
    if first is not None and second is not None:
        parts.append(f"The change per week was {_num(first, 2)} {_e(unit)} in the first half of that period and "
                     f"{_num(second, 2)} {_e(unit)} in the second half.")
    if not parts:
        return ""
    return ("<p>" + " ".join(parts) + "</p><p class='note'>Descriptive only: a change over time does not show that "
            "the medication caused it.</p>")


def _trend_section(samples, meds, policy, today) -> tuple[str, int]:
    trends_mod = _module("health.trends")
    if trends_mod is None:
        return "", 0
    if not allowed(policy, Category.WELLNESS):
        return "", 1
    blocks, start = [], today - timedelta(days=365)
    starts = [m for m in meds if m["event"] == "start" and _is_day(m["occurred_on"])]
    markers = _markers(starts)
    for metric, label in TREND_METRICS:
        series, _err = _try(_lane(trends_mod.trend), samples, metric, start=start, end=today, bucket="week")
        if not series or not series.get("points"):
            continue
        unit = series.get("unit") or ""
        svg = trends_mod.trend_svg(series["points"], unit=_UNIT.get(unit, unit), title=f"{label}, weekly average",
                                   markers=markers)
        cov = series.get("coverage") or {}
        text = ""
        if starts and metric == "weight":
            latest = starts[-1]
            pivot = date.fromisoformat(latest["occurred_on"][:10])
            after_days = max(14, min(180, (today - pivot).days + 1))
            cmp, _err = _try(_lane(trends_mod.compare_windows), samples, metric, pivot=pivot, before_days=28,
                             after_days=after_days)
            if cmp:
                from .ops import short_drug

                text = _compare_text(cmp, unit, short_drug(latest["drug"]))
        first_day, last_day = str(cov.get("first_day") or ""), str(cov.get("last_day") or "")
        gaps = sum(1 for g in cov.get("gaps") or []  # only gaps inside the data, not the months before it began
                   if first_day <= str(g.get("start", "")) and str(g.get("end", "")) <= last_day)
        blocks.append(
            f"<div class='panel'><h3>{_e(label)}</h3><div class='chart'>{svg}</div>{text}"
            f"<p class='note'>{_e(cov.get('days_with_data'))} days with data from {_e(cov.get('first_day'))} to "
            f"{_e(cov.get('last_day'))}; {gaps} gap{'s' if gaps != 1 else ''} of a week or more. Source: Apple "
            f"Health ({_e(series.get('source_policy'))}); calculation {_e(series.get('calc_version'))}.</p></div>")
    if not blocks:
        return "", 0
    return "<h2>Trends from Apple Health</h2>" + "".join(blocks), 0


def _labs_section(vault, policy) -> tuple[str, int]:
    from .ops import lab_series

    if _module("health.fhir") is None:
        return "", 0
    if not allowed(policy, Category.RECORDS):
        return "", 1
    trends_mod = _module("health.trends")
    blocks, withheld = [], 0
    for lab, label in LABS:
        series, _err = _try(lab_series, vault, lab, policy)
        if not series:
            continue
        withheld += series.get("withheld", 0)
        points = [p for p in series.get("points") or [] if p.get("value") is not None]
        if not points:
            continue
        unit = series.get("unit") if isinstance(series.get("unit"), str) else ""
        chart = ""
        if trends_mod is not None and unit and len(points) > 1:
            chart = f"<div class='chart'>{trends_mod.trend_svg(points, unit=unit, title=f'{label} over time')}</div>"
        rows = "".join(
            f"<tr><td>{_e(str(p.get('date'))[:10])}</td><td>{_value(p.get('value'))} {_e(p.get('unit') or '')}</td>"
            f"<td>{_e(_range(p.get('reference_range')))}</td><td>{_e(p.get('interpretation') or '')}</td></tr>"
            for p in points[-12:])
        blocks.append(f"<div class='panel'><h3>{_e(label)}</h3>{chart}<table><tr><th>Date</th><th>Result</th>"
                      f"<th>Reference range</th><th>Flag</th></tr>{rows}</table></div>")
    if not blocks:
        return "", withheld
    return "<h2>Lab results from your records</h2>" + "".join(blocks), withheld


def _range(ref) -> str:
    if not isinstance(ref, dict) or (ref.get("low") is None and ref.get("high") is None):
        return ""
    if ref.get("low") is None:
        return f"below {_value(ref['high'])}"
    if ref.get("high") is None:
        return f"above {_value(ref['low'])}"
    return f"{_value(ref['low'])} to {_value(ref['high'])}"


def _genes_section(vault, meds, policy) -> tuple[str, int]:
    from .ops import _pack_calls, _pgx, display_drug

    if _module("knowledge.pack") is None or _module("genome.calls") is None:
        return "", 0
    if not allowed(policy, Category.PGX):
        return "", 1
    drugs = _current(meds)
    if not drugs:
        return "", 0
    loaded, err = _try(_pack_calls, vault)
    if loaded is None:
        return f"<h2>Genes and your medications</h2><p class='note'>{_e(err)}</p>", 0
    pack, calls = loaded
    blocks, withheld = [], 0
    for drug in drugs:
        result = _pgx(pack, drug, calls, policy)
        withheld += result.get("withheld", 0)
        entries = result.get("entries") or []
        if not entries:
            blocks.append(f"<div class='panel'><h3>{_e(display_drug(drug))}</h3><p class='note'>"
                          f"{_e(result.get('note') or 'No gene–drug evidence in the knowledge pack.')}</p></div>")
            continue
        rows = []
        for entry in entries:
            status = entry.get("status", "not_assessed")
            mine = ", ".join(f"{k} {v}" for k, v in (entry.get("your_genotypes") or {}).items()) or "–"
            src = (entry.get("sources") or [{}])[0] if entry.get("sources") else {}
            cite = ", ".join(str(x) for x in (src.get("journal"), src.get("year")) if x)
            if src.get("pmid"):
                cite += f" · PMID {src['pmid']}"
            rows.append(
                f"<tr><td>{_e(entry.get('gene'))}</td><td>{_e(mine)}</td><td><span class='badge {_e(status)}'>"
                f"{_e(_STATUS.get(status, status))}</span></td><td>{_e(entry.get('finding'))}"
                f"<div class='note'>Evidence: {_e(entry.get('evidence') or entry.get('evidence_level') or '')}"
                f"{' · ' + _e(cite) if cite else ''}</div></td></tr>")
        blocks.append(f"<div class='panel'><h3>{_e(display_drug(drug))}</h3><table><tr><th>Gene</th><th>Your genotype</th>"
                      f"<th>Status</th><th>What it means</th></tr>{''.join(rows)}</table></div>")
    disclaimer = ""
    try:
        from .knowledge.pack import DISCLAIMER as disclaimer
    except ImportError:
        pass
    return ("<h2>Genes and your medications</h2>" + "".join(blocks)
            + (f"<p class='note'>{_e(disclaimer)}</p>" if disclaimer else "")), withheld


def _module(name):
    import importlib

    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError:
        return None


def _lane(fn):
    from .ops import _lane as wrap

    return lambda *a, **k: wrap(fn, *a, **k)


def write_brief(vault, samples, out_dir, *, policy, today=None) -> Path:
    from .ops import medication_visible

    today = today or date.today()
    recorded = _medications(vault)
    # Each medication passes the policy on its own category (a contraceptive is reproductive), once, here: every
    # section below sees only the medications the policy shows.
    meds = [m for m in recorded if medication_visible(policy, m["category"])]
    hidden_meds = len(recorded) - len(meds)
    sections, withheld = [], 0
    for build in (lambda: _meds_section(meds, hidden_meds), lambda: _trend_section(samples, meds, policy, today),
                  lambda: _labs_section(vault, policy), lambda: _genes_section(vault, meds, policy)):
        body, hidden = build()
        withheld += hidden
        if body:
            sections.append(body)
    if not recorded and len(sections) <= 1:  # only the "no medications" placeholder, or nothing at all
        sections = ["<p>Nothing to show yet. Import an Apple Health export, your records or your DNA raw "
                    "data first, or ask your agent to record a medication.</p>"]
    hidden_note = (f"<p class='panel warn'>{withheld} item{'s' if withheld != 1 else ''} hidden by your privacy "
                   "settings.</p>" if withheld else "")
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'><title>Health brief</title>"
        f"<style>{_STYLE}</style></head><body><main><h1>Your health brief</h1>"
        f"<p class='lede'>Prepared on this machine on {_e(today.isoformat())} from your own records. Information "
        "with limits, not medical advice: talk to your prescriber before changing any medication.</p>"
        f"{hidden_note}{''.join(sections)}"
        f"<footer><p>Caduceus {BRIEF_VERSION}. This page was made on this machine and is stored only here.</p></footer>"
        "</main></body></html>")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"health-brief-{today.isoformat()}.html"
    fd, part = tempfile.mkstemp(dir=out_dir, prefix=path.name + ".", suffix=".part")  # never shared by two writers
    os.close(fd)
    try:
        Path(part).write_text(page, encoding="utf-8")
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.unlink(part)
    return path
