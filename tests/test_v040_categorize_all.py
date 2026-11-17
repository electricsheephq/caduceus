"""U6 multi-topic privacy regressions. All records are generated synthetic data."""
import json
from datetime import date

import pytest

from caduceus import brief, ops
from caduceus.core import policy, search, sources
from caduceus.core.db import write_txn
from caduceus.genome.dtc import import_dtc
from caduceus.genome.promethease import import_promethease
from caduceus.genome.report import build_report
from caduceus.health.fhir import ingest
from synth.dtc import make_23andme
from synth.promethease import write_report


WORDS = "My anxiety got worse during pregnancy"
DAY = "2001-02-03"
HIDDEN = policy.Policy(hidden=frozenset({policy.Category.REPRODUCTIVE}))


@pytest.fixture
def dna(vault, tmp_path):
    path = make_23andme(tmp_path / "synthetic.txt", n=2,
                        inject={"rs429358": "TT", "rs7412": "CC"})
    src, _ = sources.store_source(vault, path, "dtc")
    assert import_dtc(vault, src).status == "succeeded"
    return vault


def _hide(ctx):
    ctx.config.update(privacy_level="open", hidden_categories=["reproductive"])


def _read(ctx, kind, ref, surface):
    if surface == "fetch":
        return ops.search_tool(ctx, {"ref": ref})
    args = {"kinds": [kind]}
    if surface != "browse":
        args["query"] = surface
    return ops.search_tool(ctx, args)


def _withheld(reply):
    assert reply["withheld"] == 1
    assert not reply.get("results")
    text = json.dumps({key: value for key, value in reply.items() if key != "query"})
    assert all(word not in text for word in ("anxiety", "pregnancy", WORDS, DAY))


@pytest.mark.parametrize("text,expected,first", [
    (WORDS, {"mental_health", "reproductive"}, "mental_health"),
    ("Synthetic anxiety", {"mental_health"}, "mental_health"),
    ("Synthetic ordinary words", set(), "general"),
    ("APOE BRCA2 anxiety pregnancy carrier", set(policy.SENSITIVE), "alzheimers"),
    ("APC resistance and ST depression", set(), "general"),
])
def test_sensitive_categories_preserves_categorize(text, expected, first):
    assert policy.categorize(text=text) == first
    found = policy.sensitive_categories(text)
    assert isinstance(found, frozenset) and found == expected


@pytest.mark.parametrize("surface", ["capture", "pregnancy", "anxiety", "browse", "fetch"])
def test_journal_all_topics_on_every_read(fake_ctx, vault, surface):
    _hide(fake_ctx)
    saved = ops.capture_tool(fake_ctx, {"op": "journal", "text": WORDS, "date": DAY})
    row = vault.execute("SELECT * FROM journal").fetchone()
    assert row["category"] == "mental_health"
    if surface == "capture":
        assert set(saved) == {"saved", "note"} and saved["saved"]
        assert DAY not in json.dumps(saved)
    else:
        ref = f"journal:{row['id']}"
        _withheld(_read(fake_ctx, "journal", ref, surface))
        fake_ctx.config["hidden_categories"] = []
        shown = _read(fake_ctx, "journal", ref, surface)
        assert WORDS in (shown["body"] if surface == "fetch" else
                         shown["results"][0]["snippet"].replace("[", "").replace("]", ""))


