"""0.4.2: topic checks see one plain space between words, whatever whitespace or invisible characters the text used.

Synthetic data only.
"""
import json

import pytest

from caduceus.core import search
from caduceus.core.policy import Category, Policy, categorize, plain_text, text_visible
from tests.test_v040_reports import _bundle, _fetch, _import, _report

SEPARATORS = [" ", "  ", "\t", "\n", "\r\n", " ", "　", " ", " ­"]
CANCER = Policy(hidden=frozenset({Category.CANCER_RISK}))


@pytest.mark.parametrize("sep", SEPARATORS)
def test_phrases_match_across_any_whitespace(sep):
    for phrase in (f"Hereditary breast{sep}cancer letter", f"Lynch{sep}syndrome"):
        assert not text_visible(CANCER, phrase)
        assert categorize(text=phrase) == Category.CANCER_RISK


@pytest.mark.parametrize("word", ["can­cer", "can​cer", "can‍cer", "﻿cancer", "ｃａｎｃｅｒ"])
def test_invisible_characters_and_full_width_letters_do_not_split_a_word(word):
    assert not text_visible(CANCER, f"breast {word}")
    assert plain_text(f"breast {word}") == "breast cancer"


def test_each_field_is_checked_on_its_own():
    hidden = Policy(hidden=frozenset({Category.MENTAL_HEALTH}))
    assert not text_visible(hidden, "Bone marrow", "Depression screening")
    # Two spaces no longer defeat the exclusion: bone marrow depression is a blood finding, not mental health.
    assert text_visible(hidden, "Bone marrow  depression")
    assert text_visible(hidden) and text_visible(hidden, "", "Scan copy")


@pytest.mark.parametrize("sep", [" ", "  ", "\t"])
def test_attachment_title_with_odd_spacing_withholds_report(vault, tmp_path, sep):
    title = f"Hereditary breast{sep}cancer letter"
    report = _report(code={"text": "Alzheimer risk assessment"},
                      presentedForm=[{"contentType": "application/pdf", "title": title}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    assert _fetch(vault)["attachments"][0]["title"] == title
    fetched = _fetch(vault, policy=CANCER)
    assert fetched["withheld"] is True and "cancer" not in json.dumps(fetched)
    assert search.search(vault, "Alzheimer risk", policy=CANCER, kinds=["report"])["withheld"] == 1
    assert search.browse(vault, policy=CANCER, kinds=["report"])["withheld"] == 1


@pytest.mark.parametrize("field,value", [
    ("series", [{"uid": "1.2", "bodySite": {"display": "Breast cancer site"}}]),
    ("modality", [{"code": "MG", "display": "Hereditary\tcancer screen"}]),
    ("status", "breast  cancer"),
])
def test_linked_imaging_words_with_odd_spacing_are_withheld(vault, tmp_path, field, value):
    scan = _report("ImagingStudy", "scan-one", **{field: value})
    assert _import(vault, tmp_path, _bundle(_report(imagingStudy=[{"reference": "urn:uuid:scan-one"}]), scan)).status == "succeeded"
    fetched = _fetch(vault, policy=CANCER)
    assert fetched["imaging"] == [] and fetched["withheld"] == 1 and "cancer" not in json.dumps(fetched).lower()


def test_report_status_naming_a_hidden_topic_withholds_report(vault, tmp_path):
    """Malformed FHIR can put words where the status code belongs; fetch returns the status, so it is checked."""
    assert _import(vault, tmp_path, _bundle(_report(status="breast cancer"))).status == "succeeded"
    assert _fetch(vault)["status"] == "breast cancer"
    fetched = _fetch(vault, policy=CANCER)
    assert fetched["withheld"] is True and "status" not in fetched


def test_neutral_report_with_normal_status_is_unchanged(vault, tmp_path):
    report = _report(presentedForm=[{"contentType": "application/pdf", "title": "Scan copy"}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    fetched = _fetch(vault, policy=Policy(level="standard"))
    assert fetched["status"] == "final" and fetched["attachments"][0]["title"] == "Scan copy"
