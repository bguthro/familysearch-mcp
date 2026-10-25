# Full API Coverage for the `familysearch` Library — Design

**Date:** 2026-10-25
**Status:** Draft for approval
**Author:** Ben Guthro (with Claude)
**Recon record:** `docs/superpowers/recon/2026-10-25-api-surface/` (scripts, inventory, real response shapes)

## Intent and why

The `familysearch` library covers about 25 of FamilySearch's **205 documented API operations**
(~12%). We built those because we guessed we would need them. Ben does not yet know which of the
rest he will need, and does not want the library to limit what is possible. The goal is a library
that exposes **every operation FamilySearch documents**, with an honest, machine-checked record
of which ones have actually been proven to work.

"Proven" has a precise meaning here because of what went wrong before. Two writes in our
hand-built library returned success codes while doing the wrong thing (parent-child created
*spouse* links; the memory link did nothing). **A 2xx response proves nothing. A write is proven
only by reading the resulting state back from the service and seeing the change.**

## What we know (recon, 2026-10-25, beta only)

* The official reference lists **205 operations** (≈105 reads, ≈99 writes/deletes, 1 `OPTIONS`),
  in eight areas: Family Tree 117, Standards 26 (places, dates, names, vocabularies), Genealogies 21
  (private trees), Memories 16, User Trees 14, User 8, Authentication 2, Utilities 1. 43 are
  high-impact (delete, merge, restore). Each reference page embeds a full **OpenAPI 3.0 definition**.
* A browser-session token reaches **every area**: zero 401s; one 403 (partner-only feature).
* With real fixture ids, **84% of read calls succeeded (211/251)**; no documented `Accept` value was
  rejected and no response had a different content type than requested. The spec is **reliable
  about requests** (paths, methods, parameters, media types, status codes).
* The spec **does not describe response bodies** (6 of 96 GET operations have a schema, none an
  example). Bodies are prose linking to separate guides.
* Known **spec defects:** two operations (read/update preferred parent relationship) list a doubled
  malformed path; `GET /tree/persons` needs `pids` though it is marked optional; list endpoints
  return `204` when empty though only `200` is documented; `current-person` and `sources/collections`
  document only a redirect as success; Genealogies ids need an undocumented prefix; search endpoints
  400/404 when given no criteria.
* 28 read operations could not be exercised (no fixture): mostly the private-tree areas.
* Findings from earlier work that the new library must respect: writes need
  `Content-Type: application/x-fs-v1+json`; browser `User-Agent` is mandatory (python-requests gets
  HTTP 403 on both hosts); `GET …/matches` needs `Accept: application/x-gedcomx-atom+json`;
  `q.birthLikeDate` accepts only a year or ISO date; parent-child is a `childAndParentsRelationships`
  POST, not `relationships`+`ParentChild`; couple resources/deletes live under
  `/platform/tree/couple-relationships/{id}`; deletion must be verified by read-back, never by
  tolerating a 404.

## Goals

1. A method for **every one of the 205 operations**, generated from the official spec, usable from the
   existing `FSClient` (same auth, User-Agent, throttling, change-message and origin-check rules).
2. A **registry** recording, per operation, one of: `verified` (with the live recipe that proved it and
   when), `unverifiable` (with the specific reason), or `excluded` (OAuth). Machine-readable, enforced.
3. A **lock:** a write operation that is not `verified` refuses to run. The only override is explicit
   and works on **beta only**. On **production**, an unverified write can never run, and even a
   `verified` write needs an explicit `allow_production=True` (`verified` means "proven on beta", not
   "proven on production"). The lock covers the **generated API layer**; the raw `FSClient.post`/`delete`
   stay the documented low-level escape hatch, and the MCP server keeps its own separate gates.
4. A **verification harness:** for each write (and each read), a live recipe on beta that creates
   labelled fixtures, performs the operation, **reads the state back and shows it changed** (and for
   deletes, that the object is gone), and cleans up with verified teardown.
5. **Done means:** all 205 operations are either `verified` by a recorded live run or `unverifiable`/
   `excluded` with a stated reason. No operation is silently skipped. A coverage report is generated
   from the registry.
6. A **drift check** so a change in the official spec, or in the generated code, is detected.

## Non-goals

* No new **MCP tools.** The MCP server is unchanged (its 16 tools keep their guardrails).
* No **production** verification. Everything is proven on beta; the library docs state this plainly.
  (The first production writes remain a separate, supervised pilot.)
* No typed **response models.** Responses remain raw GEDCOM X data (as today). Real shapes are
  recorded as reference only.
