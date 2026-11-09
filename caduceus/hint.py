"""The vault hint: a short note that tells the model to answer health questions from the vault.

Small models sometimes answer "Why isn't my Ozempic working?" from general knowledge without calling
any Caduceus tool, so the rules in the tool results never reach them. Hermes calls ``pre_llm_call``
once per user turn and appends returned context to that turn's user message, never to the system
prompt. Hermes stores it with the message (``api_content``) and replays it, so one note per chat is
enough: the hook looks for it in the history it is given. If the note drops out of the history (for
example after Hermes compacts a long chat), the next health turn adds it again.

The note is a constant and never carries a value from the vault. It is added only when:
- the ``vault_hint`` setting is on (the default);
- the vault holds data;
- the message is about health, medications or DNA;
- the history does not already hold it.

It never vetoes a turn, and any error means no note.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from .core import paths

HINT = (
    "[Caduceus] This user keeps a private health vault on this machine (DNA, Apple Health, health records). "
    "Before answering about their genes, medications, labs or health trends, call the caduceus_* tools (the "
    "caduceus:caduceus-guide skill explains them) and cite what they return; do not answer from general "
    "knowledge alone. Information with limits, not medical advice: never suggest starting, stopping, switching "
    "or changing a medication, its form or its dose; for any medication decision, say to talk to the prescriber."
)
# How an earlier note is recognised in the history: its opening words, which a user would not type.
NOTE_ID = HINT[:64]
# Words that mark a health, medication or DNA message on their own.
_STRONG = frozenset("""
medication medications medicine medicines meds drug drugs dose doses dosage dosing prescription prescriptions
prescribed prescriber pharmacist pharmacy metformin insulin statin statins gene genes genetic genetics genome
genomic genomics genotype genotypes genotyped dna snp snps allele alleles 23andme ancestrydna myheritage
promethease pharmacogenomic pharmacogenomics pgx metabolizer metaboliser alzheimer alzheimers glucose a1c hba1c
cholesterol ldl hdl triglycerides diabetes diabetic prediabetes hrv bmi vo2max spo2 ecg ekg
""".split())
# Gene names, including spellings like APOE4, APOE-e4, CYP2C19 and GLP-1.
_GENE = re.compile(r"(?<![a-z0-9])(?:apoe|cyp\d[a-z0-9]*|glp-?1|glp1r|gipr|hla-[a-z0-9]+|vkorc1|slco1b1|tpmt|nudt15"
                   r"|dpyd|ugt1a1|g6pd|brca[12]|mthfr)")
# Everyday words that count only when the same message has I, me or my ("how did I sleep"), and never inside a
# code or ops term (_TECH: "time.sleep", "health check").
_WEAK = frozenset("health weight sleep sleeping slept carrier labs heart blood doctor symptom symptoms pill pills"
                  .split())
_WEAK_PHRASES = re.compile(r"(?<![a-z0-9])(?:(?:stop|start|keep|quit|stopped|started|coming|come) (?:taking|off)"
                           r"|side effects?|wean(?:ing)? off|taper(?:ing)? off)(?![a-z0-9])")
_SELF = frozenset({"i", "me", "my", "mine", "myself"})
_TECH = re.compile(r"\b[a-z_]+\.sleep\b|\bhealth[ -]?(?:checks?|endpoints?|probes?)\b|\bhealthz\b"
                   r"|\b(?:brew|flutter|npm|expo|bun|pnpm|yarn|dart|react-native) doctor\b"
                   r"|\bheart (?:icons?|emojis?|buttons?|shapes?|symbols?|reactions?)\b|\bpill[ -](?:buttons?|badges?|shapes?|tabs?)\b")
_PHRASES = ("apple health", "heart rate", "blood pressure", "blood sugar", "blood test", "blood tests",
            "lab result", "lab results", "step count", "my steps", "daily steps", "steps per day", "steps a day",
            "how many steps")
# Pack names that are also ordinary English words.
_COMMON = frozenset({"tabloid"})
# Dose-form and other words in recorded medication names that say nothing about health on their own.
_FORM_WORDS = frozenset("""
oral orally tablet tablets capsule capsules extended release delayed injection injectable injector solution
suspension topical cream ointment patch spray prefilled syringe with daily weekly once twice
""".split())
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_RSID = re.compile(r"(?<![a-z0-9])rs\d{4,}(?![a-z0-9])")  # not RS232 or RS485
_SCAN = 20_000  # characters of a long message to read: its start and its end
_TTL = 60.0

_lock = threading.Lock()
_state: dict = {"at": 0.0, "path": None, "has_data": False, "names": frozenset()}
_pack: tuple[frozenset, re.Pattern] | None = None


def pre_llm_call(ctx, **kwargs):
    """``{"context": HINT}`` for a health turn when the chat's history lacks the note, else None."""
    try:
        if _setting(ctx) is False:
            return None
        text = _text(kwargs.get("user_message"))
        if not text.strip() or _hinted(kwargs.get("conversation_history")):
            return None
        has_data, recorded = _vault_state()
        if not has_data or not _about_health(text, recorded):
            return None
        return {"context": HINT}
    except Exception:
        return None


