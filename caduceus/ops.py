"""Tool operations: the glue between the four tools and the core and lane modules (orchestrator-owned).

Every result is filtered through the privacy policy; withheld items are counted, never described. The agent can
read the privacy settings but never change them: only the user can, with ``/caduceus`` or the config file.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from .core import jobs, search as search_index, sources
from .core.contracts import SourceKind
from .core.db import open_samples, open_vault, write_txn
from .core.paths import check_user_path, data_dir, reports_dir
from .core.policy import (DEFAULTS, MARKER_CATEGORY, SENSITIVE, Category, Policy, allowed, categorize,
                          medication_category, visible)
from .tools import LIMITS, ToolError

IMPORT_WAIT_S = 45.0       # Hermes cuts a tool call off at 420 s; imports keep going in the background
WATCH_WAIT_S = 300.0
_RSID = re.compile(r"^rs\d+$", re.I)
_GENE = re.compile(r"^[A-Z0-9-]{2,15}$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
_LOINC = re.compile(r"^\d{1,7}-\d$")
_SENSITIVE_MARKERS = MARKER_CATEGORY  # APOE and the BRCA founder variants keep fixed categories
CHIP_LIMITS = ("Consumer DNA chips test common variants; rare variants can be missed or miscalled. A clinical test "
               "confirms anything important.")
GUIDANCE = ("Information with limits, not medical advice. Never suggest starting, stopping or changing a medication "
            "or its dose; for any medication decision, say to talk to the prescriber.")


# ---------------------------------------------------------------- settings, connections, helpers

def _cfg(ctx, key, default=None):
    try:
        value = ctx.get_config(key, None)
    except Exception:
        value = None
    return DEFAULTS.get(key, default) if value is None else value


def policy_for(ctx) -> Policy:
    return Policy.from_settings(lambda key, default: _cfg(ctx, key, default))


class _Store:
    """Opens the vault (and samples on demand) for one tool call and closes them after."""

    def __init__(self):
        self.vault = open_vault()
        self._samples = None

    @property
    def samples(self):
        if self._samples is None:
            self._samples = open_samples()
        return self._samples

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.vault.close()
        if self._samples is not None:
            self._samples.close()


def _need(module: str, name: str):
    """Import a lane module lazily; a missing one is a clear error, not a crash."""
    try:
        mod = importlib.import_module(f"{__package__}.{module}")
        return getattr(mod, name)
    except (ImportError, AttributeError) as exc:
        raise ToolError(f"{module}.{name} is not available in this build ({type(exc).__name__})") from exc


def _date(value, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO date like 2026-05-01") from exc


def _withheld(count: int) -> dict:
    return {"withheld": count, "withheld_note": "hidden by your privacy settings"} if count else {}


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    """A whole-number argument within its schema bounds, or a clear error (never an internal one)."""
    value = args.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ToolError(f"{key} must be a whole number")
    try:
        number = int(value)
        if isinstance(value, float) and value != number:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ToolError(f"{key} must be a whole number") from None
    if not low <= number <= high:
        raise ToolError(f"{key} must be between {low} and {high}")
    return number

def _float(args: dict, key: str) -> float | None:
    value = args.get(key)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ToolError(f"{key} must be a number") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ToolError(f"{key} must be a finite number")
    return number

def _strings(args: dict, key: str) -> list[str]:
    """A list-of-strings argument; one string is taken as a one-item list."""
    value = args.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{key} must be a list of strings")
    return list(value)

def medication_visible(policy, category) -> bool:
    """A medication shows only when medications show and its own category does (a contraceptive is reproductive)."""
    return visible(policy, category or Category.MEDICATION, kind="medication")


def _file_url(path) -> dict:
    path = path.resolve()
    return {"path": str(path), "file_url": path.as_uri()}


def _count(conn, sql: str):
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:  # a table from a module this build does not have
        return None


# ---------------------------------------------------------------- dispatch

def dispatch(ctx, tool: str, args: dict) -> dict:
    handlers = {
        "caduceus_search": search_tool, "caduceus_genome": genome_tool,
        "caduceus_trend": trend_tool, "caduceus_capture": capture_tool,
    }
    if tool not in handlers:
        raise ToolError(f"unknown tool {tool}")
    return handlers[tool](ctx, args)


# ---------------------------------------------------------------- caduceus_search

def search_tool(ctx, args: dict) -> dict:
    policy = policy_for(ctx)
    limit = _int(args, "limit", 8, 1, 25)
    kinds = _strings(args, "kinds")
    with _Store() as store:
        if args.get("ref"):
            record = search_index.fetch(store.vault, str(args["ref"]), policy=policy)
            if record is None:
                raise ToolError("no record with that ref")
            if record.get("kind") == "medication":
                record["guidance"] = GUIDANCE
            return record
        query = str(args.get("query") or "").strip()
        listed = [k for k in kinds if k in search_index.KINDS]
        if query:
            found = search_index.search(store.vault, query, policy=policy, kinds=kinds, limit=limit)
        elif listed:
            found = search_index.browse(store.vault, policy=policy, kinds=listed, limit=limit)
        elif kinds:  # an unknown kind must not read as "you have none"
            raise ToolError("unknown kinds; use one of: " + ", ".join(search_index.KINDS))
        else:
            raise ToolError("give a query, or a ref to fetch")
    out = {"query": query, "results": found["results"], "match": found.get("match")}
    out.update(_withheld(found["withheld"]))
    if not query:
        out["scope_note"] = f"This lists your recorded {', '.join(listed)}, newest first, up to {limit}."
    elif found["results"]:
        out["scope_note"] = ('These results match your query; they are not a complete list. To list every medication, '
                             'call caduceus_search with kinds=["medication"] and no query.')
    if any(item["kind"] == "medication" for item in found["results"]):
        out["guidance"] = GUIDANCE
    if not found["results"]:
        out["note"] = "Nothing matched. Try fewer or different words, or import more data."
    return out


# ---------------------------------------------------------------- caduceus_genome

def _get_calls(conn, markers):
    get_calls = _need("genome.calls", "get_calls")
    no_callset = _need("genome.calls", "NoCallset")
    try:
        return get_calls(conn, markers)
    except no_callset as exc:
        raise ToolError("no DNA raw data is imported yet: import a 23andMe or AncestryDNA file first") from exc


def _marker_category(vault, marker: str) -> str:
    """The most sensitive category any variant-report record gives this marker (APOE markers are fixed)."""
    if marker.lower() in _SENSITIVE_MARKERS:
        return _SENSITIVE_MARKERS[marker.lower()].value
    sensitive = sorted(c.value for c in SENSITIVE)
    row = vault.execute(
        "SELECT category FROM annotation_snapshot WHERE marker=? COLLATE NOCASE AND kind='snp' "
        f"ORDER BY category IN ({','.join('?' * len(sensitive))}) DESC, id DESC LIMIT 1",
        (marker, *sensitive),
    ).fetchone()
    return row["category"] if row else Category.GENERAL.value


def _snapshot_rows(vault, where: str, params=(), limit=50):
    return vault.execute(
        "SELECT id, marker, title, geno, magnitude, repute, summary, genes_json, category, kind "
        f"FROM annotation_snapshot WHERE kind IN ('snp','genoset') AND {where} "
        "ORDER BY magnitude DESC, id LIMIT ?", (*params, limit)
    ).fetchall()


def _snapshot_dict(row) -> dict:
    try:
        genes = json.loads(row["genes_json"] or "[]")
    except ValueError:
        genes = []
    return {
        "marker": row["marker"] or row["title"], "your_genotype": row["geno"], "magnitude": row["magnitude"],
        "repute": row["repute"] or "neutral", "summary": row["summary"], "genes": genes,
        "category": row["category"], "source": "your variant report (SNPedia text, CC BY-NC-SA 3.0)",
    }


def _lookup(store, policy, markers) -> dict:
    wanted = list(dict.fromkeys(m.strip() for m in markers if isinstance(m, str) and m.strip()))[:50]
    if not wanted:
        raise ToolError("give one or more rsids")
    calls = _get_calls(store.vault, wanted)
    results, withheld = [], 0
    for name in wanted:
        call = calls.get(name)
        key = call.resolved_marker if call is not None and call.resolved_marker else name
        if not (allowed(policy, _marker_category(store.vault, key))
                and allowed(policy, _marker_category(store.vault, name))):
            withheld += 1
            continue
        entry = call.as_dict() if call is not None else {"marker": name, "state": "not_on_chip"}
        notes = [_snapshot_dict(r) for r in _snapshot_rows(store.vault, "marker=? COLLATE NOCASE", (key,), 5)
                 if allowed(policy, r["category"])]
        if notes:
            entry["annotations"] = notes
        results.append(entry)
    return {"results": results, "limits": [CHIP_LIMITS, LIMITS], **_withheld(withheld)}


def _pack():
    return _need("knowledge.pack", "load_pack")()


def _pack_rsids(pack) -> list[str]:
    """Every rsID the knowledge pack mentions, found by walking it (the pack's own shape may change)."""
    seen: set[str] = set()

    def walk(obj, depth=0):
        if depth > 12:
            return
        if isinstance(obj, str):
            if _RSID.match(obj):
                seen.add(obj.lower())
        elif isinstance(obj, dict):
            for key, value in obj.items():
                walk(key, depth + 1)
                walk(value, depth + 1)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for value in obj:
                walk(value, depth + 1)
        elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            for f in dataclasses.fields(obj):
                walk(getattr(obj, f.name), depth + 1)
        elif hasattr(obj, "__dict__"):
            walk(vars(obj), depth + 1)

    walk(pack)
    return sorted(seen)


def _pack_calls(vault):
    """The knowledge pack, and the user's calls for every marker it knows plus APOE (matched locally)."""
    pack = _pack()
    return pack, _get_calls(vault, sorted(set(_pack_rsids(pack)) | set(_SENSITIVE_MARKERS)))


def _pgx(pack, drug: str, calls, policy) -> dict:
    """Gene–drug entries for *drug*, filtered by the policy, with the user's own genotypes beside each marker."""
    result = _need("knowledge.pack", "pgx_check")(pack, drug, calls)
    entries = list(result.get("entries") or [])
    shown = [e for e in entries if allowed(policy, e.get("category", Category.PGX))]
    for entry in shown:
        mine = {}
        for marker in entry.get("markers") or ():
            rsid = marker.get("rsid") if isinstance(marker, dict) else None
            call = calls.get(rsid) if rsid else None
            if call is not None:
                mine[rsid] = call.genotype if call.genotype else call.state.value
        entry["your_genotypes"] = mine
    return {**result, "entries": shown, **_withheld(len(entries) - len(shown))}


def _lane(fn, *args, **kwargs):
    """Call a lane function; its ValueErrors are input problems (an unknown metric, a bad window)."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


_GENOME_OPS = ("lookup", "gene", "pgx", "derived", "summary", "filter", "report")


def genome_tool(ctx, args: dict) -> dict:
    op = args.get("op")
    if op not in _GENOME_OPS:
        raise ToolError("op must be one of " + ", ".join(_GENOME_OPS))
    policy = policy_for(ctx)
    with _Store() as store:
        warning = {}
        if op in ("lookup", "gene", "pgx", "derived", "summary"):
            active = next((c for c in _need("genome.calls", "callsets")(store.vault) if c["complete"]), None)
            if active and any(c.get("same_person") is False for c in json.loads(active["comparison_json"])):
                file = store.vault.execute("SELECT original_name FROM source_artifact WHERE id=?",
                                           (active["source_id"],)).fetchone()[0]
                warning["warning"] = ("Your vault holds DNA files from more than one person. "
                                      f"These results come from the newest file, {file}.")
        if op == "lookup":
            return {**_lookup(store, policy, _strings(args, "rsids")), **warning}
        if op == "gene":
            gene = str(args.get("gene") or "").strip().upper()
            if not _GENE.match(gene):
                raise ToolError("give a gene symbol such as GLP1R")
            if not allowed(policy, categorize(genes=[gene])):
                return {"gene": gene, "results": [], **_withheld(1), **warning}
            rows = _snapshot_rows(store.vault, "genes_json LIKE ?", (f'%"{gene}"%',), _int(args, "limit", 40, 1, 100))
            markers = sorted({r["marker"] for r in rows if r["marker"]})
            if not rows:
                # Which genes the pack covers is public, so check privacy only when it has markers.
                markers = sorted({m["rsid"] for e in _pack().entries if e["gene"] == gene for m in e["markers"]})
                if markers:
                    if not allowed(policy, categorize(genes=[gene], default=Category.PGX)):
                        return {"gene": gene, "results": [], **_withheld(1), **warning}
                    return {"gene": gene, **_lookup(store, policy, markers), **warning,
                            "note": "From the built-in knowledge pack; your variant report has no rows for this gene."}
            if not markers:
                return {"gene": gene, "results": [], "note": "No markers for this gene in your variant report.",
                        **warning}
            return {"gene": gene, **_lookup(store, policy, markers), **warning}
        if op == "pgx":
            drug = str(args.get("drug") or "").strip()
            if not drug:
                raise ToolError("give a drug name")
            if not allowed(policy, Category.PGX):
                return {"drug": drug, **_withheld(1), **warning}
            pack, calls = _pack_calls(store.vault)
            return {**_pgx(pack, drug, calls, policy), **warning}
        if op == "derived":
            pack, calls = _pack_calls(store.vault)
            derived = _need("knowledge.pack", "derive")(pack, calls)
            shown = [d for d in derived if allowed(policy, d.get("category", Category.PGX))]
            return {"derived": shown, "limits": [CHIP_LIMITS, LIMITS], **_withheld(len(derived) - len(shown)),
                    **warning}
        if op == "summary":
            return {**_genome_summary(store, policy), **warning}
        if op == "filter":
            return _filter(store, policy, args)
        try:
            path = _need("genome.report", "build_report")(store.vault, reports_dir(), policy=policy)
        except ValueError as exc:  # no report imported yet, and similar input problems
            raise ToolError(str(exc)) from exc
        return {**_file_url(path), "note": "Your variant report was rebuilt on this machine. Open it in a browser."}


def _filter(store, policy, args) -> dict:
    where, params = ["1=1"], []
    minimum = _float(args, "min_magnitude")
    if minimum is not None:
        where.append("magnitude >= ?")
        params.append(minimum)
    repute = args.get("repute")
    if repute in ("good", "bad"):
        where.append("lower(repute) = ?")
        params.append(repute)
    elif repute == "neutral":
        where.append("(repute IS NULL OR repute = '' OR lower(repute) = 'none')")
    if args.get("topic"):
        where.append("(record_json LIKE ? OR summary LIKE ?)")
        params += [f"%{args['topic']}%"] * 2
    rows = _snapshot_rows(store.vault, " AND ".join(where), params, _int(args, "limit", 30, 1, 100))
    shown = [_snapshot_dict(r) for r in rows if allowed(policy, r["category"])]
    return {"results": shown, "limits": [CHIP_LIMITS, LIMITS], **_withheld(len(rows) - len(shown))}


def _genome_summary(store, policy) -> dict:
    vault = store.vault
    callsets = [dict(r) for r in vault.execute(
        "SELECT c.id, c.vendor, c.chip_version, c.build, c.marker_count, c.comparison_json, "
        "s.original_name AS file, s.added_at "
        "FROM callset c JOIN source_artifact s ON s.id=c.source_id ORDER BY c.id DESC")]
    for callset in callsets:
        callset["comparisons"] = json.loads(callset.pop("comparison_json"))
    records = vault.execute("SELECT count(*) FROM annotation_snapshot WHERE kind IN ('snp','genoset')").fetchone()[0]
    top = _snapshot_rows(vault, "magnitude >= 3", (), 40)
    allowed_top = [r for r in top if allowed(policy, r["category"])]
    out = {"callsets": callsets, "variant_report_records": records,
           "top_findings": [_snapshot_dict(r) for r in allowed_top[:12]], "privacy_level": policy.level,
           "limits": [CHIP_LIMITS, LIMITS], **_withheld(len(top) - len(allowed_top))}
    if any(c["complete"] for c in _need("genome.calls", "callsets")(vault)):
        if not allowed(policy, Category.PGX):
            out.update(_withheld(out.get("withheld", 0) + 1))
        else:
            pack, calls = _pack_calls(vault)
            shown = [e for e in pack.entries if allowed(policy, categorize(genes=[e["gene"]], default=Category.PGX))]
            filtered = dataclasses.replace(pack, cpic=[e for e in pack.cpic if e in shown],
                                           glp1=[e for e in pack.glp1 if e in shown])
            result = _need("knowledge.pack", "profile")(filtered, calls)
            derived = result["derived"]
            result["derived"] = [d for d in derived if allowed(policy, d.get("category", Category.PGX))]
            out["gene_drug_profile"] = result
            out.update(_withheld(out.get("withheld", 0) + len(pack.entries) - len(shown)
                                 + len(derived) - len(result["derived"])))
    if not callsets and not records:
        out["note"] = "Nothing imported yet. Use caduceus_capture op=import with the file's absolute path."
    return out


# ---------------------------------------------------------------- caduceus_trend

_TREND_OPS = ("metrics", "series", "compare", "labs", "check")


def canonical_drug(text) -> str | None:
    """The knowledge pack's name for a drug ("Ozempic 0.5 mg pen" -> "semaglutide"), when the pack is present."""
    try:
        return _need("knowledge.pack", "normalize_drug")(str(text or ""))
    except ToolError:
        return None


def display_drug(text) -> str:
    """A medication as written in the record, tidied for display: "ozempic (semaglutide) …" -> "Ozempic (semaglutide) …"."""
    text = " ".join(str(text or "").split())
    return text[:1].upper() + text[1:]


def short_drug(text) -> str:
    """The first word of a medication's name, for chart labels: "ozempic (semaglutide) 0.5 mg" -> "Ozempic"."""
    first = re.split(r"[\s(]", " ".join(str(text or "").split()), maxsplit=1)[0]
    return first[:1].upper() + first[1:]


def same_drug(asked: str, recorded: str) -> bool:
    """Whether a recorded medication (a record's free text, or a name the user confirmed) is the drug asked about."""
    asked_l, recorded_l = asked.strip().lower(), " ".join(str(recorded or "").split()).lower()
    if not asked_l or not recorded_l:
        return False
    canonical = canonical_drug(asked)
    if canonical and canonical == canonical_drug(recorded):
        return True
    words = set(re.findall(r"[a-z0-9-]+", recorded_l))
    return asked_l == recorded_l or (len(asked_l) >= 4 and (asked_l in words or (canonical or "") in words))


def _drug_start(vault, drug: str, policy) -> tuple[date | None, int]:
    """The latest recorded start of *drug* (the current course when it was stopped and restarted), and how many
    matching starts the policy hid. A start the records give only to the month or year is no pivot."""
    rows = [r for r in vault.execute(
        "SELECT drug, brand, occurred_on, category FROM medication_event WHERE event='start' ORDER BY occurred_on")
        if same_drug(drug, r["drug"]) or same_drug(drug, r["brand"] or "")]
    shown = [r for r in rows if medication_visible(policy, r["category"])]
    starts = [r["occurred_on"] for r in shown if r["occurred_on"] and _ISO_DAY.match(r["occurred_on"])]
    if starts:
        return date.fromisoformat(starts[-1][:10]), 0
    if len(shown) < len(rows):
        return None, len(rows) - len(shown)
    if shown:
        raise ToolError(f"the recorded start of {drug} has no day (only {shown[-1]['occurred_on']}): give a pivot date")
    raise ToolError(f"no start date recorded for {drug}: record it with caduceus_capture op=medication")


def trend_tool(ctx, args: dict) -> dict:
    op = args.get("op")
    if op not in _TREND_OPS:
        raise ToolError("op must be one of " + ", ".join(_TREND_OPS))
    policy = policy_for(ctx)
    with _Store() as store:
        if op == "labs":
            lab = str(args.get("lab") or "").strip()
            if not lab:
                raise ToolError("give a lab name or LOINC code, e.g. HbA1c")
            return lab_series(store.vault, lab, policy, start=_date(args.get("start"), "start"),
                              end=_date(args.get("end"), "end"))
        if not allowed(policy, Category.WELLNESS):
            return _withheld(1)
        if op == "metrics":
            return {"metrics": _lane(_need("health.trends", "metrics"), store.samples)}
        if op == "check":
            return _lane(_need("health.trends", "daily_check"), store.samples)
        metric = str(args.get("metric") or "").strip()
        if not metric:
            raise ToolError("give a metric, e.g. weight; op=metrics lists them")
        drug = str(args.get("drug") or "").strip()
        if op == "series":
            end = _date(args.get("end"), "end")
            result = _lane(_need("health.trends", "trend"), store.samples, metric,
                           start=_date(args.get("start"), "start"), end=end,
                           bucket=args.get("bucket") or "week")
        else:
            before_days, after_days = _int(args, "before_days", 28, 1, 730), _int(args, "after_days", None, 1, 730)
            pivot = _date(args.get("pivot"), "pivot")
            drug_pivot = pivot is None
            if drug_pivot:
                if not args.get("drug"):
                    raise ToolError("give a pivot date or a drug whose start date is recorded")
                pivot, hidden = _drug_start(store.vault, drug, policy)
                if pivot is None:  # the drug's start is hidden: nothing about it, not even its date
                    return {"metric": metric, **_withheld(hidden)}
            explicit_after = after_days is not None
            if not explicit_after:
                after_days = default_after_days(store.samples, metric, pivot)
            result = _lane(_need("health.trends", "compare_windows"), store.samples, metric, pivot=pivot,
                           before_days=before_days, after_days=after_days)
            result = {**result, "pivot": pivot.isoformat()}
            end = pivot + timedelta(days=after_days - 1)
            if drug_pivot:
                result["next_step"] = (f"For gene–drug evidence about {drug}, "
                                       f"call caduceus_genome op=pgx drug={drug}.")
        if op == "compare" or drug:
            result["guidance"] = GUIDANCE
        first, last = store.samples.execute(
            "SELECT min(day), max(day) FROM daily_rollup WHERE metric=?", (metric,)).fetchone()
        if first is not None:
            result["data_range"] = {"first_day": first, "last_day": last}
            if end is not None and end.isoformat() < last:
                if op == "compare":
                    result["window_note"] = (
                        f"This window ends on {end.isoformat()}; your data continues to {last}. "
                        + ("Leave out after_days to cover the data after the start (up to 365 days)." if explicit_after
                           else "A compare covers at most 365 days after the start by default; use op=series "
                                "for the later data."))
                else:
                    result["window_note"] = f"This series ends on {end.isoformat()}; your data continues to {last}."
        return result


def default_after_days(samples, metric: str, pivot: date, today: date | None = None) -> int:
    """The default after window, shared with the brief: from the start to the latest data on or before today, 28 to 365
    days, so a change that slows after the first weeks (a plateau) shows in its second half."""
    row = samples.execute("SELECT max(day) FROM daily_rollup WHERE metric=? AND day<=?",
                          (metric, (today or date.today()).isoformat())).fetchone()
    last = date.fromisoformat(row[0][:10]) if row and row[0] else pivot
    return min(365, max(28, (last - pivot).days + 1))


def lab_series(vault, lab: str, policy, *, start=None, end=None) -> dict:
    """A lab result over time, with points the policy hides removed and counted (shared with the brief)."""
    series = _need("health.fhir", "observation_series")

    def allow(category):
        return visible(policy, category or Category.RECORDS, kind="observation")

    result = _lane(series, vault, lab, start=start, end=end, allow=allow)
    if not result.get("points") and not result.get("withheld") and not _LOINC.match(lab):
        # A word from the lab's own name ("A1c", "creatinine") finds its code; the most common match wins.
        like = "%" + lab.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        row = vault.execute("SELECT code FROM observation WHERE code IS NOT NULL AND name LIKE ? ESCAPE '\\' "
                            "GROUP BY code ORDER BY count(*) DESC LIMIT 1", (like,)).fetchone()
        if row:
            result = {**_lane(series, vault, row["code"], start=start, end=end, allow=allow), "name": lab}
    withheld = int(result.pop("withheld", 0) or 0)
    points = list(result.get("points") or [])
    if withheld and not points and not result.get("unplotted_points"):
        return {"name": lab, "points": [], **_withheld(withheld)}  # nothing visible: no dates, units or coverage
    units = sorted({p["unit"] for p in points if p.get("unit")})
    out = {**result, "points": points, "unit": units[0] if len(units) == 1 else units}
    if not points and not withheld:
        out["note"] = "No results for this lab. Import records (FHIR JSON or an Apple Health export with Health Records)."
    return {**out, **_withheld(withheld)}


# ---------------------------------------------------------------- caduceus_capture: import

_IMPORTERS = {
    SourceKind.DTC.value: ("genome.dtc", "import_dtc", "vault"),
    SourceKind.PROMETHEASE.value: ("genome.promethease", "import_promethease", "vault"),
    SourceKind.APPLE_HEALTH.value: ("health.apple", "import_apple_health", "both"),
    SourceKind.FHIR.value: ("health.fhir", "import_fhir", "vault"),
}


def _import(args: dict) -> dict:
    try:
        path = check_user_path(args.get("path"))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    kind = sources.sniff_kind(path)
    if kind not in _IMPORTERS:
        raise ToolError("not a recognised file: expected 23andMe, AncestryDNA, MyHeritage, FamilyTreeDNA or Living DNA raw data, "
                        "a Promethease report, "
                        "an Apple Health export.zip or FHIR JSON. Whole-genome VCF files are not supported yet.")
    module, name, dbs = _IMPORTERS[kind]
    importer = _need(module, name)
    with _Store() as store:
        src, existed = sources.store_source(store.vault, path, kind)

    def run():
        vault = open_vault()
        try:
            if dbs == "both":
                samples = open_samples()
                try:
                    return importer(samples, vault, src).as_dict()
                finally:
                    samples.close()
            return importer(vault, src).as_dict()
        finally:
            vault.close()

    outcome = jobs.submit(src.sha256, run, wait_s=IMPORT_WAIT_S)
    base = {"file": src.original_name, "kind": kind, "source_id": src.id, "already_in_vault": existed}
    if outcome["state"] == "done":
        return {**base, **outcome["result"]}
    if outcome["state"] == "error":
        raise ToolError(f"import failed: {outcome['error']}")
    with _Store() as store:
        run_id = sources.running_run_for(store.vault, src.id)
    note = ("Already importing this file." if outcome["state"] == "busy" else
            "The import continues in the background; check it with caduceus_capture op=status.")
    return {**base, "status": "running", "import_run_id": run_id, "note": note}


# ---------------------------------------------------------------- caduceus_capture: records

_EVENT_WORDS = {"start": "Started", "dose_change": "Changed the dose of", "stop": "Stopped"}


def _medication(ctx, args: dict) -> dict:
    drug_in = str(args.get("drug") or "").strip()
    if not drug_in:
        raise ToolError("give the drug name")
    event = args.get("event") or "start"
    if event not in _EVENT_WORDS:
        raise ToolError("event must be start, dose_change or stop")
    when = _date(args.get("date"), "date")
    if when is None:
        raise ToolError("give the date (ISO, e.g. 2026-05-01)")
    try:
        generic = _need("knowledge.pack", "normalize_drug")(drug_in) or drug_in.lower()
    except ToolError:
        generic = drug_in.lower()
    brand = args.get("brand") or (drug_in if generic != drug_in.lower() else None)
    dose = str(args["dose"]).strip() if args.get("dose") not in (None, "") else None
    preview = {"drug": generic, "brand": brand, "event": event, "dose": dose, "date": when.isoformat()}
    if args.get("confirm") is not True:
        return {"needs_confirmation": True, "preview": preview,
                "instruction": "Show these details to the user. Call again with confirm=true only after they agree."}
    category = medication_category(" ".join(str(x) for x in (generic, brand, drug_in) if x))
    now = sources.now_iso()
    with _Store() as store, write_txn(store.vault):
        vault = store.vault
        row = vault.execute("SELECT id, brand, dose, category FROM medication_event WHERE drug=? AND event=? "
                            "AND occurred_on=? AND source='user'", (generic, event, when.isoformat())).fetchone()
        outcome, previous_dose, conflict = "recorded", None, False
        if row is None:
            cur = vault.execute(
                "INSERT INTO medication_event(drug, brand, event, dose, occurred_on, source, note, category, created_at) "
                "VALUES (?,?,?,?,?,'user',?,?,?)",
                (generic, brand, event, dose, when.isoformat(), args.get("text"), category.value, now))
            ref = cur.lastrowid
        else:
            ref, outcome = row["id"], "already_recorded"
            if row["category"] in {c.value for c in SENSITIVE}:
                # A sensitive record keeps its category, and details that point at another one never join it.
                conflict = category in SENSITIVE and category.value != row["category"]
                category = Category(row["category"])
            changed = {} if conflict else {k: v for k, v in (("dose", dose), ("brand", brand)) if v and v != row[k]}
            if changed:  # the user confirmed new details for the same event: the record follows them
                previous_dose, outcome = row["dose"], "updated"
                vault.execute("UPDATE medication_event SET " + ", ".join(f"{k}=?" for k in changed) + " WHERE id=?",
                              (*changed.values(), ref))
            if category.value != row["category"]:
                vault.execute("UPDATE medication_event SET category=? WHERE id=?", (category.value, ref))
        stored = vault.execute("SELECT brand, dose FROM medication_event WHERE id=?", (ref,)).fetchone()
        words = _EVENT_WORDS[event]
        search_index.index_doc(
            vault, kind="medication", ref=f"user:{ref}",
            title=f"{words} {generic}" + (f" ({stored['brand']})" if stored["brand"] else ""),
            body=f"{words} {generic} on {when.isoformat()}. Dose: {stored['dose'] or 'not given'}.",
            category=category, ts=when.isoformat())
        if outcome != "already_recorded":
            vault.execute("INSERT INTO audit_event(ts, actor, action, detail_json) VALUES (?,?,?,?)",
                          (now, "agent", f"medication_{outcome}", json.dumps(preview)))
    if not medication_visible(policy_for(ctx), category):
        return {"saved": True, **preview,
                "note": (f"Recorded. Your privacy settings hide {category.value} items, so this medication and "
                         "anything already stored about it stay out of searches, the brief and this reply until the "
                         "user unlocks it with /caduceus.")}
    out = {"recorded": outcome == "recorded", "already_recorded": outcome == "already_recorded",
           "updated": outcome == "updated", **preview, "brand": stored["brand"], "dose": stored["dose"]}
    if previous_dose is not None:
        out["previous_dose"] = previous_dose
    return out


def _journal(args: dict) -> dict:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("give the note text")
    when = _date(args.get("date"), "date") or date.today()
    category = categorize(text=text, default=Category.WELLNESS)
    now = sources.now_iso()
    with _Store() as store, write_txn(store.vault):
        cur = store.vault.execute("INSERT INTO journal(ts, text, category, created_at) VALUES (?,?,?,?)",
                                  (when.isoformat(), text, category.value, now))
        search_index.index_doc(store.vault, kind="journal", ref=str(cur.lastrowid), title=f"Note {when.isoformat()}",
                               body=text, category=category, ts=when.isoformat())
    return {"saved": True, "date": when.isoformat(), "category": category.value}


# ---------------------------------------------------------------- caduceus_capture: research watch

_WATCH_LOCK = threading.Lock()
_WATCH_PENDING: list[dict] = []   # holders for runs that finished after their tool call returned


def _filter_watch(result: dict, policy) -> dict:
    items = list(result.get("new_items") or [])
    shown = [i for i in items if visible(policy, i.get("category", Category.RESEARCH), kind="research")]
    return {**result, "new_items": shown, **_withheld(len(items) - len(shown))}


def _watch_run(ctx, args: dict) -> dict:
    if _cfg(ctx, "research_watch", True) is False:
        return {"disabled": True, "note": "The research watch is turned off in the Caduceus settings."}
    policy = policy_for(ctx)
    with _WATCH_LOCK:
        pending, _WATCH_PENDING[:] = list(_WATCH_PENDING), []
    if pending:
        if "error" in pending[-1]:
            return {"status": "failed", "error": pending[-1]["error"],
                    "note": "The last research watch run failed in the background after its call returned. "
                            "Nothing new was saved from it. Run the watch again."}
        return {**_filter_watch(pending[-1]["result"], policy),
                "note": "These are from a watch run that finished in the background."}
    run_watch = _need("research.watch", "run_watch")
    default_watch = _need("research.watch", "default_watch")
    http_cls = _need("research.http", "UrllibHttp")
    get_calls = _need("genome.calls", "get_calls")
    load_pack = _need("knowledge.pack", "load_pack")
    topics = [str(t) for t in list(_cfg(ctx, "research_topics", []) or []) + _strings(args, "topics")]

    def allow(category):  # hidden medications never become search terms sent to the research services
        return medication_visible(policy, category)

    # The waiting tool call delivers the result; only a run that outlives it is kept for the next watch_run.
    # Both sides decide under the lock, so a result is delivered exactly once.
    holder: dict = {"waiting": True}

    def job():
        try:
            vault = open_vault()
            try:
                watch = default_watch(vault, topics=topics, allow=allow)
                try:
                    calls = get_calls(vault, _pack_rsids(load_pack()))
                except Exception:  # no DNA imported yet: the watch still runs, without local matching
                    calls = None
                result = run_watch(vault, watch, http=http_cls(), now=datetime.now(timezone.utc), budget=30, calls=calls,
                                   allow=allow)
            finally:
                vault.close()
        except Exception as exc:
            with _WATCH_LOCK:
                holder["error"] = type(exc).__name__
                if not holder["waiting"]:
                    _WATCH_PENDING.append(holder)
            raise
        with _WATCH_LOCK:
            holder["result"] = result
            if not holder["waiting"]:
                _WATCH_PENDING.append(holder)
        return result

    outcome = jobs.submit("research-watch", job, wait_s=WATCH_WAIT_S)
    with _WATCH_LOCK:
        holder["waiting"] = False
        finished = "result" in holder
        error = holder.get("error")
    if finished:
        return _filter_watch(holder["result"], policy)
    if error:
        raise ToolError(f"research watch failed: {error}")
    if outcome["state"] == "error":
        raise ToolError(f"research watch failed: {outcome['error']}")
    return {"status": "running",
            "note": "The research watch is still fetching. Its results come with the next watch_run."}


# ---------------------------------------------------------------- caduceus_capture: settings and the rest

def _settings(ctx) -> dict:
    policy = policy_for(ctx)
    with _Store() as store:
        v = store.vault
        counts = {
            "files": _count(v, "SELECT count(*) FROM source_artifact"),
            "dna_callsets": _count(v, "SELECT count(*) FROM callset"),
            "variant_report_records": _count(
                v, "SELECT count(*) FROM annotation_snapshot WHERE kind IN ('snp','genoset')"),
            "lab_results": _count(v, "SELECT count(*) FROM observation"),
            "conditions": _count(v, "SELECT count(*) FROM condition"),
            "medications": _count(v, "SELECT count(*) FROM medication_event"),
            "notes": _count(v, "SELECT count(*) FROM journal"),
            "research_items": _count(v, "SELECT count(*) FROM research_item"),
        }
    return {**policy.describe(), "research_watch": _cfg(ctx, "research_watch", True),
            "research_topics": _cfg(ctx, "research_topics", []), "data_dir": str(data_dir()), "counts": counts,
            "note": ("The vault stays on this machine; tools return only slices of it. Only the user can change "
                     "privacy settings, with /caduceus privacy, hide, show, unlock or lock.")}


_CAPTURE_OPS = ("import", "status", "medication", "journal", "watch_run", "settings", "brief", "sources")


def capture_tool(ctx, args: dict) -> dict:
    op = args.get("op")
    if op not in _CAPTURE_OPS:
        raise ToolError("op must be one of " + ", ".join(_CAPTURE_OPS))
    if op == "import":
        return _import(args)
    if op == "medication":
        return _medication(ctx, args)
    if op == "journal":
        return _journal(args)
    if op == "watch_run":
        return _watch_run(ctx, args)
    if op == "settings":
        return _settings(ctx)
    if op == "brief":
        policy = policy_for(ctx)
        with _Store() as store:
            path = _need("brief", "write_brief")(store.vault, store.samples, reports_dir(), policy=policy)
        return {**_file_url(path), "note": "Your health brief was written on this machine. Open it in a browser."}
    with _Store() as store:
        if op == "status":
            if args.get("run_id") not in (None, ""):
                info = sources.run_info(store.vault, _int(args, "run_id", 0, 1, 2**62))
                if info is None:
                    raise ToolError("no import run with that id")
                return info
            return {"recent_imports": sources.recent_runs(store.vault, 10)}
        rows = [dict(r) for r in store.vault.execute(
            "SELECT id, kind, original_name AS file, size_bytes, added_at FROM source_artifact ORDER BY id DESC")]
    return {"files": rows}


# ---------------------------------------------------------------- /caduceus

_HELP = ("/caduceus [status] | privacy [open|standard] | hide <category> | show <category> | unlock <category> | "
         "lock <category> | imports | demo | help.\nCategories: " + ", ".join(c.value for c in Category) + ".\n"
         "Ask your agent to import a file by its path, or to answer questions from your DNA, Apple Health and records.")


def _category_list(ctx, key) -> list[str]:
    raw = _cfg(ctx, key, []) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(x).strip().lower() for x in raw if str(x).strip()})


