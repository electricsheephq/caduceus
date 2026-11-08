"""Synthetic import acceptance, bounded input handling, and recovery."""
import json
import threading
import time
import zipfile
from io import BytesIO

import pytest

from caduceus.core import sources
from caduceus.core.contracts import SourceKind
from caduceus.genome import dtc
from synth.dtc import make_23andme, make_ancestry


def _source(vault, path):
    return sources.store_source(vault, path, SourceKind.DTC)[0]


@pytest.mark.parametrize("version", ["v3", "v4", "v5"])
@pytest.mark.parametrize("crlf", [False, True])
def test_23andme(vault, tmp_path, version, crlf):
    path = make_23andme(tmp_path / f"dna_{version}_sample.txt", version, 30,
                         {"rs1": "--", "rs2": "A", "i123": "DI", "rs3": "D", "rs4": "I",
                          "rs5": "DD", "rs6": "II"}, crlf)
    src = _source(vault, path)
    path.write_text("original is no longer readable as DTC")
    result = dtc.import_dtc(vault, src)
    assert result.status == "succeeded" and result.warnings == []
    assert result.counts == dict(markers=31, rs_ids=30, internal_ids=1, no_calls=1, haploid=3,
                                 indel_codes=5, duplicates=0, skipped_lines=0,
                                 chromosomes={**{str(i): 2 if i <= 5 else 1 for i in range(1, 23)},
                                              "X": 1, "Y": 1, "MT": 1, "6": 2})
    row = vault.execute("SELECT * FROM callset").fetchone()
    assert (row["vendor"], row["chip_version"], row["build"], row["marker_count"]) == ("23andMe", version, "37", 31)
    assert dtc.import_dtc(vault, src).status == "skipped"
    assert vault.execute("SELECT count(*) FROM import_run").fetchone()[0] == 1


@pytest.mark.parametrize("crlf", [False, True])
def test_ancestry(vault, tmp_path, crlf):
    src = _source(vault, make_ancestry(tmp_path / "ancestry.txt", n=26, inject={"rs1": "--"}, crlf=crlf))
    result = dtc.import_dtc(vault, src)
    assert result.status == "succeeded" and result.counts["no_calls"] == 1
    calls = {r[0]: (r[1], r[2]) for r in vault.execute("SELECT marker, chrom, genotype FROM genotype_call")}
    assert calls["rs1"][1] == "--"
    assert [calls[f"rs{i}"][0] for i in range(23, 27)] == ["X", "Y", "XY", "MT"]
    # Use another source identity for the mixed no-call fixture; stored copies stay immutable.
    path = tmp_path / "mixed.txt"
    path.write_bytes(sources.source_file(vault, src).read_bytes() + b"rs27\t1\t1027\t0\tA\n")
    second = dtc.import_dtc(vault, _source(vault, path))
    assert second.counts["no_calls"] == 2


def test_duplicates_and_malformed(vault, tmp_path):
    path = make_23andme(tmp_path / "dna.txt", n=1)
    original_gt = path.read_text().splitlines()[-1].split("\t")[-1]
    with path.open("a") as fh:
        fh.write("rs1\t1\t1001\t--\n")
        for line in ("bad", "rs2\t99\t1\tAA", "rs2\t1\t-2\tAA", "bad-id\t1\t2\tAA",
                     "rs2\t1\t2\tZZ", "rs2\t1\t2\tAAA", "rs2\t1\t9999999999999999999999999\tAA"):
            fh.write(line + "\n")
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "succeeded"
    assert result.counts["markers"] == 1 and result.counts["duplicates"] == 1
    assert result.counts["skipped_lines"] == 7
    assert result.warnings == ["Skipped malformed lines (first 5): 6, 7, 8, 9, 10"]
    assert vault.execute("SELECT genotype FROM genotype_call").fetchone()[0] == original_gt


@pytest.mark.parametrize("build", ["38", "unrecognized"])
def test_build_warning(vault, tmp_path, build):
    src = _source(vault, make_23andme(tmp_path / "dna.txt", n=1, build=build))
    result = dtc.import_dtc(vault, src)
    assert result.status == "succeeded" and result.warnings
    assert vault.execute("SELECT build, chip_version FROM callset").fetchone()[:] == (
        "38" if build == "38" else "unknown", "")