@pytest.mark.parametrize("marker", [None, "rs429358", "APOE"])
@pytest.mark.parametrize("surface", ["capture", "pregnancy", "anxiety", "browse", "fetch", "lookup", "gene"])
def test_phenotype_all_topics_on_every_read(fake_ctx, dna, marker, surface):
    _hide(fake_ctx)
    saved = ops.capture_tool(fake_ctx, {"op": "phenotype", "marker": marker, "trait": "Synthetic trait",
                                       "stance": "confirms", "text": WORDS})
    row = dna.execute("SELECT * FROM phenotype_note").fetchone()
    assert row["category"] == ("alzheimers" if marker else "mental_health")
    if surface == "capture":
        assert set(saved) == {"saved", "note"} and saved["saved"]
    elif surface in {"lookup", "gene"}:
        # Trait-only notes have no marker attachment; exercise their shared visibility reader directly.
        if marker is None:
            assert ops._phenotype_notes(dna, HIDDEN, [row]) == ([], 1)
        else:
            args = ({"op": "gene", "gene": "APOE"} if surface == "gene" or marker == "APOE" else
                    {"op": "lookup", "rsids": [marker]})
            hidden = ops.genome_tool(fake_ctx, args)
            assert hidden["withheld"] == 1 and WORDS not in json.dumps(hidden)
            fake_ctx.config["hidden_categories"] = []
            assert WORDS in json.dumps(ops.genome_tool(fake_ctx, args))
    else:
        ref = f"phenotype:{row['id']}"
        _withheld(_read(fake_ctx, "phenotype", ref, surface))
        fake_ctx.config["hidden_categories"] = []
        assert "Synthetic trait" in json.dumps(_read(fake_ctx, "phenotype", ref, surface))


@pytest.mark.parametrize("surface", ["capture", "query", "browse", "fetch", "brief", "pivot"])
def test_medication_notes_are_checked_on_read(fake_ctx, vault, samples, tmp_path, surface):
    _hide(fake_ctx)
    saved = ops.capture_tool(fake_ctx, {"op": "medication", "drug": "syntheticmedicine", "date": DAY,
                                       "text": WORDS, "confirm": True})
    row = vault.execute("SELECT * FROM medication_event").fetchone()
    assert row["category"] == "medication" and row["note"] == WORDS
    if surface == "capture":
        assert set(saved) == {"saved", "note"} and saved["saved"]
    elif surface == "brief":
        page = brief.write_brief(vault, samples, tmp_path / "brief", policy=HIDDEN).read_text()
        assert "syntheticmedicine" not in page and DAY not in page
        assert "1 item hidden by your privacy settings" in page
        page = brief.write_brief(vault, samples, tmp_path / "open", policy=policy.Policy()).read_text()
        assert "syntheticmedicine" in page.lower()
    elif surface == "pivot":
        assert ops._drug_start(vault, "syntheticmedicine", HIDDEN) == (None, 1)
        assert ops._drug_start(vault, "syntheticmedicine", policy.Policy()) == (date.fromisoformat(DAY), 0)
    else:
        ref = f"medication:user:{row['id']}"
        asked = "syntheticmedicine" if surface == "query" else surface
        _withheld(_read(fake_ctx, "medication", ref, asked))
        fake_ctx.config["hidden_categories"] = []
        assert "syntheticmedicine" in json.dumps(_read(fake_ctx, "medication", ref, asked))


@pytest.mark.parametrize("surface", ["anxiety", "pregnancy", "browse", "fetch"])
@pytest.mark.parametrize("field", ["title", "conclusion", "narrative"])
def test_report_words_are_checked_on_read(fake_ctx, vault, field, surface):
    resource = {"resourceType": "DiagnosticReport", "id": "synthetic-report", "status": "final",
                "code": {"text": WORDS if field == "title" else "Synthetic report"},
                "effectiveDateTime": DAY}
    if field == "conclusion":
        resource["conclusion"] = WORDS
    if field == "narrative":
        resource["text"] = {"div": f"<div>{WORDS}</div>"}
    assert ingest(vault, resource, None)["report"] == 1
    row = vault.execute("SELECT * FROM clinical_report").fetchone()
    assert row["category"] == "mental_health"
    _hide(fake_ctx)
    ref = f"report:{row['id']}"
    _withheld(_read(fake_ctx, "report", ref, surface))
    fake_ctx.config["hidden_categories"] = []
    shown = _read(fake_ctx, "report", ref, surface)
    assert WORDS in (shown["body"] if surface == "fetch" else
                     shown["results"][0]["title"] + " " +
                     shown["results"][0]["snippet"].replace("[", "").replace("]", ""))


def test_brief_questions_check_every_topic(fake_ctx, vault, samples, tmp_path):
    page = brief.write_brief(vault, samples, tmp_path / "hidden", policy=HIDDEN,
                             questions=[WORDS, "Synthetic ordinary question"]).read_text()
    assert WORDS not in page and "Synthetic ordinary question" in page
    assert "1 item hidden by your privacy settings" in page
    page = brief.write_brief(vault, samples, tmp_path / "open", policy=policy.Policy(),
                             questions=[WORDS]).read_text()
    assert WORDS in page


