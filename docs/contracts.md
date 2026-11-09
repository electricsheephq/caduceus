# Module contracts (frozen for v0.1)

Each module has one owner. Signatures here change only through review. `conn` is a `sqlite3.Connection` from
`core.db.open_vault()`. `samples` is one from `core.db.open_samples()`. Every function that writes does so inside
`core.db.write_txn(conn)`. Every importer is idempotent (natural keys) and resumable: after a crash, the next run adds no
duplicates.

## Shared core (`caduceus/core/`, orchestrator)

| Function | Contract |
|---|---|
| `sources.store_source(conn, path, kind) -> (SourceArtifact, existed)` | Copies the file once into `sources/<sha256>/` beside the vault. |
| `sources.source_file(conn, src) -> Path` | The stored copy. **Importers read only this, never the user's path.** |
| `sources.begin_import(conn, src, parser, version) -> ImportRun \| None` | `None` means this version already imported this source: return `ImportResult(status="skipped")`. It also marks runs left `running` as `interrupted`. |
| `sources.update_progress(conn, run_id, dict)` | Call about every 10k–50k rows. |
| `sources.finish_import(conn, run_id, *, counts, status, errors=())` | `status` is one of `succeeded`, `failed`, `cancelled`. |
| `search.index_doc(conn, *, kind, ref, title, body, category, ts=None)` | Upsert into the search index. `kind` is in `search.KINDS`; `category` is a `policy.Category`. |
| `search.browse(conn, *, policy, kinds, limit=8) -> dict` | Same hit shape as search, newest first, limited to visible records of the given kinds; `withheld` counts hidden records. |
| `policy.Category`, `policy.Policy`, `policy.allowed(policy, category)`, `policy.categorize(genes=…, text=…, markers=…, default=…)` | Categorize every record you index or store. `markers` applies the fixed categories in `policy.MARKER_CATEGORY` (APOE and the BRCA founder variants), whatever the text says. |
| `policy.visible(policy, category, *, kind=None)` | What the tools show: the item's own category **and** the base category of its kind (`observation` and `condition` → records, `medication` → medication, `research` → research). Hiding `medication` hides a contraceptive too. |
| `policy.medication_category(text)` | The category of one of the user's own medications: sensitive when its text says so or when the drug is prescribed mainly for a sensitive condition (an SSRI, a contraceptive). Never used for gene–drug records, which stay drug-response items. |
| `contracts.ImportResult`, `contracts.Call`, `contracts.CallState`, `contracts.SourceKind` | Shared types. |

**Importer error handling:** catch exceptions, call `finish_import(status="failed", errors=[...])`, and return the result. Check
an optional `cancel: threading.Event` between batches, and finish `cancelled` when it is set.

## Lane A1: DNA raw data (`caduceus/genome/dtc.py`, `caduceus/genome/calls.py`)

```python
PARSER = "dtc"; PARSER_VERSION = "1"
def import_dtc(conn, src, *, cancel=None) -> ImportResult
def get_calls(conn, markers, *, callset_id=None) -> dict[str, Call]
def callsets(conn) -> list[dict]
def compare_callsets(conn, callset_id, other_id) -> dict
```

**`import_dtc`:**
- **Formats:** 23andMe v3, v4 and v5 (and AncestryDNA), as a `.zip` or `.txt`.
- **Callset row:** writes one `callset` row (vendor, chip version when known, build) and `genotype_call` rows in batches.
- **Counts:**
  - `markers`, `rs_ids`, `internal_ids`;
  - `no_calls`, `haploid`, `indel_codes`;
  - `skipped_lines`, `chromosomes`.
- **Comparisons:** a successful nonempty import compares against every other complete callset and stores the list
  in `callset.comparison_json` (default `[]`, added on open for older vaults). Warnings say same person, different
  person, or too few shared markers; skipped re-imports do nothing. Warnings never include genotypes.

**`compare_callsets`:** SQL compares only shared chromosome 1–22 markers with two ACGT alleles in both callsets,
ignoring allele order. Returns `other_callset_id`, `other_file`, `compared`, `matching`, `concordance` (rounded to
three places, or `None`), and `same_person` (`None` below 1,000 comparisons, otherwise whether the match rate is
at least 95%). Tools warn when the active complete callset has a different-person comparison; summary includes
each callset's stored comparisons. The active callset remains the newest complete one.

**`get_calls`:**
- **Callset:** uses the newest *complete* callset when `callset_id` is None: its source has a succeeded `dtc` import and at least one row. Rows from a running, cancelled or failed import stay stored, so the next run can resume them, but they are never looked up. When only incomplete callsets exist, `NoCallset` says the import has not finished. An explicit `callset_id` is used as given (diagnostics). `callsets()` rows carry `complete` (0 or 1).
- **States:** a marker that is not in the callset is `NOT_ON_CHIP`, and `--` is `NO_CALL`.
- **Aliases:** names are resolved through `marker_alias`. An alias that maps to several markers is `UNRESOLVED`.

