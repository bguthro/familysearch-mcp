# Import Executor and Rollback (documents and sources) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Execute an approved `Plan.json` (upload-document and add-source actions with state `planned`) against FamilySearch with fresh pre-checks, read-back after every write, an append-only ledger, and a rollback command. This is the first code in the import engine that WRITES.

**Spec:** `docs/superpowers/specs/2026-10-29-import-engine-design.md`; consumes `Plan.json` from `planner.py`; reuses the verified library paths `familysearch.memories.Memories.upload_to_person`, `familysearch.sources.Sources.create_description` / `attach_to_person`, and the verified deletes `Memories.delete` / the `deleteSourceDescription` operation. Do not use other write paths.

## Global Constraints
- **Safety is the product.** Fail closed everywhere: any doubt, mismatch, unexpected response or read-back failure STOPS the run (no further writes) and is recorded.
- **Gates (all required to write):** (a) `--confirm-hash H` equals both the stored `plan_hash` in Plan.json AND a freshly recomputed hash of its actions AND `approved_plan_hash` in `<archive>/_import/approvals.json` (written by Ben, never by this tool); (b) the environment the Brave session is signed into equals `--env`; (c) for `--env production`: the CLI flag `--allow-production` (a CLI flag only; never an MCP tool parameter, never an env-only switch) AND `--only <line>/<folder>` (one or more people; no all-people production run in this plan) AND at most `--max-actions N` (default 10, hard cap 25 for production); (d) a lock file `<archive>/_import/execute.lock` created exclusively (refuse if present; stale locks are removed only by `--break-lock` after printing its contents).
- Only actions with state `planned` run; `likely-covered`, `already-present`, `skipped` never do. Dry-run is the default: without `--execute`, print what would happen and run all READ checks.
- **Per-action pre-checks (fresh reads):** the person still resolves to the plan's FS id (no redirect/deleted) and is not `living` on FamilySearch; the file exists and its SHA-256 equals the plan's; the archive status (living_status with the reviewed record) is still deceased; the memory title / source URL is still absent from FamilySearch's current memories/sources (else mark `already-present`, skip); the executed-keys set (ledger + this run) does not contain the action key. Dedupe: the same key under two folders runs once.
- **Read-back after every write:** upload -> the person's memories now contain the title and the count increased by exactly one; add-source -> the person's sources contain the new description (by URL or title) and count increased by one. A failed read-back records `FAILED-READBACK` with created ids and stops the run (the object is left for rollback; never auto-delete).
- **Ledger** `<archive>/_import/ledger.jsonl`, append-only, flushed per line: an `intent` line (key, kind, fs_id, env, plan_hash, file name, sha256, UTC time) BEFORE each write, then a `result` line (status `created|skipped|failed|failed-readback`, created ids: `memory_id` / `source_description_id`, reason). The planner reads `{"key": ...}` lines; keep `key` on every line and only treat `result` lines with status `created` as executed. Never write names of living people, tokens or account ids to the ledger. File names and titles of deceased people's documents are allowed (the ledger lives in the archive).
- **Rollback** (`rollback --ledger ... --env ... [--key K ...] --execute`): for `created` results only, delete the created object by id through the verified delete, then read back absence; same gates (hash not needed; require `--allow-production` and `--only` in production); append `rollback` result lines; never delete anything not named in the ledger.
- Reason text for every write: `Added from family research archive` (the library's `reason` argument). Rate limit: reuse the client throttle; stop on 429 after backoff.
- **No personal data in the repo.** Tests use fictional archives in `tmp_path` and fake FamilySearch objects; no network in unit tests. Beta live verification is run by the controller with ZZSPIKE-named fixtures (a fictional archive in a temp dir, a throwaway person created on beta).
- `--tb=short`; trailers `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`; gate commits on tests; do not push.

## Review Focus
- A re-run after a successful run must write nothing (ledger + fresh reads), and a run interrupted after an `intent` line but before the `result` line must NOT re-upload blindly: on restart the executor reads FamilySearch and reconciles (title present -> record `created (reconciled)`, absent -> eligible again).
- Wrong plan hash, edited Plan.json, edited approvals, missing `--allow-production`, missing `--only`, environment mismatch: each exits non-zero with no write and no ledger `intent` line.
- A file changed since planning (different SHA), a person who became living/merged/deleted on FamilySearch, or a title that now exists: skipped with reason, others continue; a read-back failure stops everything.
- Two folders/lines linking the same FS person with the same file: one upload.
- Ledger/lock files must never be written inside the repo; the rollback only deletes ids recorded as created by this tool.

---

### Task 1: Gates, ledger and lock (no FamilySearch writes yet)
**Files:** Create `src/familysearch_import/execute.py` (gates, `Ledger`, lock), Test `tests/unit/test_import_execute_gates.py`.
**Interfaces:** `verify_plan_for_execution(archive: Path, plan_path: Path, confirm_hash: str) -> PlanFile` (raises `GateError` with a clear message); `check_run_gates(env: str, allow_production: bool, only: Sequence[str], max_actions: int) -> None`; `class Ledger(path)` with `executed_keys() -> frozenset[str]` (only `result` lines with status `created`), `intent(...)`, `result(...)`, `unresolved_intents() -> list[dict]`; `class RunLock(archive)` context manager (exclusive create, `--break-lock` support).
- [ ] Tests for every gate and Review Focus item that concerns gates, ledger append/flush/reconcile data, lock behaviour; implement; commit `feat: executor gates and ledger`.

### Task 2: Execution engine
**Files:** Modify `execute.py`; Test `tests/unit/test_import_execute_run.py`.
**Interfaces:** `run_plan(plan: PlanFile, services: ExecServices, ledger: Ledger, *, only, max_actions, execute: bool) -> RunSummary` where `ExecServices` bundles `reader` (ReadOnlyFS-like: person, memory titles, source refs), `memories` (`upload_to_person`, `delete`), `sources` (`create_description`, `attach_to_person`, delete). Implements the pre-checks, intent/result lines, read-back, reconcile of unresolved intents, stop-on-failure, dedupe, dry-run mode. Uses fakes in tests (a fake that counts memories/sources and can simulate: success, upload that does not appear, 409 duplicate, person becomes living, file changed).
- [ ] Tests for all Review Focus items and pre-checks; implement; commit `feat: executor engine`.

### Task 3: Rollback and CLI
**Files:** Modify `execute.py`, `__main__.py`; Test `tests/unit/test_import_execute_cli.py`.
**Interfaces:** `rollback(ledger: Ledger, services, keys, execute) -> RollbackSummary`; CLI `python -m familysearch_import execute --archive PATH --env beta|production --plan Plan.json --confirm-hash H [--only LINE/FOLDER ...] [--max-actions N] [--execute] [--allow-production] [--break-lock]` and `rollback --archive PATH --env ... [--key K ...] [--only ...] [--execute] [--allow-production]`; real services built from `extract_brave_token()` + `FSClient` with the environment check (reuse `ReadOnlyFS` for reads; writes only through `Memories`/`Sources` objects built on the same client); exit codes: 0 ok, 2 gate failure, 3 run stopped on failure. Output summary never prints tokens or account ids.
- [ ] Tests (CLI gates, dry-run default, rollback only touches ledger-created ids); implement; run the full suite; commit `feat: executor rollback and CLI`.

### Controller steps
Opus skeptic review of the whole branch (it can write); beta live run with ZZSPIKE fixtures including interrupted-run reconcile and rollback; then merge. Production: only the supervised pilot, with Ben present.