def _change_category(ctx, sub: str, name: str) -> str:
    try:
        cat = Category(name.lower())
    except ValueError:
        return f"Unknown category {name!r}. Categories: " + ", ".join(c.value for c in Category) + "."
    key = "hidden_categories" if sub in ("hide", "show") else "unlocked_categories"
    current = set(_category_list(ctx, key))
    if sub in ("hide", "unlock"):
        current.add(cat.value)
    else:
        current.discard(cat.value)
    ctx.set_config(key, sorted(current))
    policy = policy_for(ctx)
    state = "shown" if allowed(policy, cat) else "hidden"
    return f"{cat.value} is now {state} (privacy level {policy.level})."


DEMO_MARKERS = 600_000
_DEMO_LOCK = threading.Lock()


def _demo() -> str:
    from .demo.persona import write_persona

    names = ("persona-23andme-v5.zip", "persona-variant-report.zip", "persona-apple-health.zip")
    separate = ("To try the demo, create a separate Hermes profile (`hermes profile create caduceus-try`), "
                "install Caduceus in it, and run /caduceus demo there.")
    with _DEMO_LOCK:
        with _Store() as store:
            rows = store.vault.execute("SELECT id, original_name FROM source_artifact ORDER BY id").fetchall()
            finishing = bool(rows)
            if rows:
                if not all(r['original_name'] in names for r in rows):
                    noun = "file" if len(rows) == 1 else "files"
                    return (f"Your vault already holds {len(rows)} imported {noun}, "
                            f"so the demo persona would mix with your data. {separate}")
                artifacts = [sources.get_source(store.vault, r['id']) for r in rows]
                stored_names = {r['original_name'] for r in rows}
                complete = stored_names == set(names) and all(
                    store.vault.execute("SELECT 1 FROM import_run WHERE source_id=? AND status='succeeded'",
                                        (src.id,)).fetchone() for src in artifacts)
                if complete:
                    return 'The demo persona is already in this vault. Ask your agent: “Why isn\'t my Ozempic working?”'
                if stored_names != set(names):
                    files = write_persona(data_dir() / "demo-files", markers=DEMO_MARKERS)
                    artifacts.extend(sources.store_source(store.vault, path, sources.sniff_kind(path))[0]
                                     for path in files.values() if path.name not in stored_names)
            else:
                medications = store.vault.execute("SELECT count(*) FROM medication_event").fetchone()[0]
                notes = store.vault.execute("SELECT count(*) FROM journal").fetchone()[0]
                if medications or notes:
                    med_word = "medication" if medications == 1 else "medications"
                    note_word = "note" if notes == 1 else "notes"
                    return (f"Your vault already holds records you added ({medications} {med_word}, {notes} {note_word}), "
                            f"so the demo persona would mix with your data. {separate}")
                files = write_persona(data_dir() / "demo-files", markers=DEMO_MARKERS)
                artifacts = [sources.store_source(store.vault, path, sources.sniff_kind(path))[0]
                             for path in files.values()]
            artifacts.sort(key=lambda src: names.index(src.original_name))

    def run():
        with _Store() as store:
            results = []
            for src in artifacts:
                module, name, dbs = _IMPORTERS[src.kind]
                importer = _need(module, name)
                result = (importer(store.samples, store.vault, src) if dbs == "both"
                          else importer(store.vault, src))
                results.append(result.as_dict())
            return results

    outcome = jobs.submit("demo:" + artifacts[0].sha256, run, wait_s=0)
    if outcome['state'] == 'busy':
        return "The demo persona is still importing. Check with /caduceus imports."
    if finishing:
        return ('Finishing the demo persona\'s import in the background. Check with /caduceus imports, '
                'then ask your agent: “Why isn\'t my Ozempic working?”')
    return ('Loading the demo persona (an invented person, not real data) in the background. '
            'It takes about a minute. Check with /caduceus imports. Then ask your agent: '
            '“Why isn\'t my Ozempic working?” or “Would Mounjaro be different for me?”')


