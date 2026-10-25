# MCP Generic Operation Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the generated `FamilySearchApi` (205 operations, behind the verification lock) through the MCP server with three generic tools: `list_operations`, `describe_operation`, `call_operation`.

**Architecture:** `Context` builds a `FamilySearchApi` alongside the existing services. A new `tools_ops.py` registers the three tools; every call goes through the generated method, so the registry lock, path quoting and `reason` rules apply unchanged. The existing 16 tools and their guards are untouched.

**Tech Stack:** Python ≥3.11, FastMCP (`mcp==1.30.0`), existing `familysearch` library; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-25-full-api-coverage-design.md` (which listed MCP tools as a non-goal for the library project; Ben has since asked for this integration — chosen shape: "Generic operation tools"). Library reference: README "Generated API" section.

## Global Constraints

- The MCP server never writes to stdout (stdio protocol); diagnostics to stderr only. Never print, log or return a token; tool results never include the client.
- Tool errors are raised, not caught (the MCP layer turns exceptions into error results), matching `tools_read.py`.
- **Reads** (`is_write(spec)` false, incl. the three allow-listed read POSTs) are allowed in any environment with no flags.
- **Writes through `call_operation` are beta-only in this iteration.** On production every non-read operation is refused with a message pointing at the guarded tools (`create_person`, `add_fact`, `create_parent_child`, `upload_memory`, …). Reason: generic writes bypass the guarded tools' duplicate/match gate, living-person routing, provenance and upload-root checks, and the MCP deliberately has no delete/merge tool on production. Ruling recorded; trivially relaxable later.
- On beta, writes additionally require: `ctx.require_writes()` (existing gate), a non-empty `reason`, and the library lock (`verified` runs; `unverified`/`stale` need `allow_unverified=True`; `unverifiable`/`excluded` never run; destructive operations need `confirm_destructive=True`). `allow_production` is NEVER a tool parameter and is never passed as True.
- `allow_unverified` and `confirm_destructive` are Ben's explicit decisions: tool descriptions and `SERVER_INSTRUCTIONS` say so, as for the existing override flags. Operations with `body == "upload"` (memory uploads) are refused: use `upload_memory`.
- Result size is capped (see Task 1) so a large response cannot flood the client.
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`. Plain `git push origin <branch>` after each task; never force.

## Review Focus

- **Production write attempts** through `call_operation` (verified ops, destructive ops, ops with `allow_unverified`/`confirm_destructive` set, case/whitespace variants of ids) must always be refused before any network call. (Test in Task 1.)
- **Argument smuggling:** `path_args` with extra/missing keys, ids like `../x` or `a/b`, `query` containing the path-param names, `body` for an op that takes none — must raise, never reshape the URL. (Runtime already validates; test that the tool surfaces it.)
- **Unknown/odd operation ids** (`""`, `"readperson"`, `"deleteUserHistoryEntry "`) raise a clear error and never fall through to a wrong operation.
- **Flags from the model:** `allow_unverified`/`confirm_destructive` default False and are not enabled by anything else; `allow_production` cannot be reached.
- **Huge responses / non-JSON bodies / 204:** capped, never crash.

## File Structure

- `src/familysearch_mcp/context.py` — modify: add `api` to `Services`, build it in `_build`.
- `src/familysearch_mcp/tools_ops.py` — create: pure helpers + `register_op_tools(mcp, ctx)`.
- `src/familysearch_mcp/server.py` — modify: register the tools; extend `SERVER_INSTRUCTIONS`.
- `README.md` — modify: MCP section lists the three tools and their rules.
- Tests: `tests/unit/test_mcp_ops.py` (new); extend `tests/unit` server/tool-list tests that count tools.

---

### Task 1: The three operation tools

**Files:**
- Modify: `src/familysearch_mcp/context.py` (`Services.api: FamilySearchApi`; `_build` constructs `FamilySearchApi(client)`)
- Create: `src/familysearch_mcp/tools_ops.py`
- Modify: `src/familysearch_mcp/server.py` (call `register_op_tools(mcp, ctx)`)
- Test: `tests/unit/test_mcp_ops.py`; fix any existing test asserting the exact tool list/count

**Interfaces:**
- Consumes: `familysearch.api` (`FamilySearchApi`, `OPERATIONS`, `OperationLockedError`), `familysearch.api.spec.is_write`, `familysearch.api.registry` (`effective_status`, `load_registry`, `check_call`), `Context` (`environment`, `writes_allowed()`, `require_writes()`, `call(fn)`, `services()`).
- Produces in `tools_ops.py`:
  - `MAX_RESULT_CHARS = 50_000`, `READ_POST_NOTE`.
  - `operation_summary(spec, registry) -> dict` → `{operation_id, area, method, path, kind, destructive, status}` (`status` = `effective_status(...)` for `generated`).
  - `describe(spec, registry, environment, writes_allowed) -> dict` → summary plus `description` (summary text), `path_params`, `required_query`, `accepts`, `body` (`none`/`json`/`upload`), `needs_reason` (is_write), and `can_run_here: bool` + `blocked_reason: str | None` computed by the same rules `call_operation` enforces (production write, upload op, unverifiable/excluded, unverified needing the flag, destructive needing confirm) — informational only.
  - `shape_result(result: ApiResult) -> dict` → `{status, content_type, entity_id, body, truncated: bool}`; `body` is the parsed JSON, or the text when not JSON; if `len(json.dumps(body))` or the text exceeds `MAX_RESULT_CHARS`, `body` is replaced by `{"truncated_preview": <first MAX_RESULT_CHARS chars of the serialization>}` and `truncated` is True; 204/empty → `body` None.
  - `register_op_tools(mcp, ctx)` registering:
    - `list_operations(area: str | None = None, kind: str | None = None, status: str | None = None, query: str | None = None, limit: int = 50) -> list[dict]` — filters (area/status/kind exact-lowercase match, `query` case-insensitive substring of operation_id/summary/path), sorted by operation_id, `limit` clamped 1–200.
    - `describe_operation(operation_id: str) -> dict`.
    - `call_operation(operation_id: str, path_args: dict[str, str] | None = None, query: dict[str, Any] | None = None, accept: str | None = None, body: Any = None, reason: str | None = None, allow_unverified: bool = False, confirm_destructive: bool = False) -> dict`.
