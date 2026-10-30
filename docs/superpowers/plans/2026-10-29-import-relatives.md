# Import Engine: Resolve Unlinked People Through Relatives (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** For deceased archive people with no confirmed FamilySearch link, find candidates by walking the FamilySearch tree outward from people who ARE linked (parents, spouses, children, two hops), comparing every relative with each unlinked folder by name and dates, and report the candidates with relationship evidence. Read-only.

**Why (Ben, 2026-10-29):** once connected into the tree, the missing people should be found as relatives of linked people; resolve all names before any writes.

**Spec:** `docs/superpowers/specs/2026-10-29-import-engine-design.md`; builds on `linking.py` (`compare`, `FsPerson`, `differences`), `prod_reader.py` (`ReadOnlyFS`), `scan.py`, `planner.current_link`, `links.json`, `approvals.json`.

## Global Constraints
- Read-only (GET/HEAD via `ReadOnlyFS`); no personal data in the repo (fictional tests; output only under `<archive>/_import/`, same output guards as `linkreport`).
- Starting points: people whose current link (`planner.current_link(pkey, approved, links)`) is non-null AND status deceased. Living folders and living FamilySearch persons (`living=True`) are never expanded or listed by name (counts only). Expansion never follows a relative flagged living.
- Targets: deceased archive people with no current link. A relative is a candidate for a target only if `compare(target, relative)` is `confirmed` or `probable`; rank by verdict, then by whether the relationship evidence is consistent with the archive's line (a relative reached from a linked person in the same line ranks higher), then by number of independent paths.
- Budget: at most 2 hops, a global cap of 600 relative reads (stop and report if exceeded), cache every read by id, reuse the adapter's throttle/backoff. Paging must follow `links.next` (already implemented in the tree reader).
- Evidence line format: `<relationship> of <linked folder> (<line> / <folder>) via <FS id>`; relationship in {parent, child, spouse, parent-of-parent, ...} stated from the target's perspective ("father/mother" not required).
- Candidates are NEVER auto-accepted; a candidate that is already some other archive person's current link is flagged `taken by <folder>` (possible duplicate in the archive); one target with two equally ranked candidates is flagged `ambiguous`.
- Tests with fake `ReadOnlyFS` returning small fictional trees. `--tb=short`, trailers as before, gate commits on tests, do not push.

## Review Focus
- A relative who is living on FamilySearch must not appear by name anywhere in the output.
- The same FamilySearch person reached by several paths is one candidate with several evidence lines.
- A target whose only same-name relative has a conflicting birth year (>2) is not a candidate.
- Hitting the 600-read cap produces a report that says it is partial, never a silent truncation.
- Re-running with new approvals (a target now linked) turns it into a new starting point and stops listing it as a target.

---

### Task 1: Relatives reader
**Files:** Modify `src/familysearch_import/prod_reader.py`; Test `tests/unit/test_import_prod_reader.py`.
**Interfaces:** `ReadOnlyFS.get_relatives(person_id: str) -> list[Relative]` with `Relative(fs: FsPerson, relationship: Literal["parent","child","spouse"])`, built from `TreeReader.get_relatives` (see `src/familysearch/tree.py`; look at the real shape handled there) and `fs_person_from_dict`; retries/backoff like `get_fs_person`; `ReadFailed` on error. Living relatives keep `living=True` in `FsPerson`.
- [ ] Tests with fake responses (parents/spouses/children, living relative, paging, error); implement; commit `feat: read relatives of a person`.

### Task 2: Resolver core (pure)
**Files:** Create `src/familysearch_import/relatives.py`; Test `tests/unit/test_import_relatives.py`.
**Interfaces:** `@dataclass(frozen=True) Evidence(relationship: str, via_fs_id: str, via_pkey: str)`; `@dataclass(frozen=True) Candidate(fs: FsPerson, verdict: str, evidence: tuple[Evidence, ...], taken_by: str | None)`; `@dataclass(frozen=True) TargetResult(pkey: str, candidates: tuple[Candidate, ...], ambiguous: bool)`; `@dataclass(frozen=True) RelativesResult(targets: tuple[TargetResult, ...], reads: int, partial: bool, living_skipped: int)`; `resolve_through_relatives(people: Sequence[Person], links: Mapping[str, str | None], approved: Mapping[str, str], reader, *, max_hops: int = 2, max_reads: int = 600, record_status=...) -> RelativesResult`.
- [ ] Tests for every Review Focus line and Global Constraints rule; implement; commit `feat: relatives resolver core`.

### Task 3: Report and CLI
**Files:** Create `src/familysearch_import/relativesreport.py`; Modify `__main__.py`; Test `tests/unit/test_import_relativesreport.py`.
**Interfaces:** `render_relatives_report(result) -> str` (Markdown: totals; per target the ranked candidates with FS name/lifespan, verdict, differences, evidence lines, flags `ambiguous`/`taken by`; a "no candidate" section; a partial warning when capped); `relatives_json(result) -> dict`; CLI `python -m familysearch_import relatives --archive PATH --env production [--max-reads N]` writing `<archive>/_import/RelativesReport.md` and `relatives.json` under the existing output rules. Suggest, per candidate, the exact line to paste into `approvals.json`.
- [ ] Tests; implement; run the full suite; commit `feat: relatives report`.

### Controller steps
Live read-only run on production; review with Ben; approvals; then the archive-vs-FamilySearch relationship comparison (parse `FamilyTree.md` chains) as a follow-up plan.
