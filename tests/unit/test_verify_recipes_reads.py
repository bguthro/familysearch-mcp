"""Offline tests for the read recipes against a scripted fake tree (no network, no cookies).

``FakeTree`` keeps the fixture family (dad, mom, child, their couple and child-and-parents
relationships) in memory and answers the person-centred reads in the GEDCOM X shapes the
recon recorded (docs/superpowers/recon/2026-10-25-api-surface/shapes.json) and the live
runs: relative lists answer 204 when empty, ``readPersons`` needs ``pids`` and answers
an unknown one 204, search (over real beta data, with the canary) answers 400 to no
parameter at all, change histories are atom feeds whose links carry the person id. For
the Task 4 reads it also keeps the relationships' records, feeds and notes, the person
note, the source (description, the child's reference to it) and the memory; only the
child has a note, a source and a memory, so the dad's lists answer 204 as in the recon. The
registered recipe objects run through the real ``run_recipe`` with the fixture world
replaced (``tools.verify.world.World``) by one that serves the fake's ids and names; each
``mode`` makes the fake lie in one way the recipes must catch.
"""
import json
import re
import shutil

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.spec.resolve import SNAPSHOT_PATH, load_overrides, resolve
from tools.verify import reads, runner, world
from tools.verify.recipes import RECIPES, validate_recipes
from tools.verify.recipes import reads_tree_persons as rtp
from tools.verify.recipes import reads_tree_relations as rtr

# The fake tree lives in tests/unit/_fake_tree.py (shared with the write recipe tests).
from _fake_tree import (ATOM_JSON, BETA, COLLECTION, FS_JSON, GIVEN, IDS, MEMORY, NOTE_IDS,
                        NOTE_TEXT, SOURCE, SURNAME, TREE, FakeTree)

PID_FORMAT = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")
OWN_RECIPES = {r.name for r in rtp.READS_TREE_PERSONS}
SCOPE_PREFIXES = ("/tree/persons", "/tree/search", "/tree/matches", "/tree/current-person")
# Sub-resources of a person that Task 4 verifies (with the other relationship reads).
TASK4_SEGMENTS = {"notes", "sources", "source-references", "memories", "discussions",
                  "comments", "collections"}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


class FakeWorld:
    """The fixture world as the fake tree knows it (what tools/verify/world.py would hold)."""

    def __init__(self, client, fixtures, reason, unique):
        self.ids = {**IDS, **NOTE_IDS, "source_description": SOURCE[0], "memory": MEMORY[0],
                    "collection": COLLECTION[0]}
        self.names = {f"{role}_name": f"{given} {SURNAME}" for role, given in GIVEN.items()}
        self.names.update({f"{key}_text": text for key, text in NOTE_TEXT.items()})
        self.names.update(source_title=SOURCE[1], memory_title=MEMORY[1],
                          collection_title=COLLECTION[1])
        self.needed: list[str] = []

    def need(self, *groups):
        self.needed.extend(groups)


@pytest.fixture
def fake_world(monkeypatch):
    # The registered recipes use default_world, which builds tools.verify.world.World:
    # replace that, so the very objects in RECIPES run (no test world factory).
    monkeypatch.setattr(world, "World", FakeWorld)
    slept: list[float] = []
    monkeypatch.setattr(reads, "sleep", slept.append)  # eventual polling, without waiting
    return slept


def run(name, reg_path, mode=None, **kw):
    tree = FakeTree(mode, **kw)
    tree.serve()
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    return runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                             today="2026-10-25"), tree


# ------------------------------------------------------------------ the fake passes


