"""0.4.2: topic checks see one plain space between words, whatever whitespace or invisible characters the text used.

Synthetic data only.
"""
import json

import pytest

from caduceus.core import search
from caduceus.core.policy import Category, Policy, categorize, plain_text, text_visible
from tests.test_v040_reports import _bundle, _fetch, _import, _report

SEPARATORS = ["\u00a0", "  ", "\t", "\n", "\r\n", "\u202f", "\u3000", "\u2028", " \u00ad"]
CANCER = Policy(hidden=frozenset({Category.CANCER_RISK}))


@pytest.mark.parametrize("sep", SEPARATORS)
def test_phrases_match_across_any_whitespace(sep):
    for phrase in (f"Hereditary breast{sep}cancer letter", f"Lynch{sep}syndrome"):
        assert not text_visible(CANCER, phrase)


@pytest.mark.parametrize("word", ["can\u00adcer", "can\u200bcer", "can\u200dcer", "\ufeffcancer", "\uff43\uff41\uff4e\uff43\uff45\uff52"])
def test_invisible_characters_and_full_width_letters_do_not_split_a_word(word):
    assert not text_visible(CANCER, f"breast {word}")
    assert plain_text(f"breast {word}") == "breast cancer"


def test_each_field_is_checked_on_its_own():
    hidden = Policy(hidden=frozenset({Category.MENTAL_HEALTH}))
    assert not text_visible(hidden, "Bone marrow", "Depression screening")
    # The stored text is read too, so odd spacing never loosens a check: as in 0.4.1 this stays withheld.
    assert not text_visible(hidden, "Bone marrow  depression")
    assert text_visible(hidden, "Bone marrow depression")
    assert text_visible(hidden) and text_visible(hidden, "", "Scan\u00a0copy")


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\u2028"])
def test_a_line_break_never_loosens_a_check(sep):
    """Collapsing a line break must not let one line's last word switch off a topic on the next line."""
    hidden = Policy(hidden=frozenset({Category.MENTAL_HEALTH}))
    for prefix in ("Biopsy of bone marrow", "ECG showed ST", "Respiratory"):
        note = f"{prefix}{sep}Depression is worse this week"
        assert not text_visible(hidden, note)
        assert categorize(text=note) == Category.MENTAL_HEALTH  # the stored text names it, as in 0.4.1
    assert text_visible(hidden, "Bone marrow depression after chemotherapy")


