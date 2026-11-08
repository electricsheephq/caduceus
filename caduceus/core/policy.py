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
_TEXT_CATEGORY = (
    (re.compile(r"alzheimer|dementia", re.I), Category.ALZHEIMERS),
    (re.compile(r"\bcarriers?\b|carrier status", re.I), Category.CARRIER),
    (re.compile(r"(breast|ovarian|colorectal|prostate|pancrea\w*) cancer|lynch syndrome|hereditary cancer", re.I),
     Category.CANCER_RISK),
    (re.compile(r"schizophreni|bipolar|depressi|autis|adhd|anxiety|psychiatr", re.I), Category.MENTAL_HEALTH),
    (re.compile(r"infertil|miscarriage|pregnan", re.I), Category.REPRODUCTIVE),
)


def categorize(*, genes: Iterable[str] = (), text: str = "", default: Category = Category.GENERAL) -> Category:
    """Pick a category for an annotation. A sensitive match always wins over the default."""
    for gene in genes or ():
        cat = _GENE_CATEGORY.get(str(gene).strip().upper())
        if cat is not None:
            return cat
    for pattern, cat in _TEXT_CATEGORY:
        if text and pattern.search(text):
            return cat
    return default
