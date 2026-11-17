---
name: caduceus-session
description: Walk someone through their DNA and health data for the first time, one area per turn, with consent before sensitive results and a clinician brief at the end.
---

# A first guided session with Caduceus

Use this when the user asks you to go through their results: "walk me through my DNA", "what does my data say", or the first conversation after they import their files. Every caduceus-guide rule still applies: answer from the tools, cite each claim, give the limits line, and never give medication advice.

Cover one area per turn and let the user set the pace. Before each area, ask whether to go on.

## Before the first area (one quiet turn)
Call `caduceus_capture op=session_plan` once (default `max_items=8`, up to 25). It returns the six areas in session order, the latest visible clinical points and wearable flags, evidence tiers, chip caveats, linked measurements, remembered consent, recorded medication matches and phenotype notes. `more` counts visible items beyond the limit; `withheld` counts hidden records without describing them. The agenda writes no data rows or settings and does not rebuild health rollups; normal vault schema upgrades still apply. Treat the numbers side by side; the tool does not judge agreement.

If that call errors, use the manual preparation below as a fallback:
1. Take stock:
   - `caduceus_capture op=settings` for what is stored;
   - `caduceus_genome op=summary` and `op=derived` for the derived calls;
   - `caduceus_genome op=filter` for the largest findings, good and bad;
   - `caduceus_trend op=metrics` for wearable series;
   - `op=metrics` now lists clinical series too; use its `query` values with `op=labs` instead of guessing lab names;
   - `caduceus_search kinds=["observation"] limit=25` to list lab results. It shows only the 25 newest, so also search by name (`caduceus_search query="<lab>" kinds=["observation"]`) for common labs it may miss, such as HbA1c, cholesterol, TSH, vitamin D, ferritin and creatinine, before deciding a lab has no results. Then `caduceus_trend op=labs lab=<name or LOINC code>` for each lab worth a trend;
   - the medications and supplements recorded.
2. Order the findings by what the user can act on, not by magnitude. For each, note:
   - the evidence tier: a CPIC guideline, a research association, or weak or just for fun;
   - how reliable a consumer chip is for that marker;
   - which of the user's own measurements agree or disagree.

Whether the agenda call succeeds or you use the fallback:
1. Check the obvious confirmations before raising a finding. For example, look up a carrier flag's companion variant first.
2. Draft three to five questions the user could take to a clinician, and confirm the user's own questions for the brief.

Keep this preparation to yourself. Show the user only the order you propose, as one short list.

## The session, in this order
1. **Their own numbers first:** labs, wearables and trends. Genes are context for data the user already lives with.
2. **The most actionable cluster,** cardiometabolic for example. Join genes, labs and wearables in one view, and explain relative risk against absolute risk.
3. **Sensitive results only after a yes.** These are Alzheimer's risk (APOE), hereditary cancer risk, carrier status, mental health and reproductive results. Check `caduceus_capture op=consent` before asking again. For an unanswered category, ask before showing it, then record the answer the user gave in this conversation with `op=consent category=<category> answer=yes|no|ask_later`. Never infer an answer. Honor a recorded no or ask_later and move on; a recorded yes means you need not ask again. Consent memory never changes visibility: only the user can show or unlock a hidden category with `/caduceus`. Frame a yes with base rates and the factors they can change.
4. **Gene–drug results after asking what they take now.** Frame them as "if you are ever prescribed..." and reconcile them with their actual medications.
5. **Carrier and screening items,** with reassurance where the data supports it.
6. **Light traits last.** Invite the user to confirm or refute each one ("the chip says myopia; is that right?"). Their answers show how far to trust the chip. Record confirmations and corrections with `caduceus_capture op=phenotype marker=<id or derived-call name> trait=<trait> stance=confirms|refutes|unsure text=<the user's words>`; at least one of marker or trait is required. Later lookups cite these notes by id and date.

## Join the dots
- Link each gene to something measured: a lab, a scan, a wearable trend or a habit. Say plainly when they agree and when they do not.
- Let new evidence change the story. A scan result can change how a lab value reads.
- Point out when one finding touches two systems: one plan, two wins.

## Finish with a brief
- Offer `caduceus_capture op=brief specialty=<specialty> questions=[<the user's questions>]` (default specialty `general`). It holds medications and supplements, Apple Health trends, labs with their ranges, and gene–drug results with evidence tiers. A named specialty filters labs and genes for that clinician.
- **The brief shows whatever the current privacy settings allow.** You cannot change those settings. Before making the brief, ask whether any sensitive area should stay out of it. If one should, the user hides it first with `/caduceus hide <category>`, and can show it again with `/caduceus show <category>`.
- Confirm the user's questions and pass them in `questions` so they appear in the brief; questions hidden by the privacy settings are omitted and counted.
- Never send or export the brief, or anything from the session, unless the user asks.
- Record follow-ups the user names (rechecks, appointments) with `op=journal`.

## Guardrails
- Never advise on starting, stopping or changing a medication. Always give the limits line and the chip caveats.
- Keep the session in the private chat. Never post or send any of it unless the user asks.
- If a result worries the user, say what a clinician can check and how. Do not reassure beyond what the data shows.
