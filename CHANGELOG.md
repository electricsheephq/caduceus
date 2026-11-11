# Changelog

All notable changes to Caduceus are recorded here. Versions follow [Semantic Versioning](https://semver.org/).

## 0.2.1 — 2026-11-12

### Fixed
- The brief's weight and lean mass panels now compare around the start of your GLP-1 medicine when one is recorded, so they match the chat. Before, a later start of any other medicine moved them. (#9)
- `/caduceus erase confirm` now also refuses while a Caduceus request is still running in another chat (a brief, a report, a record being saved); run it again when that finishes. Requests that arrive during an erase wait until it finishes. Research results kept in memory are cleared every time. (#10)
- Vaults whose Apple Health export was imported before 0.2.0 now get lean mass, protein and waist from the samples already stored. Caduceus rebuilds its daily summaries once, in the background, the first time you ask about a trend, so no new export is needed. (#11)

## 0.2.0 — 2026-11-11

### Added
- **`/caduceus erase`.** It lists what Caduceus stored on this machine. `/caduceus erase confirm` deletes the vault, the samples, the copied files, the reports and the demo files, and nothing else. It refuses while an import, the demo or a research watch is running in the same Hermes process. Settings stay. Only the user can run it; no tool erases.
- **GLP-1 companion metrics.** `lean_mass`, `protein` (grams a day, one source a day) and `waist` from Apple Health work in every trend operation, including before-and-after comparisons around a medication start. Pounds, inches and milligrams are converted. The health brief adds lean mass and protein panels.
- **The demo persona has a smart scale's lean mass, a food log's protein and waist entries**, so *"Am I losing muscle on Ozempic?"* has an answer. Earlier demo numbers are unchanged. In a vault that already holds the old demo, run `/caduceus erase confirm` and then `/caduceus demo` to see them.
- The guide skill covers muscle and protein questions: numbers and dates, no diet or exercise prescriptions.

### Notes
- Existing vaults get the new metrics when you import a new Apple Health export.

## 0.1.5 — 2026-11-11

### Docs
- **The launch video plays in the README.** It sits right under the "Watch the demo on X" link.

## 0.1.4 — 2026-11-11

### Docs
- **The README links to the launch video.** "Watch the demo on X" sits right under the opening sentence.

## 0.1.3 — 2026-11-10

### Added
- **A reminder for every model, so smaller ones answer from your vault.** Some smaller models answered "Why isn't my Ozempic working?" from general knowledge, without calling any Caduceus tool, so the guidance in the tool results never reached them. Now the first health, medication or DNA question in each chat carries one short fixed note for the model. It says:
  - answer from the `caduceus_*` tools and cite them;
  - never suggest starting, stopping, switching or changing a medication, its form or its dose, and say to talk to the prescriber.

  The note:
  - fires only on health, medication or DNA wording: drug names from the knowledge pack and from your recorded medications, words like genotype or HbA1c, and everyday words like sleep or weight only in a message with "I", "me" or "my";
  - needs data in your vault;
  - holds none of your data, and never blocks a message.

  The new `vault_hint` setting (on by default) turns it off.

### Docs
- **The README opens with what Caduceus does for you, then gets you to a first result.**
  - It installs with `hermes plugins install electricsheephq/caduceus --enable`.
  - A "Try it" walkthrough runs the demo and shows the cited answer you get.
- **New README sections:** a comparison with stock Hermes, the settings, where it works, troubleshooting, and how to report a bug.
- **CONTRIBUTING.md** is new.

## 0.1.2 — 2026-11-09

### Added
- **A gene–drug profile in the DNA summary.** With or without a variant report, `caduceus_genome op=summary` lists:
  - the genes where you carry an effect allele, with the medicines that published guidance covers for them;
  - the derived calls.

  A flag means guidance exists if the medicine is ever prescribed; "no flag" does not mean normal. Your privacy settings apply as everywhere else.
- **A check that a vault holds one person.** When you import a second DNA file, Caduceus compares it with the earlier ones on shared markers and says plainly whether it looks like the same person. While the vault holds more than one person's DNA, results carry a warning.
- **Listing your medications.** `caduceus_search` with `kinds=["medication"]` and no query lists your recorded medications, newest first.

### Changed
- **Answers stay careful even when the agent skips the guide skill.**
  - Trend comparisons, trends about a medication, and search results that include a medication carry a short instruction for the agent. It says this is information with limits, not medical advice, and never to suggest starting, stopping or changing a medication or its dose; for any medication decision, talk to the prescriber.
  - A comparison around a medication's start points to the gene–drug check for that medicine, and the tool descriptions carry the same rules.
- **Trends say where your data ends.** Results give your data's full date range and say when the window you asked for stops before it.
- **Search results say they are filtered** by your words, and how to list everything of one kind. A misspelt kind is an error that names the valid kinds, never an empty list.

### Fixed
- **Apple Health exports from iPhones set to other languages now import.** The export is found by its content, not its file name: Spanish and Brazilian Portuguese phones write `exportar.xml`, and Norwegian and Danish phones write `eksport.xml`. An unzipped export XML imports too.
- **Gene lookups work without a variant report.** `op=gene` answers from the built-in knowledge pack's markers.
- **The demo recovers from a crash** between saving its files (#3).
- **Unrecognised files:** the message now says that whole-genome VCF files are not supported yet.

## 0.1.1 — 2026-11-09

### Added
- **`/caduceus demo`:** loads an invented demo person into an empty vault, so you can try Caduceus without your own data. It refuses when the vault already holds your files, so the two never mix.
- **More DNA raw data:** MyHeritage and FamilyTreeDNA files (`.csv`, `.csv.gz`, or zipped). Living DNA files are labelled as Living DNA.

### Changed
- **The research watch puts your own gene–drug pairs first** (#2).
  - The first digest runs one query per current medication (its knowledge-pack genes and markers, with genetic terms, over five years). Papers that mention your markers come next (LitVar2), then general results.
  - Later digests use the same order, and every item says why it was picked.
  - The search terms still depend only on your medications, your topics and the public knowledge pack, never on your genotypes.
- **Doses:** answers mention a dose only as recorded, and never compare it with other doses.

### Fixed
- **A research watch run that fails after its call returned** is reported once, to the next run, instead of being lost (#1).

## 0.1.0 — 2026-11-08

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
