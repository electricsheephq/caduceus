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
medication medications medicine medicines meds drug drugs dose doses dosage dosing pill pills prescription prescriptions
prescribed prescriber pharmacist pharmacy metformin insulin statin statins gene genes genetic genetics genome
genomic genomics genotype genotypes genotyped dna snp snps allele alleles 23andme ancestrydna myheritage
promethease pharmacogenomic pharmacogenomics pgx metabolizer metaboliser alzheimer alzheimers glucose a1c hba1c
cholesterol ldl hdl triglycerides diabetes diabetic prediabetes hrv bmi vo2max spo2 ecg ekg
""".split())
# Gene names, including spellings like APOE4, APOE-e4, CYP2C19 and GLP-1.
_GENE = re.compile(r"(?<![a-z0-9_])(?:apoe(?:-?e?[234])?|cyp\d[a-z0-9]*|glp-?1(?:r|-?ras?|s)?|gipr|hla-(?:[abc]\d*|d[a-z0-9]*)|vkorc1"
                   r"|slco1b1|tpmt|nudt15|dpyd|ugt1a1|g6pd|brca[12]|mthfr)(?![a-z0-9_])")
# Everyday words and phrases that count only when the same message has I, me or my ("how did I sleep", "should I
# stop taking it").
_WEAK = frozenset("""
health healthy weight sleep sleeping slept carrier variant variants lab labs heart blood doctor symptom symptoms
tablets injection injections
""".split())
_WEAK_PHRASES = re.compile(r"(?<![a-z0-9])(?:(?:stop|start|keep|quit|stopped|started|stopping|starting) taking"
                           r"|side effects?|wean(?:ing)? off|taper(?:ing)? off|how many steps"
                           r"|take (?:my |the |this |these |your |a |an |one )?tablets?)(?![a-z0-9])")
_SELF = frozenset({"i", "me", "my", "mine", "myself"})
# Code, ops and UI terms are removed before any matching ("time.sleep", "health check", "filter pills").
_TECH = re.compile("|".join((
    r"\b[a-z_]+\.sleep\b", r"\bhealth[ -]?(?:checks?|endpoints?|probes?)\b", r"\bhealthz\b",
    r"\b(?:code|cluster|pods?|services?|nodes?|servers?|db|database|repo|build|pipeline|app|deployment|cache"
    r"|queue|disk|instance|container)s? (?:(?:is|are|looks?|stays?) )?healthy\b",
    r"\b(?:brew|flutter|npm|expo|bun|pnpm|yarn|dart|react-native) doctor\b",
    r"\bheart (?:icons?|emojis?|buttons?|shapes?|symbols?|reactions?)\b",
    r"\bpill[ -](?:buttons?|badges?|shapes?|tabs?)\b", r"\b(?:filter|tag|status|nav|category|toggle|ui) pills?\b",
    r"\b(?:enum|type|union|component|button|style|colou?r|size|product|design|ui|css|theme) variants?\b",
    r"\bvariants? (?:props?|types?|class(?:es)?|names?|api)\b",
    r"\b(?:sql|dependency|code|command|prompt|script|html|template|header|constructor|field|property|fault|process"
    r"|log|ldap|xpath|nosql|css|xml) injections?\b",
    r"\btablet (?:mode|layout|view|app|screens?|sizes?|breakpoints?|versions?|ui)\b",
    r"\b(?:home|test|dev|ml|ai|cloud|qa|staging|computer|research) ?labs?\b",
    r"\bsteps to reproduce\b", r"\brepro steps\b", r"\bsteps? (?:per|each|every) (?:epoch|batch|iteration|job|stage)\b",
    r"\b(?:training|warmup|warm-up|build|ci|pipeline|deploy|deployment|setup|install|migration|next) steps\b",
    r"\bweights? (?:decay|init(?:ialization)?|matri(?:x|ces)|files?|tying|sharing)\b",
)))
# "side effects" of code ("does my function have side effects?"): only those two words are removed, so a drug
# named in between still counts.
_CODE_SIDE_EFFECTS = re.compile(r"\b((?:functions?|methods?|hooks?|code|components?|reducers?|getters?|quer(?:y|ies)"
                                r"|mutations?|procedures?|calls?|pure)\b(?: \w+){0,3} )side effects?\b")
_PHRASES = ("apple health", "heart rate", "blood pressure", "blood sugar", "blood test", "blood tests", "blood work",
            "bloodwork", "lab work", "labwork", "lab result", "lab results", "step count", "my steps", "daily steps",
            "steps per day", "steps a day")
# Pack names that are also ordinary English words.
_COMMON = frozenset({"tabloid"})
# Dose-form, unit, supply and generic product words in recorded medication names: never a trigger on their own.
_FORM_WORDS = frozenset("""
oral orally tablet tablets capsule capsules extended release delayed injection injectable injector solution
suspension topical cream ointment patch spray prefilled syringe with daily weekly once twice test tests strip strips
blood children childrens adult adults infant infants kids junior extra strength maximum regular original daytime
nighttime relief formula supply supplies lancet lancets needle needles meter sensor sensors device pack hour hours
actuat unit units cold cough pain pressure monitor allergy sinus ultra ultra-fine plus mini liquid drops chewable
gummies gummy true
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
        try:  # an unreadable vault counts as empty until the cache expires
            conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
            try:
                has_data = bool(conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM source_artifact) OR EXISTS(SELECT 1 FROM search_doc)").fetchone()[0])
                try:
                    rows = conn.execute("SELECT drug, brand FROM medication_event").fetchall()
                except sqlite3.OperationalError:  # no medication table: the words still work
                    rows = []
                names = frozenset(word for row in rows for name in row if name for word in _name_words(str(name)))
            finally:
                conn.close()
        except sqlite3.Error:
            has_data, names = False, frozenset()
    with _lock:
        _state.update(at=now, path=str(path), has_data=has_data, names=names)
    return has_data, names


def _name_words(name: str) -> set[str]:
    """The drug word of a medication name, and of a bracketed brand: the first word that is not a dose form,
    a unit, a supply or a generic product word. "metFORMIN 500 MG Oral Tablet [Glucophage]" gives metformin and
    glucophage; "Children's Tylenol" gives tylenol; "Test Strips [OneTouch Ultra]" gives only onetouch."""
    lowered = name.lower()
    parts = re.findall(r"\[([^\]]*)\]", lowered) + [re.sub(r"\[[^\]]*\]", " ", lowered)]
    return {word for part in parts for word in _first_drug_word(part)}


def _first_drug_word(text: str) -> list[str]:
    """Strengths and quantities ("24 HR", "500 MG") are skipped, so "24 HR metformin ..." gives metformin."""
    for word in _TOKEN.findall(text):
        if len(word) >= 4 and word not in _FORM_WORDS and not any(ch.isdigit() for ch in word):
            return [word]
    return []


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
    plain = _TECH.sub(" ", _CODE_SIDE_EFFECTS.sub(r"\1", text.lower()))  # code and UI terms say nothing
    words = set(_TOKEN.findall(plain))
    single, phrases = _pack_names()
    if words & _STRONG or words & single or words & recorded:
        return True
    if _GENE.search(plain) or _RSID.search(plain) or phrases.search(plain):
        return True
    return bool(words & _SELF and (words & _WEAK or _WEAK_PHRASES.search(plain)))


def _reset() -> None:
    """Tests: forget the cached vault state and pack names."""
    global _pack
    with _lock:
        _state.update(at=0.0, path=None, has_data=False, names=frozenset())
    _pack = None
