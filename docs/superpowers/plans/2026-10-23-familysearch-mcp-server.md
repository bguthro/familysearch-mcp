# FamilySearch MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python MCP stdio server that exposes the in-repo `familysearch` library as interactive tools, so Claude (with filesystem access to the research archive) can correlate local research against FamilySearch and read/create/attach, human-in-the-loop, behind balanced guardrails.

**Architecture:** A `familysearch_mcp` package using FastMCP (the official `mcp` SDK). A single `Context` builds the library's services (`FSClient`/`Discovery`/`TreeReader`/`TreeWriter`/`Search`/`Memories`/`Sources`) from env config, lifting the Brave token and re-lifting on expiry. Pure helpers handle Claude-friendly JSON projections and the guardrails (anti-duplicate surfacing, upload provenance, living/deceased classification). Read tools and write tools are thin wrappers that call the library through `Context`; write tools additionally enforce the guardrails and the production-write gate. Transport is stdio.

**Tech Stack:** Python ≥3.11; the in-repo `familysearch` library; the official `mcp` Python SDK (FastMCP), pinned to a recent stable release; `pytest`; `responses` (for any HTTP-level tests of the new library method).

**Spec:** `docs/superpowers/specs/2026-10-23-familysearch-mcp-server-design.md` (and the library spec it builds on, `docs/superpowers/specs/2026-10-23-familysearch-sync-design.md`).

## Global Constraints

