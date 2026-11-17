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
