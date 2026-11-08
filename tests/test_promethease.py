import base64
import json
import threading
import zipfile
import zlib

import pytest

from caduceus.core import search, sources
from caduceus.core.contracts import SourceKind
from caduceus.core.policy import Policy
from caduceus.genome import promethease as pm
from synth.promethease import chunk, genotype_maps, html_report, records, write_report


def _source(vault, tmp_path, **kwargs):
    path = write_report(tmp_path / "promethease.zip", **kwargs)
    return sources.store_source(vault, path, SourceKind.PROMETHEASE)[0]


@pytest.mark.parametrize("suffix", [".html", ".zip"])
def test_decode_shapes_metadata_and_counts(tmp_path, suffix):
    decoded = pm.decode_report(write_report(tmp_path / ("report" + suffix)))
    assert decoded.records == records()
    assert decoded.chunks == 3 and not decoded.warnings
    assert decoded.metadata == {"version": "synthetic-1", "label": "invented", "count": 6, "enabled": True, "missing": None}
    rsinfo, others = genotype_maps()
    assert decoded.genotype_info["rsinfo:rs99999001"] == {"set": "rsinfo", "genotypes": rsinfo["rs99999001"]}
    assert decoded.genotype_info["othersinfo:gsSynthetic1"]["genotypes"] == others["gsSynthetic1"]
    assert len(decoded.genotype_info) == 5


def test_repeated_map_marker_in_both_sets_is_preserved(tmp_path):
    maps = ({"rs99999001": {"a": 1}}, {"rs99999001": {"b": 2}})
    decoded = pm.decode_report(write_report(tmp_path / "report.html", maps=maps))
    assert set(decoded.genotype_info) == {"rsinfo:rs99999001", "othersinfo:rs99999001"}


def test_bad_chunk_warns_and_continues(tmp_path):
    decoded = pm.decode_report(write_report(tmp_path / "report.html", bad_chunk=True))
    assert decoded.records == records()
    assert decoded.chunks == 4 and len(decoded.warnings) == 1


@pytest.mark.parametrize("raw", [b"{broken json", b'{"wrong":"shape"}'])
def test_bad_json_and_wrong_shape_warn(tmp_path, raw):
    html = html_report(bad_chunk=True).replace("'AAAA'", repr(base64.b64encode(zlib.compress(raw)).decode()))
    path = tmp_path / "report.html"
    path.write_text(html)
    decoded = pm.decode_report(path)
    assert len(decoded.warnings) == 1 and decoded.records == records()


def test_context_is_bounded_and_gzip_is_accepted(tmp_path):
    value = [{"rsnum": "rs99999123", "geno": "(A;A)"}]
    compressor = zlib.compressobj(wbits=31)
    payload = base64.b64encode(compressor.compress(json.dumps(value).encode()) + compressor.flush()).decode()
    html = html_report(rows=[], maps=({}, {}))
    html += f"mygenos.push.apply(mygenos,JSON.parse(decompressString('{payload}')));"
    html += f"decompressString('{chunk(value)}');"
    path = tmp_path / "report.html"
    path.write_text(html)
    decoded = pm.decode_report(path)
    assert decoded.records == value and len(decoded.warnings) == 1


def test_inflate_limits_and_exact_boundary(tmp_path):
    rows = [{"genosummary": "x" * 5000}]
    path = write_report(tmp_path / "report.html", rows=rows, maps=({}, {}))
    size = len(json.dumps(rows, ensure_ascii=False).encode())
    assert pm.decode_report(path, max_inflate=size).records == rows
    with pytest.raises(pm.ReportTooLarge):
        pm.decode_report(path, max_inflate=size - 1)
    # Two individually valid chunks still exceed the cumulative inflated limit.
    path.write_text(html_report(rows=rows, maps=({}, {})) +
                    f"mygenos.push.apply(mygenos,JSON.parse(decompressString('{chunk(rows)}')));")
    with pytest.raises(pm.ReportTooLarge):
        pm.decode_report(path, max_inflate=6000, max_total=7500)


@pytest.mark.parametrize("name", ["../promethease.html", "/promethease.html", "C:/promethease.html", "x\\..\\promethease.html"])
def test_zip_traversal_is_refused(tmp_path, name):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, html_report())
    with pytest.raises(ValueError, match="unsafe zip"):
        pm.decode_report(path)


def test_zip_member_cap(tmp_path):
    path = write_report(tmp_path / "report.zip")
    with pytest.raises(pm.ReportTooLarge):
        pm.decode_report(path, max_total=10)


