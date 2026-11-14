"""Registration matches the manifest and does no I/O."""
import re
from pathlib import Path

from conftest import FakeCtx, load_plugin_package

ROOT = Path(__file__).resolve().parents[1]


def manifest_list(key):
    line = next(l for l in (ROOT / "plugin.yaml").read_text().splitlines() if l.startswith(f"{key}:"))
    return set(re.findall(r"[a-z_]+", line.split(":", 1)[1]))


def manifest_tools():
    return manifest_list("provides_tools")


def test_registers_manifest_tools_command_skills_and_blueprints(data_dir):
    ctx = FakeCtx()
    plugin = load_plugin_package()
    assert plugin.register(ctx) is True
    assert set(ctx.tools) == manifest_tools()
    for name, spec in ctx.tools.items():
        assert spec["toolset"] == "caduceus" and spec["schema"]["name"] == name
        assert spec["schema"]["parameters"]["type"] == "object"
        assert "Never open or read" in spec["schema"]["description"] or name == "caduceus_trend"
    assert "caduceus" in ctx.commands
    assert set(ctx.skills) == {"caduceus-setup", "caduceus-guide", "caduceus-research", "caduceus-report-problem"}
    for entry in ctx.skills.values():
        assert entry["path"].is_file()
    assert set(ctx.blueprints) == {"research-watch", "daily-check", "hae-sync"}
    assert set(ctx.hooks) == manifest_list("provides_hooks") == {"pre_llm_call"}  # hermes plugins validate checks this
    for fields in ctx.blueprints.values():
        assert "[SILENT]" in fields["prompt_template"]
    assert not any(data_dir.iterdir())  # registration touched no storage


def test_handlers_return_json_errors_not_exceptions():
    ctx = FakeCtx()
    load_plugin_package().register(ctx)
    out = ctx.tools["caduceus_genome"]["handler"]({"op": "summary"})
    assert out.startswith("{")