* No OAuth flow (the two Authentication operations are `excluded`; our approach uses the browser session).
* No replacement of the existing hand-written classes (`TreeReader`, `TreeWriter`, `Memories`, `Sources`,
  `Search`); they stay and are recorded in the registry as verified.
* The archive → FamilySearch **sync engine** is a separate project.

## Anti-goals (a failure even if it "works")

* A write marked `verified` on the strength of a 2xx alone, or by a test that would pass even if the
  operation were a no-op.
* Any write reaching **production** by accident (the override must be impossible there).
* Test data left on beta, or a teardown that hides a wrong delete URL by tolerating 404.
* The registry being edited by hand to say "verified" without a recipe that proves it.
* Generated methods that look callable but silently use a wrong path (e.g. the spec's doubled path)
  or ignore a real server requirement.

## Constraints

* Python ≥ 3.11; runtime deps unchanged (`requests`, `browser_cookie3`, `mcp` is an MCP-package dep only).
* Reuse `FSClient` (browser User-Agent, `Authorization`, same-origin check, write throttle, required
  change message `reason` on every write, `x-fs-v1+json` writes).
* Environment is explicit (`Environment.BETA`/`PRODUCTION`); base URLs come from it, **not** from the
  spec's `apibeta`/`identbeta` hosts.
* Verification runs only against beta, with fixtures named `ZZSPIKE…`, and only by an interactive run
  (needs a fresh beta login in Brave and the macOS Keychain prompt). CI/unit runs never touch the network.
* Unit tests stay offline and fast; the live recipes are skipped without a token.
* The generated layer uses the spec's literal paths rather than HATEOAS discovery (a deliberate change
  from the original library rule; the drift check and the verification recipes are the safeguard).
* Never print or log a token. pytest runs of the live suites use `--tb=short`.

## Decisions Ben made

* Scope is **everything**: all operations, and all of them verified (or recorded unverifiable) before
  the project is called done.
* **Unverified writes refuse to run** until a live read-back test has passed; the only override is
  explicit and beta-only.

## Calls I made (Ben: please check these)

1. **Static generated code, checked in** (not a runtime-built client): easy to read, review and diff.
   A generator script plus a committed snapshot of the spec reproduce it.
2. **Library only;** MCP tools unchanged.
3. **OAuth endpoints excluded.**
4. **Extra opt-in for destructive operations** (delete, merge, restore): even when `verified`, they need
   an explicit `confirm_destructive=True`.