def test_import_rows_aliases_search_and_idempotence(vault, tmp_path):
    src = _source(vault, tmp_path)
    # Import must read the provenance copy, even if the user's file disappears.
    (tmp_path / "promethease.zip").unlink()
    result = pm.import_promethease(vault, src)
    assert result.status == "succeeded" and result.source_id == src.id
    assert result.counts == {"records": 6, "snp": 4, "genoset": 1, "other": 1, "genotype_info_markers": 5,
                             "chunks": 3, "aliases": 1,
                             "magnitude_hist": {"0.0": 1, "2.0": 1, "3.5": 1, "4.0": 1, "5.0": 1, "None": 1},
                             "repute_hist": {"Good": 2, "Bad": 2, "None": 2}}
    rows = vault.execute("SELECT * FROM annotation_snapshot ORDER BY ordinal").fetchall()
    assert [r["ordinal"] for r in rows] == list(range(11))
    for row, record in zip(rows, records()):
        assert row["record_json"] == json.dumps(record, sort_keys=True, ensure_ascii=False)
    assert rows[0]["magnitude"] == 3.5 and rows[0]["genes_json"] == '["FAKE1"]'
    assert rows[1]["category"] == "alzheimers" and rows[2]["category"] == "pgx"
    assert rows[5]["repute"] is None and rows[5]["magnitude"] is None
    assert [r["marker"] for r in rows[6:]] == ["rs99999001", "rs99999002", "rs99999003", "rs99999004", "gsSynthetic1"]
    assert json.loads(rows[6]["record_json"])["set"] == "rsinfo"
    assert rows[7]["category"] == "alzheimers"
    assert tuple(vault.execute("SELECT alias, marker, source FROM marker_alias").fetchone()) == (
        "rs99999901", "rs99999001", "promethease")
    for word in ("luminous", "lanterns", "pebbles", "prism", "cobbles", "inventomycin"):
        assert search.search(vault, word, policy=Policy())["results"]
    assert len(search.fetch(vault, f"annotation:{src.id}:0", policy=Policy())["body"]) <= 2000
    assert pm.import_promethease(vault, src).status == "skipped"
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 11
    assert vault.execute("SELECT count(*) FROM search_doc").fetchone()[0] == 6
    assert sources.run_info(vault, result.run_id)["counts"] == result.counts


def _many_rows():
    return [{"rsnum": f"rs99999{i:04}", "geno": "(A;A)", "magnitude": 1,
             "genosummary": "Invented batch record"} for i in range(2005)]


def test_crash_then_resume_committed_batches(vault, tmp_path, monkeypatch):
    src = _source(vault, tmp_path, rows=_many_rows(), maps=({}, {}))
    original = pm._write_batch
    calls = []

    def crash(conn, source_id, batch):
        calls.append(len(batch))
        if len(calls) == 2:
            raise SystemExit("synthetic process interruption")
        original(conn, source_id, batch)

    monkeypatch.setattr(pm, "_write_batch", crash)
    with pytest.raises(SystemExit):
        pm.import_promethease(vault, src)
    assert calls == [2000, 5]
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 2000
    monkeypatch.setattr(pm, "_write_batch", original)
    assert pm.import_promethease(vault, src).status == "succeeded"
    assert [r[0] for r in vault.execute("SELECT status FROM import_run ORDER BY id")] == ["interrupted", "succeeded"]
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 2005
    assert vault.execute("SELECT count(*) FROM search_doc").fetchone()[0] == 2005


def test_failed_batch_rolls_back_and_can_resume(vault, tmp_path, monkeypatch):
    src = _source(vault, tmp_path)
    original = pm.search.index_doc

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic index failure")

    monkeypatch.setattr(pm.search, "index_doc", fail)
    failed = pm.import_promethease(vault, src)
    assert failed.status == "failed"
    assert sources.run_info(vault, failed.run_id)["status"] == "failed"
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 0
    assert vault.execute("SELECT count(*) FROM marker_alias").fetchone()[0] == 0
    monkeypatch.setattr(pm.search, "index_doc", original)
    assert pm.import_promethease(vault, src).status == "succeeded"


def test_cancel_between_batches_and_resume(vault, tmp_path, monkeypatch):
    src = _source(vault, tmp_path, rows=_many_rows(), maps=({}, {}))
    cancel = threading.Event()
    original = pm._write_batch

    def first_batch(conn, source_id, batch):
        original(conn, source_id, batch)
        cancel.set()

    monkeypatch.setattr(pm, "_write_batch", first_batch)
    cancelled = pm.import_promethease(vault, src, cancel=cancel)
    assert cancelled.status == "cancelled"
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 2000
    monkeypatch.setattr(pm, "_write_batch", original)
    assert pm.import_promethease(vault, src).status == "succeeded"
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 2005


def test_pre_cancel_and_bad_source_are_recorded(vault, tmp_path):
    src = _source(vault, tmp_path)
    cancel = threading.Event()
    cancel.set()
    assert pm.import_promethease(vault, src, cancel=cancel).status == "cancelled"
    assert vault.execute("SELECT count(*) FROM annotation_snapshot").fetchone()[0] == 0
    sources.source_file(vault, src).write_bytes(b"not a zip")
    # Ordinary non-report HTML is an empty decode; malformed encoding is a failed import.
    sources.source_file(vault, src).write_bytes(b"\xff")
    result = pm.import_promethease(vault, src)
    assert result.status == "failed" and result.warnings == ["import failed: UnicodeDecodeError"]


@pytest.mark.private
def test_private_parity(vault, private_dir):
    from caduceus.genome.report import parity

    src = sources.store_source(vault, private_dir / "promethease.zip", SourceKind.PROMETHEASE)[0]
    result = pm.import_promethease(vault, src)
    assert result.status == "succeeded"
    checked = parity(vault, src.id)
    print({name: {"records": checked[name]["records"], "by_kind": checked[name]["by_kind"]}
           for name in ("decoded", "stored", "rebuilt")})
    assert checked["ok"]
