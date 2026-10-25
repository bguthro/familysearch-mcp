# P2 — Read Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the 107 read operations of the generated `FamilySearchApi` live on beta (4 are already verified by the pilot; 3 "reads" are POSTs, 9 are HEAD, 1 is OPTIONS), moving the coverage report from 4 verified to ~100, and learn the real response shapes the later write tiers depend on.

**Architecture:** A small data-driven layer on top of the existing verification framework: a `ReadCase` per operation (path args from a fixture "world", expected-content check, not-found control), grouped into recipes of ≤12 operations so one bad case fails only its group. A `World` builder creates the beta fixtures reads need (persons, couple, child-and-parents, notes, source, memory) with the proven hand-written library, tracked for strict teardown. Implementers write cases offline from the recon (`shapes.json`, `deep.json`, spec); the controller runs them live on beta and feeds failures back until they pass.

**Tech Stack:** Python ≥3.11, existing `tools/verify` framework, `responses` for offline tests; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-25-full-api-coverage-design.md` (read bar: documented status + media type, body contains the fixture's known values, not-found negative control). Recon: `docs/superpowers/recon/2026-10-25-api-surface/` (`deep.py` shows the fixture creation; `shapes.json` real response skeletons; `deep.json` per-call statuses). Foundation plan: `docs/superpowers/plans/2026-10-25-full-api-coverage-foundation.md`.

## Global Constraints

- Verification runs **only on beta**, interactively (Keychain prompt; fresh beta login); fixtures named `ZZSPIKE…`, strict teardown (GET succeeds → DELETE 2xx → GET 404/410); unit tests offline; never print or log a token; live commands use `--tb=short` where pytest is involved; the runner needs a clean git tree except `registry.json`.
- Read recipes use `evidence="read-content"` and declare only read operations; `VerifyApi` unlocks nothing for reads. A read recipe must never call a write operation through the generated API (fixture building uses the hand-written library on the beta client, as `FixtureSet` does).
- A case passes only if **all** hold: the call returns a documented success status; the response media type matches the requested `Accept`; a **content check** (non-trivial, tied to a fixture's known value such as its id or unique `ZZSPIKE` name) passes; a **not-found control** fails as expected (bogus ids → 400/404/410, or for parameterless operations a query matching nothing / a missing required parameter → 400). The positive and negative calls happen in the same recipe, so a catch-all server cannot pass. **Amendment (2026-10-25, live-driven, confirmed by Ben):** beta answers an *empty-bodied 204* — not an error — for unknown ids on some reads (`readPersons`, `findRelationship`; the known "undocumented 204" spec defect). Such a case may set `control_may_be_empty` with a reason citing the observed behavior; this is accepted only when the same recipe's positive call returned 200 with fixture-anchored content that the request did not echo (so a server answering 204 to everything fails the positive call), and never for HEAD/OPTIONS/anchor-free/`allow_empty` cases.
- An operation that cannot be exercised on beta with obtainable fixtures stays `unverified` and is listed with the reason in the P2 report (private-tree areas are P7). **Never** weaken a content check or control to get a pass; never record a read as `unverifiable` just because it failed once.
- Registry shape stays `operations[id] = {"generated": …, "handwritten"?}`; only the runner writes `verified`.
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`. Plain `git push origin <branch>` after each task; never force.

## Review Focus

- **Vacuous content checks:** an `expect` that cannot fail (asserts only status, or `body is not None`) must be rejected by the framework (a case with no content check, or whose check ignores the response, fails the unit test `test_case_without_content_check_rejected`; checks must receive the `ApiResult` and the `World`).
- **Catch-all false pass:** not-found control passing because every call errors, or positive call passing because the server answers 200 to everything (control must use the same operation with bogus ids and expect 400/404/410 — and the positive must have asserted fixture-specific content).
- **Claiming ops never called**, or a read recipe that calls a write op: the runner's `ops ⊆ called` rule plus a new rule that every called op in a read recipe is `kind=read`.
- **Fixture leakage:** world teardown problems fail the recipe; ids/paths with unusual characters; fixtures created lazily (only what a group needs).
- **HEAD/OPTIONS/read-POST/204 operations:** content checks for HEAD use headers/status (and equality with the matching GET), 204 lists are valid empties only when a populated fixture variant of the same operation returned content in the same recipe.

