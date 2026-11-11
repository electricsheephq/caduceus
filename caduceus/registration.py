"""Registration: four tools, ``/caduceus``, four skills, the vault-hint hook and two automation blueprints.

Import-light and I/O-free: nothing opens a database until a tool or command runs.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    ("caduceus-setup", "Set up Caduceus: import DNA, health and records files, then choose privacy and research watches."),
    ("caduceus-guide", "Answer health and DNA questions from the local Caduceus vault, with sources and honest limits."),
    ("caduceus-research", "Run a Caduceus research watch and write its digest."),
    ("caduceus-report-problem", "Caduceus: report a bug or problem"),
)


def register(ctx):
    if not callable(getattr(ctx, "register_tool", None)):
        return None
    from . import tools

    for spec in tools.TOOLS:
        ctx.register_tool(
            name=spec["name"], toolset="caduceus", schema=spec, handler=tools.make_handler(spec["name"], ctx),
            description=spec["description"], emoji="⚕️",
        )
    ctx.register_command(
        name="caduceus", handler=lambda raw: tools.command(ctx, raw),
        description="Caduceus health and DNA vault: status, privacy settings, imports, erase",
        args_hint="[status|privacy [open|standard]|hide|show|unlock|lock <category>|imports|demo|erase [confirm]|help]",
    )
    skill = getattr(ctx, "register_skill", None)
    if callable(skill):
        for name, description in SKILLS:
            skill(name, ROOT / "skills" / name / "SKILL.md", description)
    hook = getattr(ctx, "register_hook", None)
    if callable(hook):
        from . import hint

        try:
            hook("pre_llm_call", lambda **kwargs: hint.pre_llm_call(ctx, **kwargs))
        except Exception:  # a Hermes without this hook still gets the tools
            pass
    blueprint = getattr(ctx, "register_automation_blueprint", None)
    if callable(blueprint):
        from .blueprints import BLUEPRINTS

        for key, fields in BLUEPRINTS:
            blueprint(key, **fields)
    return True