5. **Which GETs/POSTs count as reads:** classify by *behavior*, not just method. A few "GET"s are
   mutating (e.g. "Create Partner Account") and some "POST"s are reads (e.g. "Read Person Matches by
   Example", "Compose Full Name"); a small override table records the true kind. Unclassified or
   mutating operations start locked.
6. **"Unverifiable" is a legitimate end state** with a reason (needs partner/admin rights, edits shared
   reference data, OAuth), and it stays locked. Normally an operation is classed `unverifiable` only
   after a real attempt shows why. **Exception:** where the attempt itself would modify shared or
   irreversible data (vocabulary/term definition and translation updates, partner-account endpoints
   including the mutating "Create Partner Account" GET), it is classed `unverifiable` **without being
   executed**, with the reason written down. See "Verification bars" below.
7. **Existing hand-written methods stay as they are** and are mapped to their operation ids in the
   registry. **Their `verified` status does not carry over** to the generated method for the same
   operation: the generated method uses the spec's literal path and its own body/Content-Type handling,
   so the registry keeps a **separate status per implementation** (`handwritten`, `generated`), and the
   generated method for those ~25 operations gets its own recipe before it unlocks.
8. **Production rule** (answers "do beta-verified writes run on production?"): every generated write on
   production requires an explicit `allow_production=True`, verified or not; unverified writes never run
   there. No recipe ever runs against production in this project.
9. **Lock scope:** the generated layer only. A caller who uses raw `FSClient.post`/`delete` bypasses it;
   the docs say so. The MCP server's existing gates (`FS_ALLOW_PROD_WRITES`, guardrails) are unchanged.
10. **Staleness:** the registry stores a **fingerprint** of each operation (its spec entry + overrides +
    generated signature). If a re-harvest or an override edit changes it, the operation's `verified`
    status automatically reverts to "stale" and the lock treats it as unverified until its recipe is re-run.

## Shape of the solution (guidance; the builder owns the details)

* **Spec snapshot + harvester.** `harvest.py` (from the recon) becomes a tool that fetches the index
  and every reference page and writes a normalized snapshot (operation id, area, method, path,
  parameters with in/required/example, `Accept` values, request media types, documented statuses).
  The snapshot is committed; a separate manual command re-harvests and reports differences.
* **Overrides table** (data, not code): path corrections (doubled path), parameters the server really
  requires, operations that are really reads/writes, the Genealogies id-prefix note, redirect-as-success
  operations, "204 means empty" list endpoints, destructive tags.
* **Generator** → per-area generated modules exposing one method per operation: path/query/header
  parameters, `Accept` choice (default = the first documented value), request body passthrough, the
  lock check, and a uniform result (status, headers, body-or-None; `204` → empty body, redirects followed).
* **Registry** (`registry.json` or similar): per operation and per implementation (`handwritten`,
  `generated`) `{status: verified|unverifiable|excluded|stale, recipe, verified_on, commit, fingerprint,
  evidence: state-change|read-content|none, reason, kind: read|write, destructive}`. A unit test fails
  if an operation is `verified` without an existing recipe, if a recipe references an unknown operation,
  if a stored fingerprint no longer matches (→ reported as `stale`), or if the generated code and
  registry disagree.
* **Guard.** Non-`verified` writes raise `UnverifiedOperationError` before any network call. Override:
  explicit parameter (e.g. `allow_unverified=True`) **and** `Environment.BETA`; on production an
  unverified write always raises, and a verified write needs `allow_production=True`. Reads are not
  locked. Destructive operations additionally need `confirm_destructive=True`. The check is in the
  generated methods (raw `FSClient` is not locked).
* **Recipes + runner.** Each recipe: create labelled fixtures → capture pre-state → perform the operation
  → read state back → assert the change (and a negative control: the pre-state differed) → verified
  teardown. A runner executes recipes against beta, records the result in the registry, and prints a
  coverage report. Fixture dependencies (e.g. a Genealogies tree for tree/person operations) are
  recipe-level.
* **Drift check** (offline unit test): regenerating from the committed snapshot + overrides reproduces
  the committed code exactly.

## Verification bars (what "verified" requires)

* **Writes that act on a labelled fixture** (most of them): create fixtures → capture the pre-state →
  perform the operation → read the state back and assert the intended change → show a **negative
  control** (the pre-state really differed, so a no-op would fail) → verified teardown. For deletes,
  the object reads back gone (404/410) and, where the API allows, `restore` brings it back.
* **Reads:** a successful status (including documented `204` and redirect-as-success), the documented
  media type, **and a body that contains the fixture's known values** (not just "2xx"), plus a
  **not-found negative control** (the same call on a nonexistent id returns the documented error).
  Reads that need data are given data (create the fixture first) so the assertion can be about content.
  A read that returns an empty body is `verified` only if the fixture is genuinely empty by design.
* **Writes that change the account rather than a fixture** (e.g. Set Current Tree Id, Update/Delete User
  History, preferred spouse/parent relationships, tree-person published state): the recipe **captures the
  prior state and restores it**, and verifies the restore by read-back. If the prior state cannot be
  restored with certainty, the operation is classed `unverifiable` **without being executed**.
* **Never executed:** operations that edit shared reference data (vocabulary/term definitions and
  translations) or are partner-only. They are `unverifiable` with the reason recorded, and they stay locked.
* Every recipe that touches fixtures uses the same strict teardown as the existing live suites:
  GET must succeed, DELETE must be 2xx, GET must then be 404/410; a missing tracked object is an error.

## Left to the builder

* Module/package layout, naming of methods (operation-id based vs path based), result type.
* Exact override-table format and how redirects/204 are normalized.
* Recipe format and runner UX; ordering/tiering of the ~99 write recipes (suggest: Family Tree persons,
  relationships and notes → sources/source box → memories → change-history restore (an undo path) →
  match-by-example → Genealogies/User Tree/groups → discussions → destructive operations last).
* How the recipes discover required request bodies (the spec has no body examples; use the real shapes
  in the recon record and the existing verified calls as the reference).
* Rate-limit handling (429) in the runner.

## Verification (how we know this is done)

* Offline: unit suite passes; generator drift test passes; registry consistency tests pass; the lock
  has tests for every branch (unverified write refused; override works on beta only; production always
  refuses; reads unlocked; destructive needs the extra flag).
* Live: a full runner pass on beta in which **every** operation ends `verified` or `unverifiable` with
  a reason; the coverage report is committed; beta is left clean (leftover check passes).
* No `verified` operation depends on a recipe that would still pass if the operation did nothing.

## Risks

* Some operations may need account features or rights the beta account lacks (private trees, groups,
  discussions) → classed `unverifiable` after a real attempt.
* Beta behavior may differ from production; nothing here proves production. State this in the docs.
* The spec can change underneath us; the drift check only helps if someone runs it.
* Volume: ~99 write recipes plus ~105 read checks is a lot of live runs against a session cookie that
  lasts about a day; the runner must resume cleanly and idempotently.
