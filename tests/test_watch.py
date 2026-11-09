"""Offline source-shaped fixtures and synthetic publications; no real user data."""
import copy
import io
import json
import random
import re
from datetime import date, timedelta
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
    terms = {"genes": ["GLP1R", "APOE"], "drugs": [], "topics": ["diabetes"], "rsids": list(rsids)}
    with write_txn(vault):
        vault.execute("UPDATE watch SET name='synthetic',terms_json=?,sources_json=? WHERE id=?",
                      (json.dumps(terms), json.dumps(sources), w["id"]))
    return dict(vault.execute("SELECT * FROM watch WHERE id=?", (w["id"],)).fetchone())


def test_seed_top_three_none_then_one_new(vault):
    w = watch(vault)
    http = FakeHttp()
    first = run_watch(vault, w, http=http, now=NOW)
    assert first["kind"] == "seed" and len(first["new_items"]) == 3
    assert [r["pmid"] for r in first["new_items"]] == ["90000000", "90000001", "90000002"]
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
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"


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


@pytest.mark.parametrize("errorlist,fails", [
    ({"phrasesnotfound": ["rs10305420[All Fields]"], "fieldsnotfound": []}, False),  # a marker ClinVar does not list
    ({"phrasesnotfound": [], "fieldsnotfound": ["snp"]}, True),                      # a malformed search
])
def test_clinvar_terms_it_cannot_find_are_a_notice_not_a_failure(vault, errorlist, fails):
    w = watch(vault, ("clinvar",), ("rs4244285", "rs10305420"))
    http = FakeHttp()
    original = http.get_json

    def with_errorlist(url, params):
        result = original(url, params)
        if "esearch" in url:
            result["esearchresult"]["errorlist"] = errorlist
        return result

    http.get_json = with_errorlist
    result = run_watch(vault, w, http=http, now=NOW)
    if fails:
        assert result["errors"] == [{"source": "clinvar", "error": "ValueError"}] and not result["new_items"]
    else:
        assert result["errors"] == [] and result["new_items"]


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
    with write_txn(vault):
        vault.execute("UPDATE watch SET last_since='2026-11-07' WHERE id=?", (w["id"],))
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
    assert "FIRST_PDATE:[2026-11-05 TO 2026-11-08]" in http.requests[0][1]["query"]
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


class VolumeHttp(FakeHttp):
    """2,092 date-window seed hits, 1,124 historical LitVar PMIDs per variant."""
    def __init__(self):
        super().__init__()
        template = papers(1)[0]
        self.seed_rows = [{**template, "id": str(91000000 + i), "pmid": str(91000000 + i),
                           "doi": f"10.0000/volume.{i}", "title": f"Synthetic volume paper {i}",
                           "firstPublicationDate": "2026-11-08" if i == 0 else "2026-11-07" if i == 1 else "2026-10-01"}
                          for i in range(2092)]
        self.add_new = False

    def get_json(self, url, params):
        data = super().get_json(url, params)
        if "ebi.ac.uk" in url:
            assert "EXT_ID:" not in params["query"]
            seed = "2025-11-08" in params["query"]
            rows = self.seed_rows[:100] if seed else self.seed_rows[:2]
            if self.add_new:
                rows = rows + [{**self.seed_rows[0], "id": "92000000", "pmid": "92000000",
                                "doi": "10.0000/volume.new", "firstPublicationDate": "2026-11-10"}]
            data["hitCount"] = 2092 if seed else len(rows)
            data["resultList"]["result"] = copy.deepcopy(rows)
            if seed:
                data["nextCursorMark"] = "seed-second-page-must-not-be-requested"
        elif "litvar2-api" in url:
            data["pmids"] = [91000000] + list(range(93000000, 93001123))
        return data


def test_realistic_watch_volume_seed_none_then_one_new_within_budget(vault):
    from caduceus.knowledge.pack import load_pack
    rsids = sorted(set(load_pack().watch["rsids"]) - {"rs2231142", "rs3745274", "rs2108622"})
    assert len(rsids) == 25 and "rs4244285" in rsids
    w = watch(vault, ("europepmc", "clinvar", "litvar"), rsids)
    http = VolumeHttp()
    seed = run_watch(vault, w, http=http, now=NOW)
    assert seed["kind"] == "seed" and seed["errors"] == []
    assert [r["pmid"] for r in seed["new_items"]] == ["91000000", "91000001", "91000002"]
    assert len(seed["queried"]) == 28  # relevance + overlap, two ClinVar, bounded enrichment
    assert http.requests[0][1]["sort"] == ""  # documented default is relevance
    assert vault.execute("SELECT count(*) FROM research_item").fetchone()[0] == 102
    second = run_watch(vault, w, http=http, now="2026-11-09")
    assert second["kind"] == "none" and second["errors"] == []
    http.add_new = True
    third = run_watch(vault, w, http=http, now="2026-11-10")
    assert third["kind"] == "new" and [r["pmid"] for r in third["new_items"]] == ["92000000"]
    assert all(len(result["queried"]) <= 28 for result in (seed, second, third))
    assert not any("EXT_ID:" in params.get("query", "") for _, params in http.requests)
    assert all(r["matched"]["rsids"] for r in seed["new_items"] if r["pmid"] == "91000000")


