# FamilySearch Sync — Design

**Date:** 2026-10-23
**Status:** Draft for approval
**Author:** Ben Guthro (with Claude)

## Intent and why

Ben maintains a rich, source-backed genealogical archive in Google Drive
(`GenealogicalResearch/`): **166 person folders** across 8 surname lines, each with
prose write-ups (`Biography.md`, `ResearchSummary.md`, `OralHistory.md`), some
structured data (`Places.json`), **457 source documents** (censuses, certificates,
draft cards, obituaries), and `.webloc` bookmarks — **67 of which already pin a person
to a FamilySearch production tree ID**.

FamilySearch restricts its REST API to approved partner keys and will not issue them
for personal projects (confirmed: the developer sandbox and write access are both gated
behind the Innovator Program + a compatibility review). The goal is to reach
FamilySearch programmatically *anyway*, authenticating as Ben's own logged-in browser
session rather than as a registered app, in order to:

1. **Push the archive up into the shared FamilySearch tree** — tying to existing
   persons when possible, creating new persons when not, building the relationships
   between them, and uploading + linking the source documents.
2. **Search and read** FamilySearch, as first-class capabilities, so Ben can explore
   and stage new research in the future.

The archive is the **source of truth**. Sync is **one-way, up** (archive → FamilySearch).

### Feasibility is proven, not assumed

This design is written *after* a throwaway spike proved every required capability end to
end against the FamilySearch **beta** environment, using only a token lifted from Brave's
cookie store. Verified results (all test data created was then deleted; beta left clean):

| Capability | Endpoint (discovered at runtime) | Result |
|---|---|---|
| Auth via Brave `fssessionid` cookie as Bearer token | `GET /platform/users/current` | 200 |
| Read person + GEDCOM X facts | `GET /platform/tree/persons/{id}` | 200 |
| Tree search with scored matches | `GET /platform/tree/search` | 200 |
| Historical-record (document) search | `GET /service/search/hr/v2/personas` | 200 (12,676 hits, paged) |
| Create person + facts (+ change message) | `POST /platform/tree/persons` | 201 |
| Create couple relationship | `POST /platform/tree/relationships` (type `Couple`) | 201 |
| Create parent-child relationship | `POST /platform/tree/relationships` (type `ParentChild`) | 201 |
| Add a fact / source to an existing person | `POST /platform/tree/persons/{id}` (person-merge) | 200 |
| Upload a document (memory) | `POST /platform/memories/memories` | 201 |
| Link a memory to a person | `POST /platform/memories/memories/{id}/personas` | 204 |
| Create a source description | `POST /platform/sources/descriptions` | 201 |
| Delete (cleanup) | `DELETE /platform/tree/persons/{id}` | 204 |

The spike also confirmed the API is **self-describing (HATEOAS)**: the tree collection
and each person resource advertise the real link templates (`relationships`, `artifacts`
→ `/persons/{id}/memories`, `source-descriptions` → `/persons/{id}/sources`, `matches`,
`merge`, parents/children/spouses, and the OAuth token endpoint). The client will resolve
URLs from these rels rather than hard-coding paths — this is how the spike recovered from
a wrong guessed path on the first try.

## Goals

- A **Python library** (`familysearch` client) that:
  - Authenticates with a browser-lifted session token, **auto-extracted from Brave**
    (via `browser_cookie3`; Keychain-gated), with a manual-paste fallback.
  - **Discovers endpoints** from HATEOAS link rels; does not hard-code internal paths.
  - **Reads**: person, relationships (parents/children/spouses), sources, memories,
    change history; ancestry/descendancy.
  - **Searches / matches**: tree search, per-person match candidates, and
    **historical-record (document) search** (the `/service/search/hr/v2/personas`
    surface the website uses — a different API than the tree, with its own paging;
    full-text record search exists at a path still to be discovered).
  - **Writes**: create person; add/amend facts (conclusions); create relationships
    (couple, parent-child); upload memories (documents) and link them to persons;
    attach source descriptions. Every write carries a **change message** citing the
    archive.
- An **MCP server** exposing the search / read / write surface as tools, so Ben can
  drive FamilySearch from an MCP client and stage future additions.
- An **archive → tree sync engine** (with a CLI) that:
  - **Classifies** each archive person as deceased vs. living (possibly-living) first,
    and routes deceased → shared tree, living → Ben's private space.
  - Reads the archive's per-person data and resolves each **deceased** person to a
    FamilySearch **production** person: by existing `.webloc` ID when present, else by
    **search → match**, else flags for **create**.
  - Applies **additive** changes only (see conflict policy).
  - Always runs **dry-run first**, prints a human-readable plan, and requires
    **explicit approval** before executing; executes **throttled**.
  - Maintains a **sync-state ledger** (archive person ↔ FS prod ID ↔ what was last
    pushed) as a structured file, seeded from the existing `.webloc` IDs.