def _setting(ctx):
    try:
        value = ctx.get_config("vault_hint", None)
    except Exception:
        value = None
    return True if value is None else value


def _text(message) -> str:
    text = _flat(message)  # multimodal turns: only the text parts
    if len(text) > _SCAN:  # a pasted file: the question is usually at one end
        text = text[:_SCAN // 2] + " " + text[-_SCAN // 2:]
    return text


def _hinted(history) -> bool:
    """Whether an earlier user message carries the note. Hermes stores it in that message's ``api_content``
    and restores it with the chat, so this holds across restarts."""
    for message in history or ():
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        for key in ("api_content", "content"):
            if NOTE_ID in _flat(message.get(key)):
                return True
    return False


def _flat(value) -> str:
    """A message's text: the string itself, or the text parts of a multimodal list."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part.get("text") or "") for part in value
                        if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _vault_state() -> tuple[bool, frozenset]:
    """Whether the vault holds data, and the words naming the user's recorded medications (cached)."""
    path = paths.vault_path()
    now = time.monotonic()
    with _lock:
        if _state["path"] == str(path) and now - _state["at"] < _TTL:
            return _state["has_data"], _state["names"]
    has_data, names = False, frozenset()
    if Path(path).is_file():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            has_data = bool(conn.execute(
                "SELECT EXISTS(SELECT 1 FROM source_artifact) OR EXISTS(SELECT 1 FROM search_doc)").fetchone()[0])
            rows = conn.execute("SELECT drug, brand FROM medication_event").fetchall()
            names = frozenset(word for row in rows for name in row if name for word in _name_words(str(name)))
        finally:
            conn.close()
    with _lock:
        _state.update(at=now, path=str(path), has_data=has_data, names=names)
    return has_data, names


def _name_words(name: str) -> set[str]:
    """The drug words of a medication name: its first word, and the first word of a bracketed brand.
    "metFORMIN 500 MG Oral Tablet [Glucophage]" gives metformin and glucophage; "glucose blood VI test
    strips (Accu-Chek Guide)" gives only glucose, so "test" never becomes a trigger."""
    lowered = name.lower()
    firsts = [_TOKEN.findall(part)[:1] for part in re.findall(r"\[([^\]]*)\]", lowered)]
    firsts.append(_TOKEN.findall(re.sub(r"\[[^\]]*\]", " ", lowered))[:1])
    return {w for first in firsts for w in first if len(w) >= 4 and not w.isdigit() and w not in _FORM_WORDS}


def _pack_names() -> tuple[frozenset, re.Pattern]:
    """Drug and brand names from the knowledge pack (single words), and one pattern for every phrase."""
    global _pack
    if _pack is None:
        names: set[str] = set()
        try:
            table = json.loads((Path(__file__).parent / "knowledge" / "data" / "drugs.json").read_text(encoding="utf-8"))
            names = {str(name).lower() for pair in table.items() for name in pair}
        except Exception:  # no pack: the word lists still work
            pass
        phrases = set(_PHRASES) | {n for n in names if " " in n}
        pattern = re.compile(r"(?<![a-z0-9])(?:" + "|".join(
            re.escape(p) for p in sorted(phrases, key=len, reverse=True)) + r")(?![a-z0-9])")
        _pack = (frozenset(n for n in names if " " not in n and len(n) >= 4) - _COMMON, pattern)
    return _pack


def _about_health(text: str, recorded: frozenset) -> bool:
    lowered = text.lower()
    words = set(_TOKEN.findall(lowered))
    single, phrases = _pack_names()
    if words & _STRONG or words & single or words & recorded:
        return True
    if _GENE.search(lowered) or _RSID.search(lowered) or phrases.search(lowered):
        return True
    plain = _TECH.sub(" ", lowered)
    everyday = set(_TOKEN.findall(plain))
    return bool(everyday & _SELF and (everyday & _WEAK or _WEAK_PHRASES.search(plain)))


def _reset() -> None:
    """Tests: forget the cached vault state and pack names."""
    global _pack
    with _lock:
        _state.update(at=0.0, path=None, has_data=False, names=frozenset())
    _pack = None