- `call_operation` behavior, in this order: (1) look up `operation_id` exactly in `OPERATIONS` (unknown → `ValueError` naming it and up to 5 close ids via `difflib.get_close_matches`); (2) for writes: refuse upload-body ops (`ValueError`: use `upload_memory`); refuse when `ctx.environment` is not BETA (`ContextError`-style `FSError` message naming the guarded tools); `ctx.require_writes()`; `reason` required non-blank; (3) build positional args from `spec.path_params` in order (missing → `ValueError`; extra keys in `path_args` → `ValueError`); (4) `ctx.call(lambda s: getattr(s.api, spec.method_name)(*args, **kwargs))` where kwargs = `accept`, `query`, `body` (only if `spec.body == "json"`), and for writes `reason`, `allow_unverified`, `allow_production=False`, `confirm_destructive`; (5) return `shape_result`.

- [ ] **Step 1: Write failing tests** (`tests/unit/test_mcp_ops.py`, `responses` + a fake `Context`/`Services` with a beta `FSClient` and a hand-built registry in the nested shape; follow how existing MCP tool tests build their context — read `tests/unit/test_mcp_*.py`): 
  - `test_list_operations_filters_and_limit`: 205-op free total via `limit=200` cap; `area`, `kind="read"`, `status="verified"` (use the real registry: exactly the 4 pilot ops), `query="persons"`, sorted, each row has the summary keys.
  - `test_describe_operation_fields_and_can_run_here`: a read op → `can_run_here` True; unverified write on beta → False with `blocked_reason` naming `allow_unverified`; destructive verified write → names `confirm_destructive`; production write → names the guarded tools; unverifiable → permanent; upload op → names `upload_memory`.
  - `test_call_read_returns_shaped_result` (GET `/platform/tree/persons/A1` mocked JSON, 200 → `status 200`, body parsed, `truncated False`); `test_call_read_204_body_none`; `test_call_read_post_json_body_allowed` (`performPersonMatchesByExample` sends the body, no `X-Reason`); `test_result_truncated_over_cap`.
  - **Review Focus:** `test_production_writes_refused_before_network` parametrized over a verified write, a destructive verified write (with `confirm_destructive=True`), an unverified write with `allow_unverified=True`, on a PRODUCTION context with `FS_ALLOW_PROD_WRITES` set → raises, `len(responses.calls) == 0`; `test_allow_production_unreachable` (spy on the library `check_call`/`_call`: `allow_production` is always False; the tool signature has no such parameter); `test_unknown_operation_id_variants` (`""`, `"readperson"`, trailing space, `None`-ish) raise `ValueError` with suggestions where close; `test_path_args_validation` (missing, extra, `../x`, `a/b`, empty → raises, no network; legitimate id quoted as one segment); `test_body_for_bodyless_op_raises`; `test_write_on_beta_needs_reason_gate_and_lock` (blank reason; writes disabled gate; unverified without `allow_unverified` → `OperationLockedError`; with it → request sent with `X-Reason`; destructive without confirm → refused; unverifiable never runs even with both flags); `test_upload_op_refused`; `test_tools_registered` (the three names appear in the FastMCP tool list) and update any existing test that pins the total tool count (16 → 19).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_mcp_ops.py -q` → FAIL.
- [ ] **Step 3: Implement** `Services.api`, `tools_ops.py`, server registration. Reuse `clamp` from `validation.py`; reuse the library's `is_write` and `effective_status` rather than re-deriving rules; build the `can_run_here` decision by calling `check_call` inside try/except (so it cannot drift from enforcement).
- [ ] **Step 4: Run** the full unit suite → all pass.
- [ ] **Step 5: Commit** (`"feat: generic operation tools for the MCP (list/describe/call, writes beta-only)"`), push.

---

### Task 2: Instructions and docs

**Files:**
- Modify: `src/familysearch_mcp/server.py` (`SERVER_INSTRUCTIONS`), `README.md`
- Test: `tests/unit/test_mcp_ops.py` (extend) and the README test if present

- [ ] **Step 1: Write failing tests:** `SERVER_INSTRUCTIONS` mentions `list_operations`, `describe_operation`, `call_operation`, states that `allow_unverified` and `confirm_destructive` are Ben's explicit decisions never to be set unprompted, that production writes go through the guarded tools only, and that uploads use `upload_memory`; the README MCP section names the three tools and the beta-only write rule and states that only the four pilot operations are verified.
- [ ] **Step 2: Run** → FAIL. **Step 3: Write** the instruction paragraph (concise, same tone as existing text) and README section; tools count "19". **Step 4: Run** the full suite → pass.
- [ ] **Step 5: Commit** (`"docs: MCP generic operation tools in instructions and README"`), push.
