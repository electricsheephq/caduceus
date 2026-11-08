"""Versioned public evidence, deliberately limited chip calls, and local derivations."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from itertools import combinations_with_replacement
from pathlib import Path

from ..core.contracts import CallState
from ..core.policy import Category, categorize

PACK_VERSION = "2026.11.0"
DATA = Path(__file__).with_name("data")
DISCLAIMER = (
    "Information with limits, not a diagnosis or a dosing recommendation. Chip data do not replace "
    "clinical pharmacogenetic testing. Do not start, stop, switch or dose a drug from this result; "
    "talk to your prescriber."
)


@dataclass(frozen=True)
class Pack:
    version: str
    drugs: dict
    cpic: list
    glp1: list
    watch: dict

    @property
    def entries(self):
        return self.cpic + self.glp1


def load_pack() -> Pack:
    def read(name):
        return json.loads((DATA / name).read_text(encoding="utf-8"))

    cpic, glp1 = read("cpic.json"), read("glp1.json")
    pack = Pack(PACK_VERSION, read("drugs.json"), cpic["entries"], glp1["entries"], cpic["watch"])
    seen = set()
    for entry in pack.entries:
        for field in ("id", "gene", "drugs", "markers", "allele_name", "function", "phenotype_rules",
                      "finding", "evidence", "sources", "limits", "chip_reliability"):
            if field not in entry:
                raise ValueError(f"missing {field} in pack entry")
        if entry["id"] in seen or not entry["sources"] or not entry["limits"]:
            raise ValueError("pack entries need unique ids, sources and limits")
        seen.add(entry["id"])
        if entry["chip_reliability"] not in {"reliable", "limited", "unreliable"}:
            raise ValueError("unknown chip reliability")
        if entry["evidence"] not in {"CPIC A", "CPIC B", "CPIC retired", "research"}:
            raise ValueError("unknown evidence level")
        for source in entry["sources"]:
            if not source.get("title") or not source.get("url"):
                raise ValueError("source needs title and URL")
        for marker in entry["markers"]:
            if (not marker["rsid"].startswith("rs") or marker["strand"] != "+"
                    or marker["effect"] == marker["other"]
                    or set(marker["effect"] + marker["other"]) - set("ACGT")):
                raise ValueError("invalid plus-strand marker")
    return pack


def normalize_drug(name) -> str | None:
    key = " ".join(str(name or "").strip().casefold().split())
    aliases = json.loads((DATA / "drugs.json").read_text(encoding="utf-8"))
    if key in aliases:
        return aliases[key]
    key = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|μg|ug|ml|g|units?|iu)\b", " ", key)
    ignored = {"mg", "mcg", "μg", "ug", "ml", "g", "unit", "units", "iu", "weekly", "daily",
               "once", "twice", "tablet", "tablets", "capsule", "capsules", "injection", "oral"}
    words = [w for w in re.findall(r"5-fu|[a-z]+(?:-[a-z0-9]+)*", key) if w not in ignored]
    candidates = words + [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    matches = {aliases[c] for c in candidates if c in aliases}
    return matches.pop() if len(matches) == 1 else None


def _genotype(calls, marker, effect, other, *, haploid=False):
    call = calls.get(marker)
    state = call.get("state") if isinstance(call, dict) else getattr(call, "state", None)
    if state != CallState.CALLED:
        return None, f"{marker}: {state or 'not_on_chip'}"
    value = call.get("genotype") if isinstance(call, dict) else call.genotype
    value = str(value or "").upper()
    if len(value) not in ((1, 2) if haploid else (2,)) or set(value) - {effect, other}:
        return None, f"{marker}: unsupported allele or ploidy"
    return value, None


def pgx_check(pack, drug, calls) -> dict:
    normalized = normalize_drug(drug)
    entries = []
    for entry in pack.entries:
        if normalized is None or normalized not in entry["drugs"]:
            continue
        result = copy.deepcopy(entry)
        result["category"] = categorize(genes=[entry["gene"]], default=Category.PGX).value
        result["disclaimer"] = DISCLAIMER
        counts, reasons = {}, []
        if entry["chip_reliability"] == "unreliable":
            reasons.append(entry.get("not_assessed_reason", "not assessed: needs a dedicated test"))
        for marker in entry["markers"]:
            genotype, reason = _genotype(calls, marker["rsid"], marker["effect"], marker["other"],
                                         haploid=entry["gene"] == "MT-RNR1")
            if reason:
                reasons.append(reason)
            else:
                counts[marker["rsid"]] = genotype.count(marker["effect"])
        if not entry["markers"] and not reasons:
            reasons.append("no validated chip marker in the pack")
        result["status"] = "not_assessed" if reasons else ("flag" if any(counts.values()) else "no_flag")
        result["effect_counts"] = counts
        result["reason"] = "; ".join(reasons) or None
        result["finding"] = ("Not assessed. " + result["reason"] + "." if reasons else
                             entry["finding"] if result["status"] == "flag" else
                             "The tested marker(s) lack the listed effect allele(s). This does not establish normal "
                             "response or absence of other risks.")
        entries.append(result)
    return {"drug": normalized, "entries": entries, "pack_version": pack.version,
            "note": None if entries else "No evidence in the pack for this drug.", "disclaimer": DISCLAIMER}


def _basis(calls, markers):
    basis, reasons = {}, []
    for rsid, effect, other in markers:
        genotype, reason = _genotype(calls, rsid, effect, other)
        if reason:
            reasons.append(reason)
        else:
            basis[rsid] = genotype
    return basis, reasons


def _haplotypes(basis, markers, haplotypes):
    """All unordered diplotypes compatible with unphased two-marker (or three-marker) data."""
    possible = []
    for first, second in combinations_with_replacement(haplotypes, 2):
        if all(sorted(basis[rsid]) == sorted((haplotypes[first][i], haplotypes[second][i]))
               for i, (rsid, _, _) in enumerate(markers)):
            possible.append((first, second))
    return possible


def derive(pack, calls) -> list[dict]:
    definitions = [
        ("APOE", [("rs429358", "C", "T"), ("rs7412", "T", "C")]),
        ("CYP2C19", [("rs4244285", "A", "G"), ("rs4986893", "A", "G"), ("rs12248560", "T", "C")]),
        ("CYP2C9", [("rs1799853", "T", "C"), ("rs1057910", "C", "A")]),
        ("SLCO1B1", [("rs4149056", "C", "T")]),
        ("VKORC1", [("rs9923231", "T", "C")]),
    ]
    results = []
    for name, markers in definitions:
        basis, reasons = _basis(calls, markers)
        row = {"name": name, "value": None, "confidence": "not_assessed", "basis": basis,
               "category": categorize(genes=[name], default=Category.PGX).value,
               "pack_version": pack.version, "disclaimer": DISCLAIMER,
               "limits": "Limited marker panel; rare alleles, structural variation and phase are not assessed."}
        row["sources"] = [e["sources"][0] for e in pack.entries if e["gene"] == name][:1]
        if name == "APOE":
            row["sources"] = [{"title": "dbSNP two-marker APOE inference",
                               "url": "https://www.ncbi.nlm.nih.gov/snp/rs429358"}]
        row["chip_reliability"] = "limited"
        if reasons:
            row["reason"] = "; ".join(reasons)
        elif name == "APOE":
            haplotypes = {"ε1": "CT", "ε2": "TT", "ε3": "TC", "ε4": "CC"}
            possible = _haplotypes(basis, markers, haplotypes)
            row["possibilities"] = ["/".join(p) for p in possible]
            row["confidence"] = "called" if len(possible) == 1 else "ambiguous"
            row["value"] = row["possibilities"][0] if len(possible) == 1 else " or ".join(row["possibilities"])
            row["limits"] = "Unphased two-marker inference, not a diagnosis. ε3/ε3 does not exclude Alzheimer's risk."
        elif name in {"CYP2C19", "CYP2C9"}:
            # '+' describes markers in cis, not an independently validated star-allele name.
            haplotypes = ({"*1": "GGC", "*2": "AGC", "*3": "GAC", "*17": "GGT",
                           "*2+*17": "AGT", "*3+*17": "GAT", "*2+*3": "AAC", "*2+*3+*17": "AAT"}
                          if name == "CYP2C19" else {"*1": "CA", "*2": "TA", "*3": "CC", "*2+*3": "TC"})
            possible = _haplotypes(basis, markers, haplotypes)
            row["possibilities"] = ["/".join(p) for p in possible]
            row["value"] = " or ".join(row["possibilities"]) or "unresolved haplotype"
            phenotypes, scores = set(), set()
            for pair in possible:
                if name == "CYP2C19":
                    inactive = sum("*2" in a or "*3" in a for a in pair)
                    phenotypes.add("PM" if inactive == 2 else "IM" if inactive else
                                   "UM" if pair.count("*17") == 2 else "RM" if "*17" in pair else "NM")
                else:
                    score = sum(0 if "*3" in a else 0.5 if "*2" in a else 1 for a in pair)
                    scores.add(score)
                    phenotypes.add("NM" if score == 2 else "IM" if score >= 1 else "PM")
            row["phenotypes"] = sorted(phenotypes)
            row["confidence"] = "called" if len(phenotypes) == 1 else "ambiguous"
            if len(phenotypes) == 1:
                row["phenotype"] = next(iter(phenotypes))
            if len(scores) == 1:
                row["activity_score"] = next(iter(scores))
            elif scores:
                row["activity_scores"] = sorted(scores)
            if len(possible) > 1:
                row["reason"] = "Phase unresolved; compatible diplotypes " + (
                    "agree on the phenotype." if len(phenotypes) == 1 else "have different phenotypes.")
                row["limits"] += " '+' labels a cis marker combination, not a validated star-allele assignment."
        else:
            count = basis[markers[0][0]].count(markers[0][1])
            row["confidence"] = "called"
            row["effect_count"] = count
            if name == "VKORC1":
                row["value"] = f"{count} copies of −1639A (plus-strand T)"
            else:
                row["value"] = ("normal", "decreased", "poor")[count] + " function marker pattern"
        results.append(row)
    return results
