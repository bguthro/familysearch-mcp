# Match-by-example and Private Trees (P6-lite + P7) Verification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** (A) Verify `performPersonMatchesByExample` (the match-before-create check for the import engine). (B) Verify the Genealogies private-tree API (21 ops) so the import engine can place *living* people in a private space, then the User Tree / groups API (14 ops).

**Why (Ben, 2026-10-26):** private trees and match-by-example are worth doing before the import engine. **Person merges are never done (policy).** Everything else in P6/P8/P9 stays out of scope.

**Prior plans' Global Constraints/Review Focus apply verbatim** (P2/P3/P4/P5 plans): beta only, ZZSPIKE fixtures, read-back with negative control, no weakening, defer with concrete reasons, strict teardown, redaction of account ids, models per task (Sonnet for pattern work; Opus for skeptic reviews of anything that writes), commit trailers `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`, plain pushes.

## Live facts from read-only probes (2026-10-26, beta)
- `POST /tree/matches` (`performPersonMatchesByExample`), Accept `application/x-gedcomx-atom+json`:
  - empty body `{}` → HTTP 400 "A gedcomx containing the example match person must be provided." (usable not-found/negative control).
  - `{"persons":[P]}` → 400 "The gedcomx must contain a descriptionRef."
  - `{"description":"#ex1","persons":[P]}` → 400 "Failure in upstream call: Unable to retrieve matches for given example (upstream 400)".
  - **Works:** `{"persons":[P], "sourceDescriptions":[{"id":"sd1","about":"#ex1"}], "description":"#sd1"}` with `P = {"id":"ex1","gender":{"type":"http://gedcomx.org/Female"},"names":[{"nameForms":[{"parts":[{"type":"…/Given","value":"Rosina"},{"type":"…/Surname","value":"Zopfi"}]}]}],"facts":[{"type":"http://gedcomx.org/Birth","date":{"original":"11 March 1870"},"place":{"original":"Switzerland"}}]}` → 200 atom feed, `results: 1`, entry `id: "GQYM-SVR"`, `confidence: 5`, `score≈0.997`, title "Person GQYM-SVR (Rosina Zopfi)" — i.e. the stable canary record (same canary as the search recipe, in the CANARY block).
- `GET /tree/persons/GQYM-SVR/matches` → 204. `GET /genealogies/trees` → 204 and `GET /groups` → 204 (account has no private trees/groups; fixtures must be created). `GET /trees/current` → `{"trees":[{"id":"GLOBAL"}]}`.

## Review Focus
- Private-tree objects are created/deleted only by the recipes (ZZSPIKE-named); the account's current tree (`GLOBAL`) must never change: `setCurrentTree` is account state — use a capture-and-restore recipe only if it can be done with the restore in a `finally` and verified; otherwise leave unverified/deferred with reason.
- Genealogies ids need the documented prefix; path args are quoted; read-backs go through verified reads where possible.
- Living-person data: fixtures are fictional ZZSPIKE people; mark a fixture living ONLY inside the private Genealogies space; never create living persons in the shared Family Tree.
- Match-by-example is a read POST: body is static (canary), anchor-free with `literal_anchors` (`GQYM-SVR`, `Rosina Zopfi`) and the empty-body 400 control; the case must also check the entry's confidence ≥ 3 and the score field exists.

## Tasks
- **Task A (Sonnet):** read case(s) for `performPersonMatchesByExample` using the probed body (put the example person in the existing CANARY block of `tools/verify/recipes/reads_tree_persons.py` or wherever the search canary lives; never hardcode elsewhere). `readTreePersonMatches` / `…Headers` stay deferred (204 for every available person) with that evidence. Update DEFERRED tables/reports. Live run by controller; commit registry.
- **Task B1 (Sonnet):** Genealogies trees: `createGenealogiesTree`, `updateGenealogiesTree`, `deleteGenealogiesTree` with read-backs `readGenealogiesTrees`/`readGenealogiesTree` (the reads become verified via read cases anchored on the created tree's name); World/FixtureSet kind `genealogies_tree` with strict teardown.
- **Task B2 (Sonnet):** Genealogies persons: `createGenealogiesPerson`, `updateGenealogiesPerson`, `deleteGenealogiesPerson`, `restoreGenealogiesPerson`, `deleteGenealogiesConclusion`; reads `readGenealogiesPersons`, `readGenealogiesPerson`; a living fixture person inside the private tree.
- **Task B3 (Sonnet):** Genealogies relationships and sources: `updateGenealogiesRelationship`, `deleteGenealogiesRelationship`, `createGenealogiesSourceDescription`, `updateGenealogiesSourceDescription`, `deleteGenealogiesSourceDescription`; reads `readGenealogiesSourceDescription`, `readGenealogiesNote` (needs a note: defer if no creating op), matches reads (`readGenealogiesPersonMatches`, `readGenealogiesBulkMatch`: likely 204 → defer with evidence).
- **Task C (Sonnet):** User Tree / groups: `createGroup`, `updateGroup`, `deleteGroup`, reads `getGroups`, `readGroup`; `createTree`, `updateTree`, `deleteTree`, reads `readTree`, `readUserTreePersons`, `readTreeChanges`, `readTreeMatches`, `readCurrentTree`; `setCurrentTree` only with a safe capture-and-restore design (else defer).
- **Controller:** live runs per task, fix rounds, regenerate deferred reports, README numbers, skeptic review (Opus) of the writes, merge, push, clean worktree.
