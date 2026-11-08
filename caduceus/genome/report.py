"""A local, self-contained variant report and a three-way import parity check."""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from ..core import sources
from ..core.policy import Policy, allowed
from .promethease import _summary, decode_report

_STYLE = """:root {color-scheme:light dark;--bg:#101822;--panel:#1c2937;--ink:#edf3f8;--muted:#b4c5d5;--line:#43586c}
@media(prefers-color-scheme:light){:root{--bg:#f3f6f8;--panel:white;--ink:#182b3b;--muted:#405b70;--line:#c9d6df}}
body.light{color-scheme:light;--bg:#f3f6f8;--panel:white;--ink:#182b3b;--muted:#405b70;--line:#c9d6df}
body.dark{color-scheme:dark;--bg:#101822;--panel:#1c2937;--ink:#edf3f8;--muted:#b4c5d5;--line:#43586c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:32px 20px}h1{font-size:2.5rem;margin-bottom:8px}h2{margin-top:36px}
p{color:var(--muted)}.tiles,.sections{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}
.tile,article{background:var(--panel);padding:18px;border:1px solid var(--line);border-radius:12px}
.tile strong{display:block;font-size:2rem}article h2{margin-top:0;font-size:1.2rem}ul{padding-left:20px}
button,input,select{background:var(--panel);color:var(--ink);padding:9px;border:1px solid var(--line);border-radius:6px}
.filters{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}.table-wrap{overflow:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line)}
td{vertical-align:top}caption{text-align:left;color:var(--muted)}[hidden]{display:none!important}
"""
_SCRIPT = """
const rows = [...document.querySelectorAll('tbody tr')];
function filterRows() {
  const mag = Number(document.getElementById('magnitude').value);
  const repute = document.getElementById('repute').value;
  const text = document.getElementById('text').value.toLowerCase();
  for (const row of rows) {
    row.hidden = (mag > 0 && (row.dataset.mag === '' || Number(row.dataset.mag) < mag)) ||
      (repute && row.dataset.repute !== repute) || !row.textContent.toLowerCase().includes(text);
  }
}
for (const id of ['magnitude','repute','text']) document.getElementById(id).addEventListener('input', filterRows);
document.getElementById('theme').addEventListener('click', () => {
  const dark = document.body.classList.contains('dark') ||
    (!document.body.classList.contains('light') && matchMedia('(prefers-color-scheme:dark)').matches);
  document.body.className = dark ? 'light' : 'dark';
});
"""


class NoSuccessfulReport(ValueError):
    """No complete Promethease import is available for the default report."""