- A **beta proof/verification harness**: the spike, formalized into an integration test
  suite that runs create → fact → relationship → memory → delete against beta on demand.
  This *is* the "proven to actually work" acceptance gate for the write layer — the exact
  guarantee the abandoned community projects failed to provide.

## Non-goals

- **Two-way sync.** Nothing is ever pulled from FamilySearch back into the archive. The
  archive is authoritative. (The sync-state ledger is the one thing the tool writes
  locally, and it is bookkeeping, not research content.)
- **The official partner API / API keys.** Not available for personal use; not pursued.
- **Automatic merging of existing FamilySearch duplicates.** Merge is powerful and
  destructive; out of scope for v1 (may be surfaced as a *report* of suspected
  duplicates for Ben to merge by hand).
- **Rewriting Ben's research files.** The tool reads the archive; it does not edit the
  prose/`.md` content.

## Anti-goals (failure even if it technically runs)

- Creating **duplicate persons** in the shared tree. → Match-before-create, always.
- **Overwriting or deleting** other contributors' data. → Additive-only, enforced.
- Getting Ben's FamilySearch **account flagged** for abusive automation. → Throttling,
  change messages, human-approved batches, conservative defaults.
- A library that **claims** to write but silently no-ops — the precise failure Ben hit
  with existing projects. → The beta harness must pass for the write layer to ship.

## Constraints

- **Token is short-lived** (~60 min idle, 24 h max) and **environment-scoped**: a beta
  cookie authenticates beta only; a prod cookie authenticates prod only (verified — the
  beta token returned 401 against production). The tool must detect 401/expiry and
  re-extract from the browser; a refresh endpoint exists but refresh tokens are not
  available without client credentials, so **re-extraction from the live browser session
  is the primary renewal mechanism**.
- **Beta is for proof; production is for the real sync.** The archive's 67 existing IDs
  are production IDs.
- **Internal endpoints are undocumented and may change.** Keep the client thin, rely on
  HATEOAS discovery, and treat every response as untrusted data (no eval, defensive
  parsing).
- **Writes must be throttled** and carry change messages. Respect FamilySearch's memory
  limits (≤15 MB per file) and the memories upload agreement.
- **Language: Python** — matches the archive's existing `_tools/` scripts; `requests` and
  `browser_cookie3` both work here. Target Python ≥3.11. (Note: `rookiepy` fails to build
  on this machine's Python 3.14; use `browser_cookie3` or read Brave's cookie SQLite
  directly.)
- **Archive data is mostly prose.** `Places.json` is structured, but the facts that drive
  writes (names, dates, places, relationships) live largely in Markdown narrative. The
  sync needs a structured extraction step; writes must be driven by reviewable structured
  data, never by free-text parsing at write time.

## The "how" that is already decided (Ben's calls)

- One-way, up; **archive is the source of truth**.
- Token **lifted from Brave** and auto-extracted.
- **Fact conflict policy:** additive only. The tool **never overwrites or deletes**
  another contributor's conclusions. It **adds** facts/sources/memories that are missing,
  and **reports conflicts** (e.g., a differing birth date) for Ben to resolve by hand.