@responses.activate
@pytest.mark.parametrize("name", sorted(OWN_RECIPES))
def test_recipe_passes_against_the_honest_fake(name, reg_path, fake_world):
    result, _tree = run(name, reg_path)
    notes = result.pop("notes", [])
    assert result == {"recipe": name, "passed": True, "problems": []}
    # Only a polled case reports its attempts (the honest fake answers at once).
    polled = [c.op for r in rtp.READS_TREE_PERSONS if r.name == name
              for c in (rtp.RELATIVES_CASES if name == "reads.tree.relatives" else ())
              if c.eventual]
    assert notes == [f"{op}: content after 1 of {rtp.RELATIONSHIP_EVENTUAL[0]} attempts"
                     for op in polled]
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        entry = registry[op]["generated"]
        assert entry["status"] == "verified" and entry["evidence"] == "read-content"
        assert entry["recipe"] == name


@responses.activate
def test_search_reads_the_canary_and_its_control_is_an_empty_query(reg_path, fake_world):
    # Fresh fixtures are never indexed (live runs 1-3): the case reads a stable real
    # beta record instead, anchor-free, and asks the server one question it must refuse.
    assert "searchTreePersons" not in rtp.DEFERRED
    case = next(c for c in rtp.SEARCH_CASES if c.op == "searchTreePersons")
    assert case.anchor_free_reason and not case.needs and case.eventual is None
    assert set(case.literal_anchors) == {
        rtp.CANARY_ID, rtp.CANARY_NAME, rtp.CANARY_BIRTH_DATE}
    result, tree = run("reads.tree.search", reg_path)
    assert result["passed"] is True, result["problems"]
    positive, control = tree.search_params
    assert positive == {k: str(v) for k, v in rtp.CANARY_QUERY.items()}
    assert positive["flag"] == "fst" and positive["count"] == str(rtp.CANARY_COUNT)
    assert control == {}  # no parameter at all: beta answers 400


def test_current_tree_person_stays_deferred_for_want_of_a_control():
    # A discovered anchor (readCurrentUser's personId) would be sound, but the operation
    # takes no parameter, so no request to it can be a not-found control.
    reason = rtp.DEFERRED["readCurrentTreePerson"]
    assert "control" in reason and "no path or query parameter" in reason


@responses.activate
@pytest.mark.parametrize("lie", ["ignore_count", "other_first", "no_confidence"])
def test_search_fails_when_the_canary_answer_is_wrong(lie, reg_path, fake_world):
    result, _tree = run("reads.tree.search", reg_path, search_lie=lie)
    assert result["passed"] is False, lie


@responses.activate
def test_find_relationship_control_is_an_empty_answer_for_an_unknown_opid(
        reg_path, fake_world):
    case = next(c for c in rtp.RELATIVES_CASES if c.op == "findRelationship")
    assert case.control_may_be_empty and "204" in case.control_may_be_empty
    assert case.eventual == rtp.RELATIONSHIP_EVENTUAL
    result, _tree = run("reads.tree.relatives", reg_path)
    assert result["passed"] is True, result["problems"]
    controls = [c for c in responses.calls if c.request.url.endswith(
        f"/relationships/{rtp.NO_SUCH_PERSON}")]
    assert len(controls) == 1 and controls[0].response.status_code == 204


@responses.activate
def test_read_persons_control_is_an_empty_answer_for_an_unknown_pid(reg_path, fake_world):
    # Live (runs 1 and 2), beta answers any unknown pid with an empty 204, which the
    # spec omits; the case says so, and its positive call is anchored content first.
    case = next(c for c in rtp.PERSONS_CASES if c.op == "readPersons")
    assert case.control_may_be_empty and "204" in case.control_may_be_empty
    result, _tree = run("reads.tree.persons", reg_path)
    assert result["passed"] is True, result["problems"]
    controls = [c.request.url for c in responses.calls
                if c.request.url.startswith(TREE + "/persons?")][1:]
    assert len(controls) == 1 and rtp.NO_SUCH_PERSON in controls[0]
    assert PID_FORMAT.match(rtp.NO_SUCH_PERSON)  # well-formed, just absent


