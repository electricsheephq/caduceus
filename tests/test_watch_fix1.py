"""Offline regressions for research-watch fix round 1; all records are synthetic."""
import io
import json
import re
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from caduceus.core.db import write_txn
from caduceus.research import watch
from caduceus.research.http import UrllibHttp
from test_watch import NOW, VolumeHttp
from test_watch_pairs import PairHttp, paper, persona


GENETICS = ('"variant" OR "variants" OR "polymorphism" OR "polymorphisms" OR '
            '"genotype" OR "genotypes" OR "allele" OR "alleles" OR "pharmacogenetic" OR '
            '"pharmacogenetics" OR "pharmacogenomic" OR "pharmacogenomics" OR "genetic"')


def medication_events(vault, events):
    with write_txn(vault):
        for drug, event, day in events:
            vault.execute("INSERT INTO medication_event(drug,event,occurred_on,created_at,category) "
                          "VALUES (?,?,?,?,'medication')", (drug, event, day, NOW))


def test_codeine_pair_without_rsids_has_exact_title_abstract_query(vault):
    result = watch.run_watch(vault, persona(vault, drugs=("codeine",)), http=PairHttp(pair_rows=[]), now=NOW)
    queries = [q["params"]["query"] for q in result["queried"] if q["source"] == "europepmc-pairs"]
    assert queries == ['TITLE_ABS:("CYP2D6") AND TITLE_ABS:("codeine") AND '
                       f'TITLE_ABS:({GENETICS}) AND FIRST_PDATE:[2021-11-09 TO 2026-11-08] '
                       'AND (SRC:MED OR SRC:PPR)']
    assert result["errors"] == []


def test_linked_drug_without_genes_or_rsids_has_no_pair_query(vault, monkeypatch):
    w = persona(vault)
    pack = SimpleNamespace(entries=[{"gene": "", "drugs": ["semaglutide"], "markers": []}],
                           drugs={"semaglutide": "semaglutide"})
    monkeypatch.setattr(watch, "load_pack", lambda: pack)
    result = watch.run_watch(vault, w, http=PairHttp(), now=NOW)
    assert not any(q["source"] == "europepmc-pairs" for q in result["queried"])


@pytest.mark.parametrize("shared", [False, True])
def test_two_medications_interleave_newest_start_first_and_use_minimum_duplicate_rank(vault, shared):
    medication_events(vault, [("atorvastatin", "start", "2024-01-01"),
                              ("Ozempic", "start", "2026-01-01"),
                              ("atorvastatin", "dose_change", "2026-10-01")])

    class TwoDrugHttp(PairHttp):
        def get_json(self, url, params):
            data = super().get_json(url, params)
            if '"pharmacogenetic"' in params.get("query", ""):
                semaglutide = '"semaglutide"' in params["query"]
                ids = ([90000600, 90000601, 90000602] if semaglutide else
                       [90000602 if shared else 90000700, 90000701, 90000702])
                rows = [paper(p, "Synthetic shared genetics") for p in ids]
                return {"hitCount": len(rows), "resultList": {"result": rows}}
            return data

    result = watch.run_watch(vault, watch.default_watch(vault), http=TwoDrugHttp(), now=NOW)
    # A shared paper is semaglutide #3 but atorvastatin #1: the latter rank wins,
    # even though all of semaglutide's rows are stored first.
    assert [r["pmid"] for r in result["new_items"]] == ["90000600", "90000602" if shared else "90000700", "90000601"]
    queries = [q["params"]["query"] for q in result["queried"] if q["source"] == "europepmc-pairs"]
    assert len(queries) == 2
    assert '"semaglutide"' in queries[0] and '"atorvastatin"' in queries[1]
    if shared:
        assert result["new_items"][1]["why"]["drugs"] == ["atorvastatin", "semaglutide"]


def test_medication_start_ties_are_alphabetical_and_restarts_use_latest_start(vault):
    medication_events(vault, [("Ozempic", "start", "2023-01-01"),
                              ("Ozempic", "stop", "2024-01-01"),
                              ("Ozempic", "start", "2026-01-01"),
                              ("atorvastatin", "start", "2026-01-01"),
                              ("codeine", "start", "2025-01-01")])
    w = watch.default_watch(vault)
    assert json.loads(w["terms_json"])["drugs"] == ["atorvastatin", "semaglutide", "codeine"]


