"""Lane R: synthetic pair-first digests, bounded requests and genotype-independent traffic."""
import json
import re
from urllib.parse import unquote

import pytest

from caduceus.core.contracts import Call, CallState
from caduceus.core.db import open_vault, write_txn
from caduceus.research.watch import default_watch, run_watch
from test_watch import FakeHttp, NOW


def paper(pmid, title, day="2026-10-01", abstract="Synthetic abstract."):
    return {"id": str(pmid), "pmid": str(pmid), "source": "MED", "title": title,
            "pubYear": day[:4], "firstPublicationDate": day, "abstractText": abstract,
            "doi": f"10.0000/pair.{pmid}"}


class PairHttp(FakeHttp):
    def __init__(self, *, pair_rows=None, pmids=None, metadata=None):
        super().__init__(rows=[paper(90000101 + i, f"Synthetic general paper {i}") for i in range(3)])
        self.pair_rows = [paper(90000200, "Synthetic GLP1R semaglutide genetics")] if pair_rows is None else pair_rows
        self.pmids = pmids or {}
        self.metadata = metadata or []

    def get_json(self, url, params):
        self.requests.append((url, dict(params)))
        if "litvar2-api" in url:
            rsid = re.search(r"rs\d+", unquote(url))[0]
            return {"pmids": self.pmids.get(rsid, [])}
        if "esearch" in url:
            return {"esearchresult": {"count": "0", "idlist": []}}
        query = params["query"]
        if query.startswith("EXT_ID:"):
            ids = set(re.findall(r"\d+", query))
            rows = [r for r in self.metadata if r["pmid"] in ids]
        elif '"pharmacogenetic"' in query:
            rows = self.pair_rows
        else:
            start, end = re.search(r"FIRST_PDATE:\[(.*?) TO (.*?)\]", query).groups()
            rows = [r for r in self.rows if start <= r["firstPublicationDate"] <= end]
        return {"hitCount": len(rows), "resultList": {"result": rows}}


def persona(vault, drugs=("Ozempic",), allow=None):
    with write_txn(vault):
        for drug in drugs:
            vault.execute("INSERT INTO medication_event(drug,event,occurred_on,created_at,category) "
                          "VALUES (?,'start','2026-10-01',?,'medication')", (drug, NOW))
    return default_watch(vault, allow=allow)


def test_persona_pair_first_and_general_relevance_follow(vault):
    http = PairHttp()
    result = run_watch(vault, persona(vault), http=http, now=NOW)
    first, *rest = result["new_items"]
    assert first["pmid"] == "90000200"
    assert first["why"] == {"reason": "gene_drug", "drugs": ["semaglutide"], "genes": ["GLP1R"], "rsids": []}
    assert [r["pmid"] for r in rest] == ["90000101", "90000102"]
    pair_queries = [q for q in result["queried"] if q["source"] == "europepmc-pairs"]
    assert len(pair_queries) == 1
    assert pair_queries[0]["params"] == {
        "query": '(("GLP1R") OR ("rs10305420" OR "rs6923761")) AND '
                 '("ozempic" OR "rybelsus" OR "semaglutide" OR "wegovy") AND '
                 '("variant" OR "variants" OR "polymorphism" OR "genotype" OR "allele" OR '
                 '"pharmacogenetic" OR "pharmacogenomic" OR "genetic") AND '
                 'FIRST_PDATE:[2021-11-09 TO 2026-11-08] AND (SRC:MED OR SRC:PPR)',
        "format": "json", "resultType": "core", "pageSize": 25, "sort": ""}
    assert len(result["queried"]) == 28
    assert result["errors"] == []
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"
    # Pair hits are seen durably even though their five-year window does not move the watermark.
    replay = run_watch(vault, default_watch(vault), http=http, now="2026-11-09")
    assert replay["new_items"] == []
    assert not any(q["source"] == "europepmc-pairs" for q in replay["queried"])


@pytest.mark.parametrize("with_drug", [False, True])
def test_pair_variant_metadata_fetch_matches_local_call(vault, with_drug):
    title = "Synthetic GLP1R variant" + (" Ozempic" if with_drug else "")
    http = PairHttp(pair_rows=[], pmids={"rs6923761": [90000300]}, metadata=[paper(90000300, title)])
    call = Call("rs6923761", CallState.CALLED, "AG")
    result = run_watch(vault, persona(vault), http=http, now=NOW, calls={"rs6923761": call})
    item = result["new_items"][0]
    assert item["pmid"] == "90000300"
    assert item["why"]["reason"] == ("gene_drug" if with_drug else "variant")
    assert item["why"]["rsids"] == ["rs6923761"]
    assert item["matched"]["calls"]["rs6923761"]["genotype"] == "AG"
    metadata = [q for q in result["queried"] if "EXT_ID:" in q["params"].get("query", "")]
    assert len(metadata) == 1 and metadata[0]["params"]["query"] == "EXT_ID:(90000300) AND SRC:MED"
    assert metadata[0]["params"]["pageSize"] == 25
    litvar = [re.search(r"rs\d+", q["url"])[0] for q in result["queried"] if q["source"] == "litvar"]
    assert litvar[:2] == ["rs10305420", "rs6923761"]
    assert len(result["queried"]) <= 30 and not result["errors"]