@responses.activate
def test_find_relationship_waits_for_the_calculation_but_not_forever(reg_path, fake_world):
    attempts, pause = rtp.RELATIONSHIP_EVENTUAL
    result, _tree = run("reads.tree.relatives", reg_path, relationship_lag=attempts - 1)
    assert result["passed"] is True, result["problems"]
    assert fake_world == [pause] * (attempts - 1)
    result, _tree = run("reads.tree.relatives", reg_path, relationship_lag=attempts)
    problems = "\n".join(result["problems"])
    assert result["passed"] is False and "findRelationship: positive call answered 204" in problems


@responses.activate
def test_failures_carry_the_response_skeleton(reg_path, fake_world):
    result, _tree = run("reads.tree.relatives", reg_path, "other_person")
    problems = "\n".join(result["problems"])
    assert "response: 200" in problems and GIVEN["dad"] in problems  # the names shown


# ------------------------------------------------------------------ the fake lies

LIES = {
    "other_person": OWN_RECIPES,
    "catch_all": OWN_RECIPES,
    "ignore_pids": {"reads.tree.persons"},
    "empty": {"reads.tree.persons", "reads.tree.relatives", "reads.tree.search",
              "reads.tree.history"},
    "stale_head": {"reads.tree.persons", "reads.tree.history"},
    "families_as_parents": {"reads.tree.relatives"},
    "ignore_opid": {"reads.tree.relatives"},
}
# What each lie is caught by (so a recipe cannot fail for an unrelated reason).
CAUGHT_BY = {
    "other_person": "anchor",
    "catch_all": "not-found control answered",
    "ignore_pids": "not the requested",
    "empty": "answered 204",
    "stale_head": "validator",
    "families_as_parents": "readPersonFamilies: anchor",
    "ignore_opid": "findRelationship: not-found control answered 200",
}


@responses.activate
@pytest.mark.parametrize("mode, name", sorted(
    (mode, name) for mode, names in LIES.items() for name in names))
def test_recipe_fails_when_the_fake_lies(mode, name, reg_path, fake_world):
    raw = reg_path.read_bytes()
    result, _tree = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    assert CAUGHT_BY[mode] in "\n".join(result["problems"]), result["problems"]
    assert reg_path.read_bytes() == raw  # nothing recorded


@responses.activate
def test_204_for_the_childless_variant_only_counts_after_the_populated_one(
        reg_path, fake_world):
    # The empty variants (child's children and spouses, dad's parents) pass with the
    # honest fake; with every list empty, the populated cases fail and so do they.
    result, _tree = run("reads.tree.relatives", reg_path, "empty")
    problems = "\n".join(result["problems"])
    assert "populated" in problems and "readPersonChildren" in problems


# ------------------------------------------------------------------ coverage


