"""PR #44: preserve deduplicated links, fetch snapshots and private attachments."""
import copy
import json

import pytest

from caduceus.core import db, search
from caduceus.core.policy import Policy
from caduceus.health import fhir
from tests.test_v040_reports import _attachment, _bundle, _fetch, _import, _observation, _report


def _assert_hidden(vault):
    policy = Policy(level="standard")
    assert _fetch(vault, policy=policy)["withheld"] is True
    assert search.search(vault, "repeatedvalue", kinds="report", policy=policy)["results"] == []
    assert search.browse(vault, kinds="report", policy=policy)["results"] == []


@pytest.mark.parametrize("report_first", [False, True])
def test_deduplicated_urn_ids_withhold_repeated_value(vault, report_first):
    observations = [_observation(rid, "44261-6", 987654) for rid in ("first", "second")]
    report = _report(conclusion="repeatedvalue 987654", result=[{"reference": "Observation/second"}])
    resources = [report, *observations] if report_first else [*observations, report]
    fhir.ingest(vault, _bundle(*resources), None)
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    _assert_hidden(vault)
    row = vault.execute("SELECT raw_json FROM observation").fetchone()
    assert json.loads(row[0])["_caduceus"]["typed_ids"] == ["Observation/first", "Observation/second"]
    if not report_first:
        assert vault.execute("SELECT category FROM clinical_report").fetchone()[0] == "mental_health"
    # Withdrawing one assertion must leave privacy aliases on the surviving row.
    fhir.ingest(vault, _bundle({**observations[1], "status": "entered-in-error"}), None)
    meta = json.loads(vault.execute("SELECT raw_json FROM observation").fetchone()[0])["_caduceus"]
    assert meta["resource_ids"] == ["urn:uuid:first"]
    assert meta["typed_ids"] == ["Observation/first", "Observation/second"]
    _assert_hidden(vault)


@pytest.mark.parametrize("legacy", [False, True])
def test_deduplicated_observation_remembers_all_sources(vault, tmp_path, legacy):
    observation = _observation("q", "2345-7", 90)
    first = _import(vault, tmp_path, _bundle(observation), "a.json")
    if legacy:
        vault.execute("UPDATE observation SET raw_json=json_remove(raw_json,'$._caduceus.source_ids')")
    # The report comes first to exercise refresh of an already populated call-local index.
    report = _report(result=[{"reference": "Observation/q"}])
    second = _import(vault, tmp_path, _bundle(report, observation), "b.json")
    assert second.status == "succeeded" and second.counts["observation"] == 0
    assert _fetch(vault)["results"][0]["value_num"] == 90
    row = vault.execute("SELECT source_id,raw_json FROM observation").fetchone()
    assert row["source_id"] == first.source_id
    assert json.loads(row["raw_json"])["_caduceus"]["source_ids"] == sorted([first.source_id, second.source_id])


@pytest.mark.parametrize("kind", ["Observation", "ImagingStudy"])
def test_fetch_skips_link_deleted_between_resolve_and_read(vault, monkeypatch, kind):
    linked = _observation("q", "2345-7", 90) if kind == "Observation" else _report("ImagingStudy", "q")
    field = "result" if kind == "Observation" else "imagingStudy"
    fhir.ingest(vault, _bundle(linked, _report(**{field: [{"reference": "urn:uuid:q"}]})), None)
    resolve = fhir.ReportResolver.resolve
    removed = []

    def deleting_resolve(self, references, **kwargs):
        rows = resolve(self, references, **kwargs)
        if not kwargs.get("broad") and rows and kwargs.get("resource_type") == kind:
            table = "observation" if kind == "Observation" else "clinical_report"
            self.vault.execute(f"DELETE FROM {table} WHERE id=?", (rows[0]["id"],))
            removed.append(rows[0]["id"])
        return rows

    monkeypatch.setattr(fhir.ReportResolver, "resolve", deleting_resolve)
    fetched = _fetch(vault)
    assert removed
    assert fetched["results" if kind == "Observation" else "imaging"] == []
    assert not vault.in_transaction


