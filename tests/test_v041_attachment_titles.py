"""0.4.1: a report's attachment titles get the same every-topic check as its own words. Synthetic data only."""
import json

import pytest

from caduceus.core import search
from caduceus.core.policy import Category, Policy
from tests.test_v040_reports import _bundle, _fetch, _import, _report

TITLE = "Notes on anxiety during pregnancy"


@pytest.mark.parametrize("hidden", [Category.REPRODUCTIVE, Category.MENTAL_HEALTH])
def test_attachment_title_naming_two_topics_withholds_report_when_either_is_hidden(vault, tmp_path, hidden):
    report = _report(presentedForm=[{"contentType": "application/pdf", "title": TITLE}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    assert TITLE in json.dumps(_fetch(vault))
    policy = Policy(hidden=frozenset({hidden}))
    fetched = _fetch(vault, policy=policy)
    assert fetched["withheld"] is True and TITLE not in json.dumps(fetched)
    found = search.search(vault, "Synthetic report", policy=policy, kinds=["report"])
    assert found["results"] == [] and found["withheld"] == 1
    listed = search.browse(vault, policy=policy, kinds=["report"])
    assert listed["results"] == [] and listed["withheld"] == 1


def test_neutral_attachment_title_is_unchanged(vault, tmp_path):
    report = _report(presentedForm=[{"contentType": "application/pdf", "title": "Scan copy"}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    policy = Policy(hidden=frozenset({Category.REPRODUCTIVE}))
    assert "Scan copy" in json.dumps(_fetch(vault, policy=policy))


def _stored_attachments(vault, value):
    """Overwrite the stored attachment metadata, as a damaged or hand-edited vault might hold it."""
    row = vault.execute("SELECT id, raw_json FROM clinical_report WHERE resource_type='DiagnosticReport'").fetchone()
    document = json.loads(row["raw_json"])
    document["_caduceus"]["attachments"] = value
    vault.execute("UPDATE clinical_report SET raw_json=? WHERE id=?", (json.dumps(document), row["id"]))


def _withheld_everywhere(vault, policy, words):
    fetched = _fetch(vault, policy=policy)
    assert fetched["withheld"] is True and words not in json.dumps(fetched)
    assert search.search(vault, "Synthetic report", policy=policy, kinds=["report"])["withheld"] == 1
    assert search.browse(vault, policy=policy, kinds=["report"])["withheld"] == 1


def test_attachment_content_type_naming_a_hidden_topic_withholds_report(vault, tmp_path):
    content_type = 'application/pdf; name="pregnancy.pdf"'
    report = _report(presentedForm=[{"contentType": content_type, "title": "Scan copy"}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    assert "pregnancy.pdf" in json.dumps(_fetch(vault))
    _withheld_everywhere(vault, Policy(hidden=frozenset({Category.REPRODUCTIVE})), "pregnancy.pdf")


@pytest.mark.parametrize("value", [{"title": TITLE}, {TITLE: "Scan copy"}, [[{"title": TITLE}]], [TITLE]])
def test_attachment_metadata_of_any_shape_is_checked(vault, tmp_path, value):
    assert _import(vault, tmp_path, _bundle(_report(presentedForm=[{"title": "Scan copy"}]))).status == "succeeded"
    _stored_attachments(vault, value)
    _withheld_everywhere(vault, Policy(hidden=frozenset({Category.REPRODUCTIVE})), TITLE)


@pytest.mark.parametrize("value", [17, None, True, [17, None]])
def test_attachment_metadata_without_words_does_not_crash(vault, tmp_path, value):
    assert _import(vault, tmp_path, _bundle(_report(presentedForm=[{"title": "Scan copy"}]))).status == "succeeded"
    _stored_attachments(vault, value)
    policy = Policy(hidden=frozenset({Category.REPRODUCTIVE}))
    assert _fetch(vault, policy=policy).get("withheld") == 0
    assert len(search.search(vault, "Synthetic report", policy=policy, kinds=["report"])["results"]) == 1
    assert len(search.browse(vault, policy=policy, kinds=["report"])["results"]) == 1


@pytest.mark.parametrize("field,value", [
    ("series", [{"uid": "1.2", "bodySite": {"display": "Pregnant uterus"}}]),
    ("modality", [{"code": "US", "display": "Pregnancy ultrasound"}]),
])
def test_linked_imaging_metadata_naming_a_hidden_topic_is_withheld(vault, tmp_path, field, value):
    scan = _report("ImagingStudy", "scan-one", **{field: value})
    report = _report(imagingStudy=[{"reference": "urn:uuid:scan-one"}])
    assert _import(vault, tmp_path, _bundle(report, scan)).status == "succeeded"
    words = value[0].get("bodySite", value[0])["display"]
    assert words in json.dumps(_fetch(vault))
    fetched = _fetch(vault, policy=Policy(hidden=frozenset({Category.REPRODUCTIVE})))
    assert fetched["imaging"] == [] and fetched["withheld"] == 1 and words not in json.dumps(fetched)



@pytest.mark.parametrize("titles", [["Bone marrow", "Depression screening"], ["Respiratory", "Depression screening"]])
def test_each_attachment_title_is_checked_on_its_own(vault, tmp_path, titles):
    """Joined with spaces, one title's last word can hide the next title's topic from a lookbehind."""
    report = _report(presentedForm=[{"contentType": "application/pdf", "title": t} for t in titles])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    _withheld_everywhere(vault, Policy(hidden=frozenset({Category.MENTAL_HEALTH})), "Depression screening")


def test_linked_imaging_words_are_checked_field_by_field(vault, tmp_path):
    scan = _report("ImagingStudy", "scan-one", description="Bone marrow",
                   series=[{"uid": "1.2", "bodySite": {"display": "Depression screening"}}])
    assert _import(vault, tmp_path, _bundle(_report(imagingStudy=[{"reference": "urn:uuid:scan-one"}]), scan)).status == "succeeded"
    fetched = _fetch(vault, policy=Policy(hidden=frozenset({Category.MENTAL_HEALTH})))
    assert fetched["imaging"] == [] and fetched["withheld"] == 1
