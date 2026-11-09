"""Invented report data and the observed JS container shape; no real annotations."""
import base64
import json
import zlib
import zipfile


def records():
    return [
        {"rsnum": "rs99999001", "geno": "(A;G)", "magnitude": 3.5, "repute": "Good", "genes": ["FAKE1"],
         "genosummary": "Invented luminous marbles café", "genobody": "<p>Quartz <b>lanterns</b></p>",
         "rstext": "<p>Entirely fictional pebbles</p>", "topic": ["synthetic prism"], "cond": ["imaginary cobbles"],
         "redirect_from": "rs99999901"},
        {"rsnum": "rs99999002", "geno": "(C;C)", "magnitude": 5, "repute": "Bad", "genes": ["APOE"],
         "genosummary": "Sealed orchard annotation invented for privacy tests"},
        {"rsnum": "rs99999003", "geno": "(T;T)", "magnitude": 2, "repute": "Bad", "genes": ["FAKEDRUG"],
         "genosummary": "Invented drug response association", "med": ["inventomycin"]},
        {"rsnum": "rs99999004", "geno": "(-;-)", "magnitude": 0,
         "genosummary": "<script>document.write('inert-only')</script>",
         "imgbase64": "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///w==",
         "rstext": '<img src="https://example.invalid/inert" onerror="document.write(1)">'},
        {"title": "gsSynthetic1", "magnitude": 4, "repute": "Good", "genosummary": "Invented bundle of glass beads"},
        {"magnitude": "unrated", "repute": "unknown", "genosummary": "Invented miscellaneous note"},
    ]


def genotype_maps():
    rsinfo = {r["rsnum"]: {r["geno"]: {"mag": r["magnitude"], "repute": r.get("repute")}}
              for r in records() if "rsnum" in r}
    rsinfo["rs99999001"].update(vendor="invented vendor", name="invented chip")
    return rsinfo, {"gsSynthetic1": {"invented": {"mag": 4, "repute": "Good"}}}


def chunk(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(zlib.compress(raw)).decode("ascii")


def chunk_page(*, record_chunks=(), rsinfo_chunks=(), othersinfo_chunks=()):
    """Explicit chunk boundaries for small limit and corruption regression probes."""
    parts = ['var meta={"version":"synthetic"};var mygenos=[];']
    for rows in record_chunks:
        parts.append(f"mygenos.push.apply(mygenos,JSON.parse(decompressString('{chunk(rows)}')));")
    for name, chunks in (("rsinfo", rsinfo_chunks), ("othersinfo", othersinfo_chunks)):
        parts.append(f"var {name}=$.extend({{}}," +
                     ",".join(f"JSON.parse(decompressString('{chunk(value)}'))" for value in chunks) + ");")
    return "".join(parts)


def html_report(*, rows=None, maps=None, bad_chunk=False):
    rows = records() if rows is None else rows
    maps = genotype_maps() if maps is None else maps
    parts = ["<!doctype html><html><head><title>Synthetic report</title></head><body><script>",
             '''var reportmeta={"version":"synthetic-1", label:'invented', count:6, enabled:true, missing:null,};var mygenos=[];''']
    if bad_chunk:
        parts.append("mygenos.push.apply(mygenos,JSON.parse(decompressString('AAAA')));")
    for start in range(0, len(rows), 10):
        parts.append(f"mygenos.push.apply(mygenos,JSON.parse(decompressString('{chunk(rows[start:start + 10])}')));")
    for name, mapping in zip(("rsinfo", "othersinfo"), maps):
        keys = list(mapping)
        chunks = [chunk({k: mapping[k] for k in keys[start:start + 10]}) for start in range(0, len(keys), 10)]
        parts.append(f"var {name}=$.extend({{}}," + ",".join(f"JSON.parse(decompressString('{c}'))" for c in chunks) + ");")
    parts.append("</script><script>document.write('inert synthetic input')</script></body></html>")
    return "".join(parts)


def write_report(path, **kwargs):
    html = html_report(**kwargs)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report/promethease.html", html)
            archive.writestr("report/readme.txt", "Invented synthetic report only.")
    else:
        path.write_text(html, encoding="utf-8")
    return path
