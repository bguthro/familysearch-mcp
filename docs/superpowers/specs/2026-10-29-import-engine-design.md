# Archive → FamilySearch Import Engine: Design (DRAFT for Ben's review)

Status: draft written 2026-10-29 while Ben is remote. Nothing here is built. Items marked **(my call)** are my guesses; everything else is a decision Ben already made.

## Intent
Take Ben's genealogical archive (Google Drive `GenealogicalResearch/`) and add it to FamilySearch: tie persons to existing FamilySearch records when they exist, create them when they don't, and attach sources and documents. One-way, archive is the source of truth.

## Decisions already made (Ben)
- Additive only. Never overwrite or delete others' conclusions; report conflicts.
- Person merges are never performed. Possible duplicates are reported for Ben to merge on FamilySearch's website.
- v1 handles deceased persons only. Living persons go into the private Genealogies space later.
- **No documents for living people, ever** (birth certificates, marriage licenses, any record image), in any space.
- Exclude Fold3, Ancestry, Newspapers.com, MyHeritage and Find a Grave sourced images by default.
- Dry-run first, human-approved; the first production run is a small supervised pilot; nothing has been written to production yet.
- Only operations verified on beta may write to production.

## What the archive looks like (recon, read-only, 2026-10-29)
- 8 lines, 165 person folders named `<birth>-<death> Name (alt)`; each has `ResearchSummary.md` (free prose), most have `Biography.md`, `Places.json`, `BookExclude.txt`.
- ~310 PDFs, ~100 images, 162 `.webloc` links. 95 are production FamilySearch links across ~87 folders: these are ready-made anchors. The other ~78 folders have no FamilySearch link and need search/match.
- 63 folders look living by name alone (no death year, born 1920 or later). `BookExclude.txt` already marks living people's certificates.
- `questionable/` folders are ignored entirely (existing archive rule).
- Fact data (dates, places, parents) is prose in `ResearchSummary.md` and tree structure is in each line's `FamilyTree.md`. There is no structured data to import directly.

## Pipeline
1. **Scan** (offline, read-only): walk the archive into a person list with files, links, folder-name dates.
2. **Extract** **(my call)**: produce one structured record per person (names, vital facts with source, parents/spouse links, source citations) from the prose. Reviewed by Ben before use; the engine only consumes the reviewed record.
3. **Classify**: living/deceased, per-document provenance and type.
4. **Plan** (needs search; beta preview offline, production later): for each person decide `link-existing`, `create`, `skip`, or `report-duplicate`, plus the additive actions (facts, sources, relationships, documents).
5. **Dry-run report**: a human-readable plan, hashed. Ben approves the hash, per person or in batches.
6. **Execute**: only an approved plan; every write read back; every created id appended to a ledger; stops on the first surprise.
7. **Report / rollback**: ledger enables undo of created objects through verified deletes.

## Rules for the stages
- **Living**: default living when there is no death year and birth is 1900 or later **(my call; threshold for Ben)**, or the folder is listed as living anywhere in the archive. Living persons: no public-tree writes, no documents, in v1 they are skipped and listed.
- **Documents**: dropped when the person is living; when the file name or the ResearchSummary citation names a banned provider; when the file is a "Citation:"/"SOURCE INFORMATION" text sheet; when in `questionable/`. Unknown provenance is skipped and listed, not guessed. Uploads go only through the guarded upload path.
- **Existing links**: a `.webloc` FamilySearch id is read from production and compared with the archive's name and dates; mismatches are reported, not trusted.
- **Match before create**: search and match-by-example; a confident single match becomes a link candidate for approval; ambiguous results are reported, never auto-resolved. Fresh creations are the last resort.
- **Conflicts**: where FamilySearch already has a different value, the engine adds the archive's value as an additional sourced conclusion only if the additive rules allow, otherwise reports it.

## Can be done without production access
Scan, classify, document filter, extraction format, plan format, dry-run report, ledger and rollback, and the full pipeline against beta using fictional test people. The production canary read check and the pilot cannot.

## Answers from Ben (2026-10-29)
1. **Extraction records live in the archive, not in this repo.** This repo stays free of personal data: no names, dates or ids of real people in code, tests, fixtures, docs or the ledger default location. Tests use fictional people only. The engine reads records from a configurable archive path and writes plans, reports and ledgers there too.
2. **Living rule confirmed**: no death year and born 1900 or later is living, unless the archive explicitly says deceased.
3. **Pilot subjects**: no line is low-stakes; I pick one (deceased, already linked, a few clean documents) when the plan exists, and Ben approves it. The choice is recorded in the archive, not here.
4. **Banned-provider images**: cite them as text sources only when the original source id/URL is known; otherwise skip. Many PDFs and images embed the original URL, so the scan stage must extract embedded URLs (PDF text and metadata, image EXIF/XMP). The image itself is still never uploaded.
