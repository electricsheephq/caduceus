"""Tool operations. Orchestrator-owned glue between the tools and the lane modules (filled in during M0)."""
from __future__ import annotations

from .core.policy import Policy, DEFAULTS
from .tools import ToolError


def policy_for(ctx) -> Policy:
    def get(key, default):
        try:
            value = ctx.get_config(key, None)
        except Exception:
            value = None
        return DEFAULTS.get(key, default) if value is None else value

    return Policy.from_settings(get)


def dispatch(ctx, tool: str, args: dict) -> dict:
    raise ToolError(f"{tool} is not ready yet in this build")


def command(ctx, raw: str) -> str:
    policy = policy_for(ctx)
    return f"Caduceus is installed. Privacy level: {policy.level}."
