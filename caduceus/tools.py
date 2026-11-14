"""The four Caduceus tools and ``/caduceus``. Handlers return JSON strings and never raise.

Every result passes through the privacy policy. Long imports run in the background (core.jobs).
"""
from __future__ import annotations

import json
import logging

LOG = logging.getLogger("caduceus")

NEVER_READ = (
    "Never open or read the user's DNA, health or records files yourself: pass the file's absolute path to "
    "caduceus_capture op=import and query the vault with these tools."
)
LIMITS = "Information with limits, not medical advice."


def _obj(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STR = {"type": "string"}
STRS = {"type": "array", "items": {"type": "string"}}

TOOLS = (
    {
        "name": "caduceus_search",
        "description": (
            "Search the user's private Caduceus health vault on this machine: DNA annotations, research items, lab "
            "results, conditions, medications and journal notes. Returns cited snippets; fetch one record in full "
            'with ref. List recorded kinds with kinds=["medication"] and no query. ' + NEVER_READ
        ),
        "parameters": _obj({
            "query": {"type": "string", "description": "Words to find, e.g. 'simvastatin' or 'GLP1R weight'; omit to list kinds."},
            "kinds": {"type": "array", "items": {"type": "string", "enum": [
                "annotation", "assertion", "research", "observation", "condition", "medication", "journal",
                "finding", "pgx"]}, "description": "Only these record kinds; omit query to list them newest first."},
            "ref": {"type": "string", "description": "Fetch one record in full by the ref a search returned."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        }),
    },
    {
        "name": "caduceus_genome",
        "description": (
            "The user's own genotypes and what they mean, from their imported DNA raw data and variant report. "
            "op=lookup (rsids), gene (a gene symbol), pgx (a drug name: gene-drug evidence from CPIC and published "
            "research, with each marker called, absent or not on the chip), derived (APOE, CYP2C19 and other "
            "combined calls), summary (what is imported, top findings), filter (findings by magnitude, repute or "
            "topic), report (rebuild the variant report as a local HTML file). Consumer chips miss rare variants "
            "and can be wrong; results say so. For medication questions, op=pgx gives the user's own gene–drug evidence. "
            + LIMITS + " Never suggest starting, stopping or changing a medication or its dose; for any medication "
            "decision, say to talk to the prescriber. " + NEVER_READ
        ),
        "parameters": _obj({
            "op": {"type": "string", "enum": ["lookup", "gene", "pgx", "derived", "summary", "filter", "report"]},
            "rsids": STRS,
            "gene": STR,
            "drug": {"type": "string", "description": "Generic or brand name, e.g. 'semaglutide' or 'Ozempic'."},
            "topic": STR,
            "min_magnitude": {"type": "number"},
            # No enum: Hermes checks deferred calls against this schema, which would refuse "BAD" or " bad "
            # before the handler normalises them.
            "repute": {"type": "string", "description": "good, bad or neutral. Case does not matter; results use lowercase."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }, required=["op"]),
    },
    {
        "name": "caduceus_trend",
        "description": (
            "Deterministic numbers from the user's Apple Health samples and lab results. op=metrics (what exists), "
            "series (values bucketed by day, week or month), compare (before vs after a date or a medication's "
            "start), labs (a lab result over time, e.g. HbA1c), check (today's out-of-range signals). Every result "
            "has units, source, coverage gaps and the calculation version; quote numbers exactly. "
            "For a question about how a medication is working, also call caduceus_genome op=pgx for that drug. "
            "Leave out after_days to cover the data after the start, up to a year; before_days defaults to 28 days. "
            + LIMITS
            + " Never suggest starting, stopping or changing a medication or its dose; for any medication decision, "
            "say to talk to the prescriber."
        ),
        "parameters": _obj({
            "op": {"type": "string", "enum": ["metrics", "series", "compare", "labs", "check"]},
            "metric": {"type": "string", "description": "e.g. weight, lean_mass, protein, waist, steps, resting_heart_rate, sleep_hours."},
            "lab": {"type": "string", "description": "Lab name or LOINC code, e.g. 'HbA1c' or '4548-4'."},
            "start": {"type": "string", "description": "ISO date."},
            "end": {"type": "string", "description": "ISO date."},
            "bucket": {"type": "string", "enum": ["day", "week", "month"]},
            "pivot": {"type": "string", "description": "ISO date to compare around."},
            "drug": {"type": "string", "description": "Compare around this medication's start date instead."},
            "before_days": {"type": "integer", "minimum": 1, "maximum": 730},
            "after_days": {"type": "integer", "minimum": 1, "maximum": 730},
        }, required=["op"]),
    },
    {
        "name": "caduceus_capture",
        "description": (
            "Add to or manage the user's Caduceus vault. op=import copies a file into the vault and imports it in "
            "the background: 23andMe or AncestryDNA raw data (.zip or .txt), a Promethease report (.zip), an "
            "Apple Health export.zip (including its clinical records), or FHIR JSON; pass its absolute path. "
            "op=status shows import progress. op=medication records a medication start, dose change or stop: show "
            "the user the details and set confirm=true only after they agree. op=journal stores a note. "
            "op=watch_run runs the research watch now. op=settings shows the privacy settings. op=brief writes a "
            "local HTML health brief with charts. op=sources lists imported files. " + NEVER_READ
        ),
        "parameters": _obj({
            "op": {"type": "string", "enum": [
                "import", "status", "medication", "journal", "watch_run", "settings", "brief", "sources"]},
            "path": {"type": "string", "description": "Absolute path of the file to import."},
            "run_id": {"type": "integer"},
            "drug": STR,
            "brand": STR,
            "event": {"type": "string", "enum": ["start", "dose_change", "stop"]},
            "dose": STR,
            "date": {"type": "string", "description": "ISO date."},
            "text": STR,
            "topics": STRS,
            "confirm": {"type": "boolean"},
        }, required=["op"]),
    },
)


class ToolError(Exception):
    """A user-facing error: bad input, missing data, or a feature not imported yet."""


def make_handler(name: str, ctx):
    def handle(args, **_kw) -> str:
        try:
            from . import ops

            payload = ops.dispatch(ctx, name, args if isinstance(args, dict) else {})
        except ToolError as exc:
            payload = {"error": str(exc)}
        except Exception as exc:  # never crash the agent loop
            LOG.exception("caduceus tool %s failed", name)
            payload = {"error": f"internal error ({type(exc).__name__}); see the gateway log"}
        return json.dumps(payload, ensure_ascii=False, default=str)

    return handle


def command(ctx, raw) -> str:
    try:
        from . import ops

        return ops.command(ctx, str(raw or ""))
    except Exception as exc:
        LOG.exception("/caduceus failed")
        return f"Caduceus error: {type(exc).__name__}. See the gateway log."
