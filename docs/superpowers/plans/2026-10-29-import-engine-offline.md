# Import Engine, Offline Stages (scan, classify, records, report) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the stages of the import engine that need no FamilySearch access: scan the archive, classify living/deceased and document provenance, define and validate the per-person import record, and produce an inventory report. The planner, executor and production stages are later plans.

**Architecture:** New package `src/familysearch_import/` (pure Python, no network, no dependency on `familysearch`). Pure functions over a `Person` dataclass; the archive path is always an argument. All output (reports, drafts) is written under the archive path, never into this repo.

**Tech Stack:** Python 3.11+, stdlib; optional extra `import = ["pypdf", "pillow"]` for embedded-URL extraction (degrades to "unreadable" when absent).

**Spec:** `docs/superpowers/specs/2026-10-29-import-engine-design.md`

## Global Constraints
- **This repo holds no personal data.** No real names, dates, ids or paths of real people in code, tests, fixtures or docs. Tests build fictional archives in `tmp_path`. A report/draft writer must refuse an output path inside the repo (any path under the directory containing `pyproject.toml`).
- Read-only on the archive except for explicit draft/report output files under it; never modify or delete existing archive files. Files named `*.draft.json` / `InventoryReport.md` are the only files written.
- `questionable/` folders (any capitalization), dot-files, `Family Migration Map.html` and filenames starting `Gmail -` are ignored entirely.
- Folder names are compared Unicode NFC-normalized (macOS stores decomposed).
- Living rule (Ben): no death year and birth year >= 1900 is living, unless the person's `ImportRecord.json` says `"status": "deceased"`. A missing/unparseable birth year with no death year is `unknown` (treated as not importable, listed).
- Documents are never attached to living or unknown-status people. Banned providers (default): fold3, ancestry, newspapers.com, myheritage, findagrave/find a grave. A banned-provider file may be cited as text only when an original source URL/id is known.
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01KsGTNtnTCBjqUg9MiuVJiH`. Gate commits on test exit status. Plain pushes.
- Tests: `.venv/bin/python -m pytest tests/unit/test_import_*.py --tb=short`.

## Review Focus
- A person with no death year and birth 1900+ must never get a document action, even when the folder contains an obituary or census.
- Folder names with `?`, a spaced range (`1614 - 1708 Name`), no dates at all, accented/decomposed characters, or parentheses in the name must parse or be listed as unparsed, never crash or mis-assign dates.
- A `.webloc` pointing at a non-person FamilySearch URL (search, memories, tree landing page) must not yield a person id; ids must match `^[A-Z0-9]{4}-[A-Z0-9]{3,4}$`.
- A PDF/image with no readable embedded URL, a corrupt file, or a missing optional library must yield "unknown provenance" and be skipped, not raise.
- Output path inside the repo, or an archive path that does not exist, must fail with a clear error and write nothing.

---

### Task 1: Archive scanner

**Files:**
- Create: `src/familysearch_import/__init__.py`, `src/familysearch_import/scan.py`
- Test: `tests/unit/test_import_scan.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) Person(line: str, folder: str, path: Path, birth: int | None, death: int | None, display_name: str, files: tuple[Path, ...], familysearch_ids: tuple[str, ...], book_excluded: frozenset[str])`; `parse_folder_name(name: str) -> tuple[int | None, int | None, str] | None` (birth, death, name; `None` when unparseable); `scan_archive(root: Path, lines: Sequence[str] | None = None) -> list[Person]` (`lines=None` means every top-level directory that contains person folders; skips `0000_*`, `_tools`, `books`, `To sort`); `familysearch_person_id(url: str) -> str | None`.

- [ ] **Step 1: Write failing tests** with a fictional `tmp_path` archive: `parse_folder_name` cases (`"1844-1915 Pat Example (Alt)"`, `"1815-? Pat Example"`, `"1952 Pat Example"`, `"1614 - 1708 Pat Example"`, `"?-1900 Pat Example"`, `"No Dates"` -> None, decomposed accent equals composed); `scan_archive` ignores `questionable`/`Questionable`, dot-files, `Gmail -*`, reads `BookExclude.txt` (comments and blanks skipped); `familysearch_person_id` accepts `https://www.familysearch.org/tree/person/details/ABCD-123`, `https://ancestors.familysearch.org/en/ABCD-123/pat-example-1800-1870`, rejects `https://www.familysearch.org/search/...` and `https://www.familysearch.org/memories/...`; webloc parsing reads the `<string>` URL of a plist.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the interfaces above in `scan.py` (webloc via `plistlib` with a regex fallback).
- [ ] **Step 4: Run tests; all pass.**
- [ ] **Step 5: Commit** `feat: import engine archive scanner`.

### Task 2: Classifier (living status and document provenance)

**Files:**
- Create: `src/familysearch_import/classify.py`, `src/familysearch_import/embedded.py`
- Modify: `pyproject.toml` (add optional extra `import = ["pypdf", "pillow"]`)
- Test: `tests/unit/test_import_classify.py`, `tests/unit/test_import_embedded.py`

