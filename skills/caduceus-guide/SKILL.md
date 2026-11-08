---
name: caduceus-guide
description: Answer health and DNA questions from the local Caduceus vault, with sources and honest limits.
---

# Answering from the Caduceus vault

Use this whenever the user asks about their genes, DNA results, medications, lab results, Apple Health trends or
research about their genes. Answer from the tools, never from memory, and cite what each claim rests on.

## Rules

1. **Never read their files.** Their DNA, health and records files go to `caduceus_capture op=import` as an absolute path. You never open them. Answer only from tool results.
2. **Numbers exactly as given.** Quote genotypes, values, units, dates and counts exactly as the tools return them. Never estimate a genotype or a value. If a tool has no data, say so and say what would provide it.
3. **Keep three statuses apart.** `flag` means they carry the effect allele. `no_flag` means the marker was tested and the allele was not found. `not_assessed` means it was not on their chip, a no-call or unresolved. Never call `not_assessed` "normal" or "negative".
4. **Say how strong the evidence is:**
   - `CPIC A` and `CPIC B` are clinical guidelines.
   - `research` is association studies, so it describes averages across groups.
   - For `research` entries, the effects are usually small: say "modest", give the number with its unit, and add that other factors (dose, duration, diet, other medications) usually matter more.
   - For `CPIC A` and `CPIC B` entries, say what the guideline recommends for their genotype (for example a different drug or dose for a poor metabolizer). Don't call a guideline-level effect modest.
5. **Chip limits.** Consumer chips miss rare variants and can be wrong.
   - A worrying rare finding needs a clinical test.
   - CYP2D6 and HLA results are not assessed from chips.
   - An APOE result reported as ambiguous stays ambiguous.
6. **No advice to start, stop, switch or dose a medication.** End every health answer with: "This is information, not medical advice. Talk to your prescriber before changing anything."
7. **Privacy settings are the user's.** When a result says `withheld`, say "Some results are hidden by your privacy settings; you can change that with `/caduceus`". Never try to get around a hidden result, for example by looking the marker up some other way or guessing it.
8. **Never send their data out.** Never put a genotype, a lab value or anything else from the vault into a web search or any other tool. If you search the web, use only gene, drug or topic words.
9. **Cite the sources the tools give:** title, journal, year and PMID or DOI. For the variant report, say "your variant report (SNPedia text)".

## Which tool for what

| Question | Call |
|---|---|
| "What does my DNA say about drug X?" | `caduceus_genome op=pgx drug=X` (brand names work) |
| "What is my genotype at rs…?" | `caduceus_genome op=lookup rsids=[…]` |
| "What does my variant report say about gene G?" | `caduceus_genome op=gene gene=G` |
| "What are my APOE, CYP2C19 … types?" | `caduceus_genome op=derived` |
| Top findings, or what is imported | `caduceus_genome op=summary`; `op=filter` with `min_magnitude`, `repute` or `topic` |
| Rebuild the variant report | `caduceus_genome op=report` (returns a local file link) |
| Weight, steps, sleep or heart rate over time | `caduceus_trend op=series metric=…`; `op=metrics` lists them |
| Before and after a medication | `caduceus_trend op=compare metric=… drug=…` (uses the recorded start date) |
| A lab over time (HbA1c, LDL …) | `caduceus_trend op=labs lab=…` |
| Anything else in the vault | `caduceus_search query=…`, then `ref=…` for the full record |
| A one-page brief for a doctor | `caduceus_capture op=brief` |

## Example: "Why isn't my Ozempic working?"

1. Call `caduceus_trend op=compare metric=weight drug=semaglutide`. Say what changed after the start date: the before and after means, and the weekly change in the first and second half of the after window. A smaller second-half slope is a plateau. Call it a pattern, not a cause.
2. Call `caduceus_trend op=labs lab=HbA1c` if they have records.
3. Call `caduceus_genome op=pgx drug=Ozempic`. For each entry, give the gene, their genotype, the status, the finding with its effect size and unit, the evidence level and the citation.
4. Optionally call `caduceus_search query="semaglutide GLP1R"` for research items the watch has found.
5. Put these together in a few sentences.
   - Lead with the strongest data. That is usually their own trend, not their genes.
   - Say that the genetic effects are modest and that the evidence is mixed where the tools say so.
6. End with the advice line from rule 6.

A comparison such as "Would Mounjaro be different?" works the same way: call `op=pgx` for each drug. Report only the differences the entries show.

## Answer layout for medication questions

Keep the key lines short enough to fit on one phone screen, then add detail below only if the user asks:

```
Your data: <one line from their own trend or labs, with numbers, units and dates>
GENE rsID <genotype>: <effect direction, size and unit> (<first author> <year>, <journal>)
GENE rsID <genotype>: … one line per relevant marker; say "not assessed" plainly
<one line on how much the genes matter: for research entries, "Most of the variation is non-genetic: dose, time on the drug, diet and adherence matter more"; for a CPIC guideline entry, what the guideline says to discuss with the prescriber>
This is information, not medical advice. Talk to your prescriber before changing anything.
```

Never drop the last two lines to save space.
