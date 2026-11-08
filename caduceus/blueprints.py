"""Automation blueprints (Hermes cron agent jobs). A run that finds nothing replies [SILENT]."""
from __future__ import annotations

_TIME = lambda default: {  # noqa: E731
    "name": "time", "type": "time", "label": "What time?", "default": default, "help": "24h local time, e.g. 09:00",
}
_DELIVER = {
    "name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
    "options": ["origin", "local", "telegram", "discord", "email"], "strict": False,
    "help": "origin = the chat you set this up from; local = save only, no message; or any connected platform",
}

BLUEPRINTS = (
    ("research-watch", {
        "title": "Research watch for your genes",
        "description": (
            "A digest of new studies and ClinVar changes about your genes, medications and chosen topics, from "
            "Europe PMC and NCBI. Only gene, drug and topic terms leave this machine, never genotypes."
        ),
        "category": "health",
        "schedule_template": "{minute} {hour} * * {dow}",
        "prompt_template": (
            "Run the Caduceus research watch: call caduceus_capture with op=watch_run. If it reports no new "
            "items, reply with exactly [SILENT]. Otherwise follow the caduceus:caduceus-research skill: for each "
            "new item say in plain words what was found and how it relates to the user's own genotypes or "
            "medications (from the tool result, never guessed), cite title, journal, year and PMID or DOI, and "
            "state the limits. End with: information, not medical advice; talk to your clinician before "
            "changing anything."
        ),
        "slots": [
            _TIME("09:00"),
            {"name": "recurrence", "type": "weekdays", "label": "Repeat on", "default": "monday",
             "options": ["everyday", "weekdays", "weekends"]},
            _DELIVER,
        ],
        "skills": ["caduceus:caduceus-research"],
        "tags": ["health", "research", "genetics"],
    }),
    ("daily-check", {
        "title": "Daily health check",
        "description": (
            "Once a day, checks your Apple Health trends for out-of-range signals (resting heart rate, sleep, "
            "weight change) and messages you only when something trips. Costs one model call a day."
        ),
        "category": "health",
        "schedule_template": "{minute} {hour} * * *",
        "prompt_template": (
            "Call caduceus_trend with op=check. If it returns no signals, reply with exactly [SILENT]. Otherwise "
            "list each signal in one plain sentence with its numbers, units and dates exactly as the tool gave "
            "them, add one line on what could explain it, and end with: information, not medical advice."
        ),
        "slots": [_TIME("08:30"), _DELIVER],
        "skills": ["caduceus:caduceus-guide"],
        "tags": ["health", "daily"],
    }),
)
