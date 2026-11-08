"""Offline source-shaped fixtures and synthetic publications; no real user data."""
import copy
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from caduceus.core.contracts import Call, CallState
from caduceus.core.db import open_vault, write_txn
from caduceus.core.policy import Policy
from caduceus.core.search import search
from caduceus.research.http import BudgetExceeded, USER_AGENT, UrllibHttp
from caduceus.research.watch import default_watch, run_watch

FIXTURES = Path(__file__).parent / "fixtures" / "research"
NOW = "2026-11-08T00:00:00Z"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def papers(count=5):
    template = fixture("europepmc.json")["resultList"]["result"][0]
    result = []
    for i in range(count):
        row = {**template, "id": str(90000000 + i), "pmid": str(90000000 + i), "pmcid": None,
               "title": f"Synthetic GLP1R rs6923761 study {i}", "doi": f"10.0000/synthetic.{i}",
               "pubYear": "2026", "firstPublicationDate": f"2026-11-0{i + 1}", "abstractText": "Synthetic summary. " * 60}
        result.append(row)
    return result


class FakeHttp:
    def __init__(self, rows=None, *, failing=()):
        self.rows = papers() if rows is None else rows
        self.failing = set(failing)
        self.requests = []
        self.sleeps = []
        self.tick = 0

    def monotonic(self):
        return self.tick

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.tick += delay

    def get_json(self, url, params):
        self.requests.append((url, copy.deepcopy(params)))
        self.tick += 0.001
        source = "europepmc" if "ebi.ac.uk" in url else "clinvar" if "eutils" in url else "litvar"
        if source in self.failing:
            raise URLError("synthetic source failure")
        if source == "europepmc":
            rows = self.rows
            if "EXT_ID:" in params["query"]:
                rows = [r for r in rows if "EXT_ID:" + r["pmid"] in params["query"]]
            data = fixture("europepmc.json")
            return {**data, "hitCount": len(rows), "resultList": {"result": copy.deepcopy(rows)}}
        if source == "litvar":
            data = fixture("litvar.json")
            data["pmids"] = [int(r["pmid"]) for r in self.rows]
            return data
        if "esearch" in url:
            data = fixture("clinvar_search.json")
            data["esearchresult"]["count"] = str(len(data["esearchresult"]["idlist"]))
            return data
        return fixture("clinvar_summary.json")


def watch(vault, sources=("europepmc",), rsids=("rs6923761",)):
    w = default_watch(vault, topics=("diabetes",))
    terms = {"genes": ["GLP1R", "APOE"], "drugs": ["semaglutide"], "topics": ["diabetes"], "rsids": list(rsids)}
    with write_txn(vault):
        vault.execute("UPDATE watch SET name='synthetic',terms_json=?,sources_json=? WHERE id=?",
                      (json.dumps(terms), json.dumps(sources), w["id"]))
    return dict(vault.execute("SELECT * FROM watch WHERE id=?", (w["id"],)).fetchone())


def test_seed_top_three_none_then_one_new(vault):
    w = watch(vault)
    http = FakeHttp()
    first = run_watch(vault, w, http=http, now=NOW)
    assert first["kind"] == "seed" and len(first["new_items"]) == 3
    assert [r["pmid"] for r in first["new_items"]] == ["90000004", "90000003", "90000002"]
    assert first["since"] == "2025-11-08" and first["errors"] == []
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 5
    assert all(len(r["summary"]) <= 600 for r in first["new_items"])
    # Deliberately pass the original stale dict; persisted watch state is authoritative.
    second = run_watch(vault, w, http=http, now="2026-11-09")
    assert second["kind"] == "none" and second["new_items"] == [] and second["seen_before"] == 5
    assert second["since"] == "2026-11-06"
    http.rows = papers(6)
    third = run_watch(vault, w, http=http, now="2026-11-10")
    assert third["kind"] == "new" and [r["pmid"] for r in third["new_items"]] == ["90000005"]
    assert vault.execute("SELECT count(*) FROM delivery").fetchone()[0] == 3


def test_dedupe_sources_local_matching_categories_and_no_genotype_requests(vault):
    w = watch(vault, ("europepmc", "litvar"), ("rs429358",))
    http = FakeHttp()
    value = {"rs429358": Call("rs429358", CallState.CALLED, "CT")}
    result = run_watch(vault, w, http=http, now=NOW, calls=value)
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 5
    assert all(r["category"] == "alzheimers" for r in result["new_items"])
    assert all(r["matched"]["calls"]["rs429358"]["genotype"] == "CT" for r in result["new_items"])
    payload = json.dumps(http.requests)
    assert '"CT"' not in payload and "genotype" not in payload
    with write_txn(vault):
        vault.execute("UPDATE watch SET last_since=NULL WHERE id=?", (w["id"],))
    other = FakeHttp()
    run_watch(vault, w, http=other, now=NOW, calls={"rs429358": Call("rs429358", CallState.CALLED, "CC")})
    assert http.requests == other.requests
    assert search(vault, "synthetic", policy=Policy(level="standard"))["results"] == []


def test_doi_dedupe_without_matching_pmids(vault):
    w = watch(vault)
    http = FakeHttp(rows=papers(1))
    run_watch(vault, w, http=http, now=NOW)
    http.rows[0]["pmid"] = "90099999"
    http.rows[0]["id"] = "90099999"
    result = run_watch(vault, w, http=http, now=NOW)
    assert result["kind"] == "none" and result["seen_before"] == 1
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 1


