"""A first-session agenda: normal schema upgrades, no captured data or rollup writes."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import ops
from .core.policy import Category, SENSITIVE, allowed, categorize, visible


_AREAS = (
    ("own_numbers", "Your own numbers"), ("actionable", "Actionable findings"),
    ("sensitive", "Sensitive results"), ("gene_drug", "Gene–drug results"),
    ("carrier_and_screening", "Carrier and screening"), ("traits", "Traits"),
)
_RELIABILITY = {
    "called": "called from the chip", "ambiguous": "ambiguous on a chip", "not_assessed": "not assessed",
}


def _reliability(item, derived=None):
    if derived is not None:
        return _RELIABILITY.get(derived["confidence"], "not assessed")
    if re.fullmatch(r"i\d+", item["marker"], re.I):
        return "an internal probe: confirm before acting"
    if (item.get("magnitude") or 0) >= 4 and item.get("repute") == "bad":
        return ("a high-impact result on a consumer chip: rare variants are often miscalled, "
                "so a clinical test should confirm it")
    return "chips usually call common variants reliably"


def _point(point):
    result = {key: point.get(key) for key in (
        "date", "value", "value_text", "unit", "interpretation", "reference_range")}
    result["reference_range_mismatch"] = bool(point.get("reference_range_mismatch"))
    bounds = result["reference_range"] or {}
    low, high, value = bounds.get("low"), bounds.get("high"), result["value"]
    numeric = lambda n: isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
    flag = "unknown"
    if (not result["reference_range_mismatch"] and numeric(value)
            and any(n is not None for n in (low, high))
            and all(n is None or numeric(n) for n in (low, high))
            and not (low is not None and high is not None and low > high)):
        flag = "below" if low is not None and value < low else "above" if high is not None and value > high else "within"
    return {**result, "range_flag": flag}


def _retired(entry):
    return entry.get("evidence") == "CPIC retired" or entry.get("guideline", {}).get("status") == "retired"


def session_plan(ctx, args):
    limit = ops._int(args, "max_items", 8, 1, 25)
    policy = ops.policy_for(ctx)
    areas = [{"key": key, "title": title, "items": [], "more": 0} for key, title in _AREAS]
    by_key = {a["key"]: a for a in areas}
    by_key["gene_drug"]["ask_current_medications"] = True
    by_key["traits"]["invite"] = "Ask the user to confirm or refute each one."
    result = {"areas": areas, "limits": [ops.CHIP_LIMITS, ops.LIMITS, "Information with limits, not medical advice."]}
    with ops._Store() as store:
        vault, samples = store.vault, store.samples
        result = {**ops._callset_warning(vault), **result}
        report_rows = ops._snapshot_rows(vault, "1=1", limit=ops._count(vault, "SELECT count(*) FROM annotation_snapshot"))
        # Match _marker_category's sensitive-first, newest-id precedence in one per-call snapshot.
        marker_categories = {}
        for row in sorted(report_rows, key=lambda r: (r["category"] in SENSITIVE, r["id"])):
            if row["marker"]:
                marker_categories[row["marker"].lower()] = row["category"]
        withheld = 0
        discovery = ops._need("health.fhir", "clinical_series")(
            vault, allow=lambda cat: visible(policy, cat or Category.RECORDS, kind="observation"))
        withheld += discovery["withheld"]
        clinical = []
        for series in sorted(discovery["clinical_series"], key=lambda s: s["last"] or "", reverse=True):
            values = ops.lab_series(vault, series["query"], policy, include_other_units=True)
            # Unplotted points include other numeric units on this path, plus text results.
            points = values.get("points", []) + values.get("unplotted_points", [])
            if points:
                latest = max(points, key=lambda p: (p["date"], p["observation_id"]))
                clinical.append({**series, **_point(latest)})
        metrics = ops._need("health.trends", "metrics")(samples) if samples is not None else []
        if allowed(policy, Category.WELLNESS):
            signals = ops._need("health.trends", "daily_check")(samples)["signals"] if samples is not None else []
            metrics = [{**m, "daily_check": [s for s in signals if s["metric"] == m["metric"]]} for m in metrics]
        else:
            withheld += len(metrics)
            metrics = []
        by_key["own_numbers"]["items"] = clinical + metrics
        clinical_more = discovery.get("clinical_series_truncated", 0)

        links = json.loads((Path(__file__).parent / "knowledge/data/phenotype_links.json").read_text(encoding="utf-8"))

        def your_data(genes):
            codes = {code for gene in genes for code in links.get(gene.upper(), [])}
            return ([p for p in clinical if p["code"] in codes and p["code_system"] in (None, "", "http://loinc.org")]
                    + [m for m in metrics if m["metric"] in codes])

        # Check each note once, independently of its stored category and of its eventual parent.
        notes = []
        for row in vault.execute("SELECT * FROM phenotype_note ORDER BY created_at DESC,id DESC"):
            if ops._phenotype_visible(vault, policy, row, categories=marker_categories):
                notes.append(row)
            else:
                withheld += 1

        def phenotype(markers):
            names = ops._phenotype_names(vault, *markers)
            return [{"id": n["id"], "stance": n["stance"], "trait": n["trait"], "text": n["text"], "date": n["created_at"]}
                    for n in notes if n["marker"] and n["marker"].lower() in names]

        medications = []
        for row in vault.execute("SELECT * FROM medication_event ORDER BY occurred_on DESC,id DESC"):
            if ops.medication_visible(policy, row["category"]):
                medications.append({"drug": row["drug"], "brand": row["brand"], "event": row["event"],
                                    "dose": row["dose"], "date": row["occurred_on"]})
            else:
                withheld += 1

        pack = ops._pack()
        pack_markers = {m["rsid"].lower() for e in pack.entries for m in e["markers"]}
        cpic_genes = {e["gene"] for e in pack.cpic}
        cpic_markers = {m["rsid"].lower() for e in pack.cpic if not _retired(e) for m in e["markers"]}
        research_markers = {m["rsid"].lower() for e in pack.entries if e in pack.glp1 or _retired(e)
                            for m in e["markers"]}
        consent = {r["key"].partition(":")[2]: json.loads(r["value_json"]).get("answer")
                   for r in vault.execute("SELECT key,value_json FROM settings WHERE key LIKE 'consent:%'")}
        groups = {cat: [] for cat in SENSITIVE}
        hidden_groups = {cat: 0 for cat in SENSITIVE}

        def markers_visible(markers):
            return all(ops._marker_visible(vault, policy, m, categories=marker_categories) for marker in markers
                       for m in ops._phenotype_markers(vault, marker))

        def sensitive_category(category, genes, markers):
            categories = [category, categorize(genes=genes),
                          *(ops._marker_category(vault, m, categories=marker_categories) for marker in markers
                            for m in ops._phenotype_markers(vault, marker))]
            return next((c for c in categories if c in SENSITIVE), category)

        def evidence(marker, magnitude):
            names = ops._phenotype_markers(vault, marker)
            # An ambiguous alias is not evidence that any particular target was observed.
            resolved = set(names if len(names) <= 2 else (marker.lower(),))
            return ("guideline" if resolved & cpic_markers else
                    "research" if resolved & research_markers or (magnitude or 0) >= 3 else "weak")

        def add_finding(item, derived=None):
            nonlocal withheld
            markers = [item["marker"], *(derived.get("basis", {}) if derived else ())]
            category = sensitive_category(item["category"], item["genes"], markers)
            if (not allowed(policy, item["category"]) or not allowed(policy, category) or not markers_visible(markers)
                    or (derived is not None and not ops._derived_visible(vault, policy, derived, categories=marker_categories))):
                withheld += 1
                if category in SENSITIVE:
                    hidden_groups[category] += 1
                return
            # Pack profiles own gene–drug findings; report rows do not repeat them elsewhere.
            if category not in SENSITIVE and (item["marker"].lower() in pack_markers
                                              or derived is not None and item["marker"] in cpic_genes):
                return
            item = {key: item[key] for key in ("marker", "genes", "summary", "magnitude", "repute")}
            item.update(evidence=evidence(item["marker"], item["magnitude"]), chip_reliability=_reliability(item, derived),
                        your_data=your_data(item["genes"]), phenotype_notes=phenotype([*markers, *item["genes"]]))
            if category in SENSITIVE:
                groups[category].append(item)
            else:
                by_key["traits" if category == Category.TRAIT else "actionable"]["items"].append(item)

        report_markers = {row["marker"].lower() for row in report_rows if row["marker"]}
        for row in report_rows:
            add_finding(ops._snapshot_dict(row))
        complete = any(c["complete"] for c in ops._need("genome.calls", "callsets")(vault))
        calls = ops._pack_calls(vault)[1] if complete else {}
        derived = ops._need("knowledge.pack", "derive")(pack, calls) if complete else []
        derived_by_gene = {d["name"]: d for d in derived}
        for row in derived:
            # Non-sensitive derived PGX results are represented by their pack profile entries below.
            if row["name"] in cpic_genes:
                continue
            add_finding({"marker": row["name"], "genes": [row["name"]], "summary": row.get("value") or row.get("reason"),
                         "magnitude": None, "repute": "neutral", "category": row["category"]}, row)
        for entry in pack.entries:
            markers = [m["rsid"] for m in entry["markers"]]
            if not complete and not (set(markers) & report_markers):
                continue
            assessed = ops._need("knowledge.pack", "_entry_result")(entry, calls)
            row = derived_by_gene.get(entry["gene"])
            category = sensitive_category(Category.PGX, [entry["gene"]], markers)
            if category in SENSITIVE:
                add_finding({"marker": entry["gene"], "genes": [entry["gene"]],
                             "summary": assessed["finding"], "magnitude": None, "repute": "neutral",
                             "category": assessed["category"]}, {**(row or {}), "basis": dict.fromkeys(markers),
                                                     "confidence": row["confidence"] if row else "not_assessed",
                                                     "category": assessed["category"]})
                continue
            if (not ops._pgx_entry_visible(vault, policy, entry, categories=marker_categories) or not markers_visible(markers)
                    or row is not None and not ops._derived_visible(vault, policy, row, categories=marker_categories)):
                withheld += 1
                continue
            # Use the same status calculation as pgx_check, once per entry (not once per drug).
            status = assessed["status"]
            item = {"gene": entry["gene"], "drugs": entry["drugs"], "status": status,
                    "evidence": "CPIC retired" if _retired(entry) else "CPIC guideline" if entry in pack.cpic else "research",
                    "on_record": [m for m in medications if any(ops.same_drug(d, m["drug"]) or
                                  m["brand"] and ops.same_drug(d, m["brand"]) for d in entry["drugs"])],
                    "phenotype_notes": phenotype([entry["gene"], *markers])}
            if _retired(entry):
                item["limits"] = entry["limits"]
            if row is not None:
                item["chip_reliability"] = _reliability({}, row)
            by_key["gene_drug"]["items"].append(item)
        rank = {"guideline": 0, "research": 1, "weak": 2}
        order = lambda i: (rank[i["evidence"]], -(i["magnitude"] or 0))
        by_key["actionable"]["items"].sort(key=order)
        by_key["traits"]["items"].sort(key=lambda i: -(i["magnitude"] or 0))
        by_key["gene_drug"]["items"].sort(key=lambda i: {"flag": 0, "no_flag": 1, "not_assessed": 2}[i["status"]])
        for category in ("alzheimers", "mental_health", "reproductive", "carrier", "cancer_risk"):
            shown, hidden = groups[category], hidden_groups[category]
            if not shown and not hidden:
                continue
            answer = consent.get(category)
            item = {"category": category, "consent": answer, "ask_first": answer is None and allowed(policy, category)}
            if shown:
                item["findings"] = len(shown)
                if answer == "yes":
                    item.update(items=sorted(shown, key=order)[:limit], more=max(0, len(shown) - limit))
            if hidden:
                item["withheld"] = hidden
            key = "carrier_and_screening" if category in ("carrier", "cancer_risk") else "sensitive"
            by_key[key]["items"].append(item)
        for area in areas:
            area["more"] = max(0, len(area["items"]) - limit)
            area["items"] = area["items"][:limit]
        by_key["own_numbers"]["more"] += clinical_more
        if withheld:
            result["withheld"] = withheld
    return result
