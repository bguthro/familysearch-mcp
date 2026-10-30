# Import Engine: Relationships and Person Creation (deceased only) Design

Status: draft 2026-10-29, decisions by Ben noted. Nothing here writes to FamilySearch until Plan B.

## Intent
Make FamilySearch's shared tree reflect the relationships in Ben's archive: parent-child and marriage links between the deceased people we have already linked, and, as a last resort, create the few deceased people who are missing. One connected tree.

## Decisions (Ben)
- Relationships and creation are designed and tested BEFORE the first production write (the document-upload pilot waits).
- Deceased only. Living people (including their relationship to deceased parents) are out of scope for now; Ben may add them later, and FamilySearch keeps living records private to their creator.
- Additive only, never merge, duplicates reported. Where FamilySearch disagrees with the archive, FamilySearch wins and the difference is reported ("err to FamilySearch records").

## Source of archive relationships
Each line's `FamilyTree.md` has a fenced tree diagram (indented `└─`/`├─` children, `m.` marriage lines, names with `(birth–death)` or `(b.year)`). Parse it into nodes and edges, map nodes to archive folders by name and dates (reuse `linking.compare`), and keep unmatched nodes as "no folder" (for example a first wife who has no folder).

## Stages
- **Plan A (read-only, this plan):** parse the diagrams; compare each archive edge between two linked people with FamilySearch (parents, spouses, children via `ReadOnlyFS.get_relatives`); report per edge `present`, `missing-in-FamilySearch`, or `conflict` (FamilySearch already has a different parent or spouse for that child); list creation candidates (deceased archive nodes with no confirmed link and no relative candidate). Output only under the archive `_import/`.
- **Plan B (writes, beta-drilled, later):** executor action kinds `add-couple`, `add-child-parents`, `create-person` using the verified library paths (`TreeWriter.create_couple`, `create_parent_child`, `create_person`) with the same gates, ledger, read-back and rollback as uploads. `create-person` re-runs search and match-by-example immediately before creating and refuses on any plausible match. Relationship actions require both people confirmed/approved links and Ben's per-edge approval in `approvals.json` (`approved_relationships`).

## Out of scope
Living people, merges, editing or deleting others' conclusions, facts/events beyond what creation of a new person needs, place standardization.
