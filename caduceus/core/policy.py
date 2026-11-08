"""Privacy level and categories: decides what the tools may show. Anything unknown fails closed.

Levels in v0.1: ``open`` (default: everything shows unless the user hid it) and ``standard`` (the
right-not-to-know categories stay hidden until the user unlocks them). Hidden categories apply at
every level. ``private`` and ``local`` arrive with encryption and local-model routing in v0.4.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Iterable


class Category(StrEnum):
    GENERAL = "general"
    PGX = "pgx"                    # drug response
    TRAIT = "trait"
    WELLNESS = "wellness"          # Apple Health trends
    RECORDS = "records"            # labs, conditions
    MEDICATION = "medication"
    RESEARCH = "research"
    ALZHEIMERS = "alzheimers"      # sensitive from here down
    CANCER_RISK = "cancer_risk"
    CARRIER = "carrier"
    MENTAL_HEALTH = "mental_health"
    REPRODUCTIVE = "reproductive"


SENSITIVE = frozenset({
    Category.ALZHEIMERS, Category.CANCER_RISK, Category.CARRIER,
    Category.MENTAL_HEALTH, Category.REPRODUCTIVE,
})
LEVELS = ("open", "standard")
DEFAULTS = {"privacy_level": "open", "hidden_categories": [], "unlocked_categories": []}


@dataclass(frozen=True)
class Policy:
    level: str = "open"
    hidden: frozenset = frozenset()
    unlocked: frozenset = frozenset()
    errors: tuple = ()             # settings that were invalid and were replaced by stricter values

    @classmethod
    def from_settings(cls, get: Callable[[str, object], object]) -> "Policy":
        errors: list[str] = []
        level = get("privacy_level", DEFAULTS["privacy_level"])
        if level in (None, ""):
            level = DEFAULTS["privacy_level"]
        if level not in LEVELS:
            errors.append(f"privacy_level {level!r} is not one of {', '.join(LEVELS)}; using standard")
            level = "standard"
        hidden, bad_hidden, malformed = _categories(get("hidden_categories", []))
        if malformed:
            errors.append("hidden_categories is not a list; using standard until it is fixed")
            level = "standard"
        unlocked, bad_unlocked, malformed_u = _categories(get("unlocked_categories", []))
        if malformed_u:
            errors.append("unlocked_categories is not a list; nothing is unlocked")
            unlocked = frozenset()
        errors += [f"unknown hidden category {name!r}" for name in bad_hidden]
        errors += [f"unknown unlocked category {name!r}" for name in bad_unlocked]
        return cls(level=level, hidden=hidden, unlocked=unlocked, errors=tuple(errors))

    def describe(self) -> dict:
        shown_sensitive = sorted(c.value for c in SENSITIVE if allowed(self, c))
        return {
            "privacy_level": self.level,
            "hidden_categories": sorted(c.value for c in self.hidden),
            "unlocked_categories": sorted(c.value for c in self.unlocked),
            "sensitive_categories_shown": shown_sensitive,
            "settings_errors": list(self.errors),
        }


def _categories(raw: object) -> tuple[frozenset, list[str], bool]:
    if raw is None:
        return frozenset(), [], False
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(), [], True
    good, bad = set(), []
    for item in raw:
        try:
            good.add(Category(str(item).strip().lower()))
        except ValueError:
            bad.append(str(item))
    return frozenset(good), bad, False


def allowed(policy: Policy, category: object) -> bool:
    """True when the tools may show an item of *category*. Unknown categories are refused."""
    try:
        cat = category if isinstance(category, Category) else Category(str(category))
    except ValueError:
        return False
    if cat in policy.hidden:
        return False
    if policy.level != "open" and cat in SENSITIVE and cat not in policy.unlocked:
        return False
    return True


_GENE_CATEGORY = {
    **dict.fromkeys(("APOE", "APP", "PSEN1", "PSEN2", "TREM2"), Category.ALZHEIMERS),
    **dict.fromkeys(
        ("BRCA1", "BRCA2", "MLH1", "MSH2", "MSH6", "PMS2", "EPCAM", "APC", "MUTYH", "TP53", "PALB2",
         "CHEK2", "ATM", "CDH1", "PTEN", "STK11", "BARD1", "BRIP1", "RAD51C", "RAD51D"),
        Category.CANCER_RISK,
    ),
}
# Gene symbols written inside text (a genoset title such as "APOE e4/e4" carries no gene list). Matched as
# whole, case-sensitive words. APC is left out: "APC resistance" is the Factor V Leiden phrase, not the gene.
_GENE_WORD = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted((g for g in _GENE_CATEGORY if g != "APC"), key=len, reverse=True))
    + r")(?![A-Za-z0-9])"
)
_TEXT_CATEGORY = (
    (re.compile(r"alzheimer|dementia|\bapo-?e(?:[234]|\b)", re.I), Category.ALZHEIMERS),
    (re.compile(r"\bbrca[12]?\b", re.I), Category.CANCER_RISK),
    (re.compile(r"\bcarriers?\b|carrier status", re.I), Category.CARRIER),
    (re.compile(r"(breast|ovarian|colorectal|prostate|pancrea\w*) cancer|lynch syndrome|hereditary cancer", re.I),
     Category.CANCER_RISK),
    # "ST depression" (an ECG finding) and other non-psychiatric uses of the word are not mental health.
    (re.compile(r"schizo(?:phreni|affective|id\b|typal)|bipolar|dysthymi|autis|adhd|attention[- ]deficit|anxiety"
                r"|(?<!\bst )(?<!\bst-)(?<!\bpr )(?<!segment )(?<!respiratory )(?<!marrow )(?<!\bcns )depress(?:i|ed mood)"
                r"|psychiatr|psychos[ie]s|psychotic|\bptsd\b|post-?traumatic|\bocd\b|obsessive[- ]compulsive"
                r"|anorexia nervosa|bulimi|eating disorder|binge[- ]eating|substance use|addiction|suicid|self-harm"
                r"|(?:alcohol|opioid|cannabis|cocaine|stimulant|substance|drug) (?:use disorder|dependence|abuse)"
                r"|panic disorder|\bphq-?9\b|\bgad-?7\b", re.I), Category.MENTAL_HEALTH),
    (re.compile(r"infertil|miscarriage|pregnan|endometriosis|\bpcos\b|polycystic ovar|erectile dysfunction"
                r"|contracepti|\bivf\b|in vitro fertili|ectopic|abortion|menstrua", re.I), Category.REPRODUCTIVE),
)


# Markers whose category is fixed, whatever text a record carries: APOE (e2/e3/e4), and the BRCA founder variants
# (SNPedia: rs80357906 = BRCA1 5382insC, 23andMe i4000378; rs80359550 = BRCA2 6174delT, i4000379; DisGeNET ties
# rs80357914 to BRCA1 185delAG). Listing a wrong one only hides that marker, so the list fails closed.
MARKER_CATEGORY = {
    **dict.fromkeys(("rs429358", "rs7412"), Category.ALZHEIMERS),
    **dict.fromkeys(("rs80357906", "i4000378", "rs80359550", "i4000379", "rs80357914"), Category.CANCER_RISK),
}
# The base category of each kind of record: hiding "medication" hides every medication, whatever else it is.
_KIND_BASE = {"observation": Category.RECORDS, "condition": Category.RECORDS, "medication": Category.MEDICATION,
              "research": Category.RESEARCH, "pgx": Category.PGX}


def visible(policy: Policy, category: object, *, kind: str | None = None) -> bool:
    """True when an item may be shown: its own category and the base category of its kind are both allowed."""
    base = _KIND_BASE.get(kind or "")
    return allowed(policy, category) and (base is None or allowed(policy, base))


def categorize(*, genes: Iterable[str] = (), text: str = "", markers: Iterable[str] = (),
               default: Category = Category.GENERAL) -> Category:
    """Pick a category for an annotation. A sensitive match always wins over the default."""
    for marker in markers or ():
        cat = MARKER_CATEGORY.get(str(marker or "").strip().lower())
        if cat is not None:
            return cat
    for gene in genes or ():
        cat = _GENE_CATEGORY.get(str(gene).strip().upper())
        if cat is not None:
            return cat
    if text:
        match = _GENE_WORD.search(text)
        if match:
            return _GENE_CATEGORY[match.group(1)]
        for pattern, cat in _TEXT_CATEGORY:
            if pattern.search(text):
                return cat
    return default


# Drugs prescribed mainly for a sensitive condition. Used only for the user's own medications: a gene–drug record
# about citalopram is drug-response information, but a citalopram prescription says something about its owner.
_MEDICATION_DRUGS = (
    (re.compile(r"\b(?:citalopram|escitalopram|fluoxetine|fluvoxamine|paroxetine|sertraline|vilazodone|vortioxetine"
                r"|venlafaxine|desvenlafaxine|duloxetine|levomilnacipran|amitriptyline|clomipramine|desipramine|doxepin"
                r"|imipramine|nortriptyline|protriptyline|trimipramine|bupropion|mirtazapine|trazodone|nefazodone"
                r"|phenelzine|tranylcypromine|esketamine|aripiprazole|brexpiprazole|cariprazine|clozapine|haloperidol"
                r"|lurasidone|olanzapine|paliperidone|quetiapine|risperidone|ziprasidone|asenapine|iloperidone"
                r"|lumateperone|lithium|atomoxetine|viloxazine|methylphenidate|dexmethylphenidate|lisdexamfetamine"
                r"|amphetamine|dextroamphetamine|buprenorphine|disulfiram|acamprosate|zoloft|lexapro|prozac|paxil"
                r"|celexa|effexor|cymbalta|wellbutrin|abilify|seroquel|zyprexa|risperdal|latuda|vraylar|rexulti"
                r"|adderall|vyvanse|ritalin|concerta|strattera|suboxone|trintellix)\b", re.I), Category.MENTAL_HEALTH),
    (re.compile(r"\b(?:levonorgestrel|norethindrone|norgestimate|norgestrel|desogestrel|drospirenone|etonogestrel"
                r"|norelgestromin|segesterone|medroxyprogesterone|ulipristal|mifepristone|misoprostol|clomiphene"
                r"|sildenafil|tadalafil|vardenafil|avanafil|plan b|nexplanon|mirena|kyleena|skyla|liletta"
                r"|depo-provera|nuvaring|annovera|xulane|yaz|yasmin|viagra|cialis|levitra|clomid)\b", re.I),
     Category.REPRODUCTIVE),
)


def medication_category(text: str) -> Category:
    """The category of one of the user's medications, from its name and any text the record gives."""
    category = categorize(text=text, default=Category.MEDICATION)
    if category in SENSITIVE:
        return category
    for pattern, cat in _MEDICATION_DRUGS:
        if pattern.search(text or ""):
            return cat
    return Category.MEDICATION
