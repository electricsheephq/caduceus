"""Bounded public-science queries, local matching, and durable seed/new digests."""
from __future__ import annotations

import json
import re
import time
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from ..core.db import write_txn
from ..core.policy import Category, categorize
from ..core.search import index_doc
from ..knowledge.pack import load_pack, normalize_drug
from .http import BudgetExceeded, UrllibHttp

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
LITVAR = "https://www.ncbi.nlm.nih.gov/research/litvar2-api/variant/get/"
SOURCES = ("europepmc", "clinvar", "litvar")


def _timestamp(now):
    if callable(now):
        now = now()
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime.combine(now, datetime.min.time(), tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _medications(conn, allow=None):
    """Current medications the knowledge pack knows. *allow* (a category predicate) leaves out the ones the user's
    privacy settings hide, so they never become search terms sent to the research services."""
    rows = conn.execute(
        "SELECT drug, event, category FROM medication_event ORDER BY occurred_on DESC, id DESC"
    )
    latest = {}
    for row in rows:
        if allow is not None and not allow(row["category"]):
            continue
        drug = normalize_drug(row["drug"])
        if drug and drug not in latest:
            latest[drug] = row["event"]
    return sorted(drug for drug, event in latest.items() if event != "stop")


def default_watch(conn, *, topics=(), allow=None) -> dict:
    pack = load_pack()
    if isinstance(topics, str):
        topics = [topics]
    terms = {**pack.watch, "topics": sorted(set(pack.watch.get("topics", [])) | {str(t).strip() for t in topics if str(t).strip()}),
             "drugs": _medications(conn, allow)}
    now = _timestamp(datetime.now(timezone.utc))
    with write_txn(conn):
        conn.execute(
            "INSERT INTO watch(name, terms_json, sources_json, created_at) VALUES ('default',?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET terms_json=excluded.terms_json",
            (json.dumps(terms), json.dumps(SOURCES), now),
        )
    return dict(conn.execute("SELECT * FROM watch WHERE name='default'").fetchone())


def _terms(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _quoted(values):
    # Terms are data, never executable Europe PMC query syntax.
    return " OR ".join('"' + re.sub(r'[^\w .-]', ' ', str(v)).strip() + '"' for v in values if str(v).strip())


def _publication(row, source="europepmc", matched=()):
    journal = row.get("journalTitle") or row.get("journalInfo", {}).get("journal", {}).get("title")
    return {"source": source, "external_id": str(row.get("id") or row.get("pmid") or row.get("doi") or ""),
            "title": row.get("title", ""), "journal": journal, "year": row.get("pubYear"),
            "pub_date": row.get("firstPublicationDate"), "doi": row.get("doi"),
            "pmid": str(row["pmid"]) if row.get("pmid") else None, "pmcid": row.get("pmcid"),
            "url": f"https://europepmc.org/article/{row.get('source', 'MED')}/{row.get('id', '')}",
            "summary": re.sub("<[^>]+>", "", row.get("abstractText") or "")[:600],
            "matched_rsids": list(matched)}


def _clinvar(row):
    classification = row.get("germline_classification") or row.get("clinical_significance") or {}
    evaluated = classification.get("last_evaluated") or "unknown"
    accession = row.get("accession") or "VCV" + str(row["uid"]).zfill(9)
    # Keep evaluation revisions separate even when their publications overlap.
    markers = ["rs" + str(x["db_id"]) for v in row.get("variation_set", [])
               for x in v.get("variation_xrefs", []) if x.get("db_source") == "dbSNP"]
    return {"source": "clinvar", "external_id": accession + ":" + evaluated,
            "title": row.get("title", accession), "journal": None, "year": None, "pub_date": None,
            "doi": None, "pmid": None, "pmcid": None,
            "url": "https://www.ncbi.nlm.nih.gov/clinvar/variation/" + str(row["uid"]) + "/",
            "summary": (classification.get("description", "") + "; last evaluated " + evaluated)[:600],
            "matched_rsids": markers, "genes": [g["symbol"] for g in row.get("genes", [])]}


def _match(item, terms, calls, marker_genes):
    text = item["title"] + " " + item["summary"]
    rsids = set(re.findall(r"\brs\d+\b", text)) | set(item.pop("matched_rsids", []))
    genes = set(item.pop("genes", [])) | {g for g in terms.get("genes", [])
                                               if re.search(r"\b" + re.escape(g) + r"\b", text, re.I)}
    genes.update(marker_genes[r] for r in rsids if r in marker_genes)
    local = {}
    for rsid in sorted(rsids):
        call = (calls or {}).get(rsid)
        if call is not None:
            local[rsid] = call if isinstance(call, dict) else call.as_dict()
    item["matched"] = {"rsids": sorted(rsids), "genes": sorted(genes), "calls": local}
    item["category"] = categorize(genes=genes, text=text, default=Category.RESEARCH).value
    return item


def _store(conn, item, watch_id, stamp):
    # PMID/DOI identify a paper across Europe PMC and LitVar. ClinVar revisions are distinct items.
    existing = conn.execute(
        "SELECT id, matched_json FROM research_item WHERE (source=? AND external_id=?) "
        "OR (? IS NOT NULL AND pmid=?) OR (? IS NOT NULL AND lower(doi)=lower(?)) ORDER BY id LIMIT 1",
        (item["source"], item["external_id"], item["pmid"], item["pmid"], item["doi"], item["doi"]),
    ).fetchone()
    if existing:
        matched = json.loads(existing["matched_json"])
        for key in ("rsids", "genes"):
            item["matched"][key] = sorted(set(item["matched"][key]) | set(matched.get(key, [])))
        item["matched"]["calls"] = {**matched.get("calls", {}), **item["matched"]["calls"]}
        item["category"] = categorize(genes=item["matched"]["genes"], text=item["title"] + " " + item["summary"],
                                      default=Category.RESEARCH).value
        conn.execute("UPDATE research_item SET last_seen=?, matched_json=?, category=?, "
                     "pmid=coalesce(pmid,?), doi=coalesce(doi,?), pmcid=coalesce(pmcid,?) WHERE id=?",
                     (stamp, json.dumps(item["matched"]), item["category"], item["pmid"], item["doi"],
                      item["pmcid"], existing["id"]))
        item_id = existing["id"]
    else:
        columns = ("source", "external_id", "title", "journal", "year", "pub_date", "doi", "pmid", "pmcid",
                   "url", "summary", "category")
        cur = conn.execute(
            "INSERT INTO research_item(" + ",".join(columns) + ",matched_json,first_seen,last_seen,watch_id) "
            "VALUES (" + ",".join("?" for _ in range(16)) + ")",
            [item[k] for k in columns] + [json.dumps(item["matched"]), stamp, stamp, watch_id],
        )
        item_id = cur.lastrowid
    index_doc(conn, kind="research", ref=str(item_id), title=item["title"], body=item["summary"],
              category=item["category"], ts=item["pub_date"])
    return item_id, bool(existing)


def _eutils_error(search) -> bool:
    """Whether an E-utilities search failed. Terms it could not find (``phrasesnotfound``) are only a notice: the rest
    of the search ran, and markers that ClinVar does not list (research-only variants) are expected."""
    if not isinstance(search, dict):
        return False
    if search.get("ERROR"):
        return True
    problems = search.get("errorlist") or {}
    if not isinstance(problems, dict):
        return bool(problems)
    return bool(problems.get("fieldsnotfound"))


def run_watch(conn, watch, *, http, now, budget=30, calls=None, allow=None) -> dict:
    """Run a bounded primary digest; secondary enrichment errors never freeze its watermark."""
    watch = dict(watch)
    stored = conn.execute("SELECT * FROM watch WHERE id=?", (watch["id"],)).fetchone()
    if stored is None:
        raise ValueError("watch must be persisted before running")
    watch = dict(stored)
    terms = _terms(watch["terms_json"])
    # Medication changes affect terms, never genotype selection.
    terms["drugs"] = (_medications(conn, allow) if watch["name"] == "default" else
                      sorted(set(terms.get("drugs", [])) | set(_medications(conn, allow))))
    sources = _terms(watch["sources_json"])
    stamp = _timestamp(now)
    until = stamp[:10]
    seed = watch["last_since"] is None
    since = ((date.fromisoformat(watch["last_since"][:10]) - timedelta(days=2)) if not seed else
             date.fromisoformat(until) - timedelta(days=365)).isoformat()
    budget = max(0, int(budget))
    queried, errors, fetched, skipped = [], [], [], []
    primary_items, seed_items = [], []
    watermark = watch["last_since"]
    request_start = getattr(http, "request_count", 0)
    ncbi_last = None
    sleep = getattr(http, "sleep", time.sleep)
    clock = getattr(http, "monotonic", time.monotonic)

    def remaining():
        used = http.request_count - request_start if isinstance(http, UrllibHttp) else len(queried)
        return budget - used

    def request(source, url, params):
        nonlocal ncbi_last
        if remaining() <= 0:
            raise BudgetExceeded("request budget exhausted")
        # The real transport paces every physical retry. Fake clients are paced here.
        if "ncbi.nlm.nih.gov" in url and not isinstance(http, UrllibHttp):
            if ncbi_last is not None:
                sleep(max(0, 1 / 3 - (clock() - ncbi_last)))
            ncbi_last = clock()
        queried.append({"source": source, "url": url, "params": dict(params)})
        result = http.get_json(url, params)
        if isinstance(result, dict) and (result.get("error") or _eutils_error(result.get("esearchresult"))):
            raise ValueError("source returned an API error")
        return result

    def epmc(query):
        nonlocal watermark
        # A slice is complete only after all its pages, irrespective of record date order.
        completed_until = watermark

        def page(first, last, cursor="*"):
            data = request("europepmc", EPMC, {
                "query": f"{query} AND FIRST_PDATE:[{first} TO {last}] AND (SRC:MED OR SRC:PPR)",
                "format": "json", "resultType": "core", "pageSize": 100, "cursorMark": cursor, "sort": ""})
            items = [_publication(row) for row in data["resultList"]["result"]]
            fetched.extend(items)
            primary_items.extend(items)
            return data, items

        def complete(first, last, initial=None):
            nonlocal completed_until
            data, items = initial if initial is not None else page(first, last)
            total = int(data["hitCount"])
            if total > 300 and first < last:
                days = (last - first).days + 1
                length = max(1, days * 250 // total)
                cursor_date = first
                while cursor_date <= last:
                    end = min(last, cursor_date + timedelta(days=length - 1))
                    complete(cursor_date, end)
                    cursor_date = end + timedelta(days=1)
                return
            received = len(items)
            cursor = "*"
            for _ in range(2):
                if received >= total:
                    break
                next_cursor = data.get("nextCursorMark")
                if not next_cursor or next_cursor == cursor:
                    raise ValueError("Europe PMC pagination cursor unavailable")
                cursor = next_cursor
                data, items = page(first, last, cursor)
                received += len(items)
            if received < total:
                if first != last or total <= 300:
                    raise ValueError("Europe PMC date slice incomplete")
                errors.append({"source": "europepmc", "error": "DayTruncated",
                               "date": first.isoformat(), "omitted": total - received})
            completed_until = max(completed_until or first.isoformat(), last.isoformat())

        first, last = date.fromisoformat(since), date.fromisoformat(until)
        try:
            initial = page(first, last)
            if seed:
                seed_items.extend(initial[1])
                # Store the complete future overlap as seen, without adding it to the seed digest.
                complete(last - timedelta(days=2), last)
            else:
                complete(first, last, initial)
        finally:
            # An incomplete seed retries its overlap before establishing the initial watermark.
            # Later runs keep every fully completed prefix, including empty date slices.
            if not seed or completed_until == until:
                watermark = completed_until

    pack = load_pack()
    marker_genes = {m["rsid"]: e["gene"] for e in pack.entries for m in e["markers"]}
    marker_genes.update({"rs429358": "APOE", "rs7412": "APOE"})
    rsids = sorted({r for r in terms.get("rsids", []) if re.fullmatch(r"rs\d+", str(r))})
    with http.limited(budget) if isinstance(http, UrllibHttp) else nullcontext():
        # The primary source must run first, regardless of configured source order.
        ordered = (["europepmc"] if "europepmc" in sources else []) + [s for s in sources if s != "europepmc"]
        for source in ordered:
            try:
                if source == "europepmc":
                    genes = _quoted(terms.get("genes", []))
                    topics = _quoted(terms.get("drugs", []) + terms.get("topics", []))
                    if genes and topics:
                        epmc(f"({genes}) AND ({topics})")
                elif source == "clinvar" and rsids:
                    start = 0
                    while True:
                        data = request(source, EUTILS + "esearch.fcgi", {
                            "db": "clinvar", "term": " OR ".join(r + "[snp]" for r in rsids),
                            "retmode": "json", "retmax": 100, "retstart": start, "tool": "caduceus"})
                        search = data["esearchresult"]
                        ids = search.get("idlist", [])
                        if not ids:
                            break
                        result = request(source, EUTILS + "esummary.fcgi", {
                            "db": "clinvar", "id": ",".join(ids), "retmode": "json", "tool": "caduceus"})["result"]
                        for uid in result.get("uids", []):
                            if "error" in result[uid]:
                                raise ValueError("ClinVar summary unavailable")
                            fetched.append(_clinvar(result[uid]))
                        start += len(ids)
                        if start >= int(search.get("count", start)):
                            break
                elif source == "litvar" and primary_items:
                    # Annotate date-filtered primary hits only. No EXT_ID metadata fan-out.
                    by_pmid = {}
                    for item in primary_items:
                        if item["pmid"]:
                            by_pmid.setdefault(item["pmid"], []).append(item)
                    cache = {}
                    offset = date.fromisoformat(until).toordinal() % len(rsids) if rsids else 0
                    rotated = rsids[offset:] + rsids[:offset]
                    for index, rsid in enumerate(rotated):
                        # Leave two requests spare; enrichment is optional and its omissions are visible.
                        if remaining() <= 2:
                            skipped.append({"source": source, "rsids": len(rotated) - index})
                            break
                        try:
                            if rsid not in cache:
                                variant = quote("litvar@" + rsid + "##", safe="")
                                data = request(source, LITVAR + variant + "/publications", {})
                                cache[rsid] = {str(pmid) for pmid in data["pmids"]}
                            for pmid in cache[rsid].intersection(by_pmid):
                                for item in by_pmid[pmid]:
                                    item["matched_rsids"].append(rsid)
                        except BudgetExceeded:
                            raise
                        except Exception as exc:
                            errors.append({"source": source, "rsid": rsid, "error": type(exc).__name__})
                elif source not in SOURCES:
                    errors.append({"source": source, "error": "unknown source"})
            except Exception as exc:
                errors.append({"source": source, "error": type(exc).__name__})

    if "europepmc" not in sources and not errors:
        watermark = max(watermark or since, until)

    # Persist metadata and the delivery atomically. No DB write spans a network request.
    all_ids, new_ids, seen_ids = set(), set(), set()
    primary_ids = []
    seed_keys = {(item["source"], item["external_id"]) for item in seed_items}
    with write_txn(conn):
        for item in fetched:
            if not item["external_id"]:
                continue
            item = _match(item, terms, calls, marker_genes)
            item_id, existed = _store(conn, item, watch["id"], stamp)
            if item_id not in all_ids:
                (seen_ids if existed else new_ids).add(item_id)
            all_ids.add(item_id)
            if (item["source"], item["external_id"]) in seed_keys and item_id not in primary_ids:
                primary_ids.append(item_id)
        candidates = (set(primary_ids) if primary_ids else all_ids) if seed else new_ids
        rows = [dict(conn.execute("SELECT * FROM research_item WHERE id=?", (i,)).fetchone()) for i in candidates]
        if seed:
            # Europe PMC supplied relevance order; secondary metadata cannot displace it.
            ranks = {item_id: rank for rank, item_id in enumerate(primary_ids)}
            rows.sort(key=lambda r: (ranks.get(r["id"], len(primary_ids)), r["id"]))
        else:
            rows.sort(key=lambda r: (r["pub_date"] or "", r["year"] or 0, r["id"]), reverse=True)
        selected = rows[:3] if seed else rows
        kind = "seed" if seed else "new" if selected else "none"
        conn.execute("INSERT INTO delivery(watch_id,run_at,kind,item_ids_json,errors_json) VALUES (?,?,?,?,?)",
                     (watch["id"], stamp, kind, json.dumps([r["id"] for r in selected]), json.dumps(errors)))
        conn.execute("UPDATE watch SET last_run_at=?,last_since=? WHERE id=?",
                     (stamp, watermark, watch["id"]))
    for row in selected:
        row["matched"] = json.loads(row.pop("matched_json"))
    return {"kind": kind, "new_items": selected, "seen_before": len(seen_ids), "queried": queried,
            "errors": errors, "since": since, "until": until, "skipped": skipped}
