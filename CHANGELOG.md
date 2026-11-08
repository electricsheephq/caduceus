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
- **Health records handled with care.**
  - Results marked entered-in-error or cancelled, and refuted diagnoses, are skipped, and removed if an earlier import stored them.
  - Corrected results replace the originals.
  - Stopped medications never get an invented stop date.
- **Sensitive codes.** Diagnoses are recognised from their codes as well as their text (ICD-10, ICD-9-CM, SNOMED CT, LOINC screening codes), including codes carried as values. This covers Alzheimer's disease and the dementias, genetic cancer risk and family history, and carrier status, as well as mental health and reproductive health.
- **Medications keep their own category.** A contraceptive is reproductive and an antidepressant is mental health, in the brief, before/after comparisons, search and the research watch. Hiding `medication` hides every medication. Medications the privacy settings hide are never used as research search terms.
- **Record identity.** Retractions are scoped by the record's `fullUrl` (including `urn:uuid`) and compare times as instants and every coding; medication retractions also check the drug and start. A value that another record still reports is kept, and a correction is attributed only to the record that made it. A later import updates a medication's status. Confirming a new dose for the same medication event updates the stored dose.
- **Categories hold.** A sensitive record never moves to another category, and a panel's parts carry none of the panel's own value or narrative. A medication reply under a hidden category says only that it was saved. Older vaults gain new columns when opened.
- **A synthetic demo persona** (`tests/synth/persona.py`): DNA raw data, a variant report and an Apple Health export with health records, for demos and end-to-end tests. It contains no real person's data.
- **A real Hermes loader test** in CI, plus local-only checks on a maintainer's own files that print totals only.