@pytest.mark.parametrize("title, expected", [
    ("Synthetic c.681G&gt;A; a < b", "Synthetic c.681G>A; a < b"),
    ("&lt;i&gt;CYP2C19&lt;/i&gt; &lt;SPAN class='x'&gt;variant&lt;/SPAN&gt;", "CYP2C19 variant"),
    ("<i>GLP1R</i><br/> <b>x</b><em>x</em><strong>x</strong><sup>2</sup>"
     "<sub>1</sub><u>x</u><small>x</small> <custom>x</custom>", "GLP1R xxx21xx <custom>x</custom>"),
])
def test_publication_title_unescapes_and_strips_only_inline_tags(title, expected):
    assert watch._publication(paper(90000800, title))["title"] == expected


def test_publication_summary_strips_raw_tags_before_unescape_and_truncation():
    raw = ("<h4>Summary</h4><p>&lt;i&gt;CYP2C19&lt;/i&gt; c.681G&gt;A; a &lt; b; "
           "&lt;sup&gt;2&lt;/sup&gt;</p>" + "&amp;" * 700)
    result = watch._publication(paper(90000801, "Synthetic", abstract=raw))
    assert result["summary"] == ("SummaryCYP2C19 c.681G>A; a < b; 2" + "&" * 700)[:600]


def test_ext_metadata_failure_is_attributed_to_ext_source(vault):
    class FailedExtHttp(PairHttp):
        def get_json(self, url, params):
            data = super().get_json(url, params)
            if params.get("query", "").startswith("EXT_ID:"):
                raise ValueError("synthetic metadata failure")
            return data

    http = FailedExtHttp(pmids={"rs10305420": [90000900]})
    result = watch.run_watch(vault, persona(vault), http=http, now=NOW)
    assert result["errors"] == [{"source": "europepmc-ext", "error": "ValueError"}]


def test_ext_metadata_budget_skip_after_physical_retries_is_visible(vault):
    fake = PairHttp(pmids={"rs10305420": [90000900]})

    class RetryOpener:
        attempts = 0

        def open(self, request, timeout):
            if "litvar2-api" in request.full_url:
                self.attempts += 1
                if self.attempts <= 2:
                    raise URLError("synthetic retry")
            params = {k: v[0] for k, v in parse_qs(urlsplit(request.full_url).query, keep_blank_values=True).items()}
            return io.BytesIO(json.dumps(fake.get_json(request.full_url, params)).encode())

    http = UrllibHttp(sleep=fake.sleep, monotonic=fake.monotonic)
    http._opener = RetryOpener()
    result = watch.run_watch(vault, persona(vault), http=http, now=NOW, budget=8)
    assert {"source": "europepmc-ext", "pmids": 1} in result["skipped"]
    assert not any(q["params"].get("query", "").startswith("EXT_ID:") for q in result["queried"])
    assert result["errors"] == [] and http.request_count <= 8


def test_realistic_volume_with_semaglutide_pairs_and_ext_metadata_within_budget(vault):
    class MedicationVolumeHttp(VolumeHttp):
        def get_json(self, url, params):
            query = params.get("query", "")
            if '"pharmacogenetic"' in query or query.startswith("EXT_ID:"):
                self.requests.append((url, dict(params)))
                rows = ([paper(94000000, "Synthetic &lt;i&gt;GLP1R&lt;/i&gt; semaglutide genetics")]
                        if not query.startswith("EXT_ID:") else
                        [paper(p, "Synthetic GLP1R variant") for p in re.findall(r"\d+", query)])
                return {"hitCount": len(rows), "resultList": {"result": rows}}
            return super().get_json(url, params)

    http = MedicationVolumeHttp()
    result = watch.run_watch(vault, persona(vault), http=http, now=NOW, budget=30)
    assert result["kind"] == "seed" and result["errors"] == []
    assert result["new_items"][0]["title"] == "Synthetic GLP1R semaglutide genetics"
    assert result["new_items"][0]["why"]["reason"] == "gene_drug"
    assert len(result["queried"]) <= 30
    pairs = [q for q in result["queried"] if q["source"] == "europepmc-pairs"]
    assert len(pairs) == 1 and pairs[0]["params"]["query"].startswith("TITLE_ABS:")
    metadata = [q for q in result["queried"] if q["params"].get("query", "").startswith("EXT_ID:")]
    assert len(metadata) == 1
    assert re.findall(r"\d+", metadata[0]["params"]["query"]) == [str(p) for p in range(93001122, 93001097, -1)]
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 128
