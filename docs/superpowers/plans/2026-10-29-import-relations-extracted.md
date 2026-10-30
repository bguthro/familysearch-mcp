# Import Relationships: Reviewed Relations Files (replaces regex parsing as the source) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** The FamilyTree.md diagrams are free prose; regex parsing left ~50 lines unparsed and mis-named some nodes. Instead, one reviewed JSON file per line (`<archive>/_import/relations/<line>.json`), drafted from `FamilyTree.md` by an extraction agent and reviewed by Ben, becomes the source of nodes and edges. `relgap` consumes it; the regex parser stays as a cross-check and fallback.

## Global Constraints
- No personal data in the repo (fictional fixtures only). The relations files live in the archive. Living people: a node may appear in the file (the file is Ben's private archive) but reports show them as counts only, exactly as now.
- Schema `version: 1`: `{"version":1,"line":str,"reviewed":bool,"nodes":[{"id":str,"name":str,"birth":int|null,"death":int|null,"status":"deceased"|"living"|"unknown","folder":"<line>/<folder>"|null,"source":str}],"edges":[{"kind":"parent-child"|"spouse","a":str,"b":str,"order":int|null,"marriage_year":int|null}]}`; for parent-child `a` is the parent and `b` the child; `id` unique within the file; edges reference existing ids; `folder` (when set) must be an existing person folder (validated against the scan); `status` "deceased" requires a death year or an explicit `"deceased_note"` string; a node with `birth >= 1900` and no death year cannot be `deceased` unless `deceased_note` is present; `source` is the verbatim diagram or list text it came from (used for review, never printed for living nodes).
- `relgap` uses a line's relations file when it exists AND `reviewed` is true; otherwise it falls back to the regex parser and says so in the report. Unreviewed files are listed as "awaiting review".
- Validation errors name the file, the node/edge index and the rule. `--tb=short`, trailers as before, gate commits on tests, no push.

## Tasks
### Task 1: schema, loader, validator
**Files:** Create `src/familysearch_import/relations.py`; Test `tests/unit/test_import_relations.py`.
**Interfaces:** `RelationsError(Exception)`; `load_relations(path: Path, valid_folders: set[str]) -> RelationsFile` (dataclasses `RNode`, `REdge`, `RelationsFile(line, reviewed, nodes, edges)`); `to_parsed_tree(rel: RelationsFile) -> ParsedTree` adapter into the structures `relgap` already evaluates (read `relgap.py`/`treeparse.py`; preserve `living`/`unknown`/`deceased` semantics and folder pre-mapping so `map_nodes` uses the explicit `folder` when given instead of name matching).
- [ ] Tests for each validation rule and the adapter; implement; commit `feat: reviewed relations files`.

### Task 2: relgap integration and `relations` CLI
**Files:** Modify `relgap.py`, `relgapreport.py`, `__main__.py`; Test `tests/unit/test_import_relgap.py`, `test_import_relgapreport.py`.
**Interfaces:** `relationships` command prefers `<archive>/_import/relations/<line>.json` (reviewed) per line; the report header lists per line which source was used (`reviewed relations file` / `regex parser (fallback)` / `awaiting review`); new command `python -m familysearch_import relations-check --archive PATH` that validates all relations files (no network) and prints errors/warnings plus a cross-check against the regex parser (nodes the parser found that the file lacks, by name+years, deceased only).
- [ ] Tests; implement; full suite; commit `feat: relgap uses reviewed relations files`.