@pytest.mark.parametrize("source", ["clinvar", "litvar"])
def test_secondary_errors_do_not_freeze_primary_watermark(vault, source):
    w = watch(vault, ("europepmc", "clinvar", "litvar"))
    http = FakeHttp(failing=(source,))
    first = run_watch(vault, w, http=http, now=NOW)
    assert first["errors"] and any(e["source"] == source for e in first["errors"])
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"
    second = run_watch(vault, w, http=http, now="2026-11-09")
    assert second["kind"] == "none"
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-09"


def test_fitting_window_fetches_all_three_pages_before_watermark(vault):
    w = watch(vault)
    set_watermark(vault, w, "2026-11-01")
    http = DateSliceHttp(dated_papers(299, date(2026, 11, 1), 8))
    result = run_watch(vault, w, http=http, now=NOW, budget=30)
    assert len(result["queried"]) == 3
    assert len(result["new_items"]) == 299 and result["errors"] == []
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"


def test_litvar_budget_omissions_do_not_freeze_seed_or_exceed_budget(vault):
    from caduceus.knowledge.pack import load_pack
    w = watch(vault, ("europepmc", "clinvar", "litvar"), load_pack().watch["rsids"])
    result = run_watch(vault, w, http=FakeHttp(), now=NOW)
    assert len(result["queried"]) <= 28
    assert result["errors"] == []
    assert any(e.get("rsids", 0) > 0 for e in result["skipped"])
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"


@pytest.mark.parametrize("extra", [0, 1])
def test_transport_response_size_cap(extra):
    from caduceus.research.http import MAX_RESPONSE_BYTES
    response = io.BytesIO(b"{}" + b" " * (MAX_RESPONSE_BYTES - 2 + extra))

    class ResponseOpener:
        def open(self, req, timeout):
            return response

    http = UrllibHttp()
    http._opener = ResponseOpener()
    if extra:
        with pytest.raises(ValueError, match="8 MB size limit"):
            http.get_json("https://www.ebi.ac.uk/test", {})
    else:
        assert http.get_json("https://www.ebi.ac.uk/test", {}) == {}


class DateSliceHttp(FakeHttp):
    """A date-filtered, relevance-shuffled API; sort parameters deliberately have no effect."""
    def __init__(self, rows, *, fail_on=()):
        super().__init__(rows=rows)
        self.fail_on = set(fail_on)
        self.primary_calls = 0

    def get_json(self, url, params):
        data = super().get_json(url, params)
        if "ebi.ac.uk" not in url:
            return data
        self.primary_calls += 1
        if self.primary_calls in self.fail_on:
            raise URLError("synthetic interrupted date slice")
        match = re.search(r"FIRST_PDATE:\[(\d{4}-\d{2}-\d{2}) TO (\d{4}-\d{2}-\d{2})\]", params["query"])
        assert match, params["query"]
        start, end = match.groups()
        rows = [r for r in self.rows if start <= r["firstPublicationDate"] <= end]
        offset = 0 if params["cursorMark"] == "*" else int(params["cursorMark"])
        page_size = int(params["pageSize"])
        result = {"hitCount": len(rows), "resultList": {"result": copy.deepcopy(rows[offset:offset + page_size])}}
        if offset + page_size < len(rows):
            result["nextCursorMark"] = str(offset + page_size)
        return result


def dated_papers(count, start, days):
    template = papers(1)[0]
    rows = []
    for i in range(count):
        row = {**template, "id": str(96000000 + i), "pmid": str(96000000 + i),
               "doi": f"10.0000/slices.{i}", "firstPublicationDate": (start + timedelta(days=i % days)).isoformat()}
        row.pop("abstractText", None)
        rows.append(row)
    random.Random(17).shuffle(rows)
    return rows


def set_watermark(vault, w, day):
    with write_txn(vault):
        vault.execute("UPDATE watch SET last_since=? WHERE id=?", (day, w["id"]))


def test_round2_shuffled_slices_resume_without_lost_or_duplicate_deliveries(vault):
    rows = dated_papers(1000, date(2026, 10, 20), 20)
    # Reproduce the reviewer's non-monotonic prefix exactly before the shuffled remainder.
    prefix = []
    for day in ["2026-11-02", "2026-11-03", "2026-11-03", "2026-11-03", "2026-11-02", "2026-10-27"]:
        row = next(r for r in rows if r["firstPublicationDate"] == day)
        rows.remove(row)
        prefix.append(row)
    rows = prefix + rows
    http = DateSliceHttp(rows, fail_on=(8,))
    w = watch(vault)
    set_watermark(vault, w, "2026-10-22")
    delivered = []
    for _ in range(6):
        result = run_watch(vault, w, http=http, now=NOW, budget=30)
        assert len(result["queried"]) <= 30
        delivered.extend(r["pmid"] for r in result["new_items"])
        watermark = vault.execute("SELECT last_since FROM watch").fetchone()[0]
        expected_through_watermark = {r["pmid"] for r in rows if r["firstPublicationDate"] <= watermark}
        stored = {r[0] for r in vault.execute("SELECT pmid FROM research_item")}
        assert expected_through_watermark <= stored, "watermark passed an incomplete publication date"
        if watermark == "2026-11-08":
            break
    assert watermark == "2026-11-08"
    assert len(delivered) == len(set(delivered)) == 1000
    assert set(delivered) == {r["pmid"] for r in rows}
    replay = run_watch(vault, w, http=http, now=NOW, budget=30)
    assert replay["kind"] == "none" and replay["new_items"] == []


