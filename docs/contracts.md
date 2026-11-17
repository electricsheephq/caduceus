# Module contracts (frozen for v0.1)

Each module has one owner. Signatures here change only through review. `conn` is a `sqlite3.Connection` from
`core.db.open_vault()`. `samples` is one from `core.db.open_samples()`. Every function that writes does so inside
`core.db.write_txn(conn)`. Every importer is idempotent (natural keys) and resumable: after a crash, the next run adds no
duplicates.

## Shared core (`caduceus/core/`, orchestrator)

| Function | Contract |
|---|---|
| `sources.store_source(conn, path, kind) -> (SourceArtifact, existed)` | Copies the file once into `sources/<sha256>/` beside the vault. |
| `sources.virtual_source(conn: sqlite3.Connection, kind, key: str, label: str) -> SourceArtifact` | Reuses one virtual source per MCP server key, with an empty `stored_path` and no file. |
| `sources.source_file(conn, src) -> Path` | The stored copy. **Importers read only this, never the user's path.** |
| `sources.begin_import(conn, src, parser, version) -> ImportRun \| None` | `None` means this version already imported this source: return `ImportResult(status="skipped")`. It also marks runs left `running` as `interrupted`. |
| `sources.begin_run(conn: sqlite3.Connection, src: SourceArtifact, parser: str, version: str) -> ImportRun` | Always starts a new run; marks stale `running` runs of the same source and parser `interrupted`. |
| `sources.update_progress(conn, run_id, dict)` | Call about every 10k–50k rows. |
| `sources.finish_import(conn, run_id, *, counts, status, errors=())` | `status` is one of `succeeded`, `failed`, `cancelled`. Stores at most 50 errors: when over the cap, keeps the first 49 and the last error. |
| `search.index_doc(conn, *, kind, ref, title, body, category, ts=None)` | Upsert into the search index. `kind` is in `search.KINDS`; `category` is a `policy.Category`. |
| `search.browse(conn, *, policy, kinds, limit=8, allow=None) -> dict` | Same hit shape as search, newest first, limited to visible records of the given kinds; `withheld` counts hidden records. Search, browse and fetch accept an optional `allow(row)` predicate in addition to policy filtering, applied before returning or limiting visible hits; the tools use it to recheck phenotype markers and stored medication notes. |
| `policy.Category`, `policy.Policy`, `policy.allowed(policy, category)`, `policy.categorize(genes=…, text=…, markers=…, default=…)` | Categorize every record you index or store. `markers` applies the fixed categories in `policy.MARKER_CATEGORY` (APOE and the BRCA founder variants), whatever the text says. |
| `policy.visible(policy, category, *, kind=None)` | What the tools show: the item's own category **and** the base category of its kind (`observation` and `condition` → records, `medication` → medication, `research` → research). Hiding `medication` hides a contraceptive too. |
| `policy.sensitive_categories(text, *, among=SENSITIVE) -> frozenset[Category]` | Every sensitive category matched by `categorize`'s gene-word and text patterns within `among`. Only those categories' patterns are evaluated. Does not change `categorize`'s first-match result or stored categories. |
| `policy.text_visible(policy, text) -> bool` | Checks only sensitive categories currently hidden by explicit hides or standard-level locks. With none hidden it returns true without scanning text. |
| `search.medication_text(conn, row) -> str` | Medication drug/brand/note plus its indexed body, keyed by `user:<id>` for user rows and `<id>` for imports. The body preserves FHIR `note`/`reasonCode` words absent from the medication row's metadata-only note. Read-only; no index changes. |
| `policy.medication_category(text)` | The category of one of the user's own medications: sensitive when its text says so or when the drug is prescribed mainly for a sensitive condition (an SSRI, a contraceptive). Never used for gene–drug records, which stay drug-response items. |
| `contracts.ImportResult`, `contracts.Call`, `contracts.CallState`, `contracts.SourceKind` | Shared types. |

