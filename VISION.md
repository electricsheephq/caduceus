# Vision

## Goal

Keep your whole health picture in one private vault on your own machine: DNA raw data, a Promethease report, Apple Health and US health records. Your Hermes agent then answers health questions from your own data and from current research. It cites every source and states its limits.

Caduceus gives information with limits, never medical advice. It never tells anyone to start, stop or change a medication.

## Users

- **DNA test users:** people with consumer DNA raw data (23andMe, AncestryDNA, MyHeritage, FamilyTreeDNA, Living DNA) or a Promethease report, who want to ask about it in plain words.
- **Apple Health users,** including people on a GLP-1 medicine who want their own trends before and after the start date.
- **US patients** who download their records as FHIR and want them searchable beside their DNA and wearable data.
- **People preparing for a doctor's visit,** who want a one-page brief built from their own data.

## What Caduceus always does

- **Local first.** Imports, search and reports run on the user's machine. The vault is never uploaded as a whole. Tools return only the slices a question needs.
- **Privacy is the user's choice.** The default level is `open`. At the `standard` level, right-not-to-know topics stay hidden until the user unlocks them. Any category can be hidden at any level.
- **Honest limits.**
  - Chip calls are not clinical results.
  - Genes that chips cannot read are reported as not assessed.
  - Every gene–drug result carries its evidence level.
  - Small effects are described as small.
- **Nothing is lost.** Original files are kept by hash and never overwritten. A re-import adds nothing new, and an interrupted import resumes without duplicates.
- **Synthetic data only.** The repository, tests and demo hold no real person's data.

## Coverage on every Hermes surface

| Surface | Goal | Today |
|---|---|---|
| Desktop (serve) | Attach a file or give its path; every tool and `/caduceus` command; reports and briefs open locally | Tested with the demo |
| CLI / TUI | Import by path, `/caduceus` commands, questions | Tested |
| Telegram and other messaging | Questions, research digests and daily-check alerts in chat. Files over the app's cap (20 MB on Telegram) are imported by path from the CLI or Desktop | Not formally tested yet |
| API server | The four tools answer in any agent turn the API server runs; imports by absolute path | Not tested yet |
| Scheduled jobs (`/blueprint`) | A research digest only when something is new; a daily check that speaks only when a trend leaves its threshold | Shipped |

## Non-goals

- **Medical advice:** no diagnosis, dosing or treatment decisions.
- **A hosted service:** no cloud account, no sync service, no telemetry.
- **Clinical-grade genotyping:** consumer chip calls are not confirmed results. Anything important needs a clinical test.
- **Uploading the vault whole** to any model provider or service, or sharing data with anyone the user did not choose.
- **Writing back to providers:** record imports are read-only. Caduceus does not replace a patient portal.
- **Redistributing licensed text:** no SNPedia or Promethease text in the repository. The user's own report is read only on their machine.