@pytest.mark.parametrize("kind", ["journal", "phenotype", "medication", "report"])
@pytest.mark.parametrize("surface", ["query", "browse", "fetch"])
def test_search_checks_full_document_not_snippet(vault, kind, surface):
    # The hidden word is beyond an FTS snippet, and the other topic is in the title.
    body = "Synthetic " + "ordinary " * 50 + "pregnancy"
    with write_txn(vault):
        if kind == "report":
            ingest(vault, {"resourceType": "DiagnosticReport", "id": "synthetic",
                           "code": {"text": "Synthetic"}, "effectiveDateTime": DAY}, None)
        search.index_doc(vault, kind=kind, ref="1", title="Synthetic anxiety", body=body,
                         category="mental_health", ts=DAY)
    ref = f"{kind}:1"
    if surface == "fetch":
        assert search.fetch(vault, ref, policy=HIDDEN)["withheld"] is True
    else:
        found = (search.search(vault, "Synthetic", kinds=kind, policy=HIDDEN) if surface == "query" else
                 search.browse(vault, kinds=kind, policy=HIDDEN))
        assert found["results"] == [] and found["withheld"] == 1


@pytest.mark.parametrize("kind", ["annotation", "assertion", "finding", "observation", "condition", "research"])
def test_curated_and_coded_kinds_keep_existing_visibility(vault, kind):
    # This control is deliberately green on the base: free-text filtering must not spread to coded kinds.
    with write_txn(vault):
        search.index_doc(vault, kind=kind, ref="synthetic", title="Synthetic finding", body=WORDS,
                         category="general", ts=DAY)
    assert search.search(vault, "pregnancy", kinds=kind, policy=HIDDEN)["results"]
    assert search.browse(vault, kinds=kind, policy=HIDDEN)["results"]
    assert search.fetch(vault, f"{kind}:synthetic", policy=HIDDEN)["body"] == WORDS


def test_variant_report_keeps_existing_category_rule(vault, tmp_path):
    path = write_report(tmp_path / "synthetic-report.zip", rows=[
        {"rsnum": "rs99999001", "geno": "(A;G)", "magnitude": 2,
         "genosummary": WORDS}], maps=({}, {}))
    src, _ = sources.store_source(vault, path, "promethease")
    assert import_promethease(vault, src).status == "succeeded"
    assert vault.execute("SELECT category FROM annotation_snapshot WHERE kind='snp'").fetchone()[0] == "mental_health"
    assert WORDS in build_report(vault, tmp_path / "rebuilt", policy=HIDDEN).read_text()


# Fix round 1: regressions against the reviewed, uncommitted U6 candidate.
@pytest.mark.parametrize("hidden", [False, True])
def test_fix1_browse_scans_only_hidden_categories(fake_ctx, vault, monkeypatch, hidden):
    with write_txn(vault):
        for index in range(1000):
            search.index_doc(vault, kind="journal", ref=str(index), title="Synthetic note",
                             body=WORDS, category="mental_health", ts=DAY)
    calls = []
    original = policy.sensitive_categories

    def counted(text, **kwargs):
        calls.append(kwargs.get("among"))
        return original(text, **kwargs)

    # The reviewed implementation imports the scanner directly in its consumers.
    for module in (policy, search, ops, brief):
        if hasattr(module, "sensitive_categories"):
            monkeypatch.setattr(module, "sensitive_categories", counted)
    if hidden:
        _hide(fake_ctx)
    result = ops.search_tool(fake_ctx, {"kinds": ["journal"]})
    assert calls == ([frozenset({policy.Category.REPRODUCTIVE})] * 1000 if hidden else [])
    assert result.get("withheld", 0) == (1000 if hidden else 0)


