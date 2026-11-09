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


def test_one_huge_line_is_refused_without_reading_it(vault, tmp_path):
    path = make_23andme(tmp_path / "dna.txt", n=3)
    with path.open("a") as fh:
        fh.write("rs9\t1\t9\t" + "A" * 50_000 + "\n")
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "failed"
    assert result.warnings == ["DTC import failed: DTC line is too long; this is not a raw-data file"]
    assert not any(r["complete"] for r in __import__("caduceus.genome.calls", fromlist=["x"]).callsets(vault))


def test_non_utf8_byte_skips_only_that_line(vault, tmp_path):
    path = make_23andme(tmp_path / "dna.txt", n=3)
    with path.open("ab") as fh:
        fh.write(b"rs9\t1\t9\tA\xffG\n")
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "succeeded"
    assert (result.counts["markers"], result.counts["skipped_lines"]) == (3, 1)


def test_header_only_file_fails(vault, tmp_path):
    path = make_23andme(tmp_path / "dna.txt", n=0)
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "failed" and result.warnings == ["DTC import failed: No genotype rows found in the file"]


def test_byte_cap_is_read_when_called(vault, tmp_path, monkeypatch):
    path = make_23andme(tmp_path / "dna.txt", n=50)
    monkeypatch.setattr(dtc, "MAX_INFLATE", 200)
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "failed" and "byte limit exceeded" in result.warnings[0]


def _csv_fixture(path, *, comments="", member=None, quoted=True):
    import gzip

    rows = ["RSID,CHROMOSOME,POSITION,RESULT", "rs1,1,1001,AA", "rs2,XY,1002,--",
            "rs3,MT,1003,A", "i4,X,1004,DI", "rs5,Y,1005,DD", "rs6,22,1006,I"]
    if quoted:
        rows = [",".join(f' "{field}" ' for field in row.split(",")) for row in rows]
    rows += ["bad", "rs7,99,1007,AA", "rs8,1,-1,AA", "bad-id,1,1009,AA",
             "rs10,1,1010,AZ", "rs11,1,1011,AAA", "rs12,1,1012,AA,extra",
             "rs13,1,9223372036854775808,AA", "rs14,1,0,AA"]
    raw = (comments + "\r\n".join(rows) + "\r\n").encode()
    if member:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("notes.txt", "synthetic notes")
            archive.writestr(member, raw)
    elif path.suffix == ".gz":
        path.write_bytes(gzip.compress(raw))
    else:
        path.write_bytes(raw)
    return path


@pytest.mark.parametrize("filename,comments,member,vendor,build", [
    ("data.zip", "# MyHeritage synthetic; build 37; forward (+) strand\r\n", "data.csv", "MyHeritage", "37"),
    ("FTDNA_Autosomal_o37_Results_demo.csv.gz", "", None, "FamilyTreeDNA", "37"),
    ("data.zip", "", "demo_Autosomal_o37_Results_test.csv", "FamilyTreeDNA", "37"),
    ("FamilyTreeDNA.csv.gz", "", None, "FamilyTreeDNA", "unknown"),
    ("data.csv", "# FamilyTreeDNA synthetic; build 37\r\n", None, "FamilyTreeDNA", "37"),
    ("myheritage.csv", "# build 37\r\n", None, "MyHeritage", "37"),
    ("data.csv", "", None, "MyHeritage or FamilyTreeDNA (CSV)", "unknown"),
])
@pytest.mark.parametrize("quoted", [False, True])
def test_csv_formats(vault, tmp_path, filename, comments, member, vendor, build, quoted):
    path = _csv_fixture(tmp_path / filename, comments=comments, member=member, quoted=quoted)
    assert sources.sniff_kind(path) == "dtc"
    src = _source(vault, path)
    result = dtc.import_dtc(vault, src)
    assert result.status == "succeeded", result.warnings
    assert result.as_dict()["counts"]["vendor"] == vendor
    assert result.counts["markers"] == 6 and result.counts["skipped_lines"] == 9
    assert (result.counts["no_calls"], result.counts["haploid"], result.counts["indel_codes"]) == (1, 2, 3)
    assert vault.execute("SELECT vendor, build FROM callset").fetchone()[:] == (vendor, build)
    assert ("Reference build is unknown." in result.warnings) == (build == "unknown")
    assert dtc.import_dtc(vault, src).status == "skipped"
    assert vault.execute("SELECT count(*) FROM import_run").fetchone()[0] == 1


def test_living_dna_vendor(vault, tmp_path):
    path = tmp_path / "living.tsv"
    path.write_text("# Living DNA synthetic; build 37\n# rsid\tchromosome\tposition\tgenotype\nrs1\t1\t1001\tAG\n")
    assert sources.sniff_kind(path) == "dtc"
    assert dtc.import_dtc(vault, _source(vault, path)).status == "succeeded"
    assert vault.execute("SELECT vendor, build FROM callset").fetchone()[:] == ("Living DNA", "37")


def test_gzip_inflate_and_line_limits(vault, tmp_path, monkeypatch):
    import gzip

    path = tmp_path / "FTDNA.csv.gz"
    header = b"rsid,chromosome,position,result\n"
    path.write_bytes(gzip.compress(header + b"rs1,1,1001,AA\n" * 10000))
    assert sources.sniff_kind(path) == "dtc"
    monkeypatch.setattr(dtc, "MAX_INFLATE", 1000)
    result = dtc.import_dtc(vault, _source(vault, path))
    assert result.status == "failed" and any("byte limit exceeded" in w for w in result.warnings)
    path.write_bytes(gzip.compress(header + b"A" * 10000))
    with pytest.raises(dtc.DtcError, match="line is too long"):
        with dtc._open_text(path, max_inflate=20000) as (lines, _):
            list(lines)


@pytest.mark.parametrize("head", [
    "# invented preamble\n\"RsId\",\"Chromosome\",\"Position\",\"Result\"\n",
    "# invented preamble\nrsid,chromosome,position,result\n",
    "# synthetic MyHeritage export\n", "# synthetic FamilyTreeDNA export\n",
])
def test_csv_sniff_headers(head):
    assert sources._looks_dtc(head)


@pytest.mark.parametrize('raw', [
    b'\x1f\x8b', b'\x1f\x8bnot gzip',
    b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x06',
])
def test_sniff_broken_gzip_is_unrecognised(tmp_path, raw):
    from caduceus import ops
    from caduceus.tools import ToolError

    path = tmp_path / 'broken.csv.gz'
    path.write_bytes(raw)
    assert sources.sniff_kind(path) == SourceKind.OTHER
    with pytest.raises(ToolError, match='not a recognised file'):
        ops._import({'path': str(path)})


@pytest.mark.parametrize('filename,head,kind', [
    ('record.json', '{"resourceType": "Patient", "note": "MyHeritage"}', SourceKind.FHIR),
    ('promethease.html', '<html>Promethease synthetic report mentions 23andMe</html>', SourceKind.PROMETHEASE),
    ('export.xml', '<HealthData><Note>MyHeritage</Note></HealthData>', SourceKind.APPLE_HEALTH),
])
def test_sniff_plain_specific_formats_before_dtc(tmp_path, filename, head, kind):
    path = tmp_path / filename
    path.write_text(head)
    assert sources.sniff_kind(path) == kind
