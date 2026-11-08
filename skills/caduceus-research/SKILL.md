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
- `errors` that are not empty: mention in one line which source failed. The next run retries it.

The watch sends only gene, drug and topic words to Europe PMC and NCBI. It matches results to the user's genotypes on this machine.

## 2. Write the digest

- **Start with the kind of run.** For the first (`seed`) run: "Here are the 3 most relevant recent studies about your genes and medications." For later runs: "New since <since>:".
- **For each item, in two or three plain sentences:**
  - **What was found.** Use the item's own title and summary. Never invent results or effect sizes the item doesn't state.
  - **How it relates to them.** Use only the item's match data. If it matched one of their genotypes or medications, say which ("you carry the T allele at rs10305420"). If it matched only a gene or drug they watch, say "this is about a gene you watch, not specifically about your genotype".
  - **The citation:** title, journal, year, then PMID or DOI.
  - **The limits:** say a preprint has not been peer reviewed. Name the study type and population when the item gives them. One study rarely settles anything.
- **ClinVar changes:** "ClinVar now classifies <variant> as <new> (was <old>), reviewed <date>". Then say whether the user carries it, from the match data only.
- **Hidden items:** if `withheld` is present, add "Some items are hidden by your privacy settings."
- **End with:** "This is information, not medical advice. Talk to your clinician before changing anything."

Keep the digest short enough to read on a phone. Never include their raw genotype list or any data beyond what each item needs.