def test_multi_member_zip_and_cap(vault, tmp_path):
    text = make_23andme(tmp_path / "dna.txt", n=30).read_bytes()
    zipped = tmp_path / "dna.zip"
    with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("notes.txt", "small")
        archive.writestr("data/synthetic_v5_genome.txt", text)
        archive.writestr("large.bin", b"x" * len(text) * 2)
    result = dtc.import_dtc(vault, _source(vault, zipped))
    assert result.status == "succeeded" and result.counts["markers"] == 30
    assert vault.execute("SELECT chip_version FROM callset").fetchone()[0] == "v5"
    with pytest.raises(ValueError, match="byte limit"):
        with dtc._open_text(zipped, max_inflate=len(text) - 1) as (lines, _):
            list(lines)
    with dtc._open_text(zipped, max_inflate=len(text)) as (lines, _):
        assert len(list(lines)) == 33
    with pytest.raises(ValueError, match="byte limit"):
        list(dtc._lines(BytesIO(b"abcd\n"), 4))


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "..\\escape.txt", "C:/drive.txt"])
def test_zip_refuses_traversal(vault, tmp_path, member):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, "synthetic")
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "failed"
    assert sources.run_info(vault, result.run_id)["status"] == "failed"
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("data", [b"unrecognized\n", b"# missing header\n", b"PK\x03\x04broken", b"\xff"])
def test_failure_is_recorded(vault, tmp_path, data):
    path = tmp_path / "bad.txt"
    path.write_bytes(data)
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "failed"
    assert sources.run_info(vault, result.run_id)["errors"]


def test_crash_resume(vault, tmp_path, monkeypatch):
    src = _source(vault, make_23andme(tmp_path / "dna.txt", n=20_005))
    update = sources.update_progress

    class Crash(BaseException):
        pass

    def crash(*args):
        raise Crash()

    monkeypatch.setattr(sources, "update_progress", crash)
    with pytest.raises(Crash):
        dtc.import_dtc(vault, src)
    assert vault.execute("SELECT count(*) FROM genotype_call").fetchone()[0] == 20_000
    monkeypatch.setattr(sources, "update_progress", update)
    result = dtc.import_dtc(vault, src)
    assert result.status == "succeeded"
    assert result.counts["markers"] == 20_005 and result.counts["duplicates"] == 20_000
    assert [r[0] for r in vault.execute("SELECT status FROM import_run ORDER BY id")] == ["interrupted", "succeeded"]
    assert vault.execute("SELECT marker_count FROM callset").fetchone()[0] == 20_005


def test_cancel_between_batches(vault, tmp_path, monkeypatch):
    src = _source(vault, make_23andme(tmp_path / "dna.txt", n=40_010))
    event = threading.Event()
    update = sources.update_progress
    progress = []

    def cancel(conn, run_id, counts):
        progress.append(json.loads(json.dumps(counts)))
        update(conn, run_id, counts)
        event.set()

    monkeypatch.setattr(sources, "update_progress", cancel)
    first = dtc.import_dtc(vault, src, cancel=event)
    assert first.status == "cancelled" and first.counts["markers"] == 20_000
    assert len(progress) == 1 and sources.run_info(vault, first.run_id)["progress"] == first.counts
    monkeypatch.setattr(sources, "update_progress", update)
    event.clear()
    resumed = dtc.import_dtc(vault, src, cancel=event)
    assert resumed.status == "succeeded" and resumed.counts["markers"] == 40_010


def test_precancel(vault, tmp_path):
    event = threading.Event()
    event.set()
    src = _source(vault, make_23andme(tmp_path / "dna.txt", n=3))
    result = dtc.import_dtc(vault, src, cancel=event)
    assert result.status == "cancelled"
    assert vault.execute("SELECT count(*) FROM genotype_call").fetchone()[0] == 0


@pytest.mark.parametrize("generator", [make_23andme, make_ancestry])
@pytest.mark.parametrize("zipped", [False, True])
def test_generators_deterministic(vault, tmp_path, generator, zipped):
    first = generator(tmp_path / "one", n=10, zip=zipped, seed=123)
    second = generator(tmp_path / "two", n=10, zip=zipped, seed=123)
    assert first.read_bytes() == second.read_bytes()
    assert dtc.import_dtc(vault, _source(vault, first)).counts["markers"] == 10


def test_200k_speed(vault, tmp_path):
    src = _source(vault, make_23andme(tmp_path / "large_v5_dna.txt", n=200_000))
    start = time.monotonic()
    result = dtc.import_dtc(vault, src)
    elapsed = time.monotonic() - start
    assert result.status == "succeeded" and result.counts["markers"] == 200_000
    assert elapsed < 10, f"200k-row import exceeded the lane's ten-second gate: {elapsed:.2f}s"
