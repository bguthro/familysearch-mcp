# Import tree writes: couples, parent links, new people

Status: written for Ben's remote review; no production login needed to build it. Beta drill and
Opus skeptic review gate any production use.

## Intent

The relationship report (`relationships.json`) lists edges missing on FamilySearch and deceased
people with no record. This adds the writing half: add a marriage, link a child to parents, create
a missing deceased person. Same safety model as uploads: dry-run default, plan hash, per-item
approval by Ben, ledger, read-back with negative control, rollback.

## Goals / non-goals

- Goals: additive only; deceased only; every write individually approved by Ben; fully reversible
  from the ledger; FamilySearch wins on any disagreement.
- Non-goals: merges (never), living people, editing/deleting others' data, facts/sources on new
  people (later), changing existing relationships (reported only).
- Anti-goal: creating a duplicate of someone already on FamilySearch.

## Design (calls made; Ben may veto)

1. **Separate tree plan.** New `TreePlan.<hash8>.json` built by `tree-plan` from `relationships.json`
   + `approvals.json`. Does not touch the upload Plan. Hash covers env and every action.
2. **Approvals (Ben-written, in `_import/approvals.json`):**
   `approved_relationships`: list of edge keys (`spouse:A:B`, `parent-child:CHILD:PARENT`, exactly as
   in relationships.json); `approved_creations`: list of candidate labels (as in `create_candidates`);
   `approved_tree_plan_hash`. Nothing is planned without its approval entry.
3. **Action kinds.**
   - `add-couple`: both ends linked (links.json agrees with the edge ids), neither living/merged,
     no couple relationship between them on FamilySearch (fresh read).
   - `add-child-parents`: edges grouped by child into ONE action with 1 or 2 parents
     (`TreeWriter.create_parent_child`). Child must currently have NO parents on FamilySearch; if it
     already has a relationship with one parent, the action is not planned and the report lists it
     "manual on the website".
   - `create-person`: only deceased candidates with a name and a birth or death year. Gender
     Unknown unless the relations node implies one (not guessed). Immediately before creating, search
     FamilySearch (name + years) and refuse on ANY plausible match (report it for linking instead).
     Born < 1900 or reviewed deceased only. Created id recorded in ledger; relationships to new people
     are a second plan after `links` picks them up (no in-plan dependencies).
4. **Executor.** New `treeexec.py` reuses `Ledger`, `RunLock`, `check_run_gates`, GateError from
   `execute.py` (production: `--allow-production`, `--only`, cap default 10 hard 25). Per action:
   fresh pre-check, intent line, write, read-back that proves the new state AND a negative control
   (the unrelated pair/person is not affected), result line. Failed/ambiguous keys block until
   `resolve` or `rollback`.
5. **Rollback.** Deletes only ledger-created ids: `delete_couple`, `delete_child_and_parents`,
   `delete_person` (person delete only if it has no relationships and nothing but our ledger writes),
   with id validation and absence read-back.
6. **CLI.** `tree-plan`, `tree-execute`, `tree-rollback`, `tree-resolve` in `__main__`; services built
   like `build_services` (Brave session must match env).
7. **Verification.** `tools/verify/tree_beta_drill.py` (needs beta login; written now, run later):
   create two fictional people, couple, child link, rollback, assert absence.

## Privacy

Repo contains code and fictional tests only. Plans, reports, ledger live in the archive `_import/`.