def _source(conn, source_id):
    if source_id is None:
        row = conn.execute(
            "SELECT s.id FROM source_artifact s WHERE s.kind='promethease' AND EXISTS "
            "(SELECT 1 FROM import_run r WHERE r.source_id=s.id AND r.parser='promethease' "
            "AND r.status='succeeded') ORDER BY s.id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise NoSuccessfulReport("No successfully imported Promethease report")
        source_id = row[0]
    source = sources.get_source(conn, source_id)
    if source is None or source.kind != "promethease":
        raise ValueError("no Promethease source with that id")
    return source


def _rows(conn, source_id):
    return conn.execute("SELECT * FROM annotation_snapshot WHERE source_id=? ORDER BY ordinal", (source_id,)).fetchall()


def _label(row):
    genes = json.loads(row["genes_json"])
    return " ".join(str(v) for v in (row["marker"] or row["title"], row["geno"], *genes) if v)


def _section(title, rows):
    entries = "".join(f"<li><strong>{escape(_label(r))}</strong> — {escape(str(r['summary'] or ''))}</li>" for r in rows[:8])
    return f"<article><h2>{title}</h2><ul>{entries}</ul>" + ("" if rows else "<p>No visible records.</p>") + "</article>"


def build_report(conn, out_dir, *, policy, source_id=None) -> Path:
    source = _source(conn, source_id)
    rows = _rows(conn, source.id)
    visible = [r for r in rows if allowed(policy, r["category"])]
    records = [r for r in visible if r["kind"] != "genotype_info"]
    withheld = sum(r["kind"] != "genotype_info" and not allowed(policy, r["category"]) for r in rows)
    info = [{"marker": r["marker"], **json.loads(r["record_json"])} for r in visible if r["kind"] == "genotype_info"]
    payload = {"source_id": source.id, "records": [json.loads(r["record_json"]) for r in records],
               "genotype_info": info, "withheld": withheld}
    # Escape script termination even though this script is data, never executable.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("<", "\\u003c")
    imported = conn.execute(
        "SELECT finished_at FROM import_run WHERE source_id=? AND parser='promethease' AND status='succeeded' "
        "ORDER BY id DESC LIMIT 1", (source.id,)
    ).fetchone()
    imported_at = imported[0] if imported else source.added_at
    provenance = (
        f"Rebuilt by Caduceus from {source.original_name} imported {imported_at}. "
        "Annotations: SNPedia (CC BY-NC-SA 3.0) via your Promethease report. "
        "Information, not medical advice. Consumer chips miss rare variants and can be wrong."
    )
    counts = [("Records", len(records)), ("Magnitude ≥3", sum((r["magnitude"] or 0) >= 3 for r in records)),
              ("Good", sum(r["repute"] == "Good" for r in records)),
              ("Bad", sum(r["repute"] == "Bad" for r in records)), ("Withheld", withheld)]
    tiles = "".join(f'<div class="tile"><strong>{count}</strong>{label}</div>' for label, count in counts)
    ranked = sorted(records, key=lambda r: r["magnitude"] if r["magnitude"] is not None else -1, reverse=True)
    sections = (_section("Highest magnitude", ranked) +
                _section("Drug response", [r for r in ranked if r["category"] == "pgx"]) +
                _section("Good news", [r for r in ranked if r["repute"] == "Good"]) +
                _section("Worth a closer look", [r for r in ranked if r["repute"] == "Bad"]))
    table = []
    for row in records:
        mag = str(row["magnitude"]) if row["magnitude"] is not None else ""
        repute = row["repute"] or "None"
        cells = [_label(row), mag, repute, row["category"], row["summary"] or ""]
        table.append(f'<tr data-mag="{escape(mag, quote=True)}" data-repute="{escape(repute, quote=True)}">' +
                     "".join(f"<td>{escape(str(cell))}</td>" for cell in cells) + "</tr>")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Your variant report</title><style>{_STYLE}</style></head><body><main>
<header><button type="button" id="theme">Switch theme</button><h1>Your variant report</h1><p>{escape(provenance)}</p></header>
<div class="tiles">{tiles}</div><p>{withheld} records withheld by your privacy settings.</p>
<div class="sections">{sections}</div><h2>All visible records</h2>
<div class="filters"><label>Minimum magnitude <input id="magnitude" type="number" min="0" step="0.5" value="0"></label>
<label>Repute <select id="repute"><option value="">All</option><option>Good</option><option>Bad</option><option>None</option></select></label>
<label>Search <input id="text" type="search" placeholder="Marker, gene or summary"></label></div>
<div class="table-wrap"><table><caption>Annotations describe reported associations, with limits.</caption>
<thead><tr><th>Variant / genes</th><th>Magnitude</th><th>Repute</th><th>Category</th><th>Summary</th></tr></thead>
<tbody>{''.join(table)}</tbody></table></div></main>
<script type="application/json" id="caduceus-data">{data}</script><script>{_SCRIPT}</script></body></html>
"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).date().isoformat()
    path = out_dir / f"variant-report-{source.sha256[:8]}-{date}.html"
    # A failed write never replaces a complete report, and two writers never share a temporary file.
    fd, part = tempfile.mkstemp(dir=out_dir, prefix=path.name + ".", suffix=".part")
    os.close(fd)
    try:
        Path(part).write_text(html, encoding="utf-8")
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.unlink(part)
    return path


class _DataBlock(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "caduceus-data" and attrs.get("type") == "application/json":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def parity(conn, source_id, *, report_path=None) -> dict:
    source = _source(conn, source_id)
    decoded = decode_report(sources.source_file(conn, source))
    rows = _rows(conn, source.id)
    stored_records = [{**({"rsnum": r["marker"]} if r["kind"] == "snp" else
                         {"rsnum": None} if r["kind"] == "other" else {}), "title": r["title"],
                       "geno": r["geno"], "magnitude": r["magnitude"], "repute": r["repute"]}
                      for r in rows if r["kind"] != "genotype_info"]
    result = {"decoded": _summary(decoded.records, decoded.genotype_info),
              "stored": _summary(stored_records, [r for r in rows if r["kind"] == "genotype_info"])}
    stored_kinds = Counter(r["kind"] for r in rows)
    result["stored"]["by_kind"] = {kind: stored_kinds[kind] for kind in ("snp", "genoset", "other", "genotype_info")}
    diffs = [f"decoded warning: {warning}" for warning in decoded.warnings]
    if not decoded.records:
        diffs.append("decoded: No records found in the report")
    if report_path is None:
        with tempfile.TemporaryDirectory(prefix="caduceus-parity-") as directory:
            path = build_report(conn, directory, policy=Policy(), source_id=source.id)
            rebuilt = path.read_text(encoding="utf-8")
    else:
        rebuilt = Path(report_path).read_text(encoding="utf-8")
    try:
        parser = _DataBlock()
        parser.feed(rebuilt)
        payload = json.loads("".join(parser.parts))
        result["rebuilt"] = _summary(payload["records"], payload["genotype_info"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, **result, "rebuilt": {}, "diffs": [*diffs, "rebuilt: missing or invalid data block"]}
    diffs.extend(f"decoded vs {name}: {key}" for name in ("stored", "rebuilt")
                 for key, value in result["decoded"].items() if result[name].get(key) != value)
    return {"ok": not diffs, **result, "diffs": diffs}
