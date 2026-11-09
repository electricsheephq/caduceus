---
name: caduceus-setup
description: Set up Caduceus — import DNA raw data, a Promethease report, an Apple Health export and health records, then choose privacy and research watches.
---

# Caduceus setup

Use this when the user has just installed Caduceus, asks to set it up, or wants to add their DNA, Promethease, Apple
Health or medical records. Go one step at a time, and keep each message short.

## 0. What to say first

In two or three sentences:
- Caduceus keeps a private vault of their health data, and it stays on their machine.
- To answer a question, your tools read only the slices it needs. Those slices reach the model they chose, like any message.
- The research watch is on by default (the `research_watch` setting turns it off). It sends only gene and topic words, the names of their recorded medications, and public marker and article IDs (never genotypes) to Europe PMC and NCBI.

Say "your vault stays on your machine", naming the vault. Never say "everything stays on your machine", "nothing is
uploaded", "nothing leaves your machine" or "nothing was sent":
the slices your tools return do reach the model, so those claims are false.

## 1. Import their files

Ask which of these they have. They don't need all of them:

| File | Where it comes from |
|---|---|
| 23andMe raw data (`.zip` or `.txt`) | Their 23andMe account settings: request a raw data download. |
| AncestryDNA raw data (`.zip`) | Their AncestryDNA settings: download DNA data. |
| MyHeritage raw data (`.zip` or `.csv`) | MyHeritage DNA settings: download raw DNA data. |
| FamilyTreeDNA Family Finder raw data (`.csv.gz`) | FamilyTreeDNA → Family Finder → download raw data (the build 37 file). |
| Living DNA raw data (`.zip` or `.txt`) | Living DNA settings: download raw data. |
| Promethease report (`.zip` or `.html`) | The report file they downloaded from Promethease. |
| Apple Health export (`export.zip`) | iPhone Health app → their picture → Export All Health Data, then AirDrop or save it to this machine. If Health Records is connected, the export includes their clinical records. |
| Health records as FHIR JSON | Patient portals that offer a FHIR or "download my record" export. |

**No files yet?** They can try Caduceus with an invented demo person: they type `/caduceus demo`, wait about a minute,
then ask "Why isn't my Ozempic working?". The demo only loads into an empty vault, so it never mixes with their own
data. Say it is not a real person.

For each file:
1. **Get its absolute path on this machine.**
   - When they attach a file, use the local path from the attachment note.
   - On Telegram, files over 20 MB cannot be sent: ask them to put the file on this machine and give you its path.
   - **Never open, read, quote or summarise the file yourself, even when it is attached.** It is their DNA and medical data.
2. **Call `caduceus_capture` with `op=import` and `path=<absolute path>`.** Caduceus copies the file into its vault and imports it. A large import keeps running in the background. If the result says `running`, check with `op=status` and an `import_run_id` (or with no id) until it finishes.
3. **Report the counts it returns in one line.** For example: "960k genotypes from your 23andMe v3 file" or "25k variant report records".
   - **If it says `skipped`, it was already imported.**
   - **If it says `failed`, show the error and stop on that file.**

## 2. Their medications

Ask which medications they take and roughly when each started. These dates let Caduceus compare trends before and after.

For each one:
1. Call `caduceus_capture` with `op=medication`, `drug`, `event=start`, `date` (ISO) and `dose` if known.
2. Show the user the preview it returns.
3. Only after they agree, call it again with `confirm=true`. Never set `confirm=true` on your own.

## 3. Privacy

Explain the two levels in plain words:
- **open** (the default): everything they imported can be shown.
- **standard**: hides results in categories people often prefer not to know until they choose to. These are Alzheimer's risk (APOE), hereditary cancer risk, carrier status, mental health and reproductive health. Each can be unlocked one at a time.

The user changes these themselves; you cannot. They type:
- `/caduceus privacy standard` (or `open`)
- `/caduceus hide <category>` or `/caduceus show <category>`
- `/caduceus unlock <category>` or `/caduceus lock <category>`

`/caduceus help` lists the categories. Some users want to decide before they see anything, so offer this before the first results.

## 4. Research watch and daily check

Offer, and explain what each costs:
- **Research watch:** `/blueprint caduceus:research-watch` sends a digest of new studies and ClinVar changes about their genes and medications (weekly by default). The first run shows 3 studies, led by any on their own gene–drug pairs (for example GLP1R and semaglutide). After that it reports only new ones, and stays silent when there is nothing new.
- **Topics:** to add topics such as "APOE" or "GLP-1 receptor agonists", they set `research_topics` in the Caduceus settings, or you can pass `topics` to a one-off `op=watch_run`.
- **Daily check:** `/blueprint caduceus:daily-check` checks their Apple Health trends once a day and messages them only when something is outside its threshold. It costs one model call a day.

## 5. Finish

Call `caduceus_genome op=summary` and `caduceus_capture op=settings`, then tell them in a few lines:
- what is in the vault;
- their privacy level;
- that they can now ask questions such as "Why isn't my Ozempic working?" or "What do my genes say about statins?";
- that `caduceus_genome op=report` rebuilds their variant report as a local page;
- that `caduceus_capture op=brief` writes a one-page health brief with charts to bring to a doctor.

Results are information with limits, not medical advice.
