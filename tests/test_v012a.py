"""Lane A regressions: generated exports and invented persona data only."""
import threading
import zipfile
from contextlib import closing

import pytest

from caduceus import ops, tools
from caduceus.core import jobs, sources
from caduceus.core.db import open_samples, open_vault
from caduceus.demo import persona
from caduceus.health import apple
from conftest import FakeCtx
from tests.synth.apple import make_export


XML_WARNING = ("Imported an Apple Health XML on its own. Health records are read only from the export zip "
               "(its clinical-records folder); import export.zip to include them.")
EXPORT_ERROR = "zip must contain exactly one Apple Health export XML"
FINISHING = ("Finishing the demo persona's import in the background. Check with /caduceus imports, "
             "then ask your agent: “Why isn't my Ozempic working?”")


@pytest.fixture
def export_parts(tmp_path):
    path = make_export(tmp_path / "english.zip", days=2)
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    with closing(open_vault(tmp_path / "reference-vault.db")) as vault, closing(
            open_samples(tmp_path / "reference-samples.db")) as samples:
        src = sources.store_source(vault, path, "apple_health")[0]
        result = apple.import_apple_health(samples, vault, src)
        assert result.status == "succeeded", result.warnings
    return parts, result.counts


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return path


def _import(samples, vault, path):
    src = sources.store_source(vault, path, "apple_health")[0]
    return apple.import_apple_health(samples, vault, src)


@pytest.mark.parametrize("xml_name", ["exportar.xml", "eksport.xml", "Exportieren.xml", "書き出したデータ.xml"])
def test_localized_export_with_clinical_records(samples, vault, tmp_path, export_parts, xml_name):
    parts, expected = export_parts
    xml = parts.pop("apple_health_export/export.xml")
    parts["apple_health_export/" + xml_name] = b"\xef\xbb\xbf" + xml
    path = _zip(tmp_path / "localized.zip", parts)
    assert sources.sniff_kind(path) == "apple_health"
    result = _import(samples, vault, path)
    assert result.status == "succeeded", result.warnings
    assert result.counts == expected
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == expected["observation"]


def _long_header(xml, *, entity=False):
    declarations = "".join(f'<!ELEMENT Synthetic{i} EMPTY>\n<!ATTLIST Synthetic{i} label CDATA #IMPLIED>\n'
                           for i in range(150)).encode()
    assert len(declarations) >= 8192
    if entity:
        declarations += b'<!ENTITY unsafe "synthetic-only">'
    return xml.replace(b"<!ELEMENT HealthData ANY>", b"<!ELEMENT HealthData ANY>\n" + declarations)


@pytest.mark.parametrize("zipped", [False, True])
@pytest.mark.parametrize("entity", [False, True])
def test_realistic_doctype_before_distant_root(samples, vault, tmp_path, export_parts, zipped, entity):
    parts, expected = export_parts
    xml = _long_header(parts.pop("apple_health_export/export.xml"), entity=entity)
    assert b"<HealthData" not in xml[:4096]
    if zipped:
        parts["apple_health_export/exportar.xml"] = xml
        path = _zip(tmp_path / "long-header.zip", parts)
    else:
        path = tmp_path / "exportar.xml"
        path.write_bytes(xml)
    assert sources.sniff_kind(path) == "apple_health"
    result = _import(samples, vault, path)
    if entity:
        assert result.status == "failed"
        assert any("UnsafeXml" in warning for warning in result.warnings)
        assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
        assert sources.run_info(vault, result.run_id)["status"] == "failed"
    else:
        assert result.status == "succeeded", result.warnings
        assert result.counts["records"] == expected["records"]
        assert result.counts["samples"] == expected["samples"]
        assert result.counts["workouts"] == expected["workouts"]
        assert (XML_WARNING in result.warnings) == (not zipped)