@pytest.mark.parametrize("category", sorted(policy.SENSITIVE))
def test_fix1_among_evaluates_only_requested_patterns(monkeypatch, category):
    evaluated = []

    class Counted:
        def __init__(self, pattern, cat):
            self.pattern, self.category = pattern, cat

        def search(self, text):
            evaluated.append(self.category)
            return self.pattern.search(text)

    monkeypatch.setattr(policy, "_TEXT_CATEGORY", tuple(
        (Counted(pattern, cat), cat) for pattern, cat in policy._TEXT_CATEGORY))
    if hasattr(policy, "_GENE_TEXT_CATEGORY"):
        monkeypatch.setattr(policy, "_GENE_TEXT_CATEGORY", tuple(
            (Counted(pattern, cat), cat) for pattern, cat in policy._GENE_TEXT_CATEGORY))
    words = "APOE BRCA2 anxiety pregnancy carrier"
    assert policy.sensitive_categories(words, among={category}) == {category}
    assert all(cat == category for cat in evaluated)
    evaluated.clear()
    assert policy.sensitive_categories(words, among=()) == frozenset()
    assert evaluated == []


def _fix1_medication(ctx, vault, origin):
    if origin == "user":
        ops.capture_tool(ctx, {"op": "medication", "drug": "warfarin", "text": WORDS,
                               "date": DAY, "confirm": True})
    else:
        resource = {"resourceType": "MedicationRequest", "id": "synthetic-warfarin", "status": "active",
                    "medicationCodeableConcept": {"text": "warfarin"}, "authoredOn": DAY,
                    origin: [{"text": WORDS}]}
        assert ingest(vault, resource, None)["medication"] == 1
    return vault.execute("SELECT * FROM medication_event").fetchone()


@pytest.mark.parametrize("field", ["note", "reasonCode"])
@pytest.mark.parametrize("surface", ["query", "browse", "fetch", "brief", "pivot", "trend"])
def test_fix1_fhir_medication_words_on_every_read(fake_ctx, vault, samples, tmp_path, field, surface):
    row = _fix1_medication(fake_ctx, vault, field)
    assert row["category"] == "medication" and WORDS not in row["note"]
    assert WORDS in vault.execute("SELECT body FROM search_doc WHERE kind='medication'").fetchone()[0]
    _hide(fake_ctx)
    if surface == "brief":
        page = brief.write_brief(vault, samples, tmp_path / "hidden", policy=HIDDEN).read_text()
        assert "warfarin" not in page.lower() and DAY not in page
        assert "1 item hidden by your privacy settings" in page
        assert "warfarin" in brief.write_brief(
            vault, samples, tmp_path / "open", policy=policy.Policy()).read_text().lower()
    elif surface == "pivot":
        assert ops._drug_start(vault, "warfarin", HIDDEN) == (None, 1)
        assert ops._drug_start(vault, "warfarin", policy.Policy()) == (date.fromisoformat(DAY), 0)
    elif surface == "trend":
        reply = ops.trend_tool(fake_ctx, {"op": "compare", "metric": "weight", "drug": "warfarin"})
        assert reply["withheld"] == 1 and "pivot" not in reply
        assert "warfarin" not in json.dumps(reply).lower() and DAY not in json.dumps(reply)
        fake_ctx.config["hidden_categories"] = []
        assert ops.trend_tool(fake_ctx, {"op": "compare", "metric": "weight", "drug": "warfarin"})["pivot"] == DAY
    else:
        ref = f"medication:{row['id']}"
        asked = "warfarin" if surface == "query" else surface
        _withheld(_read(fake_ctx, "medication", ref, asked))
        fake_ctx.config["hidden_categories"] = []
        assert "warfarin" in json.dumps(_read(fake_ctx, "medication", ref, asked))


class _Fix1Http:
    """An offline public-paper shape that can produce a medication-specific variant reason."""
    def __init__(self):
        self.requests = []

    def get_json(self, url, params):
        self.requests.append((url, params))
        return {"hitCount": 1, "resultList": {"result": [{
            "id": "99990001", "pmid": "99990001", "title": "Synthetic VKORC1 rs9923231 study",
            "abstractText": "Synthetic evidence", "firstPublicationDate": "2026-11-01", "pubYear": "2026"}]}}