def test_round2_incomplete_shuffled_slice_stores_items_without_advancing_watermark(vault):
    rows = dated_papers(250, date(2026, 11, 1), 8)
    http = DateSliceHttp(rows)
    w = watch(vault)
    set_watermark(vault, w, "2026-11-01")
    first = run_watch(vault, w, http=http, now=NOW, budget=1)
    assert len(first["new_items"]) == 100
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-01"
    assert first["errors"] == [{"source": "europepmc", "error": "BudgetExceeded"}]
    second = run_watch(vault, w, http=http, now=NOW, budget=30)
    assert len(second["new_items"]) == 150
    assert not {r["pmid"] for r in first["new_items"]}.intersection(r["pmid"] for r in second["new_items"])
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"


def test_round2_overfull_single_day_is_visible_and_does_not_stall(vault):
    http = DateSliceHttp(dated_papers(450, date(2026, 11, 8), 1))
    w = watch(vault)
    set_watermark(vault, w, "2026-11-07")
    result = run_watch(vault, w, http=http, now=NOW, budget=30)
    assert result["errors"] == [{"source": "europepmc", "error": "DayTruncated", "date": "2026-11-08", "omitted": 150}]
    assert len(result["new_items"]) == 300
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-08"
    replay = run_watch(vault, w, http=http, now="2026-11-09", budget=30)
    assert replay["kind"] == "none"
    assert vault.execute("SELECT last_since FROM watch").fetchone()[0] == "2026-11-09"


def test_round2_seed_overlap_is_seen_without_delivering_stale_hits(vault):
    from caduceus.knowledge.pack import load_pack
    today = date(2026, 11, 8)
    rows = dated_papers(1600, today - timedelta(days=364), 365)
    http = DateSliceHttp(rows)
    w = watch(vault, ("europepmc", "clinvar", "litvar"), load_pack().watch["rsids"])
    seed = run_watch(vault, w, http=http, now=NOW, budget=30)
    assert seed["kind"] == "seed"
    assert [r["pmid"] for r in seed["new_items"]] == [r["pmid"] for r in rows[:3]]
    stored = {r[0] for r in vault.execute("SELECT pmid FROM research_item")}
    overlap = {r["pmid"] for r in rows if r["firstPublicationDate"] >= "2026-11-06"}
    assert overlap <= stored
    second = run_watch(vault, w, http=http, now="2026-11-09", budget=30)
    assert second["kind"] == "none" and second["new_items"] == []
    new = {**rows[0], "id": "97000000", "pmid": "97000000", "doi": "10.0000/round2.new",
           "firstPublicationDate": "2026-11-10"}
    http.rows.append(new)
    third = run_watch(vault, w, http=http, now="2026-11-10", budget=30)
    assert third["kind"] == "new" and [r["pmid"] for r in third["new_items"]] == ["97000000"]
    assert all(len(r["queried"]) <= 30 for r in (seed, second, third))
    assert seed["errors"] == second["errors"] == third["errors"] == []


def test_round2_daily_rotation_enriches_every_rsid_and_planned_skips_are_not_errors(vault):
    from caduceus.knowledge.pack import load_pack
    rsids = sorted(load_pack().watch["rsids"])
    assert len(rsids) == 28
    w = watch(vault, ("europepmc", "clinvar", "litvar"), rsids)
    http = DateSliceHttp([])
    enriched = set()
    for i in range(7):
        day = date(2026, 11, 8) + timedelta(days=i)
        row = dated_papers(1, day, 1)[0]
        row.update(id=str(98000000 + i), pmid=str(98000000 + i), doi=f"10.0000/rotation.{i}")
        http.rows.append(row)
        result = run_watch(vault, w, http=http, now=day, budget=30)
        actual = [re.search(r"rs\d+", q["url"])[0] for q in result["queried"] if q["source"] == "litvar"]
        start = day.toordinal() % len(rsids)
        expected = rsids[start:] + rsids[:start]
        assert actual and actual == expected[:len(actual)]
        enriched.update(actual)
        assert result["errors"] == []
        assert result["skipped"] == [{"source": "litvar", "rsids": len(rsids) - len(actual)}]
        assert len(result["queried"]) <= 30
    assert enriched == set(rsids)
