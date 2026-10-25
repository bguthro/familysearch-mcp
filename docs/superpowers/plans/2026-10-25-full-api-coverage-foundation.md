# Full API Coverage — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a method for every one of the 205 documented FamilySearch operations, put them behind a verification registry and a lock (unverified writes refuse to run), and build the live-verification runner, proven end to end on a four-operation pilot.

**Architecture:** A harvester snapshots the official OpenAPI definitions; a small overrides table corrects known spec defects; a deterministic generator emits checked-in Python (`src/familysearch/api/`). Generated methods call a hand-written runtime on top of the existing `FSClient`; writes pass through a guard that consults `registry.json` (status per operation, fingerprinted so spec changes make `verified` go stale). A recipe runner verifies operations live on beta by read-back and is the only thing that writes `verified` into the registry.

**Tech Stack:** Python ≥3.11, `requests`, `pytest`, `responses`; no new runtime dependencies. Dev tooling lives in a top-level `tools/` package.

**Spec:** `docs/superpowers/specs/2026-10-25-full-api-coverage-design.md` (recon record: `docs/superpowers/recon/2026-10-25-api-surface/`)

**Scope of this plan.** Foundation + framework + pilot (Tasks 1–12). The remaining verification work is **separate follow-on plans, written one tier at a time after this one lands** (their recipes depend on request bodies and server behavior only learnable live): P2 read verification (~105 reads) · P3 Family Tree persons/relationships/notes · P4 sources & source box · P5 memories · P6 matches, change history, restore · P7 Genealogies/User Trees/groups · P8 discussions & account-state operations · P9 destructive operations and final unverifiable classification. The project is "done" only when `python -m tools.spec.coverage --require-done` passes (Task 9) after P9.

## Global Constraints

- Python **≥3.11**; runtime dependencies unchanged (`requests`, `browser_cookie3`, `mcp==1.30.0`); dev tooling imports nothing outside the stdlib, `requests`, and this repo.
- Reuse `FSClient`: browser `User-Agent`, `Authorization`, same-origin check, write throttle, **required non-empty change message `reason` on every write**, JSON writes as `application/x-fs-v1+json`.
- Environment is explicit (`Environment.BETA`/`PRODUCTION`); base URLs come from it, **not** from the spec's `apibeta`/`identbeta` hosts.
- Lock (generated layer only): unverified/stale writes refuse; override = explicit parameter **and** beta; **production**: unverified never runs, a verified write needs `allow_production=True`; destructive operations always need `confirm_destructive=True`; `unverifiable`/`excluded` operations never run, no override; reads are never locked. Raw `FSClient` stays unlocked.
- A write is `verified` only by **read-back with a negative control**; a 2xx proves nothing. Only the runner writes `verified` into the registry. Registry `verified` entries carry a fingerprint; a fingerprint mismatch makes the operation **stale** (locked).
- Verification runs **only on beta**, fixtures named `ZZSPIKE…`, strict teardown (GET must succeed → DELETE must be 2xx → GET must be 404/410). Unit tests are offline.
- Generated code is checked in and deterministic; regenerating must reproduce it exactly (drift test).
- No new MCP tools; `src/familysearch_mcp/` is not modified. Never print or log a token. Live pytest/runner commands use `--tb=short`.
- Commit trailers on every commit: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`. After each completed task do a plain `git push origin <branch>` (never force); the controller fast-forwards `main` and pushes it when the plan completes.

## Review Focus

- **Path-parameter injection:** an id such as `../x`, `a/b`, `a?b#c`, ` `, or `""` passed to a generated method must be percent-quoted as one segment or rejected, never reshape the URL. (Test in Task 5.)
- **A verified operation whose spec/override changes** must silently stop being callable (stale) until re-verified. (Tests in Tasks 4 and 8.)
- **A recipe that passes when the operation does nothing**, or that claims an operation it never called. (Tests in Task 10.)
- **Production safety:** any write on PRODUCTION without the right flags; `allow_unverified` ignored on production; flags on reads harmless. (Truth-table test in Task 4.)
- **Regeneration side effects:** output order/format non-determinism, method-name collisions (the operation literally named `delete`), unrelated ops changing when one override changes. (Tests in Tasks 3 and 6.)

## File Structure

- `src/familysearch/client.py` — modify: add `request()` + `environment`.
- `src/familysearch/api/spec.py` — `OpSpec` dataclass (shared by runtime, registry, generated code).
- `src/familysearch/api/registry.py`, `registry.json` — statuses, fingerprints, `check_call`.
- `src/familysearch/api/_runtime.py` — `ApiResult`, `ApiBase._call`.
- `src/familysearch/api/{_ops,family_tree,standards,genealogies,memories,user_tree,users,utilities,_api}.py` — **generated**, never hand-edited.
- `src/familysearch/api/__init__.py` — exports.
- `tools/spec/{harvest,resolve,generate,bootstrap_registry,coverage}.py`, `tools/spec/{snapshot,overrides}.json` — spec pipeline.
- `tools/verify/{core,teardown,fixtures,record,runner}.py`, `tools/verify/recipes/{__init__,pilot}.py` — verification framework.
- Tests: `tests/unit/test_client.py` (extend), `test_spec_harvest.py`, `test_spec_resolve.py`, `test_api_registry.py`, `test_api_runtime.py`, `test_spec_generate.py`, `test_api_generated.py`, `test_api_registry_consistency.py`, `test_spec_coverage.py`, `test_verify_core.py`, `test_verify_runner.py`, `test_verify_recipes_pilot.py`; fixtures in `tests/fixtures/spec/`.