def command(ctx, raw: str) -> str:
    parts = raw.strip().split()
    sub = parts[0].lower() if parts else "status"
    if sub == "demo":
        if len(parts) != 1:
            return "Usage: /caduceus demo"
        return _demo()
    if sub == "privacy":
        if len(parts) > 2:
            return "Usage: /caduceus privacy [open|standard]"
        if len(parts) > 1:
            level = parts[1].lower()
            if level not in ("open", "standard"):
                return "The privacy level must be open or standard."
            ctx.set_config("privacy_level", level)
            return f"Caduceus privacy level set to {level}."
        info = policy_for(ctx).describe()
        return (f"Privacy level: {info['privacy_level']}. Hidden: {', '.join(info['hidden_categories']) or 'none'}. "
                f"Sensitive categories shown: {', '.join(info['sensitive_categories_shown']) or 'none'}.")
    if sub in ("hide", "show", "unlock", "lock"):
        if len(parts) != 2:
            return f"Usage: /caduceus {sub} <category>"
        return _change_category(ctx, sub, parts[1])
    if sub == "imports":
        with _Store() as store:
            runs = sources.recent_runs(store.vault, 8)
        if not runs:
            return "No imports yet."
        return "\n".join(f"#{r['import_run_id']} {r['file']} ({r['kind']}): {r['status']}" for r in runs)
    if sub == "help":
        return _HELP
    if sub != "status":
        return f"Unknown command {parts[0]!r}.\n{_HELP}"
    info = _settings(ctx)
    c = info["counts"]
    return (f"Caduceus: {c['files']} files, {c['dna_callsets'] or 0} DNA callsets, {c['variant_report_records'] or 0} "
            f"variant report records, {c['lab_results']} lab results, {c['medications']} medications, "
            f"{c['notes']} notes. Privacy level: {info['privacy_level']}. /caduceus help lists the commands.")
