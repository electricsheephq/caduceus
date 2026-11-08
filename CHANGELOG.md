# Changelog

All notable changes to Caduceus are recorded here. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Added
- **The vault**
  - A local SQLite vault, plus a separate samples database for Apple Health.
  - Each imported file is stored once by SHA-256.
  - Imports are resumable, and a re-import is a no-op.
- **Imports**
  - **DNA raw data:** 23andMe v3, v4 and v5, and AncestryDNA, as `.zip` or `.txt`.
  - **Promethease reports:** decoded without running their scripts and kept verbatim, then rebuilt as a self-contained variant report in Caduceus' own design, with a parity check.
  - **Apple Health exports:** weight, steps, heart rate, HRV, sleep, blood pressure, glucose and more. Steps and sleep are de-duplicated across sources, and time zones are handled.
  - **Health records:** the export's `clinical-records/` and standalone FHIR R4 JSON (labs, conditions and medications).
- **Four tools:**
  - `caduceus_search`: full-text search with cited snippets.
  - `caduceus_genome`: genotype lookup, gene views, gene–drug checks (CPIC plus GLP-1 research), derived calls such as APOE and CYP2C19, and the rebuilt report.
  - `caduceus_trend`: series, before/after comparisons around a medication start, lab series and a daily signal check.
  - `caduceus_capture`: import, status, confirmed medication records, notes, the research watch, settings and a one-page health brief.
- **Privacy levels:** `open` (the default) and `standard`, which hides Alzheimer's risk, hereditary cancer risk, carrier status, mental health and reproductive results until unlocked. Hidden categories apply at every level. The user sets these with `/caduceus`.
- **Research watch:** Europe PMC, LitVar2 and ClinVar. It sends only gene, drug and topic terms, and gives a first top-3 digest, then only new items.
- **Two automation blueprints:** `research-watch` and `daily-check`.
- **Skills:** `caduceus-setup`, `caduceus-guide`, `caduceus-research` and `caduceus-report-problem`.