@pytest.mark.parametrize("already_open", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_fetch_transaction_ownership_and_finally(vault, monkeypatch, already_open, fail):
    fhir.ingest(vault, _report(), None)
    if already_open:
        vault.execute("BEGIN")
    visible, payload = search._visible, search._fetch_report
    seen = []

    def check_visibility(*args, **kwargs):
        seen.append("visibility")
        assert vault.in_transaction
        return visible(*args, **kwargs)

    def check_payload(*args, **kwargs):
        seen.append("payload")
        assert vault.in_transaction
        if fail:
            raise RuntimeError("synthetic payload failure")
        return payload(*args, **kwargs)

    monkeypatch.setattr(search, "_visible", check_visibility)
    monkeypatch.setattr(search, "_fetch_report", check_payload)
    try:
        if fail:
            with pytest.raises(RuntimeError, match="synthetic payload failure"):
                _fetch(vault)
        else:
            assert _fetch(vault)["title"] == "Synthetic report"
        assert seen == ["visibility", "payload"]
        assert vault.in_transaction is already_open
    finally:
        if already_open:
            vault.rollback()


def test_fetch_uses_one_snapshot_during_concurrent_write(vault, monkeypatch, data_dir):
    fhir.ingest(vault, _bundle(_observation("q", "2345-7", 90),
                              _report(result=[{"reference": "urn:uuid:q"}])), None)
    writer = db.open_vault(data_dir / "vault.db")
    visible = search._visible

    def write_after_check(*args, **kwargs):
        result = visible(*args, **kwargs)
        writer.execute("UPDATE observation SET value_num=987654,category='mental_health'")
        writer.execute("UPDATE clinical_report SET conclusion='changed snapshot'")
        return result

    monkeypatch.setattr(search, "_visible", write_after_check)
    try:
        fetched = _fetch(vault, policy=Policy(level="standard"))
        assert fetched["conclusion"] != "changed snapshot"
        assert fetched["results"][0]["value_num"] == 90
        assert not vault.in_transaction
        assert vault.execute("SELECT category FROM observation").fetchone()[0] == "mental_health"
        assert _fetch(vault, policy=Policy(level="standard"))["withheld"] is True
    finally:
        writer.close()


def test_nested_attachments_and_binary_bytes_are_stripped_without_decoding(vault):
    nested = _attachment("nestedprivateword")
    binary = _attachment("binaryprivateword")["data"]
    resource = _report("Composition", contained=[
        {"resourceType": "DocumentReference", "content": [{"attachment": nested}]},
        {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Binary", "data": binary}}]}],
        section=[{"title": "Visible section", "extension": [
            {"valueAttachment": {key: "metadata", "data": nested["data"]}}
            for key in ("contentType", "url", "title", "size")]}])
    original = copy.deepcopy(resource)
    fhir.ingest(vault, _bundle(resource), None)
    raw = vault.execute("SELECT raw_json FROM clinical_report").fetchone()[0]
    assert nested["data"] not in raw and binary not in raw
    assert resource == original
    assert "nestedprivateword" not in _fetch(vault)["narrative"]
    assert "binaryprivateword" not in _fetch(vault)["narrative"]
    assert not search.search(vault, "nestedprivateword", policy=Policy())["results"]


def test_correction_replacing_merged_row_keeps_typed_ids(vault):
    # Review of the round-3 delta: a correction that replaces a merged row keeps the replaced copies' typed ids, so a
    # report pointing at the other duplicate stays withheld (stored first, then the report arrives later).
    observations = [_observation(rid, "44261-6", 987654) for rid in ("first", "second")]
    fhir.ingest(vault, _bundle(*observations), None)
    fhir.ingest(vault, _bundle(_observation("second", "44261-6", 111, status="corrected")), None)
    typed = set()
    for (raw,) in vault.execute("SELECT raw_json FROM observation"):
        typed.update(json.loads(raw)["_caduceus"].get("typed_ids", []))
    assert {"Observation/first", "Observation/second"} <= typed
    report = _report(conclusion="repeatedvalue 987654", result=[{"reference": "Observation/first"}])
    fhir.ingest(vault, _bundle(report), None)
    _assert_hidden(vault)
