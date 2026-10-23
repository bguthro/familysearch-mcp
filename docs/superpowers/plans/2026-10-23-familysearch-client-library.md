# FamilySearch Client Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a thin, self-describing Python client for FamilySearch that authenticates with a browser-lifted session token and performs the reads, searches, and writes the spike proved — ending with a beta integration suite that is the acceptance gate for the write layer.

**Architecture:** A `requests.Session`-based client sets the required headers (Bearer token, `application/x-fs-v1+json`, browser User-Agent) and routes every call through one wrapper that handles 401/expiry and write throttling. Endpoint URLs are never hard-coded: a discovery module resolves them from the tree collection's HATEOAS link rels and from each person resource's own links. Read, search, write, memories, and sources are thin modules over that client; pure GEDCOM X "builder" helpers construct request bodies so writes are testable without the network.

**Tech Stack:** Python ≥3.11, `requests`, `browser_cookie3` (cookie extraction), `pytest`, `responses` (HTTP mocking for unit tests). The beta integration suite hits the live beta API, gated by an env var.

**Spec:** `docs/superpowers/specs/2026-10-23-familysearch-sync-design.md`

## Global Constraints

- Python **≥3.11**. (Do **not** use `rookiepy` — it fails to build on Python 3.14 here; use `browser_cookie3` or read Brave's cookie SQLite directly.)
- **No hard-coded internal endpoint paths** beyond the two discovery roots (`/platform/collections/tree`) and the record-search service base (`/service/search/hr/v2/personas`). Everything else is resolved from HATEOAS link rels.
- Every request sets a **browser `User-Agent`**; the FamilySearch edge rejects requests without one.
- Reads use `Accept: application/x-fs-v1+json`; tree search uses `Accept: application/x-gedcomx-atom+json`.
- Every **write** carries a change message via the `X-Reason` header.
- Writes are **throttled** (minimum interval between write calls, default 1.0 s, configurable).
- Token is **short-lived and environment-scoped**: a beta token authenticates beta only, a prod token prod only. The environment is an **explicit** client parameter; base URL derives from it.
- Responses are **untrusted data**: defensive parsing, no `eval`, never assume a key exists.
- Memory uploads: reject files **>15 MB** or non-image/PDF before calling the network.
- Base hosts: `https://beta.familysearch.org` (environment `beta`), `https://www.familysearch.org` (environment `production`).

## Review Focus

- **Expired/invalid token** → the client raises a clear `TokenExpiredError` on HTTP 401, not a cryptic `KeyError`/`JSONDecodeError`. (Task 3)
- **Missing browser User-Agent** → the client always sets one; a request built without it is a bug. (Task 3)
- **Absent/renamed HATEOAS rel** → discovery raises `LinkNotFoundError(rel)` naming the missing rel, never a bare `KeyError`. (Task 4)
- **Oversized or wrong-type memory file** → `upload_memory` raises `ValueError` before any network call. (Task 8)
- **Environment/token mismatch** (beta token used against production) → surfaces as `TokenExpiredError` from the 401 path, and the client never silently mixes environments because the base URL is environment-derived. (Task 3)

---

### Task 1: Package scaffold + GEDCOM X builders + errors

**Files:**
- Create: `pyproject.toml`, `src/familysearch/__init__.py`
- Create: `src/familysearch/errors.py`
- Create: `src/familysearch/models.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Produces: exceptions `FSError`, `TokenExpiredError`, `LinkNotFoundError`, `FSHTTPError` (all subclass `FSError`; `FSHTTPError` carries `.status` and `.body`).
- Produces: builders returning plain dicts (GEDCOM X fragments):
  - `build_name(given: str, surname: str, *, name_type: str = "http://gedcomx.org/BirthName", preferred: bool = True) -> dict`
  - `build_fact(fact_type: str, *, date: str | None = None, place: str | None = None) -> dict`
  - `build_person(*, given: str, surname: str, gender: str, living: bool, facts: list[dict] | None = None) -> dict` — `gender` is a short token `"Male"|"Female"|"Unknown"`, mapped to the `http://gedcomx.org/<Gender>` URI internally; returns the single-person dict (no `{"persons": [...]}` envelope).
  - `GENDER_URI`, `FACT_BIRTH`, `FACT_DEATH`, `FACT_MARRIAGE` constants.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_models.py`:
  - `test_build_name_has_parts_and_fulltext`: asserts `build_name("August","Anderson")` yields `nameForms[0].fullText == "August Anderson"` and parts contain `{"type": ".../Given", "value": "August"}` and a Surname part, `preferred is True`.
  - `test_build_fact_birth_with_date_place`: `build_fact(FACT_BIRTH, date="1 January 1850", place="Nowhere")` → `type == FACT_BIRTH`, `date.original == "1 January 1850"`, `place.original == "Nowhere"`.
  - `test_build_fact_omits_absent_fields`: no `date`/`place` keys when not given.
  - `test_build_person_maps_gender_and_living`: `build_person(given="A",surname="B",gender="Male",living=True)` → `gender.type == "http://gedcomx.org/Male"`, `living is True`, exactly one preferred name.
  - `test_build_person_rejects_bad_gender`: `gender="X"` raises `ValueError`.

- [ ] **Step 2: Run tests, verify they fail** — `pytest tests/unit/test_models.py -v` → FAIL (module/functions missing).

- [ ] **Step 3: Implement** `errors.py` (the four exceptions) and `models.py` (the builders + constants) with the signatures above. Configure `pyproject.toml` for a `src/`-layout package named `familysearch`, deps `requests`, `browser_cookie3`; dev deps `pytest`, `responses`.

- [ ] **Step 4: Run tests, verify pass** — `pytest tests/unit/test_models.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add pyproject.toml src/familysearch tests/unit/test_models.py && git commit -m "feat: package scaffold, GEDCOM X builders, errors"`

---

### Task 2: Token extraction from Brave + environment

**Files:**
- Create: `src/familysearch/auth.py`
- Test: `tests/unit/test_auth.py`

**Interfaces:**
- Produces: `Environment` enum/const with `BETA` and `PRODUCTION`, each exposing `.base_url` and `.other` (the opposite environment).
- Produces: `extract_brave_token() -> str` — reads the `fssessionid` cookie for `familysearch.org` via `browser_cookie3.brave(...)`; raises `FSError` with a readable message if absent. (Empirical reality: Brave holds a **single** `.familysearch.org` `fssessionid`, owned by whichever environment was signed into last — so there is no cookie-level environment selection; selection is validated at the HTTP layer, below.)
- Produces: `detect_environment(token: str, http_get) -> Environment` — given a callable `http_get(base_url) -> status` that calls `/platform/users/current`, returns the `Environment` whose host returns 200; raises `FSError` naming which environment Brave is actually signed into when the requested one 401s. (This is what prevents the "re-extract → same wrong-env cookie → infinite loop" failure. The client in Task 3 uses it to turn a wrong-environment 401 into an actionable message rather than a bare `TokenExpiredError`.)

- [ ] **Step 1: Write failing tests** in `tests/unit/test_auth.py` (monkeypatch `browser_cookie3.brave` to return a fake cookie jar):
  - `test_extract_returns_fssessionid_value`: jar has one `fssessionid` cookie → returns its value.
  - `test_extract_raises_when_absent`: jar without `fssessionid` → raises `FSError` whose message mentions logging in.
  - `test_environment_base_urls`: `Environment.BETA.base_url == "https://beta.familysearch.org"`, `PRODUCTION` → `https://www.familysearch.org`, and `BETA.other is PRODUCTION`.
  - `test_detect_environment_picks_200_host`: a fake `http_get` returning 200 for the prod base and 401 for beta → `detect_environment` returns `PRODUCTION`.
  - `test_detect_environment_raises_when_none`: both 401 → raises `FSError`.

- [ ] **Step 2: Run tests, verify fail** — `pytest tests/unit/test_auth.py -v` → FAIL.

- [ ] **Step 3: Implement** `auth.py`. Fake-jar objects need `.name`, `.domain`, `.value`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `git commit -am "feat: Brave token extraction and environment"`

---

### Task 3: Client core — headers, request wrapper, 401 handling, throttle

**Files:**
- Create: `src/familysearch/client.py`
- Test: `tests/unit/test_client.py`

**Interfaces:**
- Consumes: `Environment`, errors from Tasks 1–2.
- Produces: `FSClient(token: str, environment: Environment, *, user_agent: str = <chrome UA>, min_write_interval: float = 1.0)`.
  - `.get(path_or_url, *, accept=None, params=None) -> requests.Response`
  - `.post(path_or_url, *, json=None, files=None, params=None, reason: str | None = None) -> requests.Response`
  - `.delete(path_or_url, *, reason: str | None = None) -> requests.Response`
  - `.base_url` property.
  - Path handling: an argument starting with `http` is used as-is; otherwise it is joined to `base_url`.
  - Every request sets `Authorization: Bearer <token>`, the browser `User-Agent`, and default `Accept: application/x-fs-v1+json` (overridable per call).
  - `post`/`delete` set `X-Reason` from `reason`; writes block until `min_write_interval` has elapsed since the previous write.
  - Any response with status **401** raises `TokenExpiredError`; other `>=400` raise `FSHTTPError(status, body)`. 2xx responses are returned.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_client.py` using `responses`:
  - `test_get_sets_auth_and_ua_headers`: register a GET; assert the sent request had `Authorization: Bearer tkn` and a non-empty `User-Agent`.
  - `test_relative_path_joined_to_base`: GET `"/platform/users/current"` on a `BETA` client hits `https://beta.familysearch.org/platform/users/current`.
  - `test_absolute_url_used_asis`: GET a full `https://...` URL is not re-joined.
  - `test_post_sets_x_reason`: `post(..., reason="r")` sends header `X-Reason: r`.
  - `test_401_raises_token_expired`: a 401 response → raises `TokenExpiredError`.
  - `test_4xx_raises_fshttperror_with_body`: a 404 with JSON body → `FSHTTPError` with `.status == 404` and `.body` containing the payload.
  - `test_write_throttle_enforced`: with `min_write_interval=0.2`, two back-to-back `post`s take ≥0.2 s (monkeypatch `time.monotonic`/`time.sleep` and assert `sleep` called with the remaining interval).

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement** `FSClient`. Use one private `_request` that applies headers, throttling (for writes), executes, and maps status to the exceptions above.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `git commit -am "feat: FSClient core with auth headers, 401 handling, write throttle"`

---

### Task 4: HATEOAS discovery

**Files:**
- Create: `src/familysearch/discovery.py`
- Test: `tests/unit/test_discovery.py`
- Create fixture: `tests/unit/fixtures/collections_tree.json` (trimmed real response: the `links` map with `persons`, `relationships`, `artifacts`, `person-search`, `source-descriptions`, `source-references-query`, `matches`).

**Interfaces:**
- Consumes: `FSClient`.
- Produces: `Discovery(client: FSClient)` with:
  - `.links() -> dict[str, str]` — GETs `/platform/collections/tree`, returns rel → href/template (cached).
  - `.href(rel: str) -> str` — returns the href for `rel`; raises `LinkNotFoundError(rel)` if absent.
  - `.person_links(person_id: str) -> dict[str, str]` — reads a person and returns its own `links` map (rels like `artifacts`, `source-descriptions`, `parents`, `children`, `spouses`, `matches`, `merge`).

- [ ] **Step 1: Write failing tests** in `tests/unit/test_discovery.py` (`responses` serving the fixture):
  - `test_links_parsed_from_collection`: `href("relationships")` ends with `/platform/tree/relationships`.
  - `test_links_cached`: two `href()` calls → only one HTTP GET (assert `responses` call count == 1).
  - `test_missing_rel_raises_named`: `href("nope")` raises `LinkNotFoundError` whose message contains `"nope"`.
  - `test_person_links_returns_person_rels`: with a person fixture, `person_links("BMXM-2HZ")["artifacts"]` ends with `/persons/BMXM-2HZ/memories`.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement** `discovery.py`. Links may appear under the top-level `links` or under `collections[0].links`; handle both. Each link value may carry `href` or `template` — return whichever is present.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit** — `git commit -am "feat: HATEOAS discovery of tree and person link rels"`

---

### Task 5: Tree reads

**Files:**
- Create: `src/familysearch/tree.py` (read side)
- Test: `tests/unit/test_tree_read.py`
- Create fixture: `tests/unit/fixtures/person.json` (trimmed real person with `display`, `facts`, `links`).

**Interfaces:**
- Consumes: `FSClient`, `Discovery`.
- Produces: `TreeReader(client, discovery)` with:
  - `.get_person(person_id: str) -> dict` — returns the first element of `persons[]` (raises `FSError` if the list is empty/missing).
  - `.get_relatives(person_id: str) -> dict` — GET the person's `parents`, `children`, `spouses` link targets; returns `{"parents": [...], "children": [...], "spouses": [...]}` of person dicts (missing/empty sections → empty lists).
  - `.get_memories(person_id: str) -> list[dict]` — GET the person's `artifacts` link; returns the memory-description list (empty when none).
  - `.get_sources(person_id: str) -> list[dict]` — GET the person's `source-descriptions` link; returns the source list (empty when none).
  - `.get_changes(person_id: str) -> list[dict]` — GET the person's `change-history` link; returns the change entries (empty when none).

- [ ] **Step 1: Write failing tests** in `tests/unit/test_tree_read.py`:
  - `test_get_person_returns_single_person`: `get_person("BMXM-2HZ")["display"]["name"] == "Ben Guthro"`.
  - `test_get_person_empty_list_raises`: a `{"persons": []}` response → `FSError`.
  - `test_get_relatives_shapes_sections`: given fixtures for the three link targets, returns all three keys as lists.
  - `test_get_memories_sources_changes_return_lists`: each resolves the correct person link and returns a list (empty when the fixture has no items).

- [ ] **Step 2–4:** fail → implement → pass (`pytest tests/unit/test_tree_read.py -v`).

- [ ] **Step 5: Commit** — `git commit -am "feat: tree person and relatives reads"`

---

### Task 6: Search (tree + records)

**Files:**
- Create: `src/familysearch/search.py`
- Test: `tests/unit/test_search.py`
- Create fixtures: `tests/unit/fixtures/tree_search.json` (atom `entries` with `confidence`), `tests/unit/fixtures/record_personas.json` (the `{"results": N, "entries": [...], "links": {"next": {...}}}` shape).

**Interfaces:**
- Consumes: `FSClient`, `Discovery`.
- Produces: `Search(client, discovery)` with:
  - `.tree_search(*, given: str | None, surname: str | None, birth_date: str | None = None, birth_place: str | None = None, count: int = 20) -> list[dict]` — GET the `person-search` rel href with `q.givenName`/`q.surname`/`q.birthLikeDate`/`q.birthLikePlace` params and `Accept: application/x-gedcomx-atom+json`; returns the `entries` list (each carrying `confidence` + person content). Empty on no results.
  - `.record_search(*, given=None, surname=None, count: int = 20, offset: int = 0) -> dict` — GET `/service/search/hr/v2/personas` with `q.*` params; returns `{"total": int, "entries": list, "next": str | None}` parsed from `results`/`entries`/`links.next.href`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_search.py`:
  - `test_tree_search_returns_entries_with_confidence`: fixture → non-empty list, first entry has integer `confidence`.
  - `test_tree_search_sends_q_params`: assert the outgoing query had `q.surname=Anderson` and `q.givenName=August`.
  - `test_record_search_parses_total_and_next`: fixture with `"results": 12676` and a `links.next.href` → `total == 12676`, `next` is that href, `entries` non-empty.
  - `test_record_search_no_next_when_absent`: fixture without `links.next` → `next is None`.

- [ ] **Step 2–4:** fail → implement → pass.

- [ ] **Step 5: Commit** — `git commit -am "feat: tree search and historical-record (persona) search"`

---

### Task 7: Tree writes — persons, facts, relationships, delete

**Files:**
- Modify: `src/familysearch/tree.py` (add `TreeWriter`)
- Test: `tests/unit/test_tree_write.py`

**Interfaces:**
- Consumes: `FSClient`, `Discovery`, builders from Task 1.
- Produces: `TreeWriter(client, discovery)` with (each takes a `reason: str`):
  - `.create_person(person: dict, *, reason: str) -> str` — POST `{"persons": [person]}` to the `persons` rel; returns the new id from the `X-ENTITY-ID` header (fallback: last path segment of `Location`). **Proven gotcha:** the person's name MUST include `parts` (the Task 1 `build_name` does this); a name with only `fullText` is silently refused (no id returned).
  - `.add_fact(person_id: str, fact: dict, *, reason: str) -> None` — POST `{"persons": [{"facts": [fact]}]}` to the **person resource URL** (`<persons rel>/{person_id}`); this person-merge POST is proven (200/204 = success). Do NOT post to a `/conclusions` or `/sources` sub-path.
  - `.create_couple(person1_id: str, person2_id: str, *, reason: str) -> str` — POST a `Couple` relationship (`type` = `http://gedcomx.org/Couple`, `person1`/`person2` as `{resource, resourceId}` built from `base_url`) to the `relationships` rel; returns new relationship id. **(Proven: 201.)**
  - `.create_parent_child(*, child_id: str, parent_ids: list[str], reason: str) -> list[str]` — for each parent, POST a generic `ParentChild` relationship (`type` = `http://gedcomx.org/ParentChild`, `person1` = parent, `person2` = child) to the **`relationships` rel**; returns the new relationship ids. **(Proven: 201 via the generic endpoint; the hyphenated `/child-and-parents-relationships` path 404s — do not use it.)**
  - `.delete_person(person_id: str, *, reason: str) -> None` — DELETE the person URL; treats 204 as success.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_tree_write.py` (`responses`, asserting method, URL, body, and `X-Reason`):
  - `test_create_person_posts_envelope_and_returns_id`: a 201 with `X-ENTITY-ID: BMXM-GRC` → returns `"BMXM-GRC"`; assert body is `{"persons":[<person>]}` and header `X-Reason` set.
  - `test_create_person_id_from_location_fallback`: 201 with only `Location` → id parsed from the URL.
  - `test_add_fact_posts_fact_envelope`: assert outgoing body contains the fact under `persons[0].facts[0]`.
  - `test_create_couple_body_shape`: assert `relationships[0].type == ".../Couple"` and both `person1`/`person2` carry `resourceId`.
  - `test_create_parent_child_posts_parentchild_per_parent`: `parent_ids=[dad,mom]` → two POSTs to the `relationships` rel, each `type == ".../ParentChild"`, `person1` = a parent, `person2` = child; returns two ids.
  - `test_add_fact_posts_to_person_resource`: assert the POST targets the person resource URL (`.../persons/{id}`), not a `/conclusions` or `/sources` sub-path.
  - `test_delete_person_204_ok`: a 204 → returns `None` (no raise).

- [ ] **Step 2–4:** fail → implement → pass.

- [ ] **Step 5: Commit** — `git commit -am "feat: tree writes (create person/fact/relationship, delete)"`

---

### Task 8: Memories (documents) + source attachment

**Files:**
- Create: `src/familysearch/memories.py`
- Create: `src/familysearch/sources.py`
- Test: `tests/unit/test_memories.py`, `tests/unit/test_sources.py`

**Interfaces:**
- Consumes: `FSClient`, `Discovery`.
- Produces: `Memories(client, discovery)`:
  - `MAX_BYTES = 15 * 1024 * 1024`; `ALLOWED_TYPES = {"image/png","image/jpeg","image/tiff","application/pdf"}`.
  - `.upload(data: bytes, *, filename: str, content_type: str, title: str | None = None, reason: str) -> str` — raises `ValueError` if `len(data) > MAX_BYTES` or `content_type not in ALLOWED_TYPES`, **before** any network call; otherwise POSTs multipart (`files={"artifact": (filename, data, content_type)}`) to the `artifacts` rel; returns memory id from `X-ENTITY-ID`/`Location`. **(Proven: 201.)**
  - `.link_to_person(person_id: str, memory_id: str, *, reason: str) -> None` — POST `{"persons": [{"id": person_id}]}` to the **memory's personas collection** (`<artifacts rel>/{memory_id}/personas`, i.e. `/platform/memories/memories/{memory_id}/personas`); 2xx → success. **(Proven: 204. Note: the person's own `/persons/{id}/memories` link is an *upload* endpoint and returns 400 for a link attempt — do not use it to link.)**
  - `.delete(memory_id: str) -> None`.
- Produces: `Sources(client, discovery)`:
  - `.create_description(about_url: str, title: str, citation: str, *, reason: str) -> str` — POST `{"sourceDescriptions": [{"about": about_url, "titles": [{"value": title}], "citations": [{"value": citation}]}]}` to the `source-descriptions` rel (`/platform/sources/descriptions`); returns id. **(Proven: 201.)**
  - `.attach_to_person(person_id: str, source_description_url: str, *, reason: str) -> None` — POST `{"persons": [{"sources": [{"description": source_description_url}]}]}` to the **person resource URL** (`<persons rel>/{person_id}`); 2xx → success. **(Proven: 200. Note: the person's `/sources` sub-path is GET-only → 405, and `/source-references` → 404 — do not post to either.)**

- [ ] **Step 1: Write failing tests**:
  - `test_upload_rejects_oversize`: 15 MB + 1 byte → `ValueError`, and assert **no** HTTP call was made (`responses` call count 0).
  - `test_upload_rejects_bad_type`: `content_type="text/plain"` → `ValueError`, no HTTP call.
  - `test_upload_posts_multipart_and_returns_id`: valid small PNG → multipart sent, id returned from `X-ENTITY-ID`.
  - `test_link_to_person_posts_persona`: asserts the POST targets `/memories/{memory_id}/personas` with body `{"persons":[{"id": person_id}]}`.
  - `test_source_create_posts_descriptions`: `create_description` posts the `sourceDescriptions` envelope and returns the id.
  - `test_source_attach_posts_to_person_resource`: `attach_to_person` posts `{"persons":[{"sources":[{"description": <url>}]}]}` to the person resource URL (not `/sources`).

- [ ] **Step 2–4:** fail → implement → pass.

- [ ] **Step 5: Commit** — `git commit -am "feat: memory upload/link with size+type guard, source attachment"`

---

### Task 9: Beta integration suite — the acceptance gate

**Files:**
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_beta_roundtrip.py`
- Modify: `src/familysearch/__init__.py` (export the public surface: `FSClient`, `Environment`, `Discovery`, `TreeReader`, `TreeWriter`, `Search`, `Memories`, `Sources`, builders, errors)

**Interfaces:**
- Consumes: the whole library.
- `conftest.py`: a `beta_client` fixture that reads the token from env `FS_BETA_TOKEN` (or calls `extract_brave_token(prefer_beta=True)` when `FS_USE_BRAVE=1`), and **skips the whole module** (`pytest.skip`) when no token is available — so unit CI stays green without credentials.

- [ ] **Step 1: Write the integration test** `test_beta_roundtrip.py` mirroring the spike, with teardown that deletes everything it created even on failure (track ids; delete in a `finally`/fixture-finalizer):
  - `test_read_current_user`: `GET /platform/users/current` → has a `users[0].id`.
  - `test_create_addfact_delete_person`: create a `ZZSPIKE`-named `living=False` person (name only) → `TreeWriter.add_fact` a birth fact to that existing person → read back shows the fact and its `changeMessage` → delete → re-read is 410/404. (This exercises `add_fact` against an existing person — the one write path the original spike did not cover.)
  - `test_create_couple_relationship`: create two persons, `create_couple` → returns an id → cleanup.
  - `test_create_parent_child_relationship`: create child + two parents, `create_parent_child` → returns two ids → cleanup.
  - `test_upload_link_delete_memory`: upload a generated 1×1 PNG → id returned → `link_to_person` to a created person → 204 → delete memory (204) and person → cleanup.
  - `test_source_create_and_attach`: create a person → `create_description` (id returned) → `attach_to_person` (200/2xx) → cleanup.
  - `test_tree_search_runs` and `test_record_search_runs`: both return without error (record search `total >= 0`).

- [ ] **Step 2: Run against beta** — `FS_USE_BRAVE=1 pytest tests/integration/test_beta_roundtrip.py -v`. Expected: PASS (allow the Keychain prompt). Confirm beta is clean afterward (no `ZZSPIKE` persons, no test memory).

- [ ] **Step 3: Run the unit suite with no token** — `pytest tests/unit -v` and `pytest tests/integration -v` (the latter **skips**). Expected: unit PASS, integration SKIPPED.

- [ ] **Step 4: Commit** — `git commit -am "test: beta integration round-trip (write-layer acceptance gate)"`

- [ ] **Step 5: Document** running the two suites in a short `README.md` section (unit = offline/CI; integration = beta, needs token). Commit.