def _scope() -> set[str]:
    """This task's reads, from the snapshot: person-centred Family Tree reads."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    ops, _excluded = resolve(snapshot, load_overrides())
    scope = set()
    for op in ops:
        if op.module != "family_tree" or op.kind != "read":
            continue
        if not op.path.startswith(SCOPE_PREFIXES):
            continue
        segments = set(op.path.split("/"))
        if segments & TASK4_SEGMENTS:
            continue
        scope.add(op.operation_id)
    return scope - {"readPerson"}  # verified by the pilot


def test_recipes_cover_exactly_the_person_reads_minus_deferred():
    scope = _scope()
    covered = {op for r in rtp.READS_TREE_PERSONS for op in r.ops}
    assert "readPerson" not in covered and "readPerson" not in rtp.DEFERRED
    assert not covered & set(rtp.DEFERRED), covered & set(rtp.DEFERRED)
    assert covered | set(rtp.DEFERRED) == scope, (
        f"uncovered: {sorted(scope - covered - set(rtp.DEFERRED))}; "
        f"outside scope: {sorted((covered | set(rtp.DEFERRED)) - scope)}")
    for op, reason in rtp.DEFERRED.items():
        assert isinstance(reason, str) and len(reason.split()) >= 8, op
    # findRelationship lives under /tree/persons and is person-centred: this task's.
    assert "findRelationship" in covered


def test_head_cases_compare_with_their_get():
    heads = [op for op in _scope() if OPERATIONS[op].http_method == "HEAD"]
    covered = {op for r in rtp.READS_TREE_PERSONS for op in r.ops}
    assert set(heads) <= covered | set(rtp.DEFERRED)


def test_recipes_registered_and_reference_real_read_ops():
    assert validate_recipes(RECIPES) == []
    for recipe in rtp.READS_TREE_PERSONS:
        assert RECIPES[recipe.name] is recipe
        assert len(recipe.ops) <= 12
    seen: dict[str, str] = {}
    for recipe in RECIPES.values():
        if recipe.evidence != "read-content":
            continue
        for op in recipe.ops:
            assert op in OPERATIONS and OPERATIONS[op].kind == "read", op
            assert op not in seen, f"{op} in both {seen[op]} and {recipe.name}"
            seen[op] = recipe.name
    assert len({r.name for r in RECIPES.values()}) == len(RECIPES)


# ------------------------------------------------------------------ Task 4: relations

RELATION_RECIPES = {r.name for r in rtr.READS_TREE_RELATIONS}
RELATION_CASES = {
    "reads.tree.relationships": rtr.RELATIONSHIPS_CASES,
    "reads.tree.relationship_history": rtr.RELATIONSHIP_HISTORY_CASES,
    "reads.tree.pedigree": rtr.PEDIGREE_CASES,
    "reads.tree.notes": rtr.NOTES_CASES,
    "reads.tree.couple_notes": rtr.COUPLE_NOTES_CASES,
    "reads.tree.child_and_parents_notes": rtr.CHILD_AND_PARENTS_NOTES_CASES,
    "reads.tree.sources": rtr.SOURCES_CASES,
    "reads.tree.source_queries": rtr.SOURCE_QUERIES_CASES,
    "reads.tree.memories": rtr.MEMORIES_CASES,
    "reads.sources.collection": rtr.COLLECTION_CASES,
    "reads.sources.collection_descriptions": rtr.COLLECTION_DESCRIPTIONS_CASES,
}
# Only the groups each recipe reads: the couple and child-and-parents note envelopes are
# unproven on beta, so each has a recipe of its own, apart from the person note and source.
RELATION_NEEDS = {
    "reads.tree.relationships": {"persons", "couple"},
    "reads.tree.relationship_history": {"persons", "couple"},
    "reads.tree.pedigree": {"persons", "couple"},
    "reads.tree.notes": {"person_note"},
    "reads.tree.couple_notes": {"couple_note"},
    "reads.tree.child_and_parents_notes": {"child_and_parents_note"},
    "reads.tree.sources": {"source"},
    "reads.tree.source_queries": {"source"},
    "reads.tree.memories": {"memory"},
    "reads.sources.collection": {"collection"},
    "reads.sources.collection_descriptions": {"collection"},
}


def test_relation_recipes_declare_only_the_groups_they_read():
    assert set(RELATION_CASES) == RELATION_RECIPES
    for name, cases in RELATION_CASES.items():
        assert {g for c in cases for g in c.needs} == RELATION_NEEDS[name], name
        assert set(RECIPES[name].ops) == {c.op for c in cases}
        assert all(g in world.GROUPS for g in RELATION_NEEDS[name])


@responses.activate
@pytest.mark.parametrize("name", sorted(RELATION_RECIPES))
def test_relation_recipe_passes_against_the_honest_fake(name, reg_path, fake_world):
    result, _tree = run(name, reg_path)
    # Only the polled cases (the person -> source reads) report attempts.
    polled = [c.op for c in RELATION_CASES[name] if c.eventual]
    assert result.pop("notes", []) == [
        f"{op}: content after 1 of {rtr.REFERENCE_EVENTUAL[0]} attempts" for op in polled]
    assert result == {"recipe": name, "passed": True, "problems": []}
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        entry = registry[op]["generated"]
        assert entry["status"] == "verified" and entry["evidence"] == "read-content"
        assert entry["recipe"] == name


@responses.activate
def test_relationship_feed_heads_are_sent_with_the_atom_accept(reg_path, fake_world):
    # HEAD documents no Accept; beta answers 406 to the fs-v1 json the feeds do not serve.
    result, _tree = run("reads.tree.relationship_history", reg_path)
    assert result["passed"] is True, result["problems"]
    heads = [c.request for c in responses.calls if c.request.method == "HEAD"]
    assert len(heads) == 4  # each HEAD case: positive and control
    assert all(h.headers["Accept"] == ATOM_JSON for h in heads)


@responses.activate
def test_relationship_heads_are_sent_with_their_gets_accept(reg_path, fake_world):
    result, _tree = run("reads.tree.relationships", reg_path)
    assert result["passed"] is True, result["problems"]
    heads = [c.request for c in responses.calls if c.request.method == "HEAD"]
    assert heads and all(h.headers["Accept"] == FS_JSON for h in heads)


@responses.activate
def test_single_note_controls_send_an_unknown_note_of_the_real_owner(reg_path, fake_world):
    result, _tree = run("reads.tree.notes", reg_path)
    assert result["passed"] is True, result["problems"]
    control = [c.request.url for c in responses.calls if rtr.NO_SUCH_NOTE in c.request.url]
    assert control == [f"{TREE}/persons/{IDS['child']}/notes/{rtr.NO_SUCH_NOTE}"]


@responses.activate
def test_source_queries_send_the_fixture_ids_and_anchor_on_what_comes_back(
        reg_path, fake_world):
    result, _tree = run("reads.tree.source_queries", reg_path)
    assert result["passed"] is True, result["problems"]
    urls = [c.request.url for c in responses.calls]
    assert f"{TREE}/source-references?description={SOURCE[0]}" in urls
    assert f"{BETA}/platform/sources/descriptions?sdids={SOURCE[0]}" in urls
    # Live run 2: beta ignores the uid of the user's source box, so no control exists.
    assert "readUserSourceDescriptions" in rtr.DEFERRED
    assert not any("/collections/descriptions" in u for u in urls)


def test_only_the_person_to_source_and_membership_reads_poll():
    polled = {c.op for cases in RELATION_CASES.values() for c in cases if c.eventual}
    # A source just moved into the fixture folder may show late, as the attach did.
    assert polled == {"readPersonSources", "readSourceReferences",
                      "readUserDefinedCollectionSourceDescriptions"}
    empty_controls = {c.op for cases in RELATION_CASES.values() for c in cases
                      if c.control_may_be_empty}
    assert empty_controls == {"readSourceDescriptions", "readSourceReferences"}
    for case in rtr.SOURCE_QUERIES_CASES:
        assert "204" in case.control_may_be_empty and case.anchors
    # The one HEAD without validators on beta, and only it, proves existence only.
    optional = {c.op for cases in RELATION_CASES.values() for c in cases
                if c.validators_optional}
    assert optional == {"readSourceDescriptionHead"}


@responses.activate
def test_unknown_description_control_is_an_empty_answer(reg_path, fake_world):
    result, _tree = run("reads.tree.source_queries", reg_path)
    assert result["passed"] is True, result["problems"]
    [control] = [c for c in responses.calls if c.request.url.endswith(
        f"/source-references?description={rtr.NO_SUCH_SOURCE}")]
    assert control.response.status_code == 204 and not control.response.text


@responses.activate
def test_source_head_passes_on_existence_without_validators(reg_path, fake_world):
    result, _tree = run("reads.tree.sources", reg_path)
    assert result["passed"] is True, result["problems"]
    heads = [c.response for c in responses.calls if c.request.method == "HEAD"]
    assert heads and not any("ETag" in h.headers for h in heads)
    assert all("Content-Location" in h.headers for h in heads)


@responses.activate
@pytest.mark.parametrize("name", ["reads.tree.sources", "reads.tree.source_queries"])
def test_source_reference_reads_wait_for_the_attach_but_not_forever(
        name, reg_path, fake_world):
    attempts, pause = rtr.REFERENCE_EVENTUAL
    result, _tree = run(name, reg_path, reference_lag=attempts - 1)
    assert result["passed"] is True, result["problems"]
    assert fake_world == [pause] * (attempts - 1)
    result, _tree = run(name, reg_path, reference_lag=attempts)
    problems = "\n".join(result["problems"])
    assert result["passed"] is False and "positive call answered 204" in problems


@responses.activate
def test_unmatched_sdids_control_is_the_documented_empty_answer(reg_path, fake_world):
    result, _tree = run("reads.tree.source_queries", reg_path)
    assert result["passed"] is True, result["problems"]
    [control] = [c for c in responses.calls
                 if c.request.url.endswith(f"/sources/descriptions?sdids={rtr.NO_SUCH_SOURCE}")]
    assert control.response.status_code == 204 and not control.response.text


RELATION_LIES = {
    "other_person": {"reads.tree.relationships", "reads.tree.relationship_history",
                     "reads.tree.pedigree", "reads.tree.notes", "reads.tree.sources",
                     "reads.tree.memories"},
    "other_record": {"reads.tree.notes", "reads.tree.couple_notes",
                     "reads.tree.child_and_parents_notes", "reads.tree.sources",
                     "reads.tree.source_queries", "reads.tree.memories",
                     "reads.sources.collection", "reads.sources.collection_descriptions"},
    "swap_relationships": {"reads.tree.relationships", "reads.tree.relationship_history",
                           "reads.tree.couple_notes", "reads.tree.child_and_parents_notes"},
    "catch_all": RELATION_RECIPES,
    "empty": RELATION_RECIPES,
    "stale_head": {"reads.tree.relationships", "reads.tree.relationship_history"},
    "wrong_location": {"reads.tree.sources"},
    "pedigree_swap": {"reads.tree.pedigree"},
    "ignore_nid": {"reads.tree.notes", "reads.tree.couple_notes",
                   "reads.tree.child_and_parents_notes"},
    "ignore_sdids": {"reads.tree.source_queries"},
}
# What each lie is caught by: any of these (a person swapped out of a list read leaves
# the dad's empty 204, of a relationship read a missing anchor).
RELATION_CAUGHT_BY = {
    "other_person": ("anchor", "positive call answered 204"),
    "other_record": ("anchor",),
    "swap_relationships": ("anchor", "no relationships in the response", "is not the"),
    "catch_all": ("not-found control answered 200",),
    "empty": ("positive call answered 204",),
    "stale_head": ("validator",),
    "wrong_location": ("readSourceDescriptionHead: HEAD and readSourceDescription answer "
                       "another Content-Location",),
    "pedigree_swap": ("anchor",),
    "ignore_nid": ("not-found control answered 200",),
    "ignore_sdids": ("not the requested",),
}


@responses.activate
@pytest.mark.parametrize("mode, name", sorted(
    (mode, name) for mode, names in RELATION_LIES.items() for name in names))
def test_relation_recipe_fails_when_the_fake_lies(mode, name, reg_path, fake_world):
    raw = reg_path.read_bytes()
    result, _tree = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    problems = "\n".join(result["problems"])
    assert any(s in problems for s in RELATION_CAUGHT_BY[mode]), result["problems"]
    assert reg_path.read_bytes() == raw  # nothing recorded


@responses.activate
def test_every_head_case_fails_on_a_stale_validator(reg_path, fake_world):
    # Each HEAD (not only one per recipe) is judged against its own GET's validator,
    # except the source description's, which beta answers without one (existence only).
    heads = {c.op for cases in RELATION_CASES.values() for c in cases
             if OPERATIONS[c.op].http_method == "HEAD" and not c.validators_optional}
    failed = set()
    for name in ("reads.tree.relationships", "reads.tree.relationship_history"):
        result, _tree = run(name, reg_path, "stale_head")
        problems = "\n".join(result["problems"])
        failed |= {op for op in heads if f"{op}: HEAD and" in problems}
    assert failed == heads


@responses.activate
def test_empty_variants_only_count_after_their_populated_case(reg_path, fake_world):
    # The dad's notes, sources and memories are 204 (recon); with every list empty the
    # populated cases fail, and so do the empty variants that lean on them.
    for name, op in (("reads.tree.notes", "readPersonNotes"),
                     ("reads.tree.sources", "readPersonSources"),
                     ("reads.tree.memories", "getPersonMemories")):
        result, _tree = run(name, reg_path, "empty")
        problems = "\n".join(result["problems"])
        assert f"{op}: 204 (empty) accepted only after a populated" in problems, problems


def _family_tree_reads() -> set[str]:
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    ops, _excluded = resolve(snapshot, load_overrides())
    return {o.operation_id for o in ops if o.module == "family_tree" and o.kind == "read"}


def test_relation_recipes_cover_exactly_the_rest_of_the_family_tree_reads():
    every = _family_tree_reads()
    scope = every - _scope() - {"readPerson"}
    covered = {op for r in rtr.READS_TREE_RELATIONS for op in r.ops}
    deferred = set(rtr.DEFERRED)
    assert not covered & deferred, covered & deferred
    assert covered | deferred == scope, (
        f"uncovered: {sorted(scope - covered - deferred)}; "
        f"outside scope: {sorted((covered | deferred) - scope)}")
    # No overlap with Task 3, and together (with the pilot's readPerson) every read.
    task3 = {op for r in rtp.READS_TREE_PERSONS for op in r.ops} | set(rtp.DEFERRED)
    assert not (covered | deferred) & task3
    assert "readPerson" not in covered | deferred | task3
    assert covered | deferred | task3 | {"readPerson"} == every
    for op, reason in rtr.DEFERRED.items():
        assert isinstance(reason, str) and len(reason.split()) >= 12, op
        assert OPERATIONS[op].kind == "read", op


def test_relation_heads_compare_with_their_get():
    for cases in RELATION_CASES.values():
        for case in cases:
            if OPERATIONS[case.op].http_method == "HEAD":
                assert OPERATIONS[case.compare_with].path == OPERATIONS[case.op].path


def test_relation_recipes_registered():
    assert validate_recipes(RECIPES) == []
    for recipe in rtr.READS_TREE_RELATIONS:
        assert RECIPES[recipe.name] is recipe
        assert len(recipe.ops) <= 12


@responses.activate
def test_collection_reads_send_only_the_collection_id_and_anchor_on_titles(reg_path,
                                                                          fake_world):
    for name in ("reads.sources.collection", "reads.sources.collection_descriptions"):
        result, _tree = run(name, reg_path)
        assert result["passed"] is True, result["problems"]
    urls = [c.request.url for c in responses.calls]
    base = f"{BETA}/platform/sources/collections"
    assert f"{base}/{COLLECTION[0]}" in urls and f"{base}/{COLLECTION[0]}/descriptions" in urls
    # The controls: an unknown collection, which beta answers 400 (recon).
    assert f"{base}/{rtr.NO_SUCH_COLLECTION}" in urls
    assert f"{base}/{rtr.NO_SUCH_COLLECTION}/descriptions" in urls
    for case in (*rtr.COLLECTION_CASES, *rtr.COLLECTION_DESCRIPTIONS_CASES):
        assert case.anchors and case.control_may_be_empty is None
    # The uid-bearing and parameterless source box reads stay deferred.
    for op in ("readUserSourceFolders", "readSourceFolders", "readUserSourceDescriptions",
               "getSourceDescriptionChanges"):
        assert op in rtr.DEFERRED
    assert not any("/sources/current" in u or "/collections/descriptions" in u
                   for u in urls if "/platform/sources/collections/" not in u)
