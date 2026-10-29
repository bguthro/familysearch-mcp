# Import Engine: Link Discovery and Production Verification (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Replace the naive "ids from .webloc files" inventory with a link report: mine every candidate FamilySearch person id from the archive (weblocs, notes, filenames), verify each against PRODUCTION (read-only) by name and dates, and for people with no confirmed id search production and list candidates. Never writes to FamilySearch.

**Why:** Ben's real-archive check showed the first report called ~55 people "missing" whose ids are written in their notes. Only 79 of 165 had a webloc id.

**Spec:** `docs/superpowers/specs/2026-10-29-import-engine-design.md`; builds on `src/familysearch_import/` (scan, classify, record, report).

## Global Constraints
- **Read-only.** The production adapter exposes GET/HEAD only and raises on any other method; no function in this plan may call a write operation. Tests assert the guard.
- **No personal data in this repo**: fictional people in tests; all output (`_import/LinkReport.md`, `_import/links.json`) goes under the archive path; refuse an output path inside the repo (reuse existing guards in `familysearch_import/__main__.py`).
- **Living people are never looked up or searched**, and never appear by name in output (counts only). Unknown-status people are not searched either.
- Candidates are never auto-accepted: the link report records evidence; Ben approves in the later dry-run. Duplicates/multiple candidates are reported, never merged.
- Tests inject a fake lookup; no network in unit tests. Run with `--tb=short`. Commit trailers as in prior plans. Gate commits on test results. Do not push.

## Review Focus
- An id found in a note about someone else (a parent's id in a child's summary) must be rejected as `mismatch`, not linked.
- FamilySearch ids that are merged (redirect to a survivor) or deleted must be reported with the survivor id, not treated as confirmed or as plain missing.
- Names with parentheses (alt/maiden names), diacritics (NFC/NFD), `?` dates and year ranges must compare sanely; a birth year off by up to 2 is `probable`, not `confirmed`.
- Rate limiting or transient errors (429/5xx) must retry with backoff and finally record `error`, never `not found`.
- A person with two different confirmed ids is `conflict` (listed, nothing chosen).

---

### Task 1: Mine candidate ids

**Files:** Modify `src/familysearch_import/scan.py`; Test `tests/unit/test_import_scan.py`.
**Interfaces:** Produces `@dataclass(frozen=True) IdCandidate(id: str, source: str, kind: Literal["webloc","note","filename"])` and `mine_candidates(person: Person) -> tuple[IdCandidate, ...]`. Sources: `.webloc` URLs via `familysearch_person_id`; ids in `.md`/`.txt` files (skip `questionable/`), found by regex `\b[A-Z0-9]{4}-[A-Z0-9]{3,4}\b` that contain a digit and also from `ancestors.familysearch.org`/`/tree/person/...` URLs inside the text; ids in file names. Deduplicate by id keeping every source. `source` is the file name relative to the person folder (no absolute paths).
- [ ] Tests (fictional): id in webloc, in ResearchSummary prose ("FamilySearch ID: ABCD-123"), in URL inside markdown, in filename; word-like false positives such as `TEST-2024`/dates are rejected (require digit and exactly the 4-3/4 shape; ignore ids inside `questionable/`); dedupe.
- [ ] Implement, run tests, commit `feat: mine candidate FamilySearch ids from notes`.

### Task 2: Verdict logic (offline, pure)

**Files:** Create `src/familysearch_import/linking.py`; Test `tests/unit/test_import_linking.py`.
**Interfaces:** Consumes `Person`, `IdCandidate`. Produces `@dataclass(frozen=True) FsPerson(id: str, name: str, birth_year: int | None, death_year: int | None, living: bool | None, status: Literal["ok","redirected","deleted","not-found","error"], survivor_id: str | None = None)`; `compare(person: Person, fs: FsPerson) -> Literal["confirmed","probable","mismatch"]` (confirmed: name tokens match after NFC/case/punctuation/parenthesis normalization AND birth and death years each equal or absent on one side; probable: name matches and a year differs by <= 2 or only one side has years; else mismatch); `@dataclass(frozen=True) LinkResult(person: Person, candidates: tuple[tuple[IdCandidate, FsPerson, str], ...], verdict: Literal["confirmed","probable","conflict","unlinked","needs-search"], chosen_id: str | None)` and `resolve_links(person, lookups: Mapping[str, FsPerson]) -> LinkResult` (two different confirmed ids -> `conflict`, chosen None; one confirmed -> `confirmed`; else best probable -> `probable`; nothing usable -> `needs-search`). Redirected candidates are compared using the survivor's data and chosen_id is the survivor id.
- [ ] Tests covering every Review Focus line above; then implement; commit `feat: link verdict logic`.

### Task 3: Read-only production adapter and search

**Files:** Create `src/familysearch_import/prod_reader.py`; Test `tests/unit/test_import_prod_reader.py`.
**Interfaces:** `class ReadOnlyFS` wrapping a `familysearch.client.FSClient` whose `request` is replaced by a guard that allows only GET/HEAD (raises `ReadOnlyError` otherwise); `ReadOnlyFS.production()` builds it from `extract_brave_token()` and `Environment.PRODUCTION`, refusing if the signed-in environment is not production; `get_fs_person(id: str) -> FsPerson` (uses `TreeReader.get_person`; maps 301/redirect/merged responses to `redirected` with `survivor_id`, 404/410 to `not-found`/`deleted`, retries 429/5xx with exponential backoff up to 4 tries then `error`; sleeps >= 0.3s between calls); `search_candidates(person: Person, limit: int = 3) -> list[FsPerson]` using `familysearch.search.Search.tree_search` with given/surname from the folder name and birth/death years +-2, returning top matches (never called for non-deceased people; assert). Look at `src/familysearch/{tree,search,client,errors}.py` for the real shapes; unit-test with `responses`/fake client only, including the guard (POST raises) and each status mapping.
- [ ] Tests, implement, commit `feat: read-only production adapter`.

### Task 4: Link report CLI

**Files:** Modify `src/familysearch_import/__main__.py`, create `src/familysearch_import/linkreport.py`; Test `tests/unit/test_import_linkreport.py`.
**Interfaces:** `build_link_results(people, lookup: Callable[[str], FsPerson], search: Callable[[Person], list[FsPerson]]) -> list[LinkResult]` (deceased only; living/unknown excluded and counted); `render_link_report(results, counts) -> str` (Markdown: totals per verdict; sections per verdict listing `line / folder`, candidate id, where found, FS name/lifespan; search candidates for `needs-search`; living as counts only); `links_json(results) -> dict` (machine-readable, ids and verdicts, no living). CLI: `python -m familysearch_import links --archive PATH --env production [--limit N] [--out DIR]` writes `<archive>/_import/LinkReport.md` and `links.json` under the same overwrite/inside-repo rules as `report`; `--env` required and must be `production` or `beta`; a `--fake` hook is not needed (tests call the functions).
- [ ] Tests with fake lookup/search; implement; run the full suite; commit `feat: link report`.

### Controller steps
Live production READ run by the controller (Brave signed in to production); review the report with Ben; then next plan: ImportRecord drafts and the planner.