- **Relationship conflict policy** (Claude's call, as a direct corollary of the above):
  if a matched person already has a *different* parent or spouse, the tool
  **reports and holds** — it does **not** add a competing relationship, because doing so
  would create the duplicate/wrong family structure that is a named anti-goal. It only
  adds a relationship when the matched person has no conflicting one.
- **Match-before-create** to avoid duplicates.
- **Human-in-the-loop**: dry-run + explicit approval before any production write.
- **Living vs. deceased (Ben's decision): deceased + private living.** The sync
  classifies every archive person first. Deceased persons flow through the normal
  shared-tree match/enrich/create path. **Living persons are added to Ben's own private
  FamilySearch space** (visible only to him), never pushed to the shared tree and never
  matched to shared persons. FamilySearch's own living/possibly-living rule (roughly:
  born within ~110 years with no recorded death) governs the classification; the tool
  must handle the "possibly living" boundary explicitly.
- **Document upload policy (Ben's decision): conservative filter**, along **three**
  dimensions:
  1. **Person status:** only documents tied to **deceased** persons are eligible;
     anything concerning a living person is excluded.
  2. **Content rights:** anything likely rights-restricted (e.g. recent newspaper
     obituaries) is excluded.
  3. **Provenance / source (added 2026-10-23, prompted by Ben's Fold3 question):**
     documents **sourced from paywalled aggregators** — Fold3, Ancestry, Newspapers.com,
     MyHeritage, Findagrave images, and the like — are **excluded by default**, even when
     they depict a deceased person and even when the *underlying* record is public domain
     (US government/NARA). Rationale: the aggregator's *digitized image* carries its
     branding/watermark and its terms restrict redistribution; FamilySearch's upload
     agreement requires the uploader to hold the rights; FamilySearch also partners with
     Ancestry and often already holds the record. Losing the subscription does not confer
     rights. Ben may **confirm rights per file** to allow a specific upload.
  Detection of provenance uses, in order of reliability: the source **citation** in the
  person's `ResearchSummary.md`, the `.webloc` source URL, the **filename** (e.g.
  `… Fold3 …`), and — least reliable — a visible **watermark** (OCR/image heuristic).
  Where provenance cannot be determined, the document is **flagged for Ben's review**,
  never uploaded automatically. (Archive scan on 2026-10-23: ~16 files cite Fold3, ~74
  cite Ancestry, ~10 Newspapers.com, ~6 Findagrave — so this dimension is load-bearing.)
  Borderline cases on any dimension are flagged for review rather than uploaded.
- **Match expectation (Ben's input): mostly already in FamilySearch.** Of the ~99
  unpinned persons, Ben expects most ancestors already have shared-tree profiles (he has
  been using FamilySearch as a research source). The job is therefore **match-heavy**,
  but the create path and conservative match thresholds must still exist for the
  minority who are genuinely absent.
- **Proof bar (two gates):** (1) read *and* write demonstrated against **beta** (the
  formalized spike) gates the write *mechanics*; (2) a **small, supervised production
  pilot** — writing to a handful of already-`.webloc`-pinned persons with Ben watching,
  data left in place — is the acceptance gate for the *real* production path, run before
  any bulk sync.

## What is left to the builder

1. **Prose → structured facts (the central open problem).** Recommended approach: define
   a reviewable per-person **sync manifest** (structured: names, gendered facts with
   dates/places, relationships, document list) generated from the prose — plausibly
   LLM-assisted — but **human-checked before it drives any write**, so the actual writes
   are deterministic and auditable. The builder designs the manifest schema and the
   generation/review workflow. This keeps the write path from ever depending on
   free-text parsing.
2. **Sync-state ledger** schema and location (archive person key ↔ FS prod ID ↔
   last-pushed fingerprint ↔ uploaded-document IDs), seeded from the 67 existing
   `.webloc` IDs on first run. Prefer a single structured file over re-parsing `.webloc`
   plists each run.
3. **Document → memory mapping** for the 457 files: which to upload, titles/descriptions,
   de-duplication (never re-upload), and linking to the correct person(s).
4. **Matching policy**: search strategy and confidence thresholds for auto-link vs.
   prompt-for-confirmation; how near-matches are presented in the dry-run plan.
5. **MCP tool surface**: exact tool names, parameters, and which write tools (if any) are
   exposed directly vs. reserved to the audited sync path.
6. **Token lifecycle UX**: auto re-extract on 401, and what to tell Ben when the browser
   session itself has expired.
7. **Throttle/rate-limit policy** specifics.
8. **Packaging**: library + MCP server + sync CLI (`--dry-run` / `--apply`), and whether
   the MCP server should borrow `familysearch-mcp`'s MIT read-side tool shapes (fork, not
   depend — it is single-maintainer, 0-star, and its author rejects browser tokens).
9. **Test strategy** beyond the beta harness: unit tests over recorded GEDCOM X fixtures;
   a safe "create-then-delete" smoke test for CI against beta.

### Suggested phasing

1. Core client: auth + cookie extraction + HATEOAS discovery + read + search.
2. Write primitives + the **beta proof harness** (acceptance gate).
3. MCP server over the read/search/write surface.
4. Sync engine: manifest extraction + review, ledger, dry-run/apply, document upload.

## Verification

Two gates:

1. **Beta integration suite** (the formalized spike: create person → add fact → create
   relationship → upload memory → link → delete, asserting 201/200/204 and clean
   teardown) gates the write *mechanics*. No write feature is considered done until its
   beta test passes.
2. **Supervised production pilot** gates the *real* path: a few already-`.webloc`-pinned
   deceased persons, enriched additively with Ben watching, data left in place. Only
   after the pilot passes does any bulk production sync run.

Unit tests cover parsing, classification, and HATEOAS discovery against recorded
fixtures.