def test_ambiguous_exports_and_cda_only(samples, vault, tmp_path):
    path = _zip(tmp_path / "two-exports.zip", {"one.xml": "<HealthData/>", "two.xml": "<HealthData/>"})
    result = _import(samples, vault, path)
    assert result.status == "failed"
    assert EXPORT_ERROR in result.warnings
    assert EXPORT_ERROR in sources.run_info(vault, result.run_id)["errors"]
    # Even a CDA member containing a marker must not be selected by content.
    cda = _zip(tmp_path / "cda-only.zip", {"apple_health_export/export_cda.xml": "<HealthData/>"})
    assert sources.sniff_kind(cda) == "other"
    assert EXPORT_ERROR in _import(samples, vault, cda).warnings


def test_lone_xml_matches_zip_without_clinical_and_respects_size_limit(
        samples, vault, tmp_path, export_parts, monkeypatch):
    parts, expected = export_parts
    xml = parts["apple_health_export/export.xml"]
    path = tmp_path / "health.xml"
    path.write_bytes(xml)
    assert sources.sniff_kind(path) == "apple_health"
    src = sources.store_source(vault, path, "apple_health")[0]
    path.unlink()  # The lone-XML path must also read only the stored source.
    result = apple.import_apple_health(samples, vault, src)
    assert result.status == "succeeded", result.warnings
    assert XML_WARNING in result.warnings
    clinical = {"observation", "condition", "medication", "ignored", "parts"}  # lone XML has no split-run metadata
    assert result.counts == {key: value for key, value in expected.items() if key not in clinical}
    for table in ("observation", "condition", "medication_event"):
        assert vault.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert apple.import_apple_health(samples, vault, src).status == "skipped"
    path.write_bytes(xml + b"\n")
    monkeypatch.setattr(apple, "MAX_MEMBER", len(xml))
    oversized = _import(samples, vault, path)
    assert oversized.status == "failed"
    assert oversized.counts["records"] == 0


def test_sniff_content_precedes_other_formats_and_checks_ten_shallow_xmls(tmp_path):
    members = {f"deep/nested/decoy{i}.xml": "<Unrelated/>" for i in range(10)}
    members.update({"promethease.html": "synthetic report", "genome.txt": "# 23andMe\n",
                    "clinical-records/lab.json": '{"resourceType":"Bundle"}',
                    "export_cda.xml": "<HealthData/>",
                    "exportar.xml": b"\xef\xbb\xbf<!DOCTYPE HealthData [<!ELEMENT HealthData ANY>]>\n<HealthData/>"})
    # Added last but shallower than the decoys; XML sniffing precedes every other format.
    assert sources.sniff_kind(_zip(tmp_path / "shallow.zip", members)) == "apple_health"
    # CDA does not count toward the ten XML probes, and the tenth probe is included.
    probes = {"export_cda.xml": "<HealthData/>"}
    probes.update({f"decoy{i}.xml": "<Unrelated/>" for i in range(9)})
    probes["eksport.xml"] = "<HealthData/>"
    assert sources.sniff_kind(_zip(tmp_path / "tenth.zip", probes)) == "apple_health"
    probes["eksport.xml"] = "<Unrelated/>"
    probes["deep/health.xml"] = "<HealthData/>"
    assert sources.sniff_kind(_zip(tmp_path / "eleventh.zip", probes)) == "other"


def test_export_selection_falls_back_after_duplicate_english_names_and_skips_unsafe_members(
        samples, vault, tmp_path, export_parts):
    parts, expected = export_parts
    xml = parts.pop("apple_health_export/export.xml")
    parts.update({"a/export.xml": "<Unrelated/>", "b/export.xml": "<Unrelated/>",
                  "../unsafe/health.xml": xml, "apple_health_export/export_cda.xml": xml,
                  "apple_health_export/exportar.xml": xml})
    result = _import(samples, vault, _zip(tmp_path / "fallback.zip", parts))
    assert result.status == "succeeded", result.warnings
    assert result.counts == expected
    # A unique safe English export still wins over additional HealthData candidates.
    parts["a/export.xml"] = xml
    del parts["b/export.xml"]
    second = _import(samples, vault, _zip(tmp_path / "english-wins.zip", parts))
    assert second.status == "succeeded", second.warnings
    assert second.counts["records"] == expected["records"]