def test_variant_metadata_top_25_numeric_unfetched_and_all_annotations(vault):
    pmids = [90000101] + list(range(90000400, 90000430))
    http = PairHttp(pair_rows=[], pmids={"rs6923761": pmids, "rs10305420": [90000429]},
                    metadata=[paper(p, "Synthetic GLP1R variant") for p in pmids[1:]])
    result = run_watch(vault, persona(vault), http=http, now=NOW)
    query = next(q["params"]["query"] for q in result["queried"] if "EXT_ID:" in q["params"].get("query", ""))
    assert query == "EXT_ID:(" + " OR ".join(str(p) for p in range(90000429, 90000404, -1)) + ") AND SRC:MED"
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 28
    matched = json.loads(vault.execute("SELECT matched_json FROM research_item WHERE pmid='90000429'").fetchone()[0])
    assert matched["rsids"] == ["rs10305420", "rs6923761"]


@pytest.mark.parametrize("budget", [5, 30])
def test_pair_request_budget_and_visible_skip(vault, budget):
    result = run_watch(vault, persona(vault), http=PairHttp(), now=NOW, budget=budget)
    assert len(result["queried"]) <= budget
    assert result["queried"][0]["source"] == "europepmc"
    if budget == 5:
        assert {"source": "europepmc-pairs", "drugs": 1} in result["skipped"]
        assert not any(q["source"] == "europepmc-pairs" for q in result["queried"])
    else:
        assert any(q["source"] == "europepmc-pairs" for q in result["queried"])


def test_no_medications_keeps_original_seed_and_rotation(vault):
    http = PairHttp()
    result = run_watch(vault, default_watch(vault), http=http, now=NOW)
    assert [i["pmid"] for i in result["new_items"]] == ["90000101", "90000102", "90000103"]
    assert all(i["why"]["reason"] == "general" for i in result["new_items"])
    assert not any(q["source"] == "europepmc-pairs" or "EXT_ID:" in q["params"].get("query", "")
                   for q in result["queried"])


def test_later_digest_tiers_newest_first_with_whole_word_alias_matching(vault):
    w = persona(vault)
    with write_txn(vault):
        vault.execute("UPDATE watch SET last_since='2026-11-01' WHERE id=?", (w["id"],))
    http = PairHttp()
    http.rows = [paper(90000500, "General newest", "2026-11-08"),
                 paper(90000501, "GLP1R Ozempic", "2026-11-03"),
                 paper(90000502, "GLP1R rs6923761", "2026-11-07"),
                 paper(90000503, "GLP1R semaglutide-like study", "2026-11-04"),
                 paper(90000504, "GLP1R notsemaglutidex", "2026-11-06"),
                 paper(90000505, "Other research", "2026-11-05", "glp1r WEGOVY genetics")]
    result = run_watch(vault, w, http=http, now=NOW)
    assert [i["pmid"] for i in result["new_items"]] == ["90000505", "90000503", "90000501", "90000502", "90000500", "90000504"]
    assert [i["why"]["reason"] for i in result["new_items"]] == ["gene_drug"] * 3 + ["variant"] + ["general"] * 2
    assert not any(q["source"] == "europepmc-pairs" or "EXT_ID:" in q["params"].get("query", "")
                   for q in result["queried"])


def test_same_medications_different_calls_have_byte_identical_seed_and_later_queries(tmp_path):
    outputs = []
    for index, genotype in enumerate(("AG", "GG")):
        conn = open_vault(tmp_path / f"synthetic-{index}.db")
        try:
            w = persona(conn)
            calls = {"rs6923761": Call("rs6923761", CallState.CALLED, genotype),
                     "rs999999999": Call("rs999999999", CallState.CALLED, "TT")}
            http = PairHttp(pair_rows=[], pmids={"rs6923761": [90000300]}, metadata=[paper(90000300, "GLP1R variant")])
            results = [run_watch(conn, w, http=http, now=NOW, calls=calls),
                       run_watch(conn, w, http=http, now="2026-11-09", calls=calls)]
            outputs.append([json.dumps(r["queried"], separators=(",", ":")).encode() for r in results])
            for result in results:
                for q in result["queried"]:
                    # "genotype" is a fixed public keyword; no local call/allele value is a term.
                    assert not re.search(r'"(?:AG|GG|TT)"|rs999999999|"calls"|"state"|"called"', json.dumps(q))
        finally:
            conn.close()
    assert outputs[0] == outputs[1]


def test_hidden_medication_and_aliases_never_leave_vault(vault):
    allow = lambda category: category != "medication"
    w = persona(vault, allow=allow)
    for now in (NOW, "2026-11-09"):
        result = run_watch(vault, w, http=PairHttp(), now=now, allow=allow)
        requests = json.dumps(result["queried"]).casefold()
        assert all(term not in requests for term in ("semaglutide", "ozempic", "rybelsus", "wegovy"))
        assert not any(q["source"] == "europepmc-pairs" for q in result["queried"])


def test_pair_genes_missing_from_primary_are_added(vault):
    w = persona(vault, drugs=("Mounjaro",))
    with write_txn(vault):
        terms = json.loads(w["terms_json"])
        terms["genes"] = ["CYP2C19"]
        vault.execute("UPDATE watch SET terms_json=? WHERE id=?", (json.dumps(terms), w["id"]))
    result = run_watch(vault, w, http=PairHttp(pair_rows=[]), now=NOW)
    primary = result["queried"][0]["params"]["query"]
    assert '"GLP1R"' in primary and '"GIPR"' in primary
    pair = next(q["params"]["query"] for q in result["queried"] if q["source"] == "europepmc-pairs")
    assert '"mounjaro" OR "tirzepatide" OR "zepbound"' in pair