## Lane A2: Promethease (`caduceus/genome/promethease.py`, `caduceus/genome/report.py`)

```python
PARSER = "promethease"; PARSER_VERSION = "1"
def decode_report(path, *, max_inflate=1 << 20, max_total=128 << 20) -> DecodedReport
def import_promethease(conn, src, *, cancel=None) -> ImportResult
def build_report(conn, out_dir, *, policy, source_id=None) -> Path
def parity(conn, source_id, *, report_path=None) -> dict
```

**`DecodedReport`:**
- `records: list[dict]`, the list-chunk records in order;
- `genotype_info: dict[str, dict]`, the rsinfo and othersinfo maps, tagged with their set;
- `metadata: dict`, `chunks: int`, `warnings: list`.

**`import_promethease`:**
- **`annotation_snapshot` rows:** one per kept record, with `ordinal` = its position among the kept records (file order), `kind` `snp` or `genoset`, and `record_json` verbatim. Genotype-info rows follow the records. Warnings name a record by its position in the file, which differs from the stored ordinal once an earlier record was dropped.
- **The genotype maps** (`rsinfo`, `othersinfo`) go in as `kind="genotype_info"` rows.
- **Aliases:** `marker_alias` from `redirect_from`.
- **Search:** the records are indexed (kind `annotation`).
- **Categories:** each record gets a `policy.categorize` category.

**Decode limits:**
- **Inflated bytes:** 1 MiB per chunk and 128 MiB in total.
- **Counts:** items per chunk, keys per dict chunk, 5,000 JSON nodes per record and 5,000,000 in total.
- **Text depth:** 32 levels.

Exceeding a total raises `ReportTooLarge`.

An item over the per-record node limit, or with text deeper than 32 levels, is dropped with a warning. Text the classifier cannot reach is never stored or published.

The owner's real report (counts only): 3,416 chunks of under 20 KB each, 21 MB inflated, 1.83M nodes, at most 134 per record, at most 4 levels deep, and decode at about 216 MB RSS. The node cap is set above that real total. A crafted file that stays within every cap peaks near 400 MB.

**`build_report`:**
- **Output:** writes one self-contained HTML **variant report**, in its own design, with no external resources.
- **Data block:** embeds its data in `<script type="application/json" id="caduceus-data">`, so `parity` can re-read it.
- **Privacy:** honours the policy, and states how many items were withheld.
- **Source:** when `source_id` is None, the newest source with a succeeded Promethease import; `NoSuccessfulReport` (a `ValueError`) when there is none.

**`parity`:** compares the decoded, stored and rebuilt sets.
- **What it compares:** counts, the magnitude and repute histograms, the set of (`rsnum`, `geno`) pairs, and the SNP/genoset split.
- **Returns:** `{"ok": bool, "decoded": {...}, "stored": {...}, "rebuilt": {...}, "diffs": [...]}`.

## Lane B: Apple Health and records (`caduceus/health/`, `core/schema/samples/20_samples.sql`)

```python
PARSER = "apple_health"; PARSER_VERSION = "1"
def import_apple_health(samples, vault, src, *, cancel=None) -> ImportResult
def import_fhir(vault, src, *, cancel=None) -> ImportResult
def metrics(samples) -> list[dict]
def trend(samples, metric, *, start=None, end=None, bucket="week") -> dict
def compare_windows(samples, metric, *, pivot, before_days=28, after_days=28) -> dict
def daily_check(samples, *, thresholds=None, today=None) -> dict
def observation_series(vault, name_or_code, *, start=None, end=None, allow=None) -> dict   # in health/fhir.py
def trend_svg(points, *, unit, title, markers=()) -> str
```

**`import_apple_health`:**
- **Run:** the `import_run` lives in the vault; samples go to `samples`.
- **Parsing:** streams a lone Apple Health XML or a safe XML member from a zip. A single `export.xml` takes
  precedence; otherwise exactly one non-`export_cda.xml` XML must contain `<!DOCTYPE HealthData` or `<HealthData`
  in its first 4096 bytes. Localized names are supported; size limits and entity guards apply to both paths.
- **Lone XML:** warns that clinical records require the export zip.
- **Clinical records:** `clinical-records/*.json` inside the zip go through the same FHIR code into `observation`, `condition` and `medication_event`.

