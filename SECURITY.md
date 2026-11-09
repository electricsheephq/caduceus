# Security and data handling

Please report vulnerabilities privately: [open a security advisory](https://github.com/electricsheephq/caduceus/security/advisories/new).
Do not put security problems, or any health data, in public issues.

We aim to acknowledge reports within 3 business days and to share a decision or fix plan within 14 days.

## Security report scope

- Results shown that the privacy settings should hide (a category the user hid, or a sensitive category that is still locked at the `standard` level).
- Any vault data leaving the machine other than as disclosed below, and genotypes in particular.
- An imported file that escapes the plugin's limits. That covers zip path traversal, XML entity expansion, inflate or object-count bombs, script execution, and markup injected into a rebuilt report or brief.
- One Hermes profile's vault readable from another.
- A path the user supplies that reads a file Hermes refuses to read.
- Credentials or secrets exposed through the plugin.

## Out of scope

- Hermes core bugs: report them to [Hermes Agent upstream](https://github.com/NousResearch/hermes-agent/security).
- How your model provider handles conversation content. Tool results are part of the conversation (see the disclosure).
- The accuracy of third-party annotations (SNPedia text in your own Promethease report, CPIC guidance, published research). Report these as ordinary bugs if Caduceus misstates them.

Disclosure — imports, search and reports run locally, and the vault is never uploaded as a
whole; tool results (slices of your data) enter the conversation, so the model provider you
chose sees them; the research watch (on by default, runs when you or the agent ask for it or on a schedule you
create, off with the `research_watch` setting) sends gene and topic terms, the names of medications you recorded,
and public marker and article IDs, never genotypes, to Europe PMC and NCBI; scheduled checks run model turns billed
like any message;
no telemetry. Includes a report-a-problem skill: the agent drafts a bug report, asks you to
check it holds no health data, and files it to this repository with `gh` only after you
approve the draft.

The same disclosure appears in the README and the manifest.

## Boundaries

**Storage**
- Everything lives in the profile's `plugin-data/caduceus/` directory:
  - `vault.db`: genome, records, research and the search index;
  - `samples.db`: Apple Health samples;
  - `sources/`: one copy of each imported file, by SHA-256, never overwritten;
  - `reports/`: rebuilt variant reports and health briefs.
- Nothing is encrypted at rest in v0.1. Encrypted levels arrive in v0.4. Protect the profile directory as you would the original files.

**Reading files**
- The plugin reads a file only when `caduceus_capture op=import` names its absolute path.
- That path goes through Hermes' own read guard (`agent.file_safety`) before it is opened.
- Importers read only the stored copy.
- Tool descriptions and skills tell the agent never to open these files itself.

**Untrusted input**
- Every imported file is treated as hostile.
  - Zip members are streamed, never extracted, and odd member names are refused.
  - Decompressed bytes, line length, decoded objects and the compression ratio are all capped while streaming.
  - XML with entity declarations is refused before parsing.
  - The Promethease report's scripts are never run. Its data is decoded from the page text.
- Rebuilt reports and briefs:
  - escape every string;
  - embed their data in an inert `application/json` block;
  - load nothing from the network.

**Privacy policy**
- Every tool result is filtered through `core/policy.allowed`. Withheld items are counted and never described.
- Unknown categories, and malformed settings, fail closed.
- The agent can read the privacy settings but cannot change them. Only the user's `/caduceus privacy|hide|show|unlock|lock` commands, or the config file, write them.

**Network**
- Only the research watch uses the network:
  - HTTPS only;
  - a fixed host allowlist (`www.ebi.ac.uk`, `eutils.ncbi.nlm.nih.gov`, `www.ncbi.nlm.nih.gov`, `api.cpicpgx.org`);
  - redirects refused;
  - timeouts, retries and a response-size cap;
  - NCBI requests rate-limited.
- Query terms come from the knowledge pack (its watch list, and the genes and markers it links to the user's medications), the user's medication names with their brand names, their chosen topics, and article IDs that LitVar2 returns. They never come from genotypes: two users with the same medications send the same requests. Matches to the user's genotypes are computed locally.

**Writes and health statements**
- Medication records need the user's confirmation: the tool returns a preview, and writes only on `confirm=true`.
- Results are framed as information with limits, never as instructions to start, stop or dose a medication.
