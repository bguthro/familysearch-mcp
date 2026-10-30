# Import Relationships, Read-only Gap Report (Plan A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Parse each line's `FamilyTree.md` diagram into people and relationships, map people to archive folders, compare each relationship between linked people with FamilySearch (read-only), and report gaps, conflicts and creation candidates.

**Spec:** `docs/superpowers/specs/2026-10-29-import-relationships-design.md`. Builds on `scan`, `linking` (`compare`, `FsPerson`), `planner.current_link`, `prod_reader.ReadOnlyFS.get_relatives`, `relatives.py`.

## Global Constraints
- Read-only; no personal data in the repo (fictional diagrams in tests); output only under `<archive>/_import/` with the existing output guards. Living people appear as counts only (a node is living by the same rule: no death year and born 1900 or later, unless its folder says otherwise); edges touching a living node are skipped and counted.
- The diagram parser must never guess: any line it cannot parse is reported as unparsed, not dropped silently. Name+date mapping to folders uses `compare` with `confirmed`/`probable`; ambiguous (two folders fit) or none fit -> node "unmapped". Mapping is within the same line first, then other lines (the same person appears in several lines' diagrams).
- A relationship is evaluated only when BOTH ends have a current link (`planner.current_link`). Otherwise status `blocked: <end> has no link`.
- FamilySearch wins: if the child already has parents on FamilySearch and none matches the archive's parent, status `conflict` with FamilySearch's parents listed; never proposed for adding. A child with one parent on FamilySearch and the archive adding the other is `missing-in-FamilySearch` (additive) but flagged `partial-parents`.
- Spouse edges: the diagram's `m.`/`m1.`/`m2.` lines; a marriage in the diagram between two linked people with no couple relationship in FamilySearch is `missing-in-FamilySearch`.
- Budget: reuse a per-run cache; at most 600 FamilySearch reads (partial warning like the relatives report). `--tb=short`, trailers as before, gate commits on tests, no push.

## Review Focus
- The same couple/child appearing in two lines' diagrams is one edge.
- Half-siblings and multiple marriages (`m1.`/`m2.`) must attach children to the right spouse pair, or mark the other parent unknown; never assign by position alone when ambiguous.
- Diagram nodes with `?`, ranges, `~` approximate years, alternate names in quotes or parentheses, and no dates parse or report cleanly.
- A node mapped to a folder whose FamilySearch link was only "probable" is still evaluated (links come from `current_link`) and the report says which links are approved-probable.
- Output for the real archive never prints living names.

---

### Task 1: FamilyTree.md diagram parser
**Files:** Create `src/familysearch_import/treeparse.py`; Test `tests/unit/test_import_treeparse.py`.
**Interfaces:** `@dataclass(frozen=True) Node(line: str, index: int, text: str, name: str, birth: int | None, death: int | None, approx: bool)`; `@dataclass(frozen=True) Edge(kind: Literal["parent-child","spouse"], a: int, b: int, order: int | None, note: str)` (indexes into the node list; for parent-child `a` is the parent, `b` the child; the other parent, when the diagram shows a marriage, is a second edge to the same child); `parse_family_tree(line: str, text: str) -> ParsedTree(nodes, edges, unparsed_lines)`; reads the first fenced block under the heading containing "Tree diagram" (or the first fenced block whose lines contain `└─`/`├─`). Handles marriage text in the same line (`X m. 1882, Y (1862–1932)` -> two nodes + a spouse edge), `m1.`/`m2.` lines as spouse nodes under the person with an order, indentation by box-drawing prefix width.
- [ ] Tests with fictional diagrams (shape copied from the real ones: single chain, marriage inline, m1/m2, siblings, `│` continuation lines, `?`/`~`/`b.` dates, unparsable line). Implement; commit `feat: parse FamilyTree diagrams`.

### Task 2: Map nodes to folders and compare with FamilySearch (pure)
**Files:** Create `src/familysearch_import/relgap.py`; Test `tests/unit/test_import_relgap.py`.
**Interfaces:** `map_nodes(parsed_by_line: Mapping[str, ParsedTree], people: Sequence[Person]) -> dict[tuple[str,int], MappedNode]` (`MappedNode(person: Person | None, status: Literal["mapped","unmapped","ambiguous"], living: bool)`); `@dataclass(frozen=True) EdgeResult(kind, a_label, b_label, a_fs: str | None, b_fs: str | None, status: Literal["present","missing-in-familysearch","conflict","blocked","skipped-living"], note: str, lines: tuple[str, ...])`; `evaluate_edges(parsed_by_line, people, links, approved, reader, *, max_reads=600) -> RelGapResult(edges, create_candidates, unmapped_nodes, reads, partial, living_skipped)` where labels never contain living names. `reader` has `get_relatives(fs_id)`; a parent-child edge is `present` when the child's FamilySearch relatives include the parent as relationship parent; a spouse edge when spouses include each other.
- [ ] Tests for all Review Focus and Global Constraints rules with a fake reader; implement; commit `feat: relationship gap evaluation`.

### Task 3: Report and CLI
**Files:** Create `src/familysearch_import/relgapreport.py`; Modify `__main__.py`; Test `tests/unit/test_import_relgapreport.py`.
**Interfaces:** `render_relgap_report(result) -> str` (Markdown: totals by status; sections per status with `<A> -> <B>` labels, FamilySearch ids, notes; `conflict` shows FamilySearch's parents/spouses; creation candidates with the diagram line they came from; unmapped nodes; unparsed diagram lines; partial warning); `relgap_json(result) -> dict`; CLI `python -m familysearch_import relationships --archive PATH --env production [--max-reads N]` writing `<archive>/_import/RelationshipReport.md` and `relationships.json`. Suggest, for each `missing-in-familysearch` edge, the line to paste into `approvals.json` under `approved_relationships` (a stable edge key: `<kind>:<fs_id_a>:<fs_id_b>`).
- [ ] Tests; implement; run the full suite; commit `feat: relationship gap report`.

### Controller steps
Live read-only run on production; review with Ben; then Plan B (writes).
