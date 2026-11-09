"""The vault hint: a short note that tells the model to answer health questions from the vault.

Small models sometimes answer "Why isn't my Ozempic working?" from general knowledge without calling
any Caduceus tool, so the rules in the tool results never reach them. Hermes calls ``pre_llm_call``
once per user turn and appends returned context to that turn's user message, never to the system
prompt. Hermes keeps it with the message, so one note per session is enough.

The note is a constant and never carries a value from the vault. It is added only when:
- the ``vault_hint`` setting is on (the default);
- the vault holds data;
- the message is about health, medications or DNA;
- this session has not had it yet.

It never vetoes a turn, and any error means no note.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
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
medication medications medicine medicines meds drug drugs dose doses dosage dosing pill pills prescription
prescriptions prescribed prescriber pharmacist pharmacy metformin insulin statin statins gene genes genetic
genetics genome genomic genomics genotype genotypes genotyped dna snp snps allele alleles 23andme ancestrydna
myheritage promethease pharmacogenomic pharmacogenomics pgx apoe alzheimer alzheimers glucose a1c hba1c
cholesterol ldl hdl triglycerides diabetes diabetic prediabetes hrv bmi vo2max spo2 ecg ekg caduceus
""".split())
# Everyday words that count only when the same message has I, me or my ("how did I sleep"), and never inside a
# code or ops term (_TECH: "time.sleep", "health check").
_WEAK = frozenset("health weight sleep sleeping slept carrier labs heart blood doctor symptom symptoms".split())
_SELF = frozenset({"i", "me", "my", "mine", "myself"})
_TECH = re.compile(r"\b[a-z_]+\.sleep\b|\bhealth[ -]?(?:checks?|endpoints?|probes?)\b|\bhealthz\b")
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
_RSID = re.compile(r"(?<![a-z0-9])rs\d{3,}(?![a-z0-9])")
_SCAN = 20_000  # characters of a long message to read: its start and its end
_TTL = 60.0
_SEEN_MAX = 512

_lock = threading.Lock()
_seen: OrderedDict[str, None] = OrderedDict()
_state: dict = {"at": 0.0, "path": None, "has_data": False, "names": frozenset()}
_pack: tuple[frozenset, re.Pattern] | None = None


def pre_llm_call(ctx, **kwargs):
    """``{"context": HINT}`` for the first health turn of a session, else None."""
    try:
        if _setting(ctx) is False:
            return None
        text = _text(kwargs.get("user_message"))
        if not text.strip():
            return None
        session = str(kwargs.get("session_id") or "")
        if _hinted(session, kwargs.get("conversation_history")):
            return None
        has_data, recorded = _vault_state()
        if not has_data or not _about_health(text, recorded):
            return None
        _remember(session)
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


def _hinted(session: str, history) -> bool:
    """Whether this session already has the note: remembered here, or in an earlier user message
    (Hermes stores the note in the message's ``api_content``, so this survives a restart)."""
    with _lock:
        if session and session in _seen:
            return True
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


def _remember(session: str) -> None:
    if not session:
        return
    with _lock:
        _seen[session] = None
        while len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)


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
    """The drug words of a medication name: "metFORMIN 500 MG Oral Tablet [Glucophage]" gives metformin
    and glucophage (the words before the strength, and a bracketed brand)."""
    lowered = name.lower()
    words = set()
    for brand in re.findall(r"\[([^\]]*)\]", lowered):
        words.update(_TOKEN.findall(brand))
    head = re.split(r"\d", re.sub(r"\[[^\]]*\]", " ", lowered), maxsplit=1)[0]
    words.update(_TOKEN.findall(head))
    return {w for w in words if len(w) >= 4 and w not in _FORM_WORDS}


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
    if _RSID.search(lowered) or phrases.search(lowered):
        return True
    everyday = set(_TOKEN.findall(_TECH.sub(" ", lowered)))
    return bool(everyday & _WEAK and everyday & _SELF)


def _reset() -> None:
    """Tests: forget hinted sessions and the cached vault state."""
    global _pack
    with _lock:
        _seen.clear()
        _state.update(at=0.0, path=None, has_data=False, names=frozenset())
    _pack = None
