# Promethease report format (as Caduceus reads it)

Promethease 0.1.x delivers a zip containing one `promethease.html` (about 8 MB) plus a small `.txt`. The data is not plain
HTML: it is JavaScript that inflates base64 + zlib chunks at page load. **Caduceus never executes the page's JavaScript.**
It finds the chunks with a regular expression and decodes them itself.

Field names, types and counts below come from a keys-only probe of one real report (Promethease 0.1.166, a 23andMe v3
file). **Every value shown is made up.**

## Chunks

```text
...version":"0.1.166",};var mygenos=[];mygenos.push.apply(mygenos,JSON.parse(decompressString('<b64>')));...
var rsinfo=$.extend({},JSON.parse(decompressString('<b64>')),JSON.parse(decompressString('<b64>')),...);
var othersinfo=$.extend({},JSON.parse(decompressString('<b64>')),...);
```

- **Chunk payload:** each `decompressString('<base64>')` holds base64 of zlib data. Inflate it with `zlib.decompressobj(15 | 32)` and parse the result as JSON.
- **Three shapes** were seen in one report (3,416 chunks):
  - **List chunks (2,506):** `mygenos.push.apply(mygenos, …)` with a JSON **list of up to 10 record dicts**. These are the user's genotype records; 25,051 in all.
  - **Dict chunks inside `var rsinfo=$.extend({}, …)`** and **`var othersinfo=$.extend({}, …)`** (910 in all, mostly 10 keys each): a JSON object mapping `rsnum` → `{ "(A;G)": {"mag": 2.5, "repute": "Bad"}, … }`, the magnitude and repute of every genotype of that SNP, not only the user's. One entry carries `vendor` and `name`. Assign a dict chunk to `rsinfo` or `othersinfo` by its position after the `var rsinfo=` and `var othersinfo=` markers.
- **Metadata:** a JS object literal (trailing commas, not strict JSON) ends right before `var mygenos=[]`. It includes the report version (`"0.1.166"`). Parse it leniently, keeping keys and scalar values. Never evaluate it.
- **The page also has its own code:** `document.write`, jQuery, and a `<script>` UI. Treat all of it as inert text.

## Record fields (list chunks)

| field | type | present in | notes |
|---|---|---|---|
| `rsnum` | str | 25,028 | e.g. `"rs1234"`; absent on genoset records |
| `geno` | str | 25,028 | e.g. `"(A;G)"`, `"(C;C)"`, `"(I;I)"`, `"(-;-)"` |
| `magnitude` | int or float | 25,051 | 0–10, subjective importance |
| `repute` | str | 6,416 | `"Good"`, `"Bad"`, or absent (neutral) |
| `genosummary` | str | 6,328 | one-line meaning of this genotype |
| `genobody` | str | 235 | longer HTML text; strip tags for search |
| `rstext` | str | 14,530 | HTML text about the SNP |
| `genes` | list[str] | 19,563 | |
| `chrom`, `pos` | str, int | 24,209 | |
| `reference` | int | 25,028 | |
| `was` | str | 25,028 | the genotype as the vendor file gave it |
| `heterozyg`, `flipped` | bool | 25,028 | |
| `orientation`, `stabilized_orientation` | str | ~23,700 | `"plus"` or `"minus"` |
| `flip36`, `flip37`, `flip38` | str | 1–2 | rare build-specific flips |
| `ambig` | bool | 1,142 | strand-ambiguous genotype |
| `gmaf` | float | 16,325 | global minor allele frequency |
| `popfreq`, `popfreqallgenos`, `popfreqalleth`, `popfreqallnum` | dict, list, list, list | 19,953 | population frequencies |
| `numrefs` | int | 20,624 | literature count |
| `clinvar_1` | str | 4,263 | ClinVar significance label |
| `clinvar_diseases` | list | 4,145 | |
| `cond` | list | 4,254 | conditions |
| `topic` | list | 2,025 | topics |
| `med` | list | 3 | medications |
| `redirect_from` | str | 386 | an older rsID that redirects here, so store it as a `marker_alias` |
| `rstime`, `genotime` | str (rarely int) | 25,028 / 7,100 | page timestamps |
| `title` | str | 23 | genoset records (no `rsnum`), e.g. `"gs140"` |
| `imgbase64`, `imgurl` | str | 2 | embedded images; store them verbatim, never render them |

**Genoset records** (no `rsnum`) have `title`, `magnitude`, `genosummary`, `genobody`, `genotime`, and sometimes `repute`,
`topic` or `cond`.

## Made-up example

```json
{"rsnum": "rs99999901", "geno": "(A;G)", "magnitude": 2.5, "repute": "Bad",
 "genosummary": "Slightly higher chance of an invented trait",
 "genes": ["FAKE1"], "chrom": "7", "pos": 123456, "reference": 1, "was": "AG",
 "heterozyg": true, "flipped": false, "orientation": "plus", "gmaf": 0.21,
 "rstext": "<p>Invented description.</p>", "numrefs": 12, "topic": ["invented topic"]}
```

## Counts for parity (not fixed numbers)

Parity is checked inside each import: records decoded = records stored = records re-decoded from the rebuilt variant report.
It compares counts, the magnitude and repute histograms, the set of (`rsnum`, `geno`) pairs, and the split of SNP and
genoset records. One real report gave 25,051 records: 25,028 SNP records and 23 genoset records.

## Licence

The annotation text comes from SNPedia (CC BY-NC-SA 3.0) through the user's own report. It stays in the user's local
vault and is never redistributed. The rebuilt report credits SNPedia and is for the user's own use.