---

### Task 1: `FSClient.request` and `environment`

**Files:**
- Modify: `src/familysearch/client.py`
- Test: `tests/unit/test_client.py` (extend; existing tests stay untouched and green)

**Interfaces:**
- Produces on `FSClient`:
  - `environment` property → the `Environment` passed to the constructor.
  - `request(self, method: str, path_or_url: str, *, write: bool, reason: str | None = None, accept: str | None = None, params: Mapping[str, Any] | None = None, json: Any = None, files: Mapping[str, Any] | None = None, data: bytes | None = None, content_type: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> requests.Response`.
- Existing `get`/`post`/`delete` keep their signatures and now delegate to `request`.

- [ ] **Step 1: Write failing tests** (using `responses`, same style as the existing file):
  - `test_request_supports_put_head_options`: a `PUT` with `json={"a":1}` sends `Content-Type: application/x-fs-v1+json` and body `{"a": 1}`; `HEAD` and `OPTIONS` return the response with `write=False`.
  - `test_post_with_write_false_sends_no_reason_and_does_not_throttle`: POST, `write=False`, no `X-Reason` header, `time.sleep` (monkeypatched) never called even on two back-to-back calls.
  - `test_write_true_requires_reason`: `request("POST", "/x", write=True)` raises `FSError`; with a reason it sends `X-Reason`.
  - `test_raw_data_requires_content_type`: `data=b"x"` without `content_type` raises `ValueError`; with `content_type="image/png"` the body is the raw bytes and `Content-Type: image/png`.
  - `test_body_kinds_are_mutually_exclusive`: giving two of `json`/`files`/`data` raises `ValueError`.
  - `test_unknown_method_rejected`: `request("TRACE", ...)` raises `ValueError`.
  - `test_environment_property`: `FSClient("t", Environment.BETA).environment is Environment.BETA`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_client.py -q` → new tests FAIL (`request` undefined).
- [ ] **Step 3: Implement** `request` and `environment`; make `get`/`post`/`delete` delegate (`post`/`delete` → `write=True`). Keep all existing behavior (origin check, 401→`TokenExpiredError`, ≥400→`FSHTTPError`, json serialized by hand, redirects followed).
- [ ] **Step 4: Run** the full unit suite → all pass (1064 + new).
- [ ] **Step 5: Commit** `git commit -m "feat: FSClient.request for any method, raw bodies, explicit write flag"` then `git push origin <branch>`.

---

### Task 2: Spec snapshot and harvester

**Files:**
- Create: `tools/__init__.py`, `tools/spec/__init__.py`, `tools/spec/harvest.py`, `tools/spec/snapshot.json` (produced by running the tool), `tests/fixtures/spec/readperson.md`, `tests/fixtures/spec/createperson.md`, `tests/fixtures/spec/index_mini.txt`
- Modify: `pyproject.toml` → `pythonpath = ["src", "."]`
- Test: `tests/unit/test_spec_harvest.py`

**Interfaces:**
- Produces in `tools/spec/harvest.py`:
  - `INDEX_URL = "https://developers.familysearch.org/main/llms.txt"`
  - `parse_index(llms_txt: str) -> list[tuple[str, str, str]]` → `(area, title, url)` for entries under `## API Reference: …` headings, **excluding** the "API Reference Guide" page; `area` is the heading text after `API Reference: ` (e.g. `"Family Tree Endpoints"`).
  - `parse_reference_page(markdown: str, area: str) -> list[dict]` → one dict per operation in the page's embedded OpenAPI block with exactly these keys: `operation_id, area, tag, method` (upper-case), `path, summary, description, server` (first server url), `params` (list of `{name, in, required, type, enum, example}` — `type`/`enum`/`example` are `None` when absent), `request_media` (sorted list), `responses` (`{code: description}`).
  - `normalize(ops: list[dict]) -> dict` → `{"schema": 1, "source": INDEX_URL, "operations": [...]}` sorted by `(area, path, method)`.
  - `harvest(fetch: Callable[[str], str]) -> dict` → fetches the index then every reference page (4 concurrent), raises `RuntimeError` naming the page if any operation page has no OpenAPI block or invalid JSON.
  - `main(argv: list[str] | None = None) -> int` — `--out PATH` (default `tools/spec/snapshot.json`) writes the snapshot; `--check` re-harvests and prints a per-operation diff against the committed snapshot, returning 1 if different.
- Fixture pages: fetch with `curl` from `https://developers.familysearch.org/main/reference/readperson.md` and `.../createperson.md`; `index_mini.txt` is a hand-trimmed index containing one non-reference section, the "API Reference Guide" entry, and two operation entries.