@pytest.mark.parametrize("budget", [0, 1, 2, 3])
def test_budget_and_source_isolation(vault, budget):
    w = watch(vault, ("europepmc", "clinvar", "litvar"))
    http = FakeHttp(failing=("europepmc",))
    result = run_watch(vault, w, http=http, now=NOW, budget=budget)
    assert len(http.requests) <= budget and len(result["queried"]) <= budget
    assert result["errors"]
    assert vault.execute("SELECT last_since FROM watch WHERE id=?", (w["id"],)).fetchone()[0] is None
    if budget >= 3:
        assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 2
        assert all(r["source"] == "clinvar" for r in result["new_items"])


def test_one_source_failing_does_not_abort_others(vault):
    w = watch(vault, ("europepmc", "clinvar", "litvar"))
    result = run_watch(vault, w, http=FakeHttp(failing=("litvar",)), now=NOW)
    assert {r["source"] for r in result["new_items"]} <= {"clinvar", "europepmc"}
    assert len(result["errors"]) == 1 and result["errors"][0]["source"] == "litvar"
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 7


def test_clinvar_reclassification_is_new_and_ncbi_is_paced(vault):
    w = watch(vault, ("clinvar",), ("rs4244285",))
    http = FakeHttp()
    first = run_watch(vault, w, http=http, now=NOW)
    assert first["new_items"] and all(r["external_id"].startswith("VCV") for r in first["new_items"])
    assert all(delay >= 1 / 3 - 0.002 for delay in http.sleeps)
    original = http.get_json

    def reclassified(url, params):
        result = original(url, params)
        if "esummary" in url:
            uid = result["result"]["uids"][0]
            result["result"][uid]["germline_classification"]["last_evaluated"] = "2026-11-09"
        return result

    http.get_json = reclassified
    result = run_watch(vault, w, http=http, now="2026-11-09")
    assert result["kind"] == "new" and len(result["new_items"]) == 1
    assert result["new_items"][0]["external_id"].endswith(":2026-11-09")


def test_default_watch_medications_topics_and_idempotent_schema(vault, data_dir):
    with write_txn(vault):
        for drug, event, day in [("Ozempic", "start", "2026-10-01"), ("Plavix", "start", "2026-10-02"),
                                 ("Plavix", "stop", "2026-10-03")]:
            vault.execute("INSERT INTO medication_event(drug,event,occurred_on,created_at) VALUES (?,?,?,?)",
                          (drug, event, day, NOW))
    w = default_watch(vault, topics=("diabetes",))
    assert json.loads(w["terms_json"])["drugs"] == ["semaglutide"]
    assert "diabetes" in json.loads(w["terms_json"])["topics"]
    assert default_watch(vault)["id"] == w["id"]
    reopened = open_vault(data_dir / "vault.db")
    assert reopened.execute("SELECT count(*) FROM watch").fetchone()[0] == 1
    reopened.close()


def test_europepmc_pagination_stores_every_fetched_item(vault):
    w = watch(vault)
    http = FakeHttp()
    original = http.get_json

    def pages(url, params):
        result = original(url, params)
        if params["cursorMark"] == "*":
            result["resultList"]["result"] = result["resultList"]["result"][:2]
            result["nextCursorMark"] = "second"
        else:
            result["resultList"]["result"] = result["resultList"]["result"][2:]
        return result

    http.get_json = pages
    result = run_watch(vault, w, http=http, now=NOW)
    assert len(result["queried"]) == 2
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 5
    assert "FIRST_PDATE:[2025-11-08 TO 2026-11-08]" in http.requests[0][1]["query"]
    assert "(SRC:MED OR SRC:PPR)" in http.requests[0][1]["query"]


def test_recorded_fixtures_are_small_and_have_verified_api_shapes():
    for path in FIXTURES.glob("*.json"):
        assert path.stat().st_size < 50000
    assert 41307691 in fixture("litvar.json")["pmids"]
    assert fixture("clinvar_summary.json")["result"]["uids"]
    assert fixture("europepmc.json")["resultList"]["result"]


class Opener:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


@pytest.mark.parametrize("url", ["http://www.ncbi.nlm.nih.gov/x", "https://evil.test/x",
    "https://www.ncbi.nlm.nih.gov.evil.test/x", "https://user@www.ncbi.nlm.nih.gov/x",
    "https://www.ncbi.nlm.nih.gov:444/x"])
def test_transport_rejects_disallowed_origins(url):
    with pytest.raises(ValueError):
        UrllibHttp().get_json(url, {})


def test_transport_retries_timeout_useragent_ncbi_tool_and_physical_budget():
    tick = [0.0]
    delays = []

    def sleep(delay):
        delays.append(delay)
        tick[0] += delay

    http = UrllibHttp(sleep=sleep, monotonic=lambda: tick[0])
    http._opener = Opener([URLError("offline"), TimeoutError(), {"ok": True}])
    assert http.get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", {"db": "clinvar"}) == {"ok": True}
    assert len(http._opener.requests) == 3
    for request, timeout in http._opener.requests:
        assert timeout == 20 and request.get_header("User-agent") == USER_AGENT
        assert parse_qs(urlsplit(request.full_url).query)["tool"] == ["caduceus"]
    http._opener = Opener([URLError("offline"), {"ok": True}])
    with http.limited(1), pytest.raises(BudgetExceeded):
        http.get_json("https://www.ebi.ac.uk/test", {})
    assert len(http._opener.requests) == 1
    assert 1 in delays and 2 in delays


def test_transport_404_is_not_retried_and_redirects_are_not_followed():
    http = UrllibHttp()
    http._opener = Opener([HTTPError("https://www.ebi.ac.uk/test", 404, "missing", {}, None)])
    with pytest.raises(HTTPError):
        http.get_json("https://www.ebi.ac.uk/test", {})
    assert len(http._opener.requests) == 1
    from caduceus.research.http import _NoRedirect
    assert _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test/") is None
