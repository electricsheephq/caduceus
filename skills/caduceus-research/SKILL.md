---
name: caduceus-research
description: Run a Caduceus research watch and write its digest.
---

# Caduceus research digest

Use this when the `caduceus:research-watch` automation runs, or when the user asks for new research about their genes or medications.

## 1. Run the watch

Call `caduceus_capture op=watch_run`; pass `topics=[…]` only if the user asked for extra topics just now. What the result says:
- `status: running`: the watch is still fetching. In an automated run, reply exactly `[SILENT]`. In chat, say it is still running and that you will have the results on the next run.
- `disabled: true`: the watch is turned off in the settings. In an automated run, reply exactly `[SILENT]`.
- No `new_items`: in an automated run, reply exactly `[SILENT]`. In chat, say nothing new has appeared since the last run (give `since` and `until`).
- `status: failed`: the previous run failed in the background after its call had returned. Say so in one line, with its `error`, and say the next run tries again. Do this in an automated run too: never reply `[SILENT]` to a failure.
- `errors` that are not empty: mention in one line which source failed. The next run retries it.

The watch sends only gene, drug and topic words to Europe PMC and NCBI. It matches results to the user's genotypes on this machine.

## 2. Write the digest

- **Start with the kind of run.** For the first (`seed`) run: "Here are 3 studies to start with, led by any on your own genes and medications." For later runs: "New since <since>:".
- **Items arrive in order:** studies on one of their gene–drug pairs first, then studies that mention one of their markers, then general studies. Keep that order.
- **For each item, in two or three plain sentences:**
  - **What was found.** Use the item's own title and summary. Never invent results or effect sizes the item doesn't state.
  - **How it relates to them.** Use only the item's `why` and `matched` data:
    - `why.reason: gene_drug`: "This study is about <gene> and <drug>, which you take." If `matched.calls` has their genotype at one of its markers, say it exactly ("you are AA at rs6923761").
    - `why.reason: variant`: "This study mentions your marker <rsid> (<gene>)", with their genotype from `matched.calls` if present. Don't claim it is about their medication.
    - `why.reason: general`: "This is about a gene you watch, not specifically about your genotype or medications."
  - **The citation:** title, journal, year, then PMID or DOI.
  - **The limits:** say a preprint has not been peer reviewed. Name the study type and population when the item gives them. One study rarely settles anything.
- **ClinVar changes:** "ClinVar now classifies <variant> as <new> (was <old>), reviewed <date>". Then say whether the user carries it, from the match data only.
- **Hidden items:** if `withheld` is present, add "Some items are hidden by your privacy settings."
- **End with:** "This is information, not medical advice. Talk to your clinician before changing anything."

Keep the digest short enough to read on a phone. Never include their raw genotype list or any data beyond what each item needs.
