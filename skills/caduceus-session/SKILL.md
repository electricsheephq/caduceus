---
name: caduceus-session
description: Walk someone through their DNA and health data for the first time, one area per turn, with consent before sensitive results and a clinician brief at the end.
---

# A first guided session with Caduceus

Use this when the user asks you to go through their results: "walk me through my DNA", "what does my data say", or the first conversation after they import their files. Every caduceus-guide rule still applies: answer from the tools, cite each claim, give the limits line, and never give medication advice.

Cover one area per turn and let the user set the pace. Before each area, ask whether to go on.

## Before the first area (one quiet turn)
1. Take stock:
   - `caduceus_capture op=settings` for what is stored;
   - `caduceus_genome op=summary` and `op=derived` for the derived calls;
   - `caduceus_genome op=filter` for the largest findings, good and bad;
   - `caduceus_trend op=metrics` and `op=labs` for wearable and lab series;
   - the medications and supplements recorded.
2. Order the findings by what the user can act on, not by magnitude. For each, note:
   - the evidence tier: a CPIC guideline, a research association, or weak or just for fun;
   - how reliable a consumer chip is for that marker;
   - which of the user's own measurements agree or disagree.
3. Check the obvious confirmations before raising a finding. For example, look up a carrier flag's companion variant first.
4. Draft three to five questions the user could take to a clinician.

Keep this preparation to yourself. Show the user only the order you propose, as one short list.

## The session, in this order
1. **Their own numbers first:** labs, wearables and trends. Genes are context for data the user already lives with.
2. **The most actionable cluster,** cardiometabolic for example. Join genes, labs and wearables in one view, and explain relative risk against absolute risk.
3. **Sensitive results only after a yes.** These are Alzheimer's risk (APOE), hereditary cancer risk, carrier status, mental health and reproductive results. Ask before you show any of them. If the user says no, move on and do not raise it again in this session. Frame a yes with base rates and the factors they can change.
4. **Gene–drug results after asking what they take now.** Frame them as "if you are ever prescribed..." and reconcile them with their actual medications.
5. **Carrier and screening items,** with reassurance where the data supports it.
6. **Light traits last.** Invite the user to confirm or refute each one ("the chip says myopia; is that right?"). Their answers show how far to trust the chip. Record a correction with `caduceus_capture op=journal`.

## Join the dots
- Link each gene to something measured: a lab, a scan, a wearable trend or a habit. Say plainly when they agree and when they do not.
- Let new evidence change the story. A scan result can change how a lab value reads.
- Point out when one finding touches two systems: one plan, two wins.

## Finish with a brief
- Offer `caduceus_capture op=brief`: labs with their ranges, relevant gene–drug results with evidence tiers, medications and supplements, and the questions for each clinician.
- Sensitive results stay out of the brief, and out of anything sent or exported, unless the user asks for them to be included.
- Record follow-ups the user names (rechecks, appointments) with `op=journal`.

## Guardrails
- Never advise on starting, stopping or changing a medication. Always give the limits line and the chip caveats.
- Keep the session in the private chat. Never post or send any of it unless the user asks.
- If a result worries the user, say what a clinician can check and how. Do not reassure beyond what the data shows.