@pytest.mark.parametrize("origin", ["user", "note", "reasonCode"])
@pytest.mark.parametrize("surface", ["default", "run", "custom_run"])
def test_fix1_watch_excludes_hidden_medication_words(fake_ctx, vault, origin, surface):
    from caduceus.research.watch import default_watch, run_watch

    _fix1_medication(fake_ctx, vault, origin)
    # A stored watch made while everything was visible must be rechecked on the next run.
    watch = default_watch(vault)
    assert json.loads(watch["terms_json"])["drugs"] == ["warfarin"]
    allow = lambda category: policy.visible(HIDDEN, category, kind="medication")
    if surface == "default":
        hidden = default_watch(vault, allow=allow)
        assert json.loads(hidden["terms_json"])["drugs"] == []
        assert "warfarin" not in json.dumps(hidden).lower()
    else:
        with write_txn(vault):
            vault.execute("UPDATE watch SET sources_json='[\"europepmc\"]' WHERE id=?", (watch["id"],))
            if surface == "custom_run":
                vault.execute("UPDATE watch SET name='synthetic-custom' WHERE id=?", (watch["id"],))
        http = _Fix1Http()
        result = run_watch(vault, watch, http=http, now="2026-11-17", budget=8, allow=allow)
        assert result["new_items"] and result["errors"] == []
        assert all(item["why"]["drugs"] == [] for item in result["new_items"])
        assert "warfarin" not in json.dumps(result).lower()
        assert "warfarin" not in json.dumps(http.requests).lower()


def test_bot_previous_dose_naming_hidden_topic_is_not_echoed(fake_ctx, vault):
    _hide(fake_ctx)
    first = ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "dose": "5 mg during pregnancy",
                                        "date": DAY, "confirm": True})
    assert "pregnancy" not in json.dumps(first).lower()
    reply = ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "dose": "10 mg",
                                        "date": DAY, "confirm": True})
    assert reply["saved"] is True and "previous_dose" not in reply
    assert "pregnancy" not in json.dumps(reply).lower()
    fake_ctx.config["hidden_categories"] = []
    shown = ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "dose": "12 mg",
                                        "date": DAY, "confirm": True})
    assert shown["updated"] is True and shown["previous_dose"] == "10 mg"


def test_bot_hidden_stop_keeps_drug_out_of_watch(fake_ctx, vault):
    from caduceus.research.watch import _medications

    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "date": DAY, "confirm": True})
    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "event": "stop", "text": WORDS,
                                "date": "2001-03-03", "confirm": True})
    assert _medications(vault)[0] == []
    allow = lambda category: policy.visible(HIDDEN, category, kind="medication")
    current, hidden = _medications(vault, allow)
    assert current == [] and "warfarin" in hidden


def test_bot_unknown_hidden_drug_is_not_sent_from_custom_watch(fake_ctx, vault):
    from caduceus.research.watch import default_watch, normalize_drug, run_watch

    assert normalize_drug("Synthdrugol 500 mg") is None
    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "Synthdrugol 500 mg", "text": WORDS,
                                "date": DAY, "confirm": True})
    watch = default_watch(vault)
    terms = {**json.loads(watch["terms_json"]), "drugs": ["synthdrugol", "warfarin"]}
    with write_txn(vault):
        vault.execute("UPDATE watch SET name='synthetic-custom', sources_json='[\"europepmc\"]', terms_json=? "
                      "WHERE id=?", (json.dumps(terms), watch["id"]))
    allow = lambda category: policy.visible(HIDDEN, category, kind="medication")
    http = _Fix1Http()
    run_watch(vault, watch, http=http, now="2026-11-17", budget=8, allow=allow)
    sent = json.dumps(http.requests).lower()
    assert "synthdrugol" not in sent and "warfarin" in sent


def test_bot_hidden_later_start_is_counted_with_visible_pivot(fake_ctx, vault, samples):
    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "date": DAY, "confirm": True})
    ops.capture_tool(fake_ctx, {"op": "medication", "drug": "warfarin", "text": WORDS,
                                "date": "2001-05-05", "confirm": True})
    assert ops._drug_start(vault, "warfarin", HIDDEN) == (date.fromisoformat(DAY), 1)
    _hide(fake_ctx)
    reply = ops.trend_tool(fake_ctx, {"op": "compare", "metric": "weight", "drug": "warfarin"})
    assert reply["pivot"] == DAY and reply["withheld"] == 1
    assert "2001-05-05" not in json.dumps(reply)
