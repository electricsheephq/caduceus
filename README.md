# Caduceus: Personalized Health & DNA AI

![Caduceus: Personalized Health & DNA AI](docs/media/banner.jpg)

**Your Hermes agent's private health memory.** Caduceus brings these into one SQLite vault on your own machine:
- your 23andMe, AncestryDNA, MyHeritage, FamilyTreeDNA or Living DNA raw data;
- your Promethease report;
- your Apple Health export;
- your US health records.

Your agent can then answer questions like *"Why isn't my Ozempic working?"* from your genotype, your trends and the
newest research. Answers are cited, with honest limits.

- **One answer from all your data:** your genotype, your weight curve, your lab results and published research, each cited.
- **Your vault stays on your machine:** search, imports and reports run locally. The agent reads only the slices a question needs.
- **Rebuilds your variant report offline:** your Promethease report becomes a self-contained page in Caduceus' own design. It also writes a one-page health brief with charts to bring to your doctor.
- **Keeps watching new research about your genes and medications:** studies on your own gene–drug pairs come first, and a digest arrives only when something new appears.

> Information with limits, not medical advice. Caduceus never tells you to start, stop or change a medication.

| Your variant report, rebuilt offline | A one-page brief for your doctor |
|---|---|
| ![The rebuilt variant report: record counts, then summaries by magnitude, drug response, good news and what is worth a closer look](docs/media/screenshot-variant-report.png) | ![The health brief: medications, then the weekly weight average with the Ozempic start marked and the plateau after it](docs/media/screenshot-health-brief.png) |

*The screenshots use the synthetic demo persona, not a real person's data.*

## Install

Requires Hermes Agent 0.23.0 or newer. There are no extra dependencies.

```bash
hermes plugins install https://github.com/electricsheephq/caduceus --enable
```

Once it is in the plugin catalog, `hermes plugins install caduceus --enable` works too. Without `--enable`, Hermes
installs the plugin switched off; `hermes plugins enable caduceus` turns it on. Restart Hermes, then tell your agent
**"set up Caduceus"**. The setup skill takes you through:
1. importing your files;
2. recording your medications;
3. choosing a privacy level;
4. turning on the research watch.

## Try it without your own data

Type `/caduceus demo`. Caduceus writes three files for an invented person: a DNA raw-data file, a variant report and an
Apple Health export with health records. It imports them in about a minute. Then ask your agent *"Why isn't my Ozempic
working?"* or *"Would Mounjaro be different for me?"*. The demo loads only into an empty vault, so it never mixes with
your own data.

## What it reads

| File | How to get it |
|---|---|
| 23andMe raw data (v3, v4, v5), `.zip` or `.txt` | Request your raw data download in your 23andMe settings |
| AncestryDNA raw data, `.zip` | Download your DNA data in your AncestryDNA settings |
| MyHeritage raw data, `.zip` or `.csv` | Download your raw DNA data in your MyHeritage DNA settings |
| FamilyTreeDNA Family Finder raw data, `.csv.gz` | Family Finder → download raw data (the build 37 file) |
| Living DNA raw data, `.zip` or `.txt` | Download your raw data in your Living DNA settings |
| Promethease report, `.zip` or `.html` | The report file you downloaded from Promethease |
| Apple Health `export.zip` | iPhone Health app → your picture → Export All Health Data. With Health Records connected, it includes your clinical records |
| FHIR R4 JSON (labs, conditions, medications) | A patient portal's FHIR or "download my record" export |

Give your agent the file's path, and Caduceus copies the file into its vault and imports it.
- The agent is told never to open these files itself.
- Big imports keep running in the background, and re-importing a file does nothing.
- An interrupted import resumes where it stopped.

## Tools

| Tool | What it does |
|---|---|
| `caduceus_search` | Full-text search across your annotations, research, labs, conditions, medications and notes, with cited snippets |
| `caduceus_genome` | Your genotype at any marker, gene views, gene–drug checks (CPIC guidance plus GLP-1 research), derived calls (APOE, CYP2C19, CYP2C9 and others), top findings, and the rebuilt variant report |
| `caduceus_trend` | Apple Health trends with units, coverage gaps and a calculation version; before and after a medication start; lab results over time; a daily signal check |
| `caduceus_capture` | Imports and their progress, medications (recorded only after you confirm), notes, the research watch, settings, and the health brief |

