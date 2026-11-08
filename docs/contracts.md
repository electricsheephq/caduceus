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
| `policy.Category`, `policy.Policy`, `policy.allowed(policy, category)`, `policy.categorize(genes=…, text=…, default=…)` | Categorize every record you index or store. |
| `contracts.ImportResult`, `contracts.Call`, `contracts.CallState`, `contracts.SourceKind` | Shared types. |

**Importer error handling:** catch exceptions, call `finish_import(status="failed", errors=[...])`, and return the result. Check
an optional `cancel: threading.Event` between batches, and finish `cancelled` when it is set.

## Lane A1: DNA raw data (`caduceus/genome/dtc.py`, `caduceus/genome/calls.py`)

```python
PARSER = "dtc"; PARSER_VERSION = "1"
def import_dtc(conn, src, *, cancel=None) -> ImportResult
def get_calls(conn, markers, *, callset_id=None) -> dict[str, Call]
def callsets(conn) -> list[dict]
```

**`import_dtc`:**
- **Formats:** 23andMe v3, v4 and v5 (and AncestryDNA), as a `.zip` or `.txt`.
- **Callset row:** writes one `callset` row (vendor, chip version when known, build) and `genotype_call` rows in batches.
- **Counts:**
  - `markers`, `rs_ids`, `internal_ids`;
  - `no_calls`, `haploid`, `indel_codes`;
  - `skipped_lines`, `chromosomes`.

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
def observation_series(vault, name_or_code, *, start=None, end=None) -> dict   # in health/fhir.py
def trend_svg(points, *, unit, title, markers=()) -> str
```

**`import_apple_health`:**
- **Run:** the `import_run` lives in the vault; samples go to `samples`.
- **Parsing:** streams `export.xml` from inside the zip.
- **Clinical records:** `clinical-records/*.json` inside the zip go through the same FHIR code into `observation`, `condition` and `medication_event`.

**Trend results:**
- **Every result carries** `unit`, `source_policy`, `coverage` (with gaps), and `calc_version`.
- **Metric names** are friendly (`weight`, `steps`, `resting_heart_rate`, `sleep_hours`, …) and map to HealthKit identifiers.
- **`observation_series` points** carry `date`, `value`, `unit`, `reference_range`, `interpretation`, `source` and `category`; callers filter points by `category`. HbA1c is converted to % (NGSP); other mixed units are never pooled in `points`.

## Lane C: knowledge pack and research watch (`caduceus/knowledge/`, `caduceus/research/`, `core/schema/vault/30_research.sql`)

```python
PACK_VERSION = "2026.11.0"
def load_pack() -> Pack
def normalize_drug(name) -> str | None
def pgx_check(pack, drug, calls) -> dict
def derive(pack, calls) -> list[dict]
def run_watch(conn, watch, *, http, now, budget=30, calls=None) -> dict
def default_watch(conn, *, topics=()) -> dict
class UrllibHttp: get_json(url, params) -> dict
```

**`pgx_check`:**
- **Status per entry:** `flag` (the user carries the effect allele), `no_flag` (tested and lacks it) or `not_assessed` (absent, no-call, unresolved). `no_flag` and `not_assessed` are never merged.
- **Each entry carries** the evidence level (`CPIC A`, `CPIC B`, `research`, or `CPIC retired` for withdrawn guidance), sources (title, journal, year, PMID, DOI), limits, `chip_reliability`, `category` and `effect_counts`. Markers a chip cannot type (HLA, CYP2D6) are `unreliable` entries that always come back `not_assessed`.

**`derive`:** APOE, CYP2C19, CYP2C9, SLCO1B1, VKORC1 and others, each with a confidence of `called`, `ambiguous` or `not_assessed`.

**`run_watch`:**
- **Query terms** come from the pack's watch list, the user's medications and their topics, never from genotypes.
- **Matching** to the user's genotypes happens locally.
- **Dedupe:** by PMID, DOI or ClinVar id.
- **Digests:** the first run gives a top-3 seed digest; later runs give only new items.

## Orchestrator glue (`caduceus/tools.py`, `caduceus/registration.py`, `caduceus/brief.py`)

The tools call the functions above and run imports through `core.jobs.submit`. They filter every result through the policy.