## File Structure

- `tools/verify/reads.py` — `ReadCase`, `run_case`, `reads_recipe(...)`, helpers (`contains`, `json_path`).
- `tools/verify/world.py` — `World` (lazy fixture builder on top of `FixtureSet`).
- `tools/verify/recipes/reads_*.py` — case tables per area; registered in `recipes/__init__.py`.
- Tests: `tests/unit/test_verify_reads.py`, `test_verify_world.py`, `test_verify_recipes_reads.py` (one parametrized offline scenario per recipe against a scripted fake service).

---

### Task 1: Read-case framework

**Files:** Create `tools/verify/reads.py`; modify `tools/verify/core.py` only if needed to enforce "read recipes call only reads"; Test `tests/unit/test_verify_reads.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class ReadCase`: `op: str` (operation id), `path_args: Callable[[World], dict[str, str]]`, `expect: Callable[[ApiResult, World], None]` (raises `AssertionError` with a message on mismatch), `missing: dict[str, str]` (path-arg overrides that must not exist; `{}` for parameterless ops), `query: Callable[[World], dict] | dict | None = None`, `missing_query: dict | None = None` (query used for the control when there are no path args), `accept: str | None = None`, `body: Any = None` (read POSTs), `missing_statuses: tuple[int, ...] = (400, 404, 410)`, `needs: tuple[str, ...] = ()` (World fixture groups).
- `run_case(ctx, world, case) -> None`: calls `getattr(ctx.api, to_snake(op))` with the case's arguments; asserts the status is one of the operation's documented 2xx statuses (taken from the snapshot via `OPERATIONS`/snapshot responses) and the media type matches the requested/default Accept; runs `expect`; then runs the control with the missing ids/query and asserts it raises `FSHTTPError` with a status in `missing_statuses`; HEAD/OPTIONS handled (no body check; `expect` receives the headers via `ApiResult`).
- `reads_recipe(name: str, cases: Sequence[ReadCase]) -> Recipe` (`evidence="read-content"`, `ops` = the cases' operations, `run` builds the `World` from the union of `needs`, runs every case, collects **all** failures and raises one `AssertionError` listing each failing operation and message (so one live run shows every problem)).
- Helpers: `contains(result: ApiResult, *needles: str) -> None` (each needle appears in the JSON-serialized body or raises), `json_path(body, "a.b[0].c")` returning the value or raising `AssertionError`.
- Enforcement: `reads_recipe` rejects at construction a case whose operation is not `kind=read`, duplicate operations in one recipe, and a case with no `expect`; `run_case` fails if `expect` returned without having been called with the actual result (wrap: the framework calls it, and a case whose `expect` is a no-op lambda is caught by a mutation test in the unit tests — fixture response changed → case must fail).

- [ ] **Step 1: Write failing tests** (fake service via `responses`): positive case passes; wrong content fails with the message; server answering 200 to bogus ids fails the control; server answering 4xx to the positive call fails; 204 on the positive call fails unless the case declares `allow_empty`; media-type mismatch fails; HEAD case passes with headers only; read-POST case sends its body and no `X-Reason`; `reads_recipe` rejects a write op / duplicate op / missing `expect`; aggregates multiple failing cases in one error; `test_case_without_content_check_rejected` and `test_noop_expect_is_caught_by_mutation` (Review Focus 1); recipe passes through the real runner (`run_recipe`) against a fake and records `verified` with `read-content` evidence; a read recipe that somehow calls a write op is refused.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** the full unit suite → pass.
- [ ] **Step 5: Commit** (`"feat: read-case framework for read verification"`), push.

---

### Task 2: The fixture world

**Files:** Create `tools/verify/world.py`; Test `tests/unit/test_verify_world.py`.

**Interfaces — Produces:** `World(ctx_client, fixtures: FixtureSet, reason: str, unique: str)` with `need(*groups: str) -> None` (idempotent, builds only missing groups, tracking each object in `fixtures` immediately) and read-only attributes/dict `ids` populated by group:
- `persons` → `dad`, `mom`, `child` (deceased, `ZZSPIKE`-named; unique surname = `unique`).
- `couple` → `couple` relationship id (dad+mom), `child_and_parents` id (child ← dad+mom) — via `TreeWriter.create_couple` / `create_parent_child`.
- `notes` → `person_note`, `couple_note`, `child_and_parents_note` ids (raw POSTs on the beta client as in `deep.py`, tracked).
- `source` → `source_description` id, attached to `child` (`Sources.create_description` + attach), plus `source_reference` id if discoverable.
- `memory` → `memory` id uploaded to `child` (tiny PNG, `Memories.upload_to_person`).
- `names`: expected display strings (`child_name`, `unique_surname`) for content checks.
Teardown is `fixtures.teardown()` (strict, relationships first); the `World` registers only what it created.

- [ ] **Step 1: Write failing tests:** `need("persons")` creates exactly 3 persons and tracks them before returning; `need("couple")` implies persons; idempotent; groups build lazily (a recipe that needs only persons performs no memory upload); every created object is tracked even when a later step fails (inject a failure → tracked list still tears down); ids discovered as in `deep.py` (use its response parsing; read it); refuses a non-beta client.
- [ ] **Step 2–4:** run → implement (port the proven calls from `docs/superpowers/recon/2026-10-25-api-surface/deep.py` lines ~54–105; do not invent endpoints) → run. **Step 5: Commit** (`"feat: fixture world for read verification"`), push.

---

### Task 3: Family Tree reads, part 1 — persons, relatives, search, history

**Files:** Create `tools/verify/recipes/reads_tree_persons.py`; modify `recipes/__init__.py`; Test `tests/unit/test_verify_recipes_reads.py`.

Read operations (resolve the exact list from `tools/spec/snapshot.json` area "Family Tree Endpoints" with `kind=read`; this task owns every read whose path starts `/tree/persons`, `/tree/search`, `/tree/matches`, `/tree/current-person`, `/tree/persons/{pid}/…` except notes, sources, memories, relationships, discussions): e.g. `readPerson` (already verified; include only if useful as the content-check anchor — do NOT re-verify), `readPersons` (`pids` query), `readPersonParents/Children/Spouses/Families`, `searchTreePersons`, `readTreePersonMatches`, `performPersonMatchesByExample`, `readPersonChangeHistory`, `readPersonMerge`/`allowPersonMerge`, `readCurrentTreePerson`, HEAD variants. Recipes of ≤12 ops, e.g. `reads.tree.persons`, `reads.tree.relatives`, `reads.tree.search`.

- [ ] **Step 1: Write failing tests:** a scripted fake service of the tree (state kept in memory: persons, couple, c&p; search by unique surname; change history) in which every recipe passes via `run_recipe`; mutation variants (fake returns another person's data / ignores `pids` / answers 200 for bogus ids) make the group fail; `test_recipes_registered_and_reference_real_read_ops` (every op exists, `kind=read`, unique across all read recipes, recipe names unique).
- [ ] **Step 2–4:** implement cases from the recon: use `shapes.json`/`deep.json` for statuses and shapes (204s on empty lists, redirect-only documented success for `current-person`, `readPersons` requires `pids`, search needs ≥1 query param and the proven `flag=fst` shape, matches needs `Accept: application/x-gedcomx-atom+json`); content checks assert the fixture ids/names appear in the right place. **Step 5: Commit** (`"feat: read recipes for tree persons, relatives, search, history"`), push.
- [ ] **Step 6 (controller, live, beta only):** `.venv/bin/python -m tools.verify run --recipe <name>` per recipe (commit first; the runner requires a clean tree). Failures → `SendMessage` the implementer with the sanitized failure text; rerun until each passes or is documented as not exercisable. Commit `registry.json` (`"verify: tree person reads verified on beta"`), push.

---

### Task 4: Family Tree reads, part 2 — relationships, notes, sources, memories, discussions, collections

**Files:** Create `tools/verify/recipes/reads_tree_relations.py`; register; extend `tests/unit/test_verify_recipes_reads.py`.

Operations: every remaining Family Tree read (`/tree/child-and-parents-relationships/**`, `/tree/couple-relationships/**`, notes, `readPersonSources`, source references, memories-in-tree, discussions, comments, user-defined collections, preferred parent/spouse, `/tree/users/**`) not claimed by Task 3 — the union of Tasks 3 and 4 must equal all 57 Family Tree reads (a unit test asserts it, so none is silently skipped). Discussions, comments and user-defined collections need fixtures this plan cannot create without unverified writes: leave those operations `unverified` and list them (with reason) in the report for P3/P8, and have the unit test accept an explicit `DEFERRED = {op: reason}` table.

- [ ] **Steps 1–5** as Task 3 (offline fake of notes/sources/relationships; mutations; coverage-completeness test; commit `"feat: read recipes for tree relationships, notes, sources, collections"`, push). **Step 6** live as Task 3 (commit registry `"verify: tree relationship reads verified on beta"`).

---

### Task 5: Standards, Memories, Users, Utilities reads

**Files:** Create `tools/verify/recipes/reads_standards.py`, `reads_misc.py`; register; extend tests.

Operations: the 24 Standards reads (places, place types, vocab lists/terms/concepts, dates, names — `readPlace*`, `readVocab*`, `searchForParentPlaces`, `getNameSegments`, `createNameSegments` [read POST], `checkPlaceIsChild`, …), 6 Memories reads, 4 Users reads, 1 Utilities read. Standards ids are **discovered at run time** from earlier reads in the same recipe (e.g. a place id from a place search, a vocab list id from `readVocabList`) rather than fixtures: a `ReadCase.path_args` may read from a recipe-local discovery step (add `World.discover(name, fn)` or a case-level `setup` hook — decide in the implementation, with tests). Content checks tie to the discovered value echoed back (the place's id/name). Users reads: `readCurrentUser` (already handwritten-verified) etc. Memories reads use the `memory` fixture (and a comment-free memory; skip comment ops needing writes → `DEFERRED`). `readPartnerEligibility` (403 on beta): classify nothing in this task; report it.

- [ ] **Steps 1–5** as Task 3 (commit `"feat: read recipes for standards, memories, users, utilities"`, push). **Step 6** live (commit registry `"verify: standards and misc reads verified on beta"`).

---

### Task 6: Private-tree reads, coverage report and docs

**Files:** Create `tools/verify/recipes/reads_private.py` (only for operations that have obtainable fixtures: list endpoints that work on the signed-in beta account); modify README coverage numbers and `docs/api-discovery.md` (what the live runs taught: real response shapes, quirks, deferred list); Test: extend recipe tests.

- Genealogies (8), User Tree (7) reads and `readGroup`/`readTree*`: for each, try discovery through list endpoints on the beta account; with no tree/group fixture available the operation stays `unverified` and is listed in a committed `docs/superpowers/recon/2026-10-25-api-surface/p2-deferred.md` (operation, reason, which later tier unblocks it: P7 creates the fixtures).
- [ ] **Step 1: Write failing tests** for any recipes added and for `p2-deferred.md` consistency (a test parses it and checks every listed op exists, is `unverified` in the registry, and that no verified-able read is missing from both the registry-verified set and this file once P2 is complete: `verified reads ∪ deferred reads == all 107 reads`).
- [ ] **Step 2–4:** implement; run suite. **Step 5: Commit** (`"docs: P2 read verification results and deferred reads"`), push.
- [ ] **Step 6 (controller):** final `python -m tools.spec.coverage` shows the new verified count; live run of `reads_private` if any; commit registry; fast-forward `main` and push after the final review.
