# P3 — Family Tree Write Verification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Verify live on beta the Family Tree *write* operations for persons, notes, relationships and relationship conclusions/source references (≈40 of the 60 Family Tree writes; `createPerson`, `deletePerson`, `restorePerson` already verified), each proven by read-back with a negative control, then recorded `verified` by the runner.

**Architecture:** Reuse the pilot recipe style (`tools/verify/recipes/pilot.py`): a `Recipe(evidence="state-change")` whose `run` builds fixtures with `ctx.fixtures`, calls the generated operation through `ctx.api` (VerifyApi unlocks only the recipe's declared ops), reads the state back, and calls `ctx.expect_changed(before, after, what)` with values taken from server responses. Strict teardown deletes everything. No new framework except a small shared helper module for write recipes (Task 1). Implementers write recipes offline against a stateful fake; the controller runs them live on beta and iterates from failures.

**Tech Stack:** Python ≥3.11, existing `tools/verify` framework, `responses`.

**Spec:** `docs/superpowers/specs/2026-10-25-full-api-coverage-design.md` (write bar: pre-state, action, read-back change, negative control, strict teardown). Prior plans: `…foundation.md`, `…p2-read-verification.md`.

## Global Constraints

- Beta only; fixtures `ZZSPIKE…`; interactive runs (fresh beta login, Keychain prompt); runner needs a clean git tree except `registry.json`; unit tests offline; never print tokens.
- A write is verified only by **read-back of changed state** with a **negative control**; never by status code. Each recipe asserts the exact new value came back from the server and that a before-read differed (`expect_changed`). Also assert the write affected only what it should (e.g. other fact/person unchanged) where cheap.
- Destructive operations (`delete*`, `restore*`, `merge*`) are exercised only on fixtures the recipe created; recipes declare them in `ops`; VerifyApi unlocks only declared ops; `confirm_destructive` is injected only for declared ops. **No `mergePerson`, `updateMatchResolution`, `not-a-match`, portraits, `updatePublished`, preferred-parent/spouse (account state) in P3** — those are P6/P8.
- Evidence `state-change`; ops claimed must all be called; teardown problems fail the recipe; registry written only by the runner.
- If a body/endpoint shape is unknown, learn it from the spec media types + proven library calls (`src/familysearch/tree.py`, `sources.py`) and live skeletons; never weaken a control. An operation that cannot be proven stays `unverified` with a reason in `docs/superpowers/recon/2026-10-25-api-surface/p3-deferred.md` (generated or hand-maintained).
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`. Plain `git push`; never force.

## Review Focus

- **No-op writes passing:** recipes whose read-back would be identical without the write (unique `ZZSPIKE` values, before/after from server).
- **Over-broad effect:** an update that changes more than intended (e.g. replaces all names/facts) must be detected — assert untouched siblings.
- **Leftover fixtures / undeleted objects:** strict teardown, relationship deletion order, restored objects deleted again.
- **Fixture hygiene:** only recipe-created ZZSPIKE objects are ever written/deleted; no write to any pre-existing person (e.g. the account's own).
- **Destructive ops** run only on recipe-created objects and read back as gone (404/410) / restored (200).

## File Structure

- `tools/verify/recipes/writes_common.py` — helpers (`expect_value`, `read_person_view`, `gone`, builders for person/note bodies).
- `tools/verify/recipes/writes_tree_persons.py`, `writes_tree_notes.py`, `writes_tree_relationships.py`, `writes_tree_relationship_details.py`.
- Tests: `tests/unit/test_verify_recipes_writes.py` (+ stateful fake for the tree write endpoints).

---

### Task 1: Shared write-recipe helpers and fake

**Files:** Create `tools/verify/recipes/writes_common.py`; Test `tests/unit/test_verify_writes_common.py`.

- [ ] **Step 1:** Read `tools/verify/recipes/pilot.py` and its tests; extract the reusable patterns (reading a person's name/facts/gender view from `readPerson`, `FSHTTPError` gone-check 404/410 helper, unique-value generation, person document builders) into `writes_common.py` with tests; extend the stateful fake tree service used by `test_verify_recipes_pilot.py`/`test_verify_recipes_reads.py` into a shared test module `tests/unit/_fake_tree.py` that supports POST updates (facts, names, gender), notes CRUD, relationships create/delete/restore, conclusions delete, source references, ordering endpoints — behaving like live shapes (use `docs/superpowers/recon/…/shapes.json`, `src/familysearch/tree.py`, `tests/integration/*.py`).
- [ ] **Step 2:** Run tests; **Step 3:** commit `"feat: shared helpers and fake for tree write recipes"`, push.

### Task 2: Person writes — `updatePerson`, `deletePersonConclusion`, `deletePersonSourceReference`, `createPersonMemory`

Recipes `writes.tree.person_update` (updatePerson: change name, add a fact, change gender — read back each; assert siblings untouched), `writes.tree.person_conclusions` (deletePersonConclusion: delete the added fact by id; read back gone, other facts intact), `writes.tree.person_source_reference` (attach a source description via `updatePerson` sources or via the proven `Sources.attach_to_person` for the fixture, then `deletePersonSourceReference` by srid; read back), `writes.tree.person_memory` (createPersonMemory multipart upload of a tiny PNG to the fixture child; read back via `getPersonMemories`/memory fixture; `deleteMemory` is Memories area — leave it and tear down with the proven library).
- [ ] TDD against the shared fake (lies: ignored update, update replacing all facts, delete deleting wrong conclusion); commit; **live step (controller):** run each recipe, iterate; commit registry; push.

### Task 3: Notes — person, couple, child-and-parents (create/update/delete)

Operations: `createPersonNote`, `updatePersonNote`, `deletePersonNote`, `createCoupleRelationshipNote`, `updateCoupleRelationshipNote`, `deleteCoupleRelationshipNote`, `createChildAndParentsRelationshipNote`, `updateChildAndParentsRelationshipNote`, `deleteChildAndParentsRelationshipNote`. One recipe per owner type (3 ops each: create → read back text; update → text changed, id stable; delete → gone from list). Reads used for read-back are verified reads (`readPersonNotes`, …).
- [ ] TDD; commit; live step; registry commit; push.

### Task 4: Relationships — create, update, delete, restore

Operations: `createRelationship` (couple body and child-and-parents body — two cases, same op), `updateCoupleRelationship`, `updateChildAndParentsRelationship`, `deleteCoupleRelationship`, `deleteChildAndParentsRelationship`, `restoreCoupleRelationship`, `restoreChildAndParentsRelationship`, `deleteChildAndParentsRelationshipParent`. Recipes verify with read-backs through verified reads (`readPersonSpouses/Parents/Children/Families`, `readCoupleRelationship`, `getChildAndParentsRelationship`). **Known hard lesson:** child-and-parents creation = ONE POST with `childAndParentsRelationships` (see docs/api-discovery.md); couple vs c&p confusion was the original bug — assert the relationship TYPE read back.
- [ ] TDD (lies: creates spouse instead of parent-child, delete leaves relationship, restore no-op); commit; live step; registry commit; push.

### Task 5: Relationship details — conclusions, source references, ordering

Operations: `deleteCoupleRelationshipConclusion`, `deleteChildAndParentsRelationshipConclusion`, `createCoupleRelationshipSourceReference`, `deleteCoupleRelationshipSourceReference`, `createChildAndParentsRelationshipSourceReference`, `deleteChildAndParentsRelationshipSourceReference`, `updateCoupleRelationshipSpousesOrder`, `updateChildAndParentsRelationshipParentsOrder`. Ordering ops need ≥2 spouses/parents: build the fixture (second spouse/parent) with the proven library; if order cannot be read back meaningfully, defer with the reason.
- [ ] TDD; commit; live step; registry commit; push; write `p3-deferred.md` for any unverified Family Tree write left (changes/restore, matches, merges, discussions, portraits, published, preferred*, collections → later tiers) and update README counts.

### Task 6: Final review and merge
- [ ] Skeptic review of all P3 recipes (weakest claims, over-broad effects, hygiene), fix, fast-forward `main`, push.