**Trend results:**
- **Every result carries** `unit`, `source_policy`, `coverage` (with gaps), and `calc_version`.
- **Metric names** are friendly (`weight`, `steps`, `resting_heart_rate`, `sleep_hours`, …) and map to HealthKit identifiers.
- **`observation_series` points** carry `date`, `value`, `unit`, `reference_range`, `interpretation`, `source` and `category`. HbA1c is converted to % (NGSP); other mixed units are never pooled in `points`.
  - With `allow` (a category predicate), hidden results are removed **before** anything else is computed, so `unit`, `other_units`, `coverage` and `unplotted_points` describe visible results only, and `withheld` counts the rest. Callers that show results always pass it.

**Records semantics:**
- **Identity.** A resource is named by its Bundle entry's `fullUrl` when there is one, absolute URL or `urn:uuid` alike (ids repeat across providers and bundles), otherwise by its id. Every stored row lists the identities that assert it (`_caduceus.resource_ids`; for medications, `resource_ids` in the note).
- **Statuses.**
  - Observations with status `entered-in-error` or `cancelled`, Conditions verified `entered-in-error` or `refuted`, and medications `entered-in-error` or `cancelled`, are skipped.
  - If an earlier import stored the same resource, it is withdrawn. Rows are matched by identity, then by time (timestamps compared as UTC instants) and by any of the resource's codes, when the retraction gives them. A row that another resource still asserts is kept, without the withdrawn identity.
  - A `corrected` or `amended` Observation supersedes stored rows with the same identity, code and time, including identical copies other resources sent. Only the correcting resource asserts the new value. Each copy's resource is listed in `_caduceus.superseded` with the content key of the copy it sent, so a re-send of that same copy is ignored as stale, a different value from it is stored as its own, and a later retraction of the correction removes it.
- **Panels.** Each component row stores only its own component, without the panel's own value, interpretation, reference range or notes, so neither a sibling's nor the panel's content sits under another row's category. No row of a panel keeps the narrative (`text`), which describes every part.
- **Medications.**
  - `completed` and `stopped` with an end date give a start and a stop.
  - With no end date, the start is kept with `{"status": ..., "current": false}` in its note. No date is ever invented.
  - A later import of the same medication and date updates the stored status.
  - A row's category never loosens. A sensitive row keeps its category even when a later import describes it under another sensitive category; it then keeps its stored text, and only its status moves (conditions too). The same resource's other events (a new stop) take the kept category and the stored text. A user's own record does the same: details that point at another sensitive category are not applied.
  - When a confirmed medication's category is hidden, the reply is `{"saved": true, ...the details just given, "note"}`: nothing stored earlier (dose, brand, previous dose, whether it already existed) comes back.
  - `medication_event.category` holds each row's category (`policy.medication_category` for the user's own records and for imports).
- **Sensitive categories** come from display text first, then from codes in `code`, in components and in `valueCodeableConcept`:
  - ICD-10 (CM and WHO) and ICD-9-CM ranges, written with or without the dot: mental health and pregnancy, plus Alzheimer's disease and the dementias (G30, F00–F03; 290, 294.1, 331.0), genetic susceptibility to and family history of cancer (Z15, Z80; V84.0, V16) and carrier status (Z14, Z84.81; V83);
  - a curated list of SNOMED CT concepts;
  - mental-health and pregnancy LOINC codes.

  A code with no system is matched by its shape, so privacy fails closed. The SNOMED list is not exhaustive: unlisted child concepts with no display text are not caught.
- **Known limits (follow-ups):**
  - Without a `fullUrl`, two providers' resources with the same id can still be confused when a retraction gives no time (medication retractions also check the drug and start).
  - A resource imported with a `fullUrl` and retracted without one (or the reverse) is not matched: identities are compared as given.
  - An exporter that writes a new `urn:uuid` for the same resource in every export makes corrections and retractions miss across exports. A missed match is preferred to deleting another resource's row.
  - The drug lists fail closed. Drugs with ordinary uses (diazepam for vertigo, clonazepam for seizures, methadone for pain, sildenafil for pulmonary hypertension) are filed under the sensitive category, so they show only once that category is unlocked.
  - Schema columns added after a vault was created are added on open (`db._migrate_vault`). `medication_event.category` is backfilled from the drug names, a sensitive category the search index already had wins, and the search index then takes the row's category.
  - Corrections and status updates apply in arrival order, not by `meta.lastUpdated`.
  - Sleep nights are assigned noon to noon in each sample's own offset, so a large eastward flight can split one night.

**`trend_svg`:**
- Plots by date.
- The vertical range spans at least a tenth of the values' size, so noise is not drawn as a swing.
- Axis numbers are rounded for reading; the first and last dates are labelled.
- Series of 24 points or fewer show a dot per measurement.
- Values beyond ±1e12 are not drawn (no health value comes close, and the arithmetic stays finite).
- No external resources.