**Free-text privacy on read (#43):** `journal`, `phenotype`, `medication` and clinical `report` items
must also pass `allowed` for **every** category in `sensitive_categories` of their words. Search query,
browse and fetch check the full search document title and body, never just its snippet. Other readers check
the stored text: journal text, phenotype trait/text, and report name/conclusion/narrative. Medication readers
combine drug/brand/note with the search-document body: user documents omit notes, whereas FHIR notes and
reasons live only in that body. Search, capture replies, medication listings, brief medications and their
chart markers, trend drug-start dates and research-watch terms/reasons all use this complete text.
Lookup/gene phenotype notes use the same visibility rule; hidden
captures return only `saved` and a note, without names, text or dates. Hidden read items are counted without
their details. Brief questions likewise check every named category. Existing marker, linked-report and base-kind
checks still apply. Annotations, assertions, findings, observations, conditions and research keep their curated
or coded category checks and are **not** re-scanned. Read-time scans run only patterns for currently hidden
sensitive categories and do no scanning when none are hidden. Writes and stored single-category selection are unchanged.

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
def sleep_morning(moment: datetime) -> date
def observation_series(vault, name_or_code, *, start=None, end=None, allow=None, include_other_units=False) -> dict   # in health/fhir.py
def trend_svg(points, *, unit, title, markers=()) -> str
```

**`import_apple_health`:**
- **Run:** the `import_run` lives in the vault; samples go to `samples`.
- **Parsing:** streams a lone Apple Health XML or a safe XML member from a zip. A single `export.xml` takes
  precedence; otherwise exactly one non-`export_cda.xml` XML must contain `<!DOCTYPE HealthData` or `<HealthData`
  in its first 4096 bytes. Localized names are supported; size limits and entity guards apply to both paths.
- **Lone XML:** warns that clinical records require the export zip.
- **Clinical records:** `clinical-records/*.json` inside the zip go through the same FHIR code into `observation`, `condition`, `medication_event` and `clinical_report`.

**Clinical history recovery (`health/replay.py`, #31):**

```python
REPLAY_VERSION = "1"
def inventory(vault) -> list  # every source with any fhir/apple_health run, before opening files
def settings(vault) -> dict  # needed and, when blocked, reason keys only
def replay(vault, *, cancel=None, progress=None) -> dict
```

- Raise `REPLAY_VERSION` whenever a parser change must be applied to history; a FHIR `PARSER_VERSION` bump alone does not trigger recovery.
- Readable Apple files with no safe `clinical-records/*.json` members (including standalone XML) are outside
  clinical history and listed in `skipped_sources`, whatever their runs. An Apple export with clinical members
  needs exactly one succeeded run; every additional run blocks as `ambiguous_runs`, regardless of status or
  zero counts. Apple accumulates counts after each clinical document returns, so a failed document can leave
  committed effects absent from the receipt, even after earlier documents returned zero clinical counts.
- FHIR needs one succeeded effectful run, except that failed runs with returned zero-effect receipts
  remain exempt. An exception inside ingest leaves `{}` and blocks, even if earlier resources committed.
  A cancelled run blocks: it can delete a recorded-only Condition without counting it.
  Base store counters must be present and zero, with no nonzero update/retraction/supersession counters.
  Cancelled, running and interrupted runs and unknown receipts block. No-success zero-effect sources are listed as skipped.
- Missing/unreadable sources (including Apple files without known clinical content), ambiguous receipts/runs,
  and overlapping effectful runs block without writes. Equal finish/start seconds overlap. Order is by the
  effectful run id. A read or parse failure during reconstruction returns `missing_sources` naming that
  source's id and kind. Settings only check file accessibility and ZIP member names; they do not parse
  clinical payloads, so reconstruction may discover a source error that settings cannot report in advance.
- Replay copies original subject/source ids into a full-schema in-memory vault, then uses current `fhir.ingest`
  on whole stored files. `fhir.read_document(path)` shares the FHIR JSON limit; Apple import and replay share
  `apple.clinical_documents(archive, members, *, cancel=None)` after `_safe_members`. Replay never reads samples.
- Publication adds only missing Conditions, uncoded Observations and reports, preserving every existing row.
  Conditions match by dedupe key or identity plus normalized name and code/system; uncoded Observations use
  only their key; reports match by key or identity within the same resource type. Columns, provenance and search
  documents come from scratch ingest; added raw JSON gains `_caduceus.recovered_by="clinical_replay"`.
- Additions, their search documents and the sole replay settings write (`clinical_replay` with version, covered
  source ids and timestamp) commit in one transaction. Changes to the import-run fingerprint
  `(count(*), max(id), max(started_at))`, checked at start and inside publication, cause `retry`, including
  reuse of the newest id by a later-started run. Cancellation/errors publish nothing. A newer replay version
  replaces the marker. On open, absent markers are initialized only when the
  same inventory is empty. The tool runs one `clinical-replay` job under the existing preparation/erase gate,
  refuses admission while jobs run, and exposes `sources_done`/`sources_total` progress without import receipts.
  A replay that finishes after its call answered `running` keeps its result in memory, per plugin data folder;
  the next `op=replay` returns it once with `finished_in_background: true`.
- Replies contain status, sources_read, added counts and blocked/skipped ids only. Settings expose needed and
  blocked reason keys. No record text is returned. Repeated current-version replay returns nothing_to_replay.
- Known limits: recorded-only Conditions with the same code system, code and name merge even with different
  recorded dates (the key uses onset, which is NULL). A pre-0.3.1 collision row remains as stored.

**Health Auto Export (`caduceus/health/hae.py`):**

```python
def ingest_payload(samples, data: dict, source_id, *, run_state, counts, warnings, cancel=None)
def import_hae(samples, vault, src, *, cancel=None) -> ImportResult
def sync_hae(call, samples, vault, *, server, backfill_days, today, cancel=None, local_offset=None) -> ImportResult
```

- **`ingest_payload`:** batches at most 10,000 rows in `write_txn`; HAE natural keys upsert and each metric-day is
  cleared once per run. `counts.samples` counts distinct sample dedupe keys written across the whole run,
  including upserts, so repeated buckets count once; `samples_replaced` still counts deleted rows and `workouts`
  still counts newly inserted workouts. Heart rate uses `avg` when present, otherwise `Avg`, otherwise `qty`;
  missing all three skips the entry. Its `min` and `max` are ignored. Sleep keys include their category, and sleep `local_day` is the start's noon-to-noon
  morning in the end's offset (which still supplies `tz_offset_min`). Sleep nights in `run_state["sleep_keep"]`
  (a set of ISO dates) preserve stored rows except for each source whose summary the file uses on that night:
  its HAE sleep rows are cleared before insertion, once per run per night/source using
  `run_state["sleep_sources_cleared"]`. Other sources and Apple rows stay. Nights before
  `run_state["sleep_clear_from"]` when that local `date` cutoff is set are upsert-only; nights outside these
  protections still replace the whole HAE night. Sync sets no `sleep_keep`, so is unchanged.
  Other metric types keep the usual replacement rule. In `counts["metrics_ignored"]`, unknown
  metrics keep their names only when they match `^[a-z][a-z0-9_]{0,63}$`, up to 50 distinct names per run;
  repeats of kept names keep counting there. Every other unknown metric counts under `"other"`, for files and sync.
  Entry dates use `date` when present, otherwise `start`; missing or invalid dates are skipped. Offset-bearing
  24-hour and ISO timestamps and locale-independent 12-hour AM/PM timestamps (hours 1–12, optional dots and
  colon in the offset) are accepted. `source` still defaults to `"Health Auto Export"`; merged `sources` is ignored.
  Sleep segments with `value` keep their explicit interval. Summaries without `value` use `totalSleep` when
  above zero, otherwise sum `asleep`, `core`, `deep` and `rem`. All supplied duration fields (including `inBed`)
  must be finite, nonnegative numbers, and the chosen sleep total must not exceed 24 hours. A valid zero-sleep
  summary counts as parsed but returns no rows, stores nothing and does not clear stored nights; both ingestion
  and the file pre-scan accept that empty row list. Export summaries keep `sleepEnd`/`inBedEnd` as the end and
  subtract the hours. Other summaries use `_sleep_bucket_start(entry, hours)` and add the hours, ignoring the
  bucket's `end`: the interval ends at noon of the bucket start's day in its offset and starts `hours` earlier,
  so even a night over 12 hours stays in that day's noon-to-noon night.
- **`import_hae`:** imports a stored JSON file or safe JSON zip members. Before ingesting, scans valid sleep entries
  across the whole file, including every JSON member, tracking segment and summary sources per night. Sets
  `sleep_keep` to the earliest and latest segment nights, except when every segment source on an edge night also
  has a summary for that night in the file. A summary from another source cannot remove that protection.
  For each night and source with both formats in the file, uses the summary and omits that source's segments;
  `counts.sleep_segments_omitted` counts the omitted valid segment entries (absent when zero), and a warning
  lists only the affected night dates, never source names. `run_state["sleep_summaries"]` maps nights to summary
  source sets for this file-only selection. Does not set `sleep_clear_from`.
  Non-JSON members are skipped unopened.
- **`sync_hae`:** `call(tool, arguments)` supplies Hermes MCP envelopes. Sleep requests start one day before
  `since`, in seven-day windows, with `sleep_clear_from=since` and no `sleep_keep`. Quantity windows span 14 days,
  workout windows 31 days. A logical window S–E sends plain-date `start=S`, `end=E+1 day`, including after
  truncation splits, so the boundary day is requested on both sides when end dates are inclusive and no day is
  lost when they are exclusive. Duplicate buckets upsert the same natural key without doubling rollups.
  The quantity request includes every `MAPPING` name except `body_mass` and `sleep_analysis`, using
  `weight_body_mass` for weight; files still accept both weight names. Sleep requests keep `aggregate=False`
  and accept both summaries and segments. The phone interprets request dates in its own time zone;
  `local_offset` is retained for caller compatibility but no longer affects requests. Only truncated replies split
  a window. An unusable non-truncated reply counts as `windows_empty` and warns using only request dates;
  usable empty data objects count as readable and give no warning. A run with some readable replies may succeed
  despite unreadable windows. A run with unreadable windows and no readable replies fails, appending last:
  "None of Health Auto Export's replies could be read, so nothing was imported. The next sync reads these days again."
  Succeeded runs store `through=today`; failed runs never advance the successful-sync watermark.
  A first sync starts at `today - (backfill_days - 1)`. Until a run of this source succeeds, the most recent
  earlier `hae_sync` run with a stored `since` preserves that start: takes the earlier of its `since` and the
  backfill start, never before `today - 3650 days`. After a succeeded run, starts two days before that run's
  `through`, with the same 3,650-day floor. If that latest succeeded run's `parser_version` differs from
  `PARSER_VERSION` (now `"3"`), re-reads from the earlier of that overlap start and the earliest recorded `since`
  among all this source's `hae_sync` runs, including failed or interrupted runs, with the same 3,650-day floor.
  Records `counts.reread=true` only when this version rule fires. A failed re-read does not advance the succeeded
  run, so the next sync re-reads again; after a successful re-read the same parser returns to the two-day overlap.
  Stores ISO `since` and `through` in the running import's `counts_json`
  inside `write_txn` before its first MCP request, so an interrupted first sync also preserves its start;
  `finish_import` replaces these with the full counts.
  Sync-only `counts.entries_by_kind` holds run-wide `parsed` and `skipped` entry totals for `metrics`, `sleep`
  and `workouts`, collected only from readable replies; unknown metrics in `metrics_ignored` count as neither.
  A parsed entry counts even when it upserts an existing row. `run_state["sync_entries"]` shares these totals;
  `sync_kind` identifies the request kind for malformed metric containers with no usable name. After all windows,
  any kind with skipped > 0 and parsed == 0 fails the run and appears in `counts.unreadable_kinds` (a list of
  kind keys, absent when empty). Each gets the fixed warning: "Health Auto Export sent <kind> data that Caduceus
  cannot read (<reason>), so none was imported. The next sync reads these days again." The label is `health metric`, `sleep`
  or `workout`, with no server text. `<reason>` is the plain words for that kind's most frequent skip reason
  (ties go to the first in this order): `time` "times not recognised", `source` "source not recognised",
  `number` "values not numbers", `missing` "fields missing", `interval` "end before start", `other`
  "unexpected format". Every skipped entry, in syncs and file imports, adds 1 to
  `counts.skip_reasons.<metrics|sleep|workouts>.<code>`, so `entries_skipped` equals their sum. The code is the
  first fault an entry raises; no value, name or server text is recorded. A time string longer than 64 characters
  is rejected as `time` before it is parsed. Already written rows stay, and the failed run never moves the watermark;
  retrying replaces them. A kind with some parsed entries keeps the usual malformed-entry warnings and does
  not fail for partial skips. Empty metrics lists succeed. This failure rule does not apply to file imports.
  A permission-denied run with no sample/workout
  writes and no other runs for its virtual source removes its run and source together; the failure still comes
  back in the result. Other failures retain the run so the next sync can report it, even on a first empty sync.
  An `ok=false` server envelope fails with exactly: "Health Auto Export did not answer the request. Check that
  the app is open and its MCP server is running." Server-provided error text is never copied into warnings,
  stored errors, progress or logs; `previous_sync.error` receives the fixed message too.
  Skipped one-day truncated windows fail the run, retain writes and never advance the successful-sync watermark.
- **Run exclusion:** both HAE entry points begin a visible `running` run before acquiring a per-samples-file
  thread lock, held through rollup recomputation. On systems with `fcntl`, a nonempty `main` path from
  `PRAGMA database_list` also uses an exclusive nonblocking flock on `<samples path>-hae-lock`. In-memory databases
  and systems without `fcntl` use only the thread lock. Waiting polls at intervals of at most 0.25 s, checks
  cancellation, and records `{"waiting": "another Health Auto Export run"}` once. Every exit releases the locks;
  erase removes the lock file with the databases.
- **Capture `op=sync`:** before submitting the job, reads the source's latest finished `hae_sync` run. If it is
  `failed` or `interrupted` and its `progress_json` has no truthy `reported` flag, all done/running/busy results
  include `previous_sync` with `import_run_id`, `status` and `error` (the last stored error, or null).
  A later succeeded run suppresses this notice. When the job finishes within the caller's wait (`done`), marks
  that run's `progress_json.reported=1` in `write_txn`, since its final status was returned directly; a deleted
  denied run needs no row. The `hae-sync` blueprint reports an earlier failure and its error instead of staying
  silent when `previous_sync` is present.
- **`trends.sleep_morning(moment: datetime) -> date`:** names a noon-to-noon night by its morning in the supplied
  offset: the date plus one day when the hour is at least 12. Used by both HAE partitions and sleep rollups.

**Trend results:**
- **Every result carries** `unit`, `source_policy`, `coverage` (with gaps), and `calc_version`.
- **Metric names** are friendly (`weight`, `lean_mass`, `protein`, `waist`, `steps`, `resting_heart_rate`, `sleep_hours`, …) and map to HealthKit identifiers.
- **`observation_series` points** carry `date`, `value`, `unit`, `reference_range`, `reference_ranges`, `interpretation`, `source` and `category`. HbA1c is converted to % (NGSP); other mixed units are never pooled in `points`.
  - `reference_ranges` preserves every range on the stored row's own part (its component for panel rows), without conversion. Present keys only: `low`/`high` quantities (`value`, `unit`, `code`, `system`), `text`, `type` (display or code), `applies_to` (display or code list), and `age` with `low`/`high` quantities.
  - Compatibility `reference_range` keeps the first range's numeric bounds. If a bound supplies a unit or code matching neither the result's original unit nor its quantity code, `reference_range_mismatch: true` nulls both bounds. Unitless bounds keep existing behavior; known HbA1c/glucose conversions apply only to comparable bounds. The brief shows the first range's text for mismatches or text-only ranges. Mismatches append "(different units)", or use "not comparable: different units" without text. When a converted point displays recorded text without a mismatch, it appends "(in <original unit>)" using `converted_from`. Unit comparison stays exact and case-sensitive; recorded text is never converted. Stored JSON is unchanged.
  - Read helpers accept an object or an array for categories, coding, reference ranges, type, appliesTo, age and quantity bounds, skipping non-object entries and unreadable fields. Singular output fields use the first readable object; missing labels are omitted rather than invented. Ingest behavior is unchanged.
  - With `allow` (a category predicate), hidden results are removed **before** anything else is computed, so `unit`, `other_units`, `coverage` and `unplotted_points` describe visible results only, and `withheld` counts the rest. Callers that show results always pass it.

**Records semantics:**
- **Clinical reports.** DiagnosticReport, DocumentReference, Composition and ImagingStudy are ingested into
  `clinical_report`, one row per resource, and indexed as search kind `report`. The idempotent vault schema
  creates this table on opening existing vaults. `name`, status, ordering date (`effective_at`), issued date,
  conclusion, narrative, category, source provenance and sanitized raw JSON are retained. DiagnosticReport uses
  its code and effective date/period start, falling back to issued; conclusionCode displays join the narrative.
  DocumentReference uses its type, status (or docStatus), date (or context period start), and description.
  Composition uses its type (or title), date, title and recursively ordered section titles/text; section entry
  references are not followed. ImagingStudy stores description (or modalities) and started date with modalities,
  series body sites and series/instance counts in metadata; instance data is omitted. Missing/invalid dates skip
  the resource with reason `date`; a valid fallback date is accepted. Partial date precision is preserved.
  Reports' categories accept one concept or a list, ignoring non-object entries. Code, conclusion codes and
  category concepts use `_category`; sensitive title, conclusion, conclusionCode displays, narrative,
  description and attachment-title text takes precedence. Linked Observations and ImagingStudy rows resolved at write time promote
  an ordinary report to a sensitive category; the existing first-sensitive tie rule remains. The row and index
  share that category, and hiding `records` hides all reports, including sensitive ones. Malformed optional
  fields are skipped: concept text/coding display/code accept strings or numbers (converted to strings),
  ignoring other shapes; non-object text/effectivePeriod/context.period are ignored. A DocumentReference
  context list uses its first object.
- **Report attachments.** URLs are never fetched. Only inline base64 text/plain, text/html,
  application/xhtml+xml and text/markdown up to 2 MiB decoded are read as UTF-8. ASCII whitespace is stripped
  from base64 before size estimation and strict decoding, accepting line-wrapped exports. HTML uses `html.parser`,
  omits script/style contents, decodes entities and collapses whitespace. Invalid base64 or unreadable UTF-8
  counts as `skipped:<resourceType>:attachment_unreadable`, with a plain-language warning; other attachments
  and oversized text are metadata only (no PDF extraction). `_caduceus.attachments` lists `content_type`,
  declared `size`, `title`, `has_inline_data`, and `has_url`. The entire copied resource tree is stripped of
  `data` on every Binary and every attachment-shaped object (`contentType`, `url`, `title` or `size` beside
  `data`), including nested and contained resources. Only the report's own supported attachments are decoded;
  nested bytes do not enter its narrative. The original input and private source file remain intact.
  DiagnosticReport uses its own text.div when
  no usable text attachment exists. Narrative is capped at 200,000 characters and marked
  `_caduceus.narrative_truncated: true` when cut; privacy classification sees the text before truncation.
- **Report identity and updates.** Reports use fullUrl/id identity, except an export-local `urn:uuid:` fullUrl
  with a resource id uses that id for dedupe and retraction. With no id, the urn remains the identity; absolute
  provider URLs remain distinct. Every observed fullUrl/id is retained in `_caduceus.resource_ids`; the
  `identity_aliases` list tracks aliases of the report's own identity for retraction. The dedupe key is
  `_key(["Report", resource_type, identity, code_system, code, date])`, or
  `_key(["Report", resource_type, code_system, code, date, normalized_name])` without identity. Re-reading
  adds no rows. Changed statuses/content update the same row and index (`report_updated`) only when the
  incoming status rank is at least the stored one: registered/partial/preliminary < final/unknown <
  amended/corrected/appended, using docStatus when supplied for DocumentReference. When both give issued,
  an earlier incoming issued date also prevents replacement. A same-rank copy missing issued cannot replace
  a stored copy with issued. Skipped older copies still add their identities and imported source ids;
  stored content, status, issued date, source and index remain. A previously sensitive row keeps its category;
  conflicting sensitive-category text is not substituted under it, and retained references keep their source.
  `entered-in-error` for any report type (including DocumentReference docStatus), and `cancelled` for
  DiagnosticReport/ImagingStudy, withdraw only that type's identity assertions and aliases, including their
  `report` search documents, counting deleted rows as `report_retracted`. Supplied codes narrow by code and
  system when given; supplied dates narrow by the same instant. An invalid supplied date cannot broaden a
  withdrawal. A row still asserted by another identity remains. Retractions without codes/dates match identity.
  **Known limits:** two providers' reports sharing a bare id, code and date under `urn:uuid:` fullUrls merge
  into one row. A retraction by that bare id withdraws the merged row. Provider-scoped UUID dedupe is deferred.
- **Report search and fetch.** Search body contains title, conclusion and narrative only, never raw JSON or
  attachment bytes. `search.fetch("report:<id>")` retains the usual ref/kind/title/body/category/date fields,
  adding status, conclusion, narrative, attachments, results, imaging and `withheld`. DiagnosticReport's
  original `result` and `imagingStudy` references remain in `_caduceus` without following URLs. Linked rows
  use two resolutions. Privacy (write-time categorization and read-time withholding) matches every row of the
  target type by exact resource_ids, identity suffix `/` + reference, or bare id matching `Observation/<id>` /
  `ImagingStudy/<id>`, across all sources. However the reference is written (an absolute URL with any base, a
  `/_history/<version>` suffix, a query or fragment), its trailing `Type/id` is also matched for privacy.
  Over-withholding on shared short ids is the accepted safe side. Results or studies contained in the report and
  referenced as `#id` are not stored on their own: their sensitive categories are recorded with the report
  (`_caduceus.contained_categories`), lift its category at import, and are checked on every read. Search, browse and
  fetch each read their rows and visibility checks in one read snapshot.
  Displayed results/imaging require an exact resource_ids match, an exact identity after resolving the relative
  reference against the report's absolute fullUrl FHIR base, or a relative match in any of the report's imported
  sources. `_caduceus.source_ids` retains all imported source ids on both reports and deduplicated Observations,
  including skipped older report copies; narrow resolution uses membership in both histories. Legacy rows
  without that list use row.source_id. Observations also retain every contributing `Observation/<id>` in
  `_caduceus.typed_ids`, so deduplication under UUID fullUrls does not lose privacy matches. These aliases remain
  after partial retraction; they are not resource assertions and cannot keep a withdrawn row alive.
  An absent source id does not establish a shared artifact. Each search,
  browse, fetch or ingest call shares one ephemeral resolver, lazily projecting only row id, source id, category,
  raw resource id, resource_ids, typed_ids and source_ids once per linked table. Dictionary lookups resolve references; ingest refreshes
  affected entries after later writes. No resolver or policy decision is cached between calls.
  Fetch reads visibility and payload in one read transaction, reusing an existing transaction without closing it.
  Transactions opened by fetch end in `finally`, including on errors; vanished linked rows are skipped.
  Search, kind browse and fetch withhold the whole report whenever any resolved linked Observation or ImagingStudy is hidden
  under the current policy, including after late imports or category changes. A withheld report contributes
  one count and returns no title, text, date or attachments. Results expose visible observations' ref/title/name/date/category,
  status, code/system, value_num/value_text and unit; imaging exposes visible ImagingStudy ref/title/date,
  category/status and modality/body-site/count metadata. Each matched row appears once. A hidden linked
  Observation or ImagingStudy withholds the entire report, and a sensitive linked row's category is the report's
  category at import when its own is not sensitive. Hidden reports return the usual withheld response without content. No hidden
  linked name or value is returned. The public search tool's kinds enum includes `report`.
  Word checks (0.4.1, 0.4.2): search, browse and fetch also run `text_visible` over the report's status and every key
  and string in the stored `_caduceus.attachments`, whatever its shape (titles, content types, malformed containers),
  and withhold the whole report when one names a hidden topic. A linked ImagingStudy row is withheld and counted when its name, conclusion,
  narrative, status, stored modalities, body sites or series/instance counts name a hidden topic. Each field is passed to
  `text_visible` separately, so one field's last word never hides the next field's topic from a lookbehind exclusion.
  `text_visible(policy, *texts)` and `categorize(text=...)` read text through `plain_text`: NFKC, invisible in-word
  characters (soft hyphen, zero-width space and joiners, word joiner, BOM) removed, every whitespace run one space.
  They test the text as stored, the whole text as one plain line (invisible characters dropped, and read as spaces)
  and each of its lines on its own; a topic counts when any reading names it. Reading the stored text keeps every
  earlier match (folding can remove a boundary: BRCA1 plus a superscript 1 is BRCA11), a wrapped phrase is found,
  and a line's last word never switches off a match on the next line. `categorize` takes the readings one at a time,
  the stored text first, and returns the first category found, so a stored text that names a topic keeps the
  category earlier versions gave it.
- **FHIR import receipts.** `report` counts newly stored report rows. `ignored` remains the total count of
  unsupported resources; `ignored_by_type` splits it by resourceType, retaining only 1-64 ASCII letters,
  otherwise `other`. `observation_by_category` counts newly stored observation rows once per distinct category
  using the shared `observation_categories` helper, including component rows. Standard observation-category
  codes alone are literal, other codes become `other`, and missing readable categories become `unspecified`.
  These aggregate receipts are independent of visibility. Re-reading a deduplicated row adds no count.
- **Repeated corrections.** A file that re-sends an already stored corrected/amended observation with the same
  content key upserts it without deleting it or counting another insertion or supersession; its superseded-copy
  references remain intact. Its reference-range low/high bounds and interpretation refresh from its incoming
  part using the same extraction as an insert; non-corrected updates are unchanged.
  Re-importing a file already read by this parser version is skipped, so records that
  earlier versions skipped or merged are not recovered by a re-import.
- **Uncoded observations.** When both `code_system` and `code` are NULL, the content key appends the concept's
  lowercased, whitespace-collapsed, stripped name (the component's own name for panel rows); non-string names
  are converted with `str()` before normalization. Identity-based correction lookups also match that normalized
  name. Coded observation keys are unchanged. On open,
  `db._migrate_vault` recomputes existing uncoded FHIR observation keys from their stored name, effective date,
  numeric/text value and unit with the same key helper. Values come from the row's own resource or component
  in `raw_json` when present, preserving negative zero and numeric text/unit types that SQLite columns coerce;
  value extraction is shared with the importer, whose keys do not change. Collisions keep the existing key and data. The settings row
  `migration:fhir_uncoded_names_v1` guards this migration once, in the same write transaction. Previously merged
  concepts cannot be reconstructed; the kept row survives, and re-import adds the other concept.
- **Condition dates.** `condition.recorded` is nullable and is added on open for older vaults. `recordedDate`
  is validated with the same precision-preserving normalization as onset, stored even when onset is present,
  and its precision is kept in `raw_json._caduceus.recorded_precision`. A Condition with no onset but a valid
  recorded date has NULL onset and key `_key([system, code, None, name])`; known-onset keys are unchanged.
  Its search timestamp uses the recorded date and its title appends ` [onset unknown; recorded <date>]`.
  When a sensitive-category conflict keeps the stored Condition document, its `recorded` column,
  `_caduceus.recorded_precision`, search timestamp and recorded-date title also stay with that document;
  clinical/verification status may still update. When the incoming document wins, its recorded values apply.
  Identity-based retraction removes a matching NULL-onset row even when the retraction gives an onset.
  A known onset withdraws the same identity's matching unknown-onset assertion through the same removal path,
  including its search document; other identities' assertions remain. Before withdrawal, the sensitive category
  on the lowest-id matching unknown-onset row, if any, applies through `_stricter`. A sensitive-category conflict
  keeps that row's document, recorded date/precision, title text and search body, with incoming clinical and
  verification statuses and onset fields; rejected incoming text is not indexed under any category. An existing
  known-onset row also applies its own category through the usual update path. Without a sensitive withdrawn
  category, the incoming category still applies. A recorded-only copy arriving after a
  matching known-onset row adds nothing, but applies its incoming clinical and verification statuses through
  the existing update path, preserving the stored onset, recorded date and their precisions. The search document
  receives the updated status too; the incoming identity is retained without restoring withdrawn assertions.
  This update targets the known row by id, preserving its existing dedupe key even when a category conflict
  retained a different name. Conflicts are resolved before change detection, so an identical dated re-send
  counts no update and writes nothing when the retained values and statuses are unchanged.
  Conditions with neither valid date are skipped; no
  missing year/month/day or onset is invented.
- **Identity.** A resource is named by its Bundle entry's `fullUrl` when there is one, absolute URL or `urn:uuid` alike (ids repeat across providers and bundles), otherwise by its id. Every stored row lists the identities that assert it (`_caduceus.resource_ids`; for medications, `resource_ids` in the note).
- **Statuses.**
  - Observations with status `entered-in-error` or `cancelled`, Conditions verified `entered-in-error` or `refuted`, and medications `entered-in-error` or `cancelled`, are skipped.
  - If an earlier import stored the same resource, it is withdrawn. Rows are matched by identity, then by time (timestamps compared as UTC instants) and by any of the resource's codes, when the retraction gives them. Conditions with neither a code system nor a code also require the same lowercased, whitespace-collapsed, stripped name (a nameless concept is `Unknown`), as storing does; a malformed retraction concept (not an object, or with an unreadable coding) matches no such row and does not stop the rest of the Bundle. A row that another resource still asserts is kept, without the withdrawn identity.
  - A `corrected` or `amended` Observation supersedes stored rows with the same identity, code and time, including identical copies other resources sent. Only the correcting resource asserts the new value. Each copy's resource is listed in `_caduceus.superseded` with the content key of the copy it sent, so a re-send of that same copy is ignored as stale, a different value from it is stored as its own, and a later retraction of the correction removes it. For uncoded observations, stale matching accepts both the current key and the pre-0.3.1 key without the normalized name, preserving superseded references across migration; coded matching is unchanged.
- **Panels.** Each component row stores only its own component, without the panel's own value, interpretation, reference range or notes, so neither a sibling's nor the panel's content sits under another row's category. No row of a panel keeps the narrative (`text`), which describes every part.
- **Medications.**
  - `completed` and `stopped` with an end date give a start and a stop.
  - With no end date, the start is kept with `{"status": ..., "current": false}` in its note. No date is ever invented.
  - A later import of the same medication and date updates the stored status.
  - A row's category never loosens. A sensitive row keeps its category even when a later import describes it under another sensitive category; it then keeps its stored text, and only its status moves (conditions too). The same resource's other events (a new stop) take the kept category and the stored text. A user's own record does the same: details that point at another sensitive category are not applied.
  - When a confirmed medication's category or any sensitive topic in its stored name/notes is hidden, the reply
    contains only `saved` and a note: no name, date, dose, brand, previous dose or existence details come back.
  - `medication_event.category` holds each row's category (`policy.medication_category` for the user's own records and for imports).
- **Sensitive categories** come from display text first, then from codes in `code`, in components and in `valueCodeableConcept`:
  - ICD-10 (CM and WHO) and ICD-9-CM ranges, written with or without the dot: mental health and pregnancy, plus Alzheimer's disease and the dementias (G30, F00–F03; 290, 294.1, 331.0), genetic susceptibility to and family history of cancer (Z15, Z80; V84.0, V16) and carrier status (Z14, Z84.81; V83);
  - a curated list of SNOMED CT concepts;
  - mental-health and pregnancy LOINC codes.

  A code with no system is matched by its shape, so privacy fails closed. The SNOMED list is not exhaustive: unlisted child concepts with no display text are not caught.
- **Erase backfill disclosure.** Erase preview and confirmation use the same `hae_backfill_days` validation as
  sync: an integer from 1 through 3,650, excluding booleans; other values fall back to 90. The schedule text thus
  describes the backfill the next sync would actually use. Erase targets and settings are unchanged.
- **Known limits (follow-ups):**
  - Without a `fullUrl`, two providers' resources with the same id can still be confused when the available narrowing fields do not distinguish them. Uncoded Conditions always check the normalized name; coded Conditions check codes when supplied. Medication retractions check drug/start when supplied; Observation retractions remain identity/time-based.
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
The five marker definitions live in module-level `DERIVED_MARKERS`, shared by `derive` and the gene tool.

**`profile`:** shares the per-entry status calculation with `pgx_check`, whose output is unchanged. Returns
`flagged` (up to 20 sorted genes, each with up to 15 sorted generic drugs and its evidence labels), `derived`,
`counts` over entries (`flag`, `no_flag`, `not_assessed`), and an informational `note`. Summary supplies a pack
filtered by `allowed(policy, categorize(genes=[gene], default=Category.PGX))` before aggregation, filters each
entry's markers through `_marker_visible` before its flags and counts are aggregated, filters derived items
through `_derived_visible`, and counts withheld entries and derived items without naming them. The `op=pgx`
reply likewise withholds the whole entry (including `your_genotypes` and its status flag) unless every marker
it reports or uses passes `_marker_visible`, as well as its own category. `_pgx` requires the caller's vault
for these checks; the genome tool and medication brief both pass it. With nothing hidden, outputs are unchanged.
If PGX is hidden, the whole profile
is omitted and counted. Every complete DNA callset supports a profile even without a variant report; existing
`top_findings` are unchanged. The gene tool keeps the gene-level policy check first (using PGX as the default
for genes with pack or derived markers even when report rows exist), then looks up the ordered,
deduplicated union of derived-definition markers, knowledge-pack markers and annotated report markers.
`_lookup` still caps results at 50, and every result includes `annotations` (an empty list when none are visible),
for both gene and lookup operations. A reverse marker-to-genes index is built once on first use from the pack
and `DERIVED_MARKERS`. Lookup markers must pass their annotation category and every indexed gene's
`categorize(genes=[gene], default=Category.PGX)` category; aliases check both requested and resolved markers.
Markers outside that index keep their annotation-only rule. When `derive` has a row for the gene, the gene reply
puts that single row under `derived` before `results`. Both gene and derived operations use one visibility helper:
the row's own category and every basis marker's annotation and indexed gene categories must be allowed.
Hidden markers and derived rows are omitted and counted in `withheld`, without naming them. A note identifies
markers supplied by the pack or derived definitions that have no report row. APOE stays hidden at standard level.

**`run_watch`:**
- **Query terms** come from the pack's watch list, the user's medications and their topics, never from genotypes. With `allow` (a category predicate), medications the privacy settings hide are left out, so they never become search terms sent to the research services.
- Medication selection in `default_watch` and every `run_watch` checks each row's category and every hidden
  sensitive topic in its drug/brand/note plus indexed body. The same filtered terms build `_why`'s medication
  pairs, so a hidden medication supplies neither requests nor personalized reason text. Stored custom-watch
  drug terms cannot reintroduce a hidden-only medication, including its normalized aliases; a drug the pack does
  not know is matched by its brand and each word (in any script) of its name. A drug's newest event decides whether it is current even when
  that event is hidden, so a hidden stop or restart keeps it out of the watch. With no sensitive category hidden,
  medication selection does no text scan.
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

**Session memory and specialty briefs (0.4.0 U4a):**
- `caduceus_capture op=consent category=<sensitive category> answer=yes|no|ask_later|clear` records only an answer the user gave in the conversation, never an inferred answer. The five `SENSITIVE` categories are accepted. `settings['consent:<category>']` holds JSON `{answer, at}`; clear deletes it. Without answer, return every recorded answer under `consent`. Every response explains that **consent never changes visibility**; only the user's privacy settings do.
- `op=phenotype marker=<rs/i/gs id, known pack gene or derived-call name> trait=<up to 200 characters> stance=confirms|refutes|unsure text=<the user's words, up to 2,000 characters>` needs marker or trait. `phenotype_note(id, subject_id DEFAULT 1, marker, trait, stance, text, category, created_at)` is added on open, including existing vaults. Markers are lowercased. Check the marker and its lookup-resolved alias at write time: a sensitive category from either takes precedence and is never replaced by the trait/text category. Otherwise sensitive trait/text wins, then PGX for markers with pack or derived genes, then the marker category (TRAIT for a trait-only note). The row and search document (`kind=phenotype`) share that category; the search document's title is `<Stance>: <trait or marker>` (for example `Refutes: lactose intolerance`), so search and fetch keep the stance. Hidden capture replies return only `saved` and a fixed note, without stored details. An identical normalized marker, trait, stance and exact text on the same UTC day reuses the existing row and search document and replies that it was already saved; a later day or changed content records a new note.
- Lookup and gene results attach visible `phenotype_notes: [{id, stance, trait, text, date}]` to matching markers, newest first (id breaks timestamp ties); hidden notes add to `withheld`. Search query/browse/fetch and lookup/gene check the stored category, every sensitive category named in the note's own trait and text (a marker's sensitive category is stored, so the words are re-checked on every read), AND current `_marker_visible` for the note marker and its resolved alias. A lookup of a marker also attaches notes recorded against any alias of it. Without a complete callset, known alias targets are still checked; ambiguous aliases require every candidate to be visible. Hidden notes contribute only to `withheld`, never text, marker or date, even when the marker category changed after capture. Trait-only notes use their stored category and every sensitive category in their words. Gene-name or derived-name notes attach to the gene reply under top-level `phenotype_notes`, using the same checks. Browse stays newest first.
- `brief.write_brief(vault, samples, out_dir, *, policy, today=None, specialty="general", questions=None) -> Path`: specialty definitions and display titles live in `knowledge/data/specialties.json`. General and primary care retain the default sections; other specialties use the stored clinical codes and U3b series readers/ranges and filter gene–drug entries by specialty genes. Specialty lab tables also include visible text-valued `unplotted_points`, once each; points with a different known unit remain excluded. Medications, supplements and Apple Health trends retain their policy. Non-general briefs show `Prepared for: <title>` and use `health-brief-<date>-<specialty>.html`; general keeps its original filename. Unknown specialties raise `ToolError` listing valid names. Up to ten questions of at most 500 characters each appear HTML-escaped at the end; hidden-category questions are omitted and counted. All output remains local.

The tools call the functions above and run imports through `core.jobs.submit`. They filter every result through the policy.
Both erase preview and confirmation explain that schedules armed with `/blueprint` keep running: the next research
watch starts a new vault, Health Auto Export sync re-imports the configured `hae_backfill_days` (default 90), and the
daily check finds nothing until data returns. Settings stay unchanged.
Search distinguishes query matches from kind listings with `scope_note`; medication hits carry prescriber `guidance`.
Trend `compare`/`series` add the metric's full stored `data_range` when present, without changing window `coverage`;
`window_note` flags an earlier end. Compares carry `guidance`; drug pivots also suggest a local `op=pgx` call. A drug
pivot uses the latest visible start; matching starts the policy hides add to `withheld`, never their dates.

**Clinical discovery and counts:**
- `caduceus_trend op=metrics` returns the existing wellness `metrics` plus `clinical_series`, even when wellness is hidden (`metrics: []`, counted in `withheld`). A rollup `rebuilding` note is preserved. Clinical rows pass `visible(policy, category or records, kind="observation")` before grouping; hidden rows add only their count to the top-level `withheld`/`withheld_note`.
- Clinical entries group coded rows by `(code_system, code)`, merging LOINC and systemless (null/empty system) rows with the same code into one bare-code series, and uncoded rows by lowercased, whitespace-collapsed name. A merged entry reports LOINC as `code_system` when any visible row explicitly uses it; its name still comes from the newest visible row. Fields: newest `name`, `code`, `code_system`, sorted distinct non-null `units`, row `count`, numeric row `numeric`, stored dates `first`/`last`, distinct `sources`, `fhir_categories`, and `query`. Entries sort by name, capped at 300 with `clinical_series_truncated` counting omitted series.
- Use `query` with `op=labs lab=<query>`: a LOINC or systemless code, the uncoded name, or `system|code` for another code system. `observation_series` accepts `system|code` as an exact system/code match. If there are no such stored rows, it tries the whole string as an uncoded name (including names containing `|`); a hidden or out-of-window coded match does not trigger this fallback. It normalizes whitespace when matching uncoded names and includes the literal query alongside any alias codes. Lab-name fallback uses the same bare-code versus system-qualified query rule as discovery. Clinical vital signs never enter sample rollups.
- `op=settings counts` includes `observations` (all rows), `lab_results` (FHIR `laboratory`), `vital_signs` (FHIR `vital-signs`), `observations_by_category`, and `other_observations` (neither laboratory nor vital signs). Each row counts once overall and once per distinct FHIR category. Categories come from stored `category[].coding[].code`, preferring the standard observation-category system per concept. Only `social-history`, `vital-signs`, `imaging`, `laboratory`, `procedure`, `survey`, `exam`, `therapy` and `activity` are returned literally; every other code becomes `other`, and absent readable codes count as `unspecified`. This applies both to settings counts and clinical-series category lists, so aggregate counts never expose a hidden row's local category code. These are totals, independent of visibility. Status and erase preview say "N observations (L lab results, V vital signs, O other)"; overlapping categories do not double-subtract from other.


## First guided session agenda (U4b)

`caduceus_capture op=session_plan max_items=8` reads only; `max_items` is a whole number from 1 to 25.
It returns `{warning?, areas, limits, withheld?}`. Limits are `[CHIP_LIMITS, LIMITS, "Information with limits, not medical advice."]`.
When the active complete callset has a `same_person: false` comparison, `warning` comes first and carries
the same different-person warning as `genome_tool`, including the newest source filename. Both use `_callset_warning`.
Each area has `key`, `title`, `items` (at most `max_items`), and `more` (visible items omitted).
The six areas always exist, including in an empty vault, in this order:

1. `own_numbers`: `clinical_series` entries, newest visible `last` first, with their latest visible `lab_series`
   point flattened into the entry (`date`, `value`, `value_text`, `unit`, `interpretation`, `reference_range`,
   `reference_range_mismatch`, `range_flag`). The newest point across plotted and unplotted points wins,
   including numeric points in a different unit and text-only results; equal dates use the largest observation id.
   Session-plan reads opt into
   `lab_series(..., include_other_units=True)`, passed to `observation_series`, so its unplotted list retains
   numeric points outside the dominant unit and visible points carry `observation_id` for tie-breaking (not exposed
   in the agenda). Other callers keep the existing default behavior. A comparable finite numeric
   value is `below`, `within`, or `above` its recorded bounds; missing/inverted/mismatched bounds or text give
   `unknown`. Then wellness `metrics`, each with a `daily_check` list of matching descriptive signals.
   Stored rollups are read without rebuilding. The normal `_Store` opens the databases and applies schema
   initialization/migrations, including the phenotype table for old vaults. The agenda itself writes no data rows
   or settings; database creation and normal WAL sidecars are permitted.
2. `actionable`: nonsensitive non-PGX non-trait findings, ordered by evidence then descending magnitude.
   Finding fields: `marker`, `genes`, `summary`, `magnitude`, `repute`, `evidence`, `chip_reliability`, `your_data`,
   `phenotype_notes`. Evidence is `guideline` only for the finding's own marker (or resolved alias) in a non-retired CPIC entry;
   `research` for a retired CPIC or GLP-1 pack marker (or resolved alias) or variant-report magnitude at least 3; otherwise
   `weak`. A matching gene alone does not promote the tier. Ambiguous aliases do not establish a target.
3. `sensitive`: one category item for present `alzheimers`, `mental_health`, and `reproductive` findings.
   Fields: `category`, `findings` (visible count), `consent` (remembered answer or null), `ask_first` (only an
   unanswered visible category). Only a recorded `yes` plus current visibility adds nested finding `items`
   and `more`, also capped at `max_items`. Hidden findings contribute only the top-level `withheld`, never a finding
   description. A category the privacy settings hide is listed as `{category, consent, ask_first: false, hidden: true}`
   whenever the vault holds DNA results (a variant report or a complete DNA import), with no counts, so the item
   is the same whether or not findings exist in it. Hidden items follow the visible ones in their area (0.4.1).
4. `gene_drug`: pack entries after a complete DNA import (or entries matching a report-only pack marker), flags first, with `gene`, `drugs`, `status`
   (`flag`, `no_flag`, `not_assessed`), `evidence` (`CPIC guideline`, `CPIC retired`, or `research`), `on_record` (visible matching
   medication events by `same_drug`), and visible `phenotype_notes`; derived genes also carry `chip_reliability`.
   The area always has `ask_current_medications: true`; historical events do not establish current use.
   Report-only entries are `not_assessed` without chip calls. Sensitive pack entries move behind consent and
   use the pack's assessed finding text, including its no-flag/not-assessed limits, never the raw effect assertion.
   Entries with `evidence: "CPIC retired"` or `guideline.status: "retired"` carry `CPIC retired` and their `limits` text.
5. `carrier_and_screening`: `carrier`, then `cancer_risk`, using the same category/consent shape as sensitive.
6. `traits`: trait findings by descending magnitude, with `invite: "Ask the user to confirm or refute each one."`.

Sensitive/carrier/cancer categories take precedence, then pack markers belong to gene–drug profiles, then traits,
then actionable. Non-PGX derived calls use the finding shape with null magnitude and neutral repute. PGX derived
calls supply the profile's reliability rather than repeat the finding. Derived confidence maps to `called from the
chip`, `ambiguous on a chip`, or `not assessed`. Otherwise an i-id says `an internal probe: confirm before acting`;
a bad-repute magnitude at least 4 says `a high-impact result on a consumer chip: rare variants are often miscalled,
so a clinical test should confirm it`; other markers say `chips usually call common variants reliably`.

`knowledge/data/phenotype_links.json` holds the curated gene → LOINC/metric links specified by U4b.
`your_data` contains present visible linked clinical entries with the same latest-point fields as own_numbers,
and linked wellness metrics. Only LOINC/systemless codes match LOINC links. Measurements are placed side by
side, with no agreement judgment. The normal store resolves the active Hermes profile. Notes attach to the finding's markers, aliases, and gene names, newest first,
as `{id, stance, trait, text, date}`.

**Privacy (including U4b Amendment 1):** every note passes `_phenotype_visible`, including its marker and resolved
alias via `_marker_visible`, regardless of its stored category. Every displayed marker of an actionable, trait,
or gene–drug finding must pass `_marker_visible`; derived calls also pass `_derived_visible`. Each finding's
OWN stored category must be allowed in addition to its placement category, even after consent; moving a PGX
finding into a sensitive area never removes its PGX visibility gate. Hidden findings,
observation rows, metrics, notes and medication events each add once to the top-level `withheld`, regardless of
how often a visible record is linked. No hidden item contributes a name, value, unit, or date. Consent never
changes policy visibility; a hidden category can report its count only. The clinical discovery cap of 300 applies
before agenda limiting; beyond-cap series are reflected in own_numbers `more`.
The agenda batches report categories into a per-call lowercase marker map, preserving sensitive-first then
newest-id precedence. The internal `_marker_category`, `_marker_visible`, `_derived_visible`, `_phenotype_visible`,
and `_pgx_entry_visible` helpers accept optional `categories=` to reuse this complete map; omitted maps retain
their existing queries. Fixed sensitive markers and pack-gene policy checks still apply.