@pytest.mark.parametrize("stored_keys", [("dna",), ("dna", "report"), ("apple", "report", "dna")])
@pytest.mark.parametrize("busy", [False, True])
def test_demo_recovers_storage_crash_with_fresh_job_key(data_dir, vault, tmp_path, monkeypatch, stored_keys, busy):
    monkeypatch.setattr(ops, "DEMO_MARKERS", 30)
    fresh_keys = []
    with monkeypatch.context() as fresh:
        fresh.setenv("CADUCEUS_DATA_DIR", str(tmp_path / "fresh-demo"))
        fresh.setattr(jobs, "submit", lambda key, fn, **kw: fresh_keys.append(key) or {"state": "running"})
        assert tools.command(FakeCtx(), "demo").startswith("Loading the demo persona")
    files = persona.write_persona(data_dir / "demo-files", markers=30)
    stored = [sources.store_source(vault, files[key], sources.sniff_kind(files[key]))[0] for key in stored_keys]
    original_bytes = {src.id: sources.source_file(vault, src).read_bytes() for src in stored}
    submitted, results, writes = [], [], []
    submit, store_source, write_persona = jobs.submit, sources.store_source, persona.write_persona

    def generate(*args, **kwargs):
        acquired = ops._DEMO_LOCK.acquire(blocking=False)
        if acquired:
            ops._DEMO_LOCK.release()
        assert not acquired, "recovery generation must hold the demo lock"
        return write_persona(*args, **kwargs)

    def store(conn, path, kind):
        writes.append(path.name)
        return store_source(conn, path, kind)

    def tracked(key, fn, *, wait_s):
        submitted.append(key)

        def run():
            result = fn()
            results.append(result)
            return result

        return submit(key, run, wait_s=wait_s)

    monkeypatch.setattr(persona, "write_persona", generate)
    monkeypatch.setattr(sources, "store_source", store)
    monkeypatch.setattr(jobs, "submit", tracked)
    release = threading.Event()
    try:
        if busy:
            assert submit(fresh_keys[0], lambda: release.wait(10), wait_s=0)["state"] == "running"
            assert tools.command(FakeCtx(), "demo") == "The demo persona is still importing. Check with /caduceus imports."
            release.set()
            assert jobs.join(fresh_keys[0], timeout=5)
        assert tools.command(FakeCtx(), "demo") == FINISHING
    finally:
        release.set()
        for key in set(submitted + fresh_keys):
            assert jobs.join(key, timeout=30)
    assert submitted and set(submitted) == set(fresh_keys)
    assert results and [r["status"] for r in results[-1]] == ["succeeded"] * 3
    assert [r["source_id"] for r in results[-1]] == [vault.execute(
        "SELECT id FROM source_artifact WHERE original_name=?", (files[key].name,)).fetchone()[0]
        for key in ("dna", "report", "apple")]
    assert set(writes) == {files[key].name for key in files if key not in stored_keys}
    assert len(writes) == 3 - len(stored_keys)
    assert vault.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == 3
    assert vault.execute("SELECT count(DISTINCT source_id) FROM import_run WHERE status='succeeded'").fetchone()[0] == 3
    for src in stored:
        assert sources.source_file(vault, src).read_bytes() == original_bytes[src.id]


def test_vcf_refusal_explains_unsupported_format(tmp_path):
    path = tmp_path / "synthetic.vcf"
    path.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    with pytest.raises(tools.ToolError, match="VCF") as error:
        ops._import({"path": str(path)})
    assert str(error.value) == ("not a recognised file: expected 23andMe, AncestryDNA, MyHeritage, FamilyTreeDNA "
                                "or Living DNA raw data, a Promethease report, an Apple Health export.zip, "
                                "Health Auto Export JSON or FHIR JSON. Whole-genome VCF files are not supported yet.")