- [ ] **Step 1: Write failing tests:** `test_parse_index_skips_guide_and_non_reference` (mini index → exactly the two operation entries, correct areas); `test_parse_readperson` (one op: `operation_id=="readPerson"`, `method=="GET"`, `path=="/tree/persons/{pid}"`, param `pid` in `path` required with example `"LZJW-C31"`, an `Accept` header param whose `enum` includes `"application/x-fs-v1+json"`, responses contain `"404"` and `"410"`); `test_parse_createperson_request_media` (`request_media == ["application/x-gedcomx-v1+json"]`); `test_normalize_sorted_and_idempotent`; `test_harvest_with_fake_fetch`; `test_harvest_raises_on_page_without_block`; `test_committed_snapshot_shape` (205 operations, 205 unique `operation_id`s, exactly the 8 areas from the spec, every `{param}` in every path has a matching path param).
- [ ] **Step 2: Run** → FAIL (module missing).
- [ ] **Step 3: Implement** `harvest.py` (extract the first ```` ```json ```` block after `# OpenAPI definition`; reuse the logic in `docs/superpowers/recon/2026-10-25-api-surface/harvest.py`, extended with full param detail).
- [ ] **Step 4: Run the real harvest once** (network, public docs only): `.venv/bin/python -m tools.spec.harvest`; then run the tests → all pass (incl. `test_committed_snapshot_shape`).
- [ ] **Step 5: Commit** snapshot + tool + tests (`"feat: harvest official OpenAPI definitions into a committed snapshot"`), push.

---

### Task 3: Overrides and resolver

**Files:**
- Create: `tools/spec/overrides.json`, `tools/spec/resolve.py`
- Test: `tests/unit/test_spec_resolve.py`

**Interfaces:**
- Consumes: the snapshot from Task 2.
- Produces in `tools/spec/resolve.py`:
  - `OVERRIDES_PATH`, `SNAPSHOT_PATH` (Paths).
  - `to_snake(operation_id: str) -> str` (`readPerson` → `read_person`; `getAccessToken` → `get_access_token`).
  - `@dataclass(frozen=True) class ResolvedOp` with fields: `source_operation_id, operation_id, method_name, area, module, http_method, path, summary, kind` (`"read"`/`"write"`), `destructive: bool`, `path_params: tuple[str, ...]` (in template order), `required_query: tuple[str, ...]`, `accepts: tuple[str, ...]`, `body: str` (`"none"`/`"json"`/`"upload"`), `fingerprint: str`.
  - `load_overrides(path=OVERRIDES_PATH) -> dict` (raises `ValueError` on unknown top-level keys or on a non-`schema: 1` file).
  - `resolve(snapshot: dict, overrides: dict) -> tuple[list[ResolvedOp], dict[str, str]]` → `(generated ops sorted by (module, operation_id), excluded {source_operation_id: reason})`; raises `ValueError` if any override names an operation or path that does not exist, or if two `method_name`s collide.
- Rules: `module` from area (`Family Tree Endpoints→family_tree`, `Standards Endpoints→standards`, `Genealogies Endpoints→genealogies`, `Memories Endpoints→memories`, `User Tree Endpoints→user_tree`, `User Endpoints→users`, `Utilities Endpoints→utilities`, `Authentication Endpoints→authentication`); `kind` = `read` for GET/HEAD/OPTIONS else `write`, then `overrides["kind"]`; `destructive` = `kind=="write"` and summary matches `delete|merge|restore` (case-insensitive), then `overrides["destructive"]["add"/"remove"]`; `body`: `none` for GET/HEAD/OPTIONS/DELETE, `json` for POST/PUT/PATCH, then `overrides["body_kind"]` (`upload`); `accepts` = enum of the `Accept` header param (else `()`); `required_query` = snapshot-required query params ∪ `overrides["required_query"][source_operation_id]`; `path` after `overrides["path_fix"]` (exact bad path → good path); `operation_id` after `overrides["rename"]`; `fingerprint` = sha256 hex of canonical JSON (`sort_keys=True, separators=(",",":")`) of every other field.
- `overrides.json` initial content (exact):
  - `exclude`: `getAuthorizationPage`, `getAccessToken` → reason "OAuth endpoint on the identity host; this project authenticates with the browser session".
  - `rename`: `{"delete": "deleteUserHistoryEntry"}`.
  - `path_fix`: `{"/tree/users/{uid}/preferred-parent-relationships/{pid}/tree/users/{uid}/preferred-parent-relationships/{pid}": "/tree/users/{uid}/preferred-parent-relationships/{pid}"}`.
  - `kind`: `performPersonMatchesByExample→read`, `createNameSegments→read`, `getSourceDescriptionChanges→read`, `createPartnerAccount→write`.
  - `required_query`: `{"readPersons": ["pids"]}`.
  - `body_kind`: `createPersonMemory`, `createMemories`, `updateMemoryArtifact` → `upload`.
  - `destructive`: `{"add": [], "remove": []}`. `schema`: 1. Plus a `notes` object (documentation only, ignored by `resolve`) recording: lists return 204 when empty; Genealogies ids need a prefix; redirects documented as success on `readCurrentTreePerson`/`readSourceFolders`.

- [ ] **Step 1: Write failing tests** against the **real** snapshot: `test_counts` (203 generated + 2 excluded = 205; exactly 43 destructive; no method_name collisions); `test_rename_delete_operation` (an op with `source_operation_id=="delete"` has `operation_id=="deleteUserHistoryEntry"` and `method_name=="delete_user_history_entry"`); `test_path_fix_applied_to_both_doubled_ops` (both listed operations resolve to `/tree/users/{uid}/preferred-parent-relationships/{pid}`); `test_kind_overrides` (the four ops); `test_required_query_pids`; `test_upload_ops_have_body_upload`; `test_path_params_in_template_order`; `test_fingerprint_stable_and_sensitive` (same input → same hash; changing the path, `kind`, or `accepts` of one op changes only that op's hash); `test_unknown_override_target_raises`; `test_unknown_top_level_key_raises`; `test_collision_raises` (synthetic snapshot).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `resolve.py` + `overrides.json`. **Step 4: Run** → pass.
- [ ] **Step 5: Commit** (`"feat: spec overrides and operation resolver with fingerprints"`), push.

---

### Task 4: Operation spec, registry and the lock

**Files:**
- Create: `src/familysearch/api/__init__.py` (empty for now), `src/familysearch/api/spec.py`, `src/familysearch/api/registry.py`, `src/familysearch/api/registry.json` (initial `{"schema": 1, "operations": {}}`)
- Modify: `pyproject.toml` → `[tool.setuptools.package-data] familysearch = ["api/registry.json"]`
- Test: `tests/unit/test_api_registry.py`

**Interfaces:**
- `spec.py`: `@dataclass(frozen=True) class OpSpec` with fields `operation_id, method_name, area, http_method, path, summary, kind, destructive, path_params, required_query, accepts, body, fingerprint` (types as in `ResolvedOp`).
- `registry.py`:
  - `class OperationLockedError(FSError)`.
  - `REGISTRY_PATH: Path`; `load_registry(path: Path = REGISTRY_PATH) -> dict`.
  - `effective_status(operation_id: str, fingerprint: str, registry: dict, impl: str = "generated") -> str` → `"verified"` (entry says verified **and** its stored fingerprint equals `fingerprint`), `"stale"` (verified but fingerprint differs), `"unverifiable"`, `"excluded"`, else `"unverified"` (including a missing entry).
  - `check_call(spec: OpSpec, environment: Environment, registry: dict, *, allow_unverified: bool = False, allow_production: bool = False, confirm_destructive: bool = False) -> None` raising `OperationLockedError` per the truth table below; the message names the missing flag or the status and, for unverifiable, the registry `reason`.
- Truth table (writes only; `kind=="read"` never raises): status `unverifiable` → always raise. If `destructive` and not `confirm_destructive` → raise. Status `verified`: BETA allowed; PRODUCTION allowed only with `allow_production`. Status `unverified`/`stale`: PRODUCTION → always raise; BETA → allowed only with `allow_unverified`.

- [ ] **Step 1: Write failing tests:** an exhaustive parametrized `test_truth_table` over `kind × status × environment × destructive × (allow_unverified, allow_production, confirm_destructive)` with the expected allow/deny written out as a table in the test (not computed from the implementation); `test_read_never_locked_even_with_unverifiable_status_flags_irrelevant`; `test_stale_when_fingerprint_changes` (registry verified with fingerprint `"aaa"`, spec fingerprint `"bbb"` → `"stale"` and `check_call` treats it as unverified); `test_missing_entry_is_unverified`; `test_error_messages_name_the_flag` (denial on production mentions `allow_production`; unverified on beta mentions `allow_unverified`; destructive mentions `confirm_destructive`; unverifiable includes the reason); `test_override_flags_on_production_for_unverified_still_raise`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** (`"feat: operation registry and the write lock"`), push.

---

### Task 5: Runtime (`ApiBase`, `ApiResult`)

**Files:**
- Create: `src/familysearch/api/_runtime.py`
- Test: `tests/unit/test_api_runtime.py`

**Interfaces:**
- Consumes: `OpSpec` (Task 4), `FSClient.request` (Task 1), `check_call`/`load_registry` (Task 4).
- Produces:
  - `@dataclass class ApiResult: status: int; headers: dict[str, str]; content_type: str; body: Any | None; text: str` with property `entity_id -> str | None` (the `X-ENTITY-ID` header, else the last path segment of `Location`, else `None`). `body` is parsed JSON, or `None` for an empty body (incl. 204) or non-JSON.
  - `class ApiBase: __init__(self, client: FSClient, *, registry: dict | None = None)` (registry defaults to `load_registry()`), and `_call(self, spec: OpSpec, path_args: Mapping[str, Any], *, query: Mapping[str, Any] | None = None, accept: str | None = None, body: Any = None, files: Mapping[str, Any] | None = None, data: bytes | None = None, data_content_type: str | None = None, reason: str | None = None, allow_unverified: bool = False, allow_production: bool = False, confirm_destructive: bool = False) -> ApiResult`.
- Behavior: (1) writes run `check_call` **before any network I/O**; (2) every `spec.path_params` name must be present in `path_args` (else `ValueError`) and each value is `str(v)`, rejected if empty/whitespace, and percent-quoted with `urllib.parse.quote(v, safe="")`; (3) `accept`, if given, must be in `spec.accepts` when `spec.accepts` is non-empty (else `ValueError`), default = first of `spec.accepts` or `None`; (4) every name in `spec.required_query` must be a key of `query` (else `ValueError`); `None` query values are dropped, `bool` → `"true"`/`"false"`; (5) calls `client.request(spec.http_method, "/platform" + path, write=(spec.kind=="write"), reason=reason, ...)`; `body` → `json=`, `files`/`data` only allowed when `spec.body=="upload"` (else `ValueError`); a `read`-kind POST uses `write=False`.

- [ ] **Step 1: Write failing tests** (hand-built `OpSpec`s + `responses`; set `registry` explicitly): `test_path_injection_is_quoted` (ids `"../x"`, `"a/b"`, `"a?b#c"`, `"é"` arrive as single quoted segments in the request URL) and `test_empty_or_blank_id_rejected` (**Review Focus 1**); `test_accept_default_and_validation`; `test_required_query_enforced`; `test_query_none_dropped_and_bool_rendered`; `test_204_gives_none_body_and_json_list_body_parsed`; `test_non_json_body_is_none_text_kept`; `test_entity_id_from_header_then_location`; `test_write_requires_reason_via_client`; `test_locked_write_makes_no_network_call` (`responses` with no registered URL and `assert_all_requests_are_fired=False`: `len(responses.calls)==0`); `test_read_kind_post_has_no_reason_header`; `test_upload_only_for_upload_ops`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** (`"feat: generated-API runtime with path quoting and lock enforcement"`), push.

---

### Task 6: Generator

**Files:**
- Create: `tools/spec/generate.py`, `tests/fixtures/spec/mini_snapshot.json`, `tests/fixtures/spec/golden/` (expected generated files for the mini snapshot)
- Test: `tests/unit/test_spec_generate.py`

**Interfaces:**
- Consumes: `resolve()` (Task 3).
- Produces in `generate.py`:
  - `render(ops: list[ResolvedOp], excluded: dict[str, str]) -> dict[str, str]` → `{relative_filename: file_text}` for `_ops.py`, each area module (`family_tree.py`, …; `authentication` is not emitted — its ops are excluded), and `_api.py`; pure and deterministic (sorted, `\n` newlines, header comment `# GENERATED by tools/spec/generate.py — do not edit; run python -m tools.spec.generate`).
  - `write_all(out_dir: Path) -> list[Path]`; `main(argv=None) -> int` — default writes `src/familysearch/api/`; `--check` regenerates in memory and returns 1 (printing the first differing file and the fix command) if anything differs.
- Generated shapes (exact):
  - `_ops.py`: `OPERATIONS: dict[str, OpSpec] = {"readPerson": OpSpec(...), ...}`.
  - Each area module defines `class <Module>Ops:` (e.g. `FamilyTreeOps`) with one method per operation, `method_name` = `to_snake(operation_id)`:
    ```python
    def read_person(self, pid: str, *, accept: str | None = None, query: Mapping[str, Any] | None = None) -> ApiResult:
        """Read Person (GET /tree/persons/{pid})."""
        return self._call(OPERATIONS["readPerson"], {"pid": pid}, accept=accept, query=query)
    ```
    Path params are positional `str` in template order; then `*`. **Writes** add, after `*` and before `accept`: `reason: str`, then — by `body`: `json` → `body: Any = None`; `upload` → `files: Mapping[str, Any] | None = None, data: bytes | None = None, data_content_type: str | None = None`; and, last, `allow_unverified: bool = False, allow_production: bool = False, confirm_destructive: bool = False` (all passed through to `_call`). Reads have none of the write-only parameters.
  - `_api.py`: `class FamilySearchApi(ApiBase, FamilyTreeOps, StandardsOps, GenealogiesOps, MemoriesOps, UserTreeOps, UsersOps, UtilitiesOps): pass`.

- [ ] **Step 1: Write failing tests:** `test_golden_mini_snapshot` (a 3-operation snapshot — a read, a JSON write, a destructive delete — renders byte-for-byte to the files in `tests/fixtures/spec/golden/`); `test_render_is_deterministic_and_order_independent` (shuffled input → same output); `test_generated_code_compiles_and_imports` (`compile()` every rendered file); `test_check_detects_drift` (mutate one rendered file in a temp dir → `main(["--check", "--out", tmp])` returns 1 and names the file); **Review Focus 5:** `test_changing_one_override_changes_only_that_ops_output` (change `kind` of one op → the diff between renders is confined to that operation's block, its `_ops.py` entry and its fingerprint).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `generate.py` (string templating; no formatter dependency). **Step 4: Run** → pass.
- [ ] **Step 5: Commit** (`"feat: deterministic API generator with drift check"`), push.

---

### Task 7: Generate the real surface and test it end to end

**Files:**
- Create (by running the generator): `src/familysearch/api/_ops.py`, `family_tree.py`, `standards.py`, `genealogies.py`, `memories.py`, `user_tree.py`, `users.py`, `utilities.py`, `_api.py`
- Modify: `src/familysearch/api/__init__.py` (export `FamilySearchApi`, `ApiResult`, `OperationLockedError`, `OPERATIONS`), `src/familysearch/__init__.py` (re-export `FamilySearchApi`, `ApiResult`)
- Test: `tests/unit/test_api_generated.py`

**Interfaces:**
- Produces: `FamilySearchApi(client: FSClient, *, registry: dict | None = None)` — 203 methods, one per generated operation.

- [ ] **Step 1: Write failing tests** (all parametrized over `OPERATIONS`, one case per operation, using `responses` and a registry that marks every op `verified` with its current fingerprint so the lock does not interfere, except where a test says otherwise): `test_drift_committed_equals_regenerated` (runs `tools.spec.generate.main(["--check"])` == 0); `test_every_operation_has_a_method` (name == `to_snake(operation_id)`, positional params == `path_params`, writes have keyword-only `reason`, `allow_unverified`, `allow_production`, `confirm_destructive`; reads do not); `test_request_shape_per_operation` (call each with dummy ids `"A1"`, mocked 200 JSON: HTTP method, URL path == `/platform` + path with ids substituted, `Accept` equals the default, `X-Reason` present iff `kind=="write"`, JSON writes carry `Content-Type: application/x-fs-v1+json`); `test_fingerprints_match_resolver` (recompute with `resolve()` and compare); `test_unverified_write_is_refused_before_network` (empty registry, beta client → every write op raises `OperationLockedError`, `len(responses.calls)==0`); `test_reads_work_with_empty_registry`; `test_op_count` (203 operations; 99 writes ± the override reclassifications — assert the exact numbers from the resolver rather than hard-coding).
- [ ] **Step 2: Run** → FAIL. **Step 3:** run `.venv/bin/python -m tools.spec.generate`, add the exports. **Step 4: Run** the full unit suite → pass.
- [ ] **Step 5: Commit** (`"feat: generated FamilySearchApi covering all 203 operations (writes locked)"`), push.

---

### Task 8: Registry bootstrap and consistency

**Files:**
- Create: `tools/spec/bootstrap_registry.py`; (generates) `src/familysearch/api/registry.json`
- Test: `tests/unit/test_api_registry_consistency.py`

**Interfaces:**
- Produces: `build_registry(snapshot: dict, overrides: dict) -> dict` and `main()` writing `registry.json`. Registry entry shape: `operations[<source operation id>] = {"generated": {...}, "handwritten": {...}?}` where each implementation entry is `{"status": "verified"|"unverified"|"unverifiable"|"excluded", "reason"?, "recipe"?, "evidence"?, "fingerprint"?, "commit"?, "verified_on"?}`. Registry keys use the **post-rename** operation id for generated operations and the source id for excluded ones.
- Initial statuses: every generated operation `unverified`; `getAuthorizationPage`, `getAccessToken` → `excluded` (reason from overrides); `updateVocabConceptDefinition`, `updateVocabTranslation` → `unverifiable` ("edits shared reference data; never executed"); `createPartnerAccount`, `updatePartnerAccount` → `unverifiable` ("partner-only account feature; never executed").
- `handwritten` entries (status `verified` with `recipe` = an existing live test node id) for exactly this mapping:

  | operation id | hand-written method | recipe (live test) |
  |---|---|---|
  | readPerson | `TreeReader.get_person` | `tests/integration/test_beta_roundtrip.py::test_create_addfact_delete_person` |
  | readPersonParents, readPersonChildren, readPersonSpouses | `TreeReader.get_relatives` | `tests/integration/test_beta_roundtrip.py::test_create_parent_child_relationship` |
  | getPersonMemories | `TreeReader.get_memories` | `tests/integration/test_beta_roundtrip.py::test_upload_to_person_then_link_to_second_person` |
  | readPersonSources | `TreeReader.get_sources` | `tests/integration/test_beta_roundtrip.py::test_source_create_and_attach` |
  | readTreePersonMatches | `TreeReader.get_matches` | `tests/integration/test_mcp_beta.py::test_matches_and_record_search` |
  | searchTreePersons | `Search.tree_search` | `tests/integration/test_beta_roundtrip.py::test_tree_search_runs` |
  | readCurrentUser | auth probe / `Context` | `tests/integration/test_beta_roundtrip.py::test_read_current_user` |
  | createPerson | `TreeWriter.create_person` | `tests/integration/test_beta_roundtrip.py::test_create_addfact_delete_person` |
  | updatePerson | `TreeWriter.add_fact`, `Sources.attach_to_person` | `tests/integration/test_beta_roundtrip.py::test_create_addfact_delete_person` |
  | createRelationship | `TreeWriter.create_couple`, `create_parent_child` | `tests/integration/test_beta_roundtrip.py::test_create_parent_child_relationship` |
  | deletePerson | `TreeWriter.delete_person` | `tests/integration/test_beta_roundtrip.py::test_create_addfact_delete_person` |
  | deleteCoupleRelationship | `TreeWriter.delete_couple` | `tests/integration/test_beta_roundtrip.py::test_create_couple_relationship` |
  | deleteChildAndParentsRelationship | `TreeWriter.delete_child_and_parents` | `tests/integration/test_beta_roundtrip.py::test_create_parent_child_relationship` |
  | createMemories | `Memories.upload` | `tests/integration/test_beta_roundtrip.py::test_standalone_upload_is_floating_until_linked` |
  | createPersonMemory | `Memories.upload_to_person` | `tests/integration/test_beta_roundtrip.py::test_upload_to_person_then_link_to_second_person` |
  | createMemoryPersona | `Memories.link_to_person` | `tests/integration/test_beta_roundtrip.py::test_upload_to_person_then_link_to_second_person` |
  | deleteMemory | `Memories.delete` | `tests/integration/test_beta_roundtrip.py::test_upload_to_person_then_link_to_second_person` |
  | createSourceDescription | `Sources.create_description` | `tests/integration/test_beta_roundtrip.py::test_source_create_and_attach` |
  | deleteSourceDescription | (live-suite teardown) | `tests/integration/test_beta_roundtrip.py::test_source_create_and_attach` |

  (`readPersonChangeHistory`/`get_changes` has no live assertion → no handwritten entry; `record_search` is a website service, not an official operation.)

- [ ] **Step 1: Write failing tests:** `test_every_snapshot_operation_is_accounted_for` (registry operations == all 205 source/renamed ids exactly once); `test_status_values_valid_and_reasons_present` (`unverifiable`/`excluded` entries have a non-empty `reason`); `test_verified_entries_reference_existing_recipes` (a handwritten `recipe` is `path::function` and that function exists in that file — checked with `ast`, no import of live modules; a `generated` verified entry's `recipe` must be a key of `tools.verify.recipes.RECIPES` once Task 11 exists — until then there are none); `test_verified_generated_entry_has_fingerprint_and_evidence`; **Review Focus 2:** `test_stale_detected_when_spec_changes` (copy the registry, set one generated op `verified` with a wrong fingerprint → `effective_status` returns `"stale"` and `check_call` refuses without `allow_unverified`); `test_unverifiable_ops_have_no_generated_method_unlock` (their `check_call` raises even with every flag, on beta).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `bootstrap_registry.py`, run it to produce `registry.json`. **Step 4: Run** the full suite → pass.
- [ ] **Step 5: Commit** (`"feat: registry bootstrapped for all 205 operations"`), push.

---

### Task 9: Coverage report

**Files:**
- Create: `tools/spec/coverage.py`
- Test: `tests/unit/test_spec_coverage.py`

**Interfaces:**
- `build_report(ops: list[ResolvedOp], excluded: dict, registry: dict) -> dict` → `{"total": int, "by_area": {area: {status: count}}, "pending": [{"operation_id", "kind", "status", "reason"?}]}` using `effective_status` for the `generated` implementation; `format_report(report: dict) -> str` (a readable table + the pending list); `main(argv=None) -> int` with `--require-done` → exit 1 unless every operation is `verified`, `unverifiable` (with a reason) or `excluded`.

- [ ] **Step 1: Write failing tests:** counts on a synthetic registry; `test_stale_counts_as_pending`; `test_require_done_fails_with_pending_and_passes_when_complete`; `test_format_lists_every_pending_operation`; run against the real registry: total 205, 2 excluded, 4 unverifiable, `--require-done` returns 1.
- [ ] **Step 2–4: Run → implement → run.** **Step 5: Commit** (`"feat: coverage report with a done gate"`), push.

---

### Task 10: Verification framework and runner (offline-tested)

**Files:**
- Create: `tools/verify/__init__.py`, `core.py`, `teardown.py`, `fixtures.py`, `record.py`, `runner.py`, `tools/verify/recipes/__init__.py` (`RECIPES: dict[str, Recipe] = {}` for now)
- Modify: `tests/integration/_tracker.py` — import `verified_delete` from `tools.verify.teardown` (move the function, behavior unchanged)
- Test: `tests/unit/test_verify_core.py`, `tests/unit/test_verify_runner.py` (and the existing `tests/unit/test_live_teardown.py` must stay green)

**Interfaces:**
- `teardown.py`: `verified_delete(label, url, get, delete, errors) -> None` — moved verbatim from `tests/integration/_tracker.py` (GET must succeed → DELETE must be 2xx → GET must be 404/410; problems appended to `errors`).
- `core.py`:
  - `@dataclass(frozen=True) class Recipe: name: str; ops: tuple[str, ...]; evidence: str; run: Callable[[VerifyContext], None]` (`evidence` ∈ `"state-change"`, `"read-content"`).
  - `class VerifyApi(FamilySearchApi)`: constructor refuses a client whose `environment is not Environment.BETA` (`FSError`); overrides `_call` to pass `allow_unverified=True` and `confirm_destructive=True` for writes, to record `spec.operation_id` into `self.called: set[str]`, then delegates to `super()._call`.
  - `@dataclass class VerifyContext: api: VerifyApi; fixtures: FixtureSet; client: FSClient; reason: str; unique: str` (`unique` = `"ZZSPIKE"+8 hex`).
  - `expect_changed(before: Any, after: Any, what: str) -> None` — raises `AssertionError(f"negative control failed: {what} did not change")` when `before == after`.
- `fixtures.py`: `class FixtureSet: __init__(self, client: FSClient, *, reason: str)`, `person(self, given: str = "Test", gender: str = "Male", birth_year: str | None = "1850") -> str` (creates a deceased `ZZSPIKE…` person with the library's `TreeWriter`, tracked immediately), `track(self, kind: str, object_id: str) -> None` (`kind` ∈ `person|couple|child_and_parents|memory|source`), `mark_deleted(self, kind, object_id) -> None`, `teardown(self) -> list[str]` (strict via `verified_delete`, relationships → sources → memories → persons; returns the problem list, empty on success).
- `record.py`: `record_verified(operation_id, recipe, evidence, fingerprint, commit, verified_on, path=REGISTRY_PATH) -> None` (sets the `generated` entry; preserves everything else; writes sorted keys, indent 1, trailing newline) and `record_unverifiable(operation_id, reason, path=REGISTRY_PATH) -> None` (reason must be non-empty).
- `runner.py`: `run_recipe(recipe: Recipe, ctx: VerifyContext, *, registry_path, commit: str, today: str) -> dict` → `{"recipe", "passed": bool, "problems": [...]}`: runs the recipe, then `fixtures.teardown()`; passes only if the recipe raised nothing, teardown returned no problems, **and** `set(recipe.ops) <= ctx.api.called`; on pass calls `record_verified` for each op with `OPERATIONS[op].fingerprint`; on failure the registry file is untouched. `main(argv=None) -> int`: `python -m tools.verify list | run [--recipe NAME ...] [--all] [--dry-run] | classify OPERATION_ID --status unverifiable --reason TEXT`. `run` builds the beta client from Brave's cookie, **aborts unless `GET /platform/users/current` is 200 on beta**, never targets production.

- [ ] **Step 1: Write failing tests** with a fake `FSClient`-level transport (`responses`) and a fake recipe set: `test_verify_api_refuses_production_client`; `test_verify_api_injects_unlock_flags_and_records_calls`; **Review Focus 3:** `test_noop_operation_fails_negative_control` (a recipe whose write is mocked to do nothing and which uses `expect_changed(before, after, …)` → `AssertionError`, runner reports `passed False`, registry untouched); `test_recipe_claiming_uncalled_op_fails` (`ops=("createPerson","readPerson")` but only `createPerson` called → not passed, registry untouched); `test_teardown_problem_blocks_verification` (teardown returns a problem → not passed); `test_pass_records_fingerprint_commit_date_and_evidence`; `test_record_preserves_other_entries_and_formatting`; `test_classify_requires_reason`; `test_runner_main_refuses_when_beta_check_fails`; `test_fixtureset_person_is_tracked_before_assertions`; `test_verified_delete_moved_and_live_teardown_tests_still_pass`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** the full suite → pass.
- [ ] **Step 5: Commit** (`"feat: live verification framework and runner (beta only)"`), push.

---

### Task 11: Pilot recipes (implemented offline; run live by the controller)

**Files:**
- Create: `tools/verify/recipes/pilot.py`; modify `tools/verify/recipes/__init__.py` to register them
- Test: `tests/unit/test_verify_recipes_pilot.py`

**Interfaces:**
- `RECIPES` gains:
  - `family_tree.person_create_read` — `ops=("createPerson","readPerson")`, evidence `"state-change"`. Steps: create via `ctx.api.create_person(reason=ctx.reason, body=<person document with name f"Pilot {ctx.unique}" built with the library's `build_person`>)`; take `entity_id`; track it; `ctx.api.read_person(id)` → assert `body["persons"][0]["display"]["name"]` equals the unique name (the unique marker is the negative control: it cannot appear unless the write took effect); also assert `read_person("ZZZZ-ZZZ")` raises `FSHTTPError` with status 404 or 410.
  - `family_tree.person_delete_restore` — `ops=("deletePerson","restorePerson")`, evidence `"state-change"`. Steps: fixture person via `ctx.fixtures.person()`; capture `before = read_person(id).status`; `delete_person(id, reason=…)`; `after` read raises `FSHTTPError` 410 (or 404) → `expect_changed(before, after_status, "person readability after delete")`; `restore_person(id, reason=…)`; read-back → 200 with the original name; `ctx.fixtures.mark_deleted` is **not** called (teardown deletes it again, strictly).
- Expect live adjustment: the exact response codes and the restore body are discovered live; the recipe must assert on read-back state, not on the write's status code.

- [ ] **Step 1: Write failing tests:** against a scripted fake service (`responses`) that behaves like the real API: both recipes pass; and fail when the service is changed to ignore the write (create does nothing / delete does nothing) — proving the negative controls bite; `test_recipes_are_registered_and_reference_real_operations` (every name in `ops` exists in `OPERATIONS`; recipe names unique).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** the recipes. **Step 4: Run** → pass.
- [ ] **Step 5: Commit** (`"feat: pilot verification recipes for person create/read/delete/restore"`), push.
- [ ] **Step 6 (controller, live, beta only; needs a fresh beta login in Brave and the Keychain prompt):** `FS_USE_BRAVE=1 .venv/bin/python -m tools.verify run --all` then `.venv/bin/python -m tools.spec.coverage`. Expected: all four operations `verified` for `generated` in `registry.json`, coverage shows them, beta left clean. Fix recipes against the real behavior if a recipe fails (never weaken a negative control to make it pass). Commit the updated `registry.json` (`"verify: pilot operations verified on beta"`) and push.

---

### Task 12: Documentation

**Files:**
- Modify: `README.md`, `docs/api-discovery.md`
- Test: `tests/unit/test_readme_api_section.py` (asserts the README documents the commands/flags that exist)

- [ ] **Step 1: Write failing test:** the README contains a "Generated API" section naming `FamilySearchApi`, `python -m tools.spec.generate`, `python -m tools.spec.coverage`, `python -m tools.verify`, and the flags `allow_unverified`, `allow_production`, `confirm_destructive`; every `python -m tools.*` command it names is importable (`importlib.util.find_spec`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Write** the section: usage example (`FamilySearchApi(client).read_person("ABCD-123")`), what the lock does and does not cover (raw `FSClient` is unlocked), the registry statuses (`verified`/`stale`/`unverified`/`unverifiable`/`excluded`), "verified means proven on beta, not production", how to verify an operation (recipe + runner) and read the coverage report, and the pilot result. Add to `docs/api-discovery.md` a pointer plus the rule that new endpoints are added by regenerating, not by hand. **Step 4: Run** → pass.
- [ ] **Step 5: Commit** (`"docs: generated API, the lock, the registry and how to verify"`), push.