**Interfaces:**
- Consumes: `Person` from Task 1.
- Produces: `living_status(person: Person, record_status: str | None = None) -> Literal["living", "deceased", "unknown"]`; `embedded_urls(path: Path) -> tuple[str, ...]` (PDF: text, metadata and link annotations via pypdf; images: EXIF/XMP text via Pillow; any error or missing library returns `()`); `@dataclass(frozen=True) DocumentVerdict(path: Path, kind: str, provider: str | None, source_url: str | None, action: Literal["include", "cite-only", "skip"], reason: str)`; `classify_document(person: Person, path: Path, status: str, banned: Sequence[str] = DEFAULT_BANNED) -> DocumentVerdict`.
- Rules for `classify_document`: any non-`deceased` status -> `skip`, reason `"living"` or `"unknown status"`; `Citation:`/`SOURCE INFORMATION` sheets, non-document extensions (`.md .json .webloc .txt .html`), and `BookExclude` is *not* a rule (book exclusion is a book concern) -> `skip`; provider found in filename or embedded URL host matching a banned provider -> `cite-only` when an embedded URL exists, otherwise `skip` (reason `"banned provider, no original source"`); everything else -> `include`; `kind` from filename keywords (census, birth, marriage, death, obituary, baptism, manifest, naturalization, will/probate, photo, other).

- [ ] **Step 1: Write failing tests** for every rule above and every Review Focus living/provenance case (generate tiny PDFs with a link annotation using pypdf in the test; `pytest.importorskip("pypdf")` for those; one test with `embedded_urls` on a corrupt file returns `()`; one with the library import monkeypatched away).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** `living_status`: record_status wins; else death present -> deceased; else birth >= 1900 -> living; else unknown.
- [ ] **Step 4: Run tests; all pass.**
- [ ] **Step 5: Commit** `feat: import engine classifier and embedded-url extraction`.

### Task 3: Import record schema and draft generator

**Files:**
- Create: `src/familysearch_import/record.py`
- Test: `tests/unit/test_import_record.py`

**Interfaces:**
- Consumes: `Person`, `living_status`.
- Produces: JSON schema for `ImportRecord.json` (kept in each person's archive folder, reviewed by Ben): `{"version": 1, "status": "deceased"|"living"|null, "familysearch_id": str|null, "names": [{"given": str, "surname": str, "note": str?}], "gender": "Male"|"Female"|null, "facts": [{"type": "Birth"|"Death"|"Marriage"|"Burial"|"Baptism"|"Residence"|"Immigration"|"Naturalization"|"Occupation", "date": str?, "place": str?, "sources": [str]}], "relationships": [{"kind": "parent"|"spouse"|"child", "folder": str, "line": str}], "sources": [{"title": str, "citation": str, "url": str?, "file": str?}], "reviewed": bool}`; `load_record(path: Path) -> dict` (validates, raises `RecordError` with the field path); `draft_record(person: Person) -> dict` (names/dates from the folder name, `familysearch_id` when exactly one id is found, `reviewed: false`, empty `relationships`/`sources`); `write_draft(person: Person, repo_root: Path) -> Path` writes `ImportRecord.draft.json` beside the folder's files, refusing to overwrite an existing draft or a reviewed `ImportRecord.json`, and refusing any path inside the repo.
- Facts are filled in later by Claude reading `ResearchSummary.md` with Ben reviewing; this task builds only the schema, validation and skeleton generator.

- [ ] **Step 1: Write failing tests**: valid record loads; each invalid field (bad enum, missing version, non-list facts, `reviewed` not bool) raises `RecordError` naming the field; draft for a fictional person has the right names/dates/id; `write_draft` refuses overwrite and refuses a path inside the repo.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests; all pass.**
- [ ] **Step 5: Commit** `feat: import record schema and draft generator`.

### Task 4: Inventory report CLI

**Files:**
- Create: `src/familysearch_import/__main__.py`, `src/familysearch_import/report.py`
- Test: `tests/unit/test_import_report.py`

**Interfaces:**
- Consumes: `scan_archive`, `living_status`, `classify_document`, `load_record`.
- Produces: `build_report(root: Path, repo_root: Path) -> str` (Markdown: counts by line and status; people with a FamilySearch id / without / with several ids; unparsed folders; per-person document actions summarized as counts of include/cite-only/skip with skip reasons; people needing an `ImportRecord`; no document names for living people beyond counts); CLI `python -m familysearch_import report --archive PATH [--out PATH]` default out `<archive>/_import/InventoryReport.md`; `python -m familysearch_import drafts --archive PATH --line LINE` writes missing drafts. Both refuse a missing archive or an out path inside the repo (exit 2, message, nothing written).

- [ ] **Step 1: Write failing tests** on a fictional archive: report numbers match; living people appear only as counts; out path inside repo exits 2 and writes nothing; missing archive exits 2.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `argparse` CLI.
- [ ] **Step 4: Run tests; all pass; run the full suite (`.venv/bin/python -m pytest -q --tb=short`).**
- [ ] **Step 5: Commit** `feat: import engine inventory report CLI`, then push.

### Controller steps (after Task 4)
- Run `report` against the real archive; read the report in the archive (not pasted into the repo) and summarize counts to Ben.
- Opus skeptic review is not needed (nothing here writes to FamilySearch); one Sonnet whole-branch review focused on the privacy constraint and the living rule.
- Update memory `familysearch-mcp-status.md`; next plan: extraction review workflow, then planner (needs search), executor, ledger.