## Lane C: knowledge pack and research watch (`caduceus/knowledge/`, `caduceus/research/`, `core/schema/vault/30_research.sql`)

```python
PACK_VERSION = "2026.11.0"
def load_pack() -> Pack
def normalize_drug(name) -> str | None
def pgx_check(pack, drug, calls) -> dict
def profile(pack, calls) -> dict
def derive(pack, calls) -> list[dict]
def run_watch(conn, watch, *, http, now, budget=30, calls=None, allow=None) -> dict
def default_watch(conn, *, topics=(), allow=None) -> dict
class UrllibHttp: get_json(url, params) -> dict
```

**`pgx_check`:**
- **Status per entry:** `flag` (the user carries the effect allele), `no_flag` (tested and lacks it) or `not_assessed` (absent, no-call, unresolved). `no_flag` and `not_assessed` are never merged.
- **Each entry carries** the evidence level (`CPIC A`, `CPIC B`, `research`, or `CPIC retired` for withdrawn guidance), sources (title, journal, year, PMID, DOI), limits, `chip_reliability`, `category` and `effect_counts`. Markers a chip cannot type (HLA, CYP2D6) are `unreliable` entries that always come back `not_assessed`.

**`derive`:** APOE, CYP2C19, CYP2C9, SLCO1B1, VKORC1 and others, each with a confidence of `called`, `ambiguous` or `not_assessed`.

**`profile`:** shares the per-entry status calculation with `pgx_check`, whose output is unchanged. Returns
`flagged` (up to 20 sorted genes, each with up to 15 sorted generic drugs and its evidence labels), `derived`,
`counts` over entries (`flag`, `no_flag`, `not_assessed`), and an informational `note`. Summary supplies a pack
filtered by `allowed(policy, categorize(genes=[gene], default=Category.PGX))` before aggregation, filters each
derived item's own category, and counts withheld items without naming them. If PGX is hidden, the whole profile
is omitted and counted. Every complete DNA callset supports a profile even without a variant report; existing
`top_findings` are unchanged. With no variant-report rows for a gene, the gene tool looks up that gene's pack
markers with the existing privacy checks and identifies the pack fallback in its note.

**`run_watch`:**
- **Query terms** come from the pack's watch list, the user's medications and their topics, never from genotypes. With `allow` (a category predicate), medications the privacy settings hide are left out, so they never become search terms sent to the research services.
- **Matching** to the user's genotypes happens locally.
- **Dedupe:** by PMID, DOI or ClinVar id.
- **Digests:** the first run gives a top-3 seed digest; later runs give only new items.
- **Pair-first seed:** allowed current drugs select public pack genes/rsids and all pack drug aliases. Pair genes
  supplement the primary query. After primary/ClinVar work, each linked drug gets one relevance-ranked Europe PMC
  page (25, five years, genetics terms), with genes/rsids, drug aliases and genetics terms each restricted to
  `TITLE_ABS`. Empty gene/rsid parts are omitted; a drug with neither gets no pair query. Linked drugs are queried
  by most recent medication start first, with alphabetical ties. At most two requests remaining skips the page
  (reported in `skipped`). Seed LitVar
  requests try pair rsids first, then the daily rotation of the rest; one optional Europe PMC `EXT_ID` page fetches
  the 25 numerically largest unseen pair PMIDs. All requests share the budget; enrichment leaves two spare and
  never advances the primary watermark. Skipped metadata pages report `europepmc-ext` and a PMID count;
  failed metadata pages report errors under `europepmc-ext`. Later runs add no request kinds.
  Titles are unescaped then stripped of inline formatting tags. Abstracts lose raw tags first, then are
  unescaped and stripped of inline tags before truncation to 600 characters. Stored titles are not migrated.
- **Order and explanation:** every digest item includes `why = {reason, drugs, genes, rsids}`, computed locally
  without calls. Tiers are `gene_drug`, `variant`, `general`. The seed interleaves pair-query relevance ranks
  across medications (rank within medication, medication index), using the minimum rank for duplicate papers,
  then newest other gene–drug hits, newest variant hits and primary general relevance order, capped at three.
  Later digests sort each tier newest first. All fetched items are stored as seen; seed/overlap/watermark and
  delivery semantics are otherwise unchanged. Calls affect only local `matched.calls`, never requests or rank.

## Orchestrator glue (`caduceus/tools.py`, `caduceus/registration.py`, `caduceus/brief.py`)

The tools call the functions above and run imports through `core.jobs.submit`. They filter every result through the policy.
Search distinguishes query matches from kind listings with `scope_note`; medication hits carry prescriber `guidance`.
Trend `compare`/`series` add the metric's full stored `data_range` when present, without changing window `coverage`;
`window_note` flags an earlier end. Compares carry `guidance`; drug pivots also suggest a local `op=pgx` call.
