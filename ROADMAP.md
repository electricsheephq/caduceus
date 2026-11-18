# Roadmap

Updated 2026-11-18, after 0.4.2.

**Status: maintenance.** Privacy and security fixes (Now) continue. The Next items are paused and have no release date.

The work is grouped into Now, Next and Later. Only Now and Next count toward progress. Later items have no release date, and a version number is set only when a release is cut. The [CHANGELOG](CHANGELOG.md) lists what shipped. The [VISION](VISION.md) gives the goal and the non-goals.

## Now: privacy follow-ups (0.4.x)

- [ ] **Word checks (#50):**
  - check text the way it is joined across fields for import and search;
  - check conditions and observations by their words, not only their category;
  - cover more invisible and text-direction characters;
  - read hyphenated topic words.
- [ ] **No duplicate condition (#39):** a dated copy with the original text, kept after a category conflict, should not add a second row.

## Next (paused): reports from PDFs and photos

- [ ] **PDF and photo reports (#19):** read medical reports from PDFs, including image-only pages, on the user's machine. It uses macOS PDFKit and Vision OCR, with Python extras as a fallback. The user reviews what was read before anything is saved.
- [ ] **Procedures and allergies (#29):** read FHIR Procedure and AllergyIntolerance, and add a way to record them in chat.

## Later (no release date)

- **Encrypted (`private`) and local-model (`local`) privacy levels.** They are not built yet, and no release is scheduled for them.
- **Health Auto Export re-read gaps (#37):** a ledger of windows a re-read could not fill. It is parked until a real sync shows a gap inside the data range.
- **More record formats:** C-CDA, and more portal exports.
- **More live sources:** wearable sources beyond Health Auto Export.
- **Daily-check baselines:** compare against the user's own baseline before raising an alert.
- **Search:** local embeddings alongside full-text search.
- **Desktop:** a Caduceus pane in Hermes Desktop.
- **Genome:**
  - local imputation;
  - polygenic scores with their caveats;
  - a carrier view;
  - household members.
- **Japanese record sources.**

## Shipped

- **0.1:** the vault, DNA and Promethease imports, the rebuilt variant report, Apple Health trends, the research watch and the daily check.
- **0.2:** GLP-1 companion metrics (lean mass, protein, waist), `/caduceus erase`, and the doctor brief compared around the medication start.
- **0.3:** the Health Auto Export sync, gene views with every relevant marker, and the first guided-session skill.
- **0.4:** lab and vital series with reference ranges, clinical reports and documents, remembered consent, phenotype notes, specialty briefs, and the first guided session agenda. 0.4.1 and 0.4.2 fixed privacy gaps in report text.