@pytest.mark.parametrize("sep", ["\u00a0", "  ", "\t"])
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
    ("series", [{"uid": "1.2", "bodySite": {"display": "Breast\u00a0cancer site"}}]),
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
    report = _report(presentedForm=[{"contentType": "application/pdf", "title": "Scan\u00a0copy"}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    fetched = _fetch(vault, policy=Policy(level="standard"))
    assert fetched["status"] == "final" and fetched["attachments"][0]["title"] == "Scan\u00a0copy"


@pytest.mark.parametrize("text", ["BRCA1\u00b9 sequencing", "BRCA1\u200bpositive", "BRCA1\u2075", "breast\u200bcancer",
                                  "Lynch\u200dsyndrome", "APOE4\u00b2 carrier note"])
def test_normalizing_never_removes_a_match(text):
    """Folding can glue a boundary away (BRCA1 + superscript 1 = BRCA11); the stored text is always read as well."""
    hidden = Policy(hidden=frozenset({Category.CANCER_RISK, Category.ALZHEIMERS}))
    assert not text_visible(hidden, text)


def test_superscript_after_a_gene_in_an_attachment_title_withholds_report(vault, tmp_path):
    report = _report(code={"text": "APOE assessment"},
                      presentedForm=[{"contentType": "application/pdf", "title": "BRCA1\u00b9 sequencing"}])
    assert _import(vault, tmp_path, _bundle(report)).status == "succeeded"
    fetched = _fetch(vault, policy=CANCER)
    assert fetched["withheld"] is True and "BRCA1" not in json.dumps(fetched)


CORPUS = ["Bone marrow  depression", "ST\u00a0depression on ECG", "BRCA1\u00b9", "breast\tcancer", "Scan copy",
          "marrow\ndepression", "APC resistance", "\uff21\uff30\uff2f\uff25", "attention de\ufb01cit", "can\u00adcer screening",
          "Respiratory\u2028Depression", "PALB2-related", "carrier\u200b status", "Normal\u3000result",
          "BRCA1\u00b9 pathogenic variant", "TP53\u2122 panel", "MSH2\u2460 result", "Panel BRCA1\u200bBRCA2",
          "non\u200bcarrier", "x\u200bPTSD", "IVF\u2122", "anxiety\u0301 score", "Lynch syndrome\u0301",
          "bone marrow\tdepression", "bone marrow\x1fdepression", "Gene: BRCA2\u1d43"]


@pytest.mark.parametrize("text", CORPUS)
def test_never_looser_than_matching_the_stored_text(text):
    """Anything a pattern finds in the text as stored (all 0.4.1 checked) is still withheld."""
    from caduceus.core.policy import SENSITIVE, _GENE_TEXT_CATEGORY, _TEXT_CATEGORY

    raw = {cat for pattern, cat in (*_GENE_TEXT_CATEGORY, *_TEXT_CATEGORY) if pattern.search(text)}
    for category in raw:
        assert not text_visible(Policy(hidden=frozenset({category})), text)
    assert raw <= set(SENSITIVE)
    if raw:  # and the import-time category is one the stored text names, as in 0.4.1
        assert categorize(text=text) in raw


def test_import_categories_are_unchanged_from_0_4_1():
    """0.4.2 changes only read-time checks. Import-time categories still read the stored text alone (issue #50):
    a normalized match would pre-empt the clinical-code and medication-name fallbacks that follow categorize."""
    assert categorize(text="Depression; breast\u200bcancer") == Category.MENTAL_HEALTH
    assert categorize(text="breast cancer and \uff21\uff30\uff2f\uff25 assessment") == Category.CANCER_RISK
    assert categorize(text="\uff21\uff30\uff2f\uff25 assessment") == Category.GENERAL
    assert categorize(text="Normal\u00a0result; breast\u00a0cancer") == Category.GENERAL


def test_clinical_code_still_decides_when_the_stored_text_names_no_topic(vault, tmp_path):
    from caduceus.health.fhir import import_fhir
    from caduceus.core import sources

    condition = {"resourceType": "Condition", "id": "coded", "onsetDateTime": "2024-01-02",
                 "code": {"text": "\uff21\uff30\uff2f\uff25 assessment",
                          "coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "F32.9"}]}}
    path = tmp_path / "coded.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": condition}]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"
    assert vault.execute("SELECT category FROM condition").fetchone()[0] == "mental_health"
    hidden = Policy(hidden=frozenset({Category.MENTAL_HEALTH}))
    assert search.browse(vault, policy=hidden, kinds=["condition"])["results"] == []


def test_condition_keeps_the_topic_the_stored_text_names(vault, tmp_path):
    from caduceus.health.fhir import import_fhir
    from caduceus.core import sources

    condition = {"resourceType": "Condition", "id": "synthetic-condition",
                 "code": {"text": "Depression; breast\u200bcancer"}, "onsetDateTime": "2024-01-02"}
    path = tmp_path / "condition.json"
    path.write_text(json.dumps({"resourceType": "Bundle", "entry": [{"resource": condition}]}))
    assert import_fhir(vault, sources.store_source(vault, path, "fhir")[0]).status == "succeeded"
    assert vault.execute("SELECT category FROM condition").fetchone()[0] == "mental_health"
    hidden = Policy(hidden=frozenset({Category.MENTAL_HEALTH}))
    found = search.search(vault, "Depression", policy=hidden, kinds=["condition"])
    assert found["results"] == [] and found["withheld"] == 1