A gene–drug result keeps three outcomes apart:
- **flag:** you carry the effect allele;
- **no flag:** it was tested and is absent;
- **not assessed:** not on your chip, or a no-call.

"Not assessed" is never reported as normal.

## Privacy

| Level | What shows |
|---|---|
| `open` (default) | Everything you imported, except categories you hide |
| `standard` | Alzheimer's risk (APOE), hereditary cancer risk, carrier status, mental health and reproductive results stay hidden until you unlock each one |

You change these yourself, with `/caduceus privacy standard`, `/caduceus hide <category>`, `/caduceus unlock <category>`
and so on (`/caduceus help` lists them), or in the plugin settings. Your agent can read the settings but cannot change
them. Hidden results are counted, never described.

Disclosure — imports, search and reports run locally, and the vault is never uploaded as a
whole; tool results (slices of your data) enter the conversation, so the model provider you
chose sees them; a research watch you turn on sends gene, drug and topic terms and public
marker and article IDs, never genotypes, to Europe PMC and NCBI; scheduled checks run model turns billed like any message;
no telemetry. Includes a report-a-problem skill: the agent drafts a bug report, asks you to
check it holds no health data, and files it to this repository with `gh` only after you
approve the draft.

Your data lives in your Hermes profile's `plugin-data/caduceus/`. In v0.1 it is not encrypted at rest; encrypted and local-model levels are planned for v0.4. See [SECURITY.md](SECURITY.md) for the boundaries.

## Research watch and daily check

- **`/blueprint caduceus:research-watch`:**
  - **What it does:** a digest of new studies (Europe PMC, LitVar2) and ClinVar changes about your genes, medications and chosen topics, weekly by default.
  - **First run:** 3 studies to start with. Studies on your own gene–drug pairs come first (for example GLP1R and semaglutide, from the last five years), then papers that mention your markers, then general results. Each item says why it was picked.
  - **After that:** only new ones, in the same order, and nothing when there is nothing new.
  - **What it sends:** gene, drug and topic words, marker IDs from the built-in knowledge pack, and article IDs. Which terms it sends depends only on your medications and topics, never on your genotypes.
- **`/blueprint caduceus:daily-check`:** once a day, checks your Apple Health trends (resting heart rate, sleep, weight, steps) and messages you only when something is outside its threshold. It costs one model call a day.

## Accuracy and limits

- **Chips miss things.** Consumer DNA chips test common variants. Rare variants can be missed or miscalled: in one large study, most rare pathogenic chip calls did not confirm. Anything important needs a clinical test.
- **Some genes can't be read from chips.** CYP2D6, HLA types and copy-number changes are reported as *not assessed*. An ambiguous APOE pattern stays ambiguous.
- **Small effects, labelled evidence.** Genetic effects on drug response are usually small. Every result carries its evidence level (a CPIC guideline, or research association), its sources and its limits.
- **No medical advice.** Caduceus never tells you to start, stop or change a medication. Talk to your prescriber.

## Data sources and licences

- **CPIC:** gene–drug guidance data, CC0.
- **NCBI:** ClinVar, dbSNP and LitVar2 (public).
- **Europe PMC:** article metadata.
- **SNPedia:** its text (CC BY-NC-SA 3.0) appears only from your own Promethease report. Caduceus keeps it on your machine, labels it, and never ships or fetches it.

## Development

Python 3.11+, standard library only.

```bash
python -m pip install pytest
python -m pytest
```

Tests use synthetic data generated at test time and never touch the network. Module contracts are in
[docs/contracts.md](docs/contracts.md), and agent guidance in [AGENTS.md](AGENTS.md). Report bugs with the issue form,
which your agent can draft (with no health data). Report security problems privately (see [SECURITY.md](SECURITY.md)).

## Licence

Apache-2.0. Made by [Electric Sheep](https://github.com/electricsheephq) for Hermes Agent.
