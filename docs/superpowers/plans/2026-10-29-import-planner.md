# Import Planner v1 (documents and sources for linked people; read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** For people whose FamilySearch link is confirmed (or approved), compare the archive with what FamilySearch already holds (memories and sources, read from production) and produce a dry-run plan of ADDITIVE actions: upload a document, add a source citation, or skip (already there / not allowed). Nothing is written to FamilySearch; the executor is a later plan.

**Spec:** `docs/superpowers/specs/2026-10-29-import-engine-design.md`. Builds on `familysearch_import` (scan, classify, linking, prod_reader, linkreport) and `links.json`.

## Global Constraints
- **Read-only** against FamilySearch (reuse `ReadOnlyFS`); no write operation is called anywhere. **No personal data in the repo**; all output under the archive (`_import/Plan.json`, `_import/PlanReport.md`); refuse output inside the repo; never overwrite an existing Plan.json that has an approval file (write `Plan.<hash8>.json` instead).
- Only people with a link verdict `confirmed` OR listed in `<archive>/_import/approvals.json` (`{"approved_links": {"<line>/<folder>": "<FS id>"}}`, written by Ben) are planned. Everyone else is listed as skipped with a reason (`no confirmed link`, `living`, `unknown status`). `probable`/`linked-differs` people are planned only when approved.
- Documents: only classifier verdict `include` becomes an upload; `cite-only` becomes a source citation (URL + title, no file); `skip` stays skipped with the classifier reason. Living/unknown people get no document action at all (belt and braces: re-check status here).
- Idempotency: every planned action has a stable `key` = sha256 of (FS person id, action kind, file SHA-256 or citation URL). A document is `already-present` when FamilySearch already has a memory for that person whose title equals the file's stem (case/whitespace-insensitive) OR the key appears in `<archive>/_import/ledger.jsonl` (written by the future executor; read if present). A source citation is `already-present` when a source description with the same URL or identical title exists for the person.
- Conservative file limits: skip files over 15 MB or with extensions other than pdf/jpg/jpeg/png (reason `unsupported`), and list them.
- The plan carries a content hash (`plan_hash`) over its actions; the report prints it. Tests use fakes (fake `ReadOnlyFS` returning memories/sources), no network. `--tb=short`, commit trailers as before, gate commits on tests, do not push.

## Review Focus
- A re-run after a (future) successful upload must show those documents `already-present`, not planned again.
- A living person in a folder with an id and documents: zero document actions, even when the id was approved.
- A title collision between two different files of one person (same stem, different content) must not be silently collapsed: plan both with a `-2` style disambiguated title, flagged.
- FamilySearch memory/source read errors must mark the person `unplanned (read error)`, never `empty` (which would plan duplicate uploads).
- An approved id that no longer resolves (redirected/deleted) must be skipped with that reason.

---

### Task 1: Reading what FamilySearch already holds
**Files:** Modify `src/familysearch_import/prod_reader.py`; Test `tests/unit/test_import_prod_reader.py`.
**Interfaces:** `ReadOnlyFS.get_memory_titles(person_id: str) -> list[str]` and `get_source_refs(person_id: str) -> list[SourceRef]` with `SourceRef(title: str, url: str | None)` (use `TreeReader.get_memories` / `get_sources`; look at `src/familysearch/tree.py` and the verified Memories/Sources shapes in `src/familysearch/memories.py`, `sources.py`); retries/backoff as in `get_fs_person`; raise `FSError` subclass `ReadFailed` on error (callers distinguish from empty).
- [ ] Tests with fake responses (empty, populated, error, redirect); implement; commit `feat: read existing memories and sources`.

### Task 2: Planner core (pure)
**Files:** Create `src/familysearch_import/planner.py`; Test `tests/unit/test_import_planner.py`.
**Interfaces:** `@dataclass(frozen=True) Action(key: str, kind: Literal["upload-document","add-source","skip"], person_line: str, person_folder: str, fs_id: str, file: str | None, title: str | None, url: str | None, state: Literal["planned","already-present","skipped"], reason: str, sha256: str | None)`; `@dataclass(frozen=True) PersonPlan(line: str, folder: str, fs_id: str | None, status: str, actions: tuple[Action, ...], note: str)`; `@dataclass(frozen=True) Plan(people: tuple[PersonPlan, ...], plan_hash: str)`; `build_plan(people: Sequence[Person], links: Mapping[str, str | None], approved: Mapping[str, str], reader, ledger_keys: frozenset[str] = frozenset(), max_bytes: int = 15_000_000) -> Plan`, where `links` maps "<line>/<folder>" to the confirmed FS id and `reader` has `get_memory_titles`/`get_source_refs`/`get_fs_person`. Compute the hash with sha256 over canonical JSON of the planned actions only (stable ordering).
- [ ] Tests for every Review Focus line and Global Constraints rule; implement; commit `feat: import planner core`.

### Task 3: Plan CLI and report
**Files:** Create `src/familysearch_import/planreport.py`; Modify `src/familysearch_import/__main__.py`; Test `tests/unit/test_import_planreport.py`.
**Interfaces:** `load_links(path: Path) -> dict[str, str | None]` (reads `links.json` verdicts confirmed only), `load_approvals(path: Path) -> dict[str, str]` (missing file -> `{}`; validates shape); `render_plan_report(plan) -> str` (Markdown: totals of planned/already-present/skipped by kind and reason; per person actions with file names and titles for DECEASED people only; plan hash on top); `plan_json(plan) -> dict`; CLI `python -m familysearch_import plan --archive PATH --env production [--limit N]` writing `<archive>/_import/Plan.json` and `PlanReport.md` under the existing output rules.
- [ ] Tests (fake reader; no network); implement; run the full suite; commit `feat: import plan report`.

### Controller steps
Live read-only run against production; review the PlanReport with Ben; then plans for ImportRecord drafts (facts, relationships) and the executor with ledger and rollback.