- Python **≥3.11**. New package `familysearch_mcp` under `src/`, alongside the existing `familysearch` package; it depends on `familysearch`.
- MCP via the **official `mcp` SDK (FastMCP)**, **stdio** transport. Pin an exact recent stable `mcp` version in `pyproject.toml`.
- **`FS_ENVIRONMENT`** selects `production` (default) or `beta`; base URL derives from it (via the library's `Environment`). The server **never silently mixes environments** — if `FS_ENVIRONMENT` disagrees with the environment Brave's token is actually valid for (per `detect_environment`), the server **fails with a clear error** naming where Brave is signed in.
- **Production writes are opt-in:** on `production`, write tools are **disabled** unless `FS_ALLOW_PROD_WRITES` is truthy; reads are always allowed. On `beta`, writes are always allowed. A disabled write returns an actionable refusal, performs no network call.
- **Token** is lifted via `familysearch.extract_brave_token()` and **re-lifted once on `TokenExpiredError`**; if re-lift still fails, the tool returns a clear re-auth message (which environment Brave is signed into).
- **Every write passes a required, non-blank `reason`** (the library enforces `X-Reason`).
- **Guardrails (write tools):** `create_person` runs `tree_search` first and returns candidates without creating unless `create_despite_matches=True`; `upload_memory` refuses aggregator-sourced files (filename patterns + caller provenance string: fold3/ancestry/newspapers.com/myheritage/findagrave) unless `override_rights=True`; living/deceased routing sets the `living` flag, and a *possibly-living* person (born within ~110 years, no death) is **not created** until the caller passes an explicit `living`/`confirm_deceased`. Per the spec's confirmation-authority rule, `create_despite_matches` and `confirm_deceased` represent **Ben's** explicit go-ahead — the tool descriptions state Claude must surface the evidence and get Ben's OK before setting them.
- **Returns are compact JSON** shaped for Claude (projections), never raw GEDCOM X blobs.
- **No `delete_person`** tool and no merge/destructive tool.
- Guardrail outcomes return a structured result (`{"status": "...", ...}`); genuine failures (expired token, HTTP ≥400) raise.

## Review Focus

- **Token expires mid-session** → a tool re-lifts once; if still bad, returns a clear re-auth message (which env Brave is in), never a raw traceback. (Task 2)
- **Production write attempted while disabled** → actionable refusal, zero network calls. (Task 6)
- **`create_person` when a plausible match exists** → returns the candidates and does NOT create (the anti-duplicate gate); creates only with `create_despite_matches`. (Task 6)
- **`upload_memory` with an aggregator-sourced file** → refused before any upload, unless `override_rights`. (Task 6)
- **Possibly-living person** → never silently created on the shared tree; requires explicit `living`/`confirm_deceased`, and a living person is created with `living=True` (private). (Task 4 + Task 6)

---

### Task 1: Library addition — `TreeReader.get_matches`

**Files:**
- Modify: `src/familysearch/tree.py` (add `get_matches` to `TreeReader`)
- Modify: `src/familysearch/__init__.py` (no new export needed; method on existing class)
- Test: `tests/unit/test_tree_read.py` (add cases)

**Interfaces:**
- Produces: `TreeReader.get_matches(person_id: str) -> list[dict]` — GET the person's `matches` link (`/platform/tree/persons/{id}/matches`); return the candidate entries (empty list when none/absent), defensively parsed like the other reads.

- [ ] **Step 1: Write failing test** in `tests/unit/test_tree_read.py`: `test_get_matches_returns_candidates` — with a fixture body served for the person's `matches` link, returns a list; and `test_get_matches_empty` — missing/empty → `[]`.
- [ ] **Step 2: Run → FAIL** (`.venv/bin/python -m pytest tests/unit/test_tree_read.py -v`).
- [ ] **Step 3: Implement** `get_matches` resolving the URL via `discovery.person_links(person_id)["matches"]`, same `_items`/defensive pattern as `get_sources`/`get_memories`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat: TreeReader.get_matches` + trailer lines).

---

### Task 2: MCP package scaffold + `Context` (config, auth, env gate, write gate)

**Files:**
- Create: `src/familysearch_mcp/__init__.py`, `src/familysearch_mcp/context.py`
- Modify: `pyproject.toml` (add the `familysearch_mcp` package, the `mcp` dep pinned, and a `familysearch-mcp` console entry point → `familysearch_mcp.server:main`)
- Test: `tests/unit/test_mcp_context.py`

**Interfaces:**
- Produces: `Context` with:
  - construction from env: `FS_ENVIRONMENT` (default `production`), `FS_ALLOW_PROD_WRITES` (default off).
  - `.services()` → lazily builds and caches `FSClient` + `Discovery` + `TreeReader`/`TreeWriter`/`Search`/`Memories`/`Sources` using `extract_brave_token()`; on first build, validates the token's environment via `detect_environment` and raises `ContextError` with a clear message if it disagrees with `FS_ENVIRONMENT`.
  - `.call(fn)` helper that runs `fn(services)` and, on `TokenExpiredError`, re-lifts the token once (rebuild services) and retries; if it still fails, raises a re-auth error carrying the environment Brave is signed into.
  - `.writes_allowed() -> bool` (True on beta; on production only if `FS_ALLOW_PROD_WRITES`), and `.require_writes()` raising/returning the standard refusal when not allowed.
- Produces: `ContextError(FSError-like)`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_mcp_context.py` (monkeypatch `familysearch_mcp.context.extract_brave_token` and `detect_environment`; never touch a real browser/network):
  - `test_default_environment_is_production`.
  - `test_env_mismatch_raises_clear_error`: `FS_ENVIRONMENT=beta` but token validates as production → `ContextError` whose message names "production".
  - `test_writes_allowed_beta_true`; `test_writes_blocked_on_prod_without_flag`; `test_writes_allowed_on_prod_with_flag`.
  - `test_call_relifts_once_on_token_expired`: a fake service that raises `TokenExpiredError` once then succeeds → `.call` retries after re-lift and returns the result; assert the token was re-lifted exactly once.
  - `test_call_reauth_message_when_relift_fails`: persistent `TokenExpiredError` → raises an error mentioning re-login and the environment.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `context.py` + pyproject changes.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit.**

---

### Task 3: Projections — compact JSON for Claude

**Files:**
- Create: `src/familysearch_mcp/projections.py`
- Test: `tests/unit/test_mcp_projections.py`
- Fixtures: reuse `tests/unit/fixtures/person.json` / `person_named.json` and add a trimmed search-entry fixture if needed.

**Interfaces:**
- Produces pure functions (GEDCOM X dict → compact dict):
  - `project_person(person: dict) -> dict` — `{id, name, lifespan, gender, living, facts: [{type, date, place}], links_summary}`.
  - `project_search_hit(entry: dict) -> dict` — `{id, confidence, name, birth, death, ...}` from a tree-search/atom entry.
  - `project_record_hit(entry: dict) -> dict` — from a personas entry.
  - `project_source(desc: dict) -> dict` — `{id, title, citation, about}`.
  - `project_memory(mem: dict) -> dict`.
  - All tolerate missing keys (never raise).

- [ ] **Step 1: Write failing tests** asserting each projection pulls the right fields from a fixture and returns `{}`/defaults on sparse input (name from `display.name`, confidence as int, fact type/date/place, source title from `titles[0].value`).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit.**

---

### Task 4: Guards — provenance + living/deceased classification

**Files:**
- Create: `src/familysearch_mcp/guards.py`
- Test: `tests/unit/test_mcp_guards.py`

**Interfaces:**
- Produces:
  - `AGGREGATOR_PATTERNS` (fold3, ancestry, newspapers.com, myheritage, findagrave).
  - `provenance_blocked(filename: str, provenance: str | None) -> str | None` — returns the matched aggregator name if the filename or the caller-supplied provenance string indicates one, else `None`. Case-insensitive substring match.
  - `classify_life_status(birth_date: str | None, death_date: str | None, *, today_year: int) -> str` — returns `"deceased"` (has death), `"living"` (born within ~110y, no death — caller-asserted living), or `"possibly_living"` (born within ~110y, no death, unknown) / `"deceased_by_age"` (born >110y ago, no death → treat as deceased). Parses a leading 4-digit year from the date string; unknown birth → `"possibly_living"`.

- [ ] **Step 1: Write failing tests**:
  - `test_provenance_blocks_fold3_filename` ("1940 Fold3 Muster Roll.jpg" → "fold3"); `test_provenance_blocks_from_citation` (clean filename, provenance="Ancestry.com" → "ancestry"); `test_provenance_clear_returns_none` (a census PDF, provenance="NARA" → None).
  - `test_classify_deceased_with_death`; `test_classify_old_birth_no_death_is_deceased_by_age` (b.1850); `test_classify_recent_birth_no_death_possibly_living` (b.1990); `test_classify_unknown_birth_possibly_living`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit.**

---

### Task 5: Read tools

**Files:**
- Create: `src/familysearch_mcp/tools_read.py`
- Test: `tests/unit/test_mcp_tools_read.py`

**Interfaces:**
- Consumes: `Context`, projections, the library services.
- Produces a `register_read_tools(mcp, ctx)` that registers FastMCP tools, each returning projected JSON:
  - `auth_status()` → `{environment, user, writes_allowed}` (via current-user read).
  - `search_tree(given=None, surname=None, birth_date=None, birth_place=None, count=20)` → list of `project_search_hit`.
  - `search_records(given=None, surname=None, count=20, offset=0)` → `{total, next, entries:[project_record_hit]}`.
  - `get_person(person_id)` → `project_person`.
  - `get_relatives(person_id)` → `{parents, children, spouses}` of `project_person`.
  - `get_person_sources(person_id)` → `[project_source]`.
  - `get_person_memories(person_id)` → `[project_memory]`.
  - `get_matches(person_id)` → `[project_search_hit]` (via Task 1's library method).
- Tools call `ctx.call(lambda s: ...)`; errors surface as the standardized error payload/raise.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_mcp_tools_read.py` using a **fake services** object (no network): register the tools against a fake `Context` whose `.call` runs against fakes returning fixture-shaped dicts; assert each tool returns the projected shape and passes the right args to the library (e.g. `search_tree` forwards `given/surname`; `get_person` returns the projected person; `auth_status` reports environment + writes_allowed). Use the FastMCP tool objects' underlying callables, or factor each tool body into a plain function the test calls directly.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit.**

---

### Task 6: Write tools (guarded)

**Files:**
- Create: `src/familysearch_mcp/tools_write.py`
- Test: `tests/unit/test_mcp_tools_write.py`

**Interfaces:**
- Consumes: `Context`, guards, projections, builders (`build_person`/`build_name`/`build_fact`), the library writers.
- Produces `register_write_tools(mcp, ctx)`. Every write first calls `ctx.require_writes()` (refusal payload + no network if disabled). Tools:
  - `create_person(given, surname, gender, *, birth_date=None, birth_place=None, death_date=None, living=None, reason, create_despite_matches=False, confirm_deceased=False)`:
    1. `require_writes`.
    2. **Match gate:** run `search_tree` on the name+birth; if candidates exist and not `create_despite_matches` → return `{"status":"matches_found","candidates":[...]}` (no create).
    3. **Life status:** `classify_life_status`; if `possibly_living` and neither `living` given nor `confirm_deceased` → return `{"status":"needs_life_confirmation", ...}` (no create). Map to the `living` flag (`living`/`possibly_living`→ living unless `confirm_deceased`; `deceased`/`deceased_by_age`→ not living).
    4. Build the person (`build_person` with facts) and `TreeWriter.create_person(..., reason=...)`; return `{"status":"created","id":...}`.
  - `add_fact(person_id, fact_type, *, date=None, place=None, reason)` → build fact, `add_fact`.
  - `create_couple(person1_id, person2_id, *, reason)`; `create_parent_child(child_id, parent_ids, *, reason)`.
  - `upload_memory(file_path, *, title=None, provenance=None, reason, override_rights=False)`:
    1. `require_writes`; read the file bytes + infer content_type.
    2. **Provenance gate:** `provenance_blocked(basename, provenance)`; if blocked and not `override_rights` → `{"status":"provenance_blocked","aggregator":...}` (no upload).
    3. `Memories.upload(...)` (the library also enforces 15MB/type); return `{"status":"uploaded","memory_id":...}`.
  - `link_memory(person_id, memory_id, *, reason)`; `create_source(about_url, title, citation, *, reason)`; `attach_source(person_id, source_description_url, *, reason)`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_mcp_tools_write.py` with fake services (assert NO library write call happens on a blocked path):
  - `test_write_blocked_when_prod_writes_disabled` (prod, no flag → refusal, zero writer calls).
  - `test_create_person_returns_matches_and_does_not_create` (fake `search_tree` returns a hit → `matches_found`, writer.create_person NOT called).
  - `test_create_person_creates_with_override` (`create_despite_matches=True` → writer.create_person called, returns id).
  - `test_create_person_possibly_living_needs_confirmation` (b.1990, no death, no flags → `needs_life_confirmation`, not created).
  - `test_create_person_living_flag_private` (living person → `build_person(living=True)`).
  - `test_create_person_deceased_by_age_creates_nonliving` (b.1850 → living False).
  - `test_upload_memory_blocks_aggregator` ("...Fold3....jpg" → `provenance_blocked`, upload NOT called) and `test_upload_memory_override` (`override_rights=True` → uploads).
  - `test_each_write_requires_reason` (missing/blank reason refused).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit.**

---

### Task 7: Server assembly, entry point, config + README

**Files:**
- Create: `src/familysearch_mcp/server.py`
- Modify: `README.md`
- Test: `tests/unit/test_mcp_server.py`

**Interfaces:**
- Produces: `build_server() -> FastMCP` (constructs `Context`, a `FastMCP("familysearch")`, calls `register_read_tools`/`register_write_tools`, returns it) and `main()` (`build_server().run()` over stdio).
- README: a **Claude Code** MCP config snippet (command = the `familysearch-mcp` entry point / `python -m familysearch_mcp.server`, env `FS_ENVIRONMENT`, and the `FS_ALLOW_PROD_WRITES` opt-in explained), how auth/Keychain works, and a pointer to `docs/api-discovery.md`.

- [ ] **Step 1: Write failing test** `test_mcp_server.py`: `build_server()` returns a server exposing exactly the intended tool names (read + write, and **no `delete`**); registration doesn't require network/token (lazy `Context`).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit.**

---

### Task 8: Beta integration (acceptance) + prod read-only smoke

**Files:**
- Create: `tests/integration/test_mcp_beta.py`
- Modify: `tests/integration/conftest.py` if a shared fixture helps.

**Interfaces:**
- Consumes: the assembled server tools + a beta `Context`.
- Gated by a token like the library suite (`FS_USE_BRAVE=1`/`FS_BETA_TOKEN`); **skips** without one.

- [ ] **Step 1: Write the integration test** driving the tool functions (not raw library) against **beta**, mirroring the library's acceptance run but through the MCP layer, with teardown that deletes every created person/relationship/memory even on failure:
  - `auth_status` reports beta + writes_allowed.
  - `search_tree` returns entries.
  - `create_person` for a `ZZSPIKE` person (no match → created; assert the match gate lets a clearly-unique name through), `add_fact`, `create_couple`, `create_parent_child`, `upload_memory` (a generated PNG with clean provenance), `link_memory`, `create_source`, `attach_source` — each returns its `status:"...ok"` shape.
  - A provenance-blocked upload (filename contains "Fold3") returns `provenance_blocked` and uploads nothing.
  - Cleanup asserts no leftovers.
- [ ] **Step 2: Run against beta** (`FS_USE_BRAVE=1 .venv/bin/python -m pytest tests/integration/test_mcp_beta.py -v`) — PASS, beta left clean. **(Controller runs this live, per the library plan's precedent — interactive Keychain.)**
- [ ] **Step 3: Unit suite with no token** → all unit pass, integration skips.
- [ ] **Step 4: Commit.**
- [ ] **Step 5:** README note on running both suites.
- [ ] **Step 6 (prod read smoke, optional/manual):** the read-only production path (bearer+UA, no bot cookies) was already verified manually during design (live `search_tree` + `get_person` + sources against the real tree). Since prod reads share all code with beta reads except the base URL, no separate automated prod test is required; a one-command manual re-run (`FS_ENVIRONMENT=production` + a prod login, `auth_status`/`get_person`) is documented in the README for when needed.
