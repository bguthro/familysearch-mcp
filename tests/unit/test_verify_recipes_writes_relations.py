"""Offline tests for the note and relationship write recipes against the stateful fake.

As test_verify_recipes_writes.py: the registered recipes run through the real
``run_recipe`` (no network, no cookies) against ``WriteTree``. Honest, each passes, is
recorded verified and leaves nothing behind; in each lie mode the recipe fails for
that reason, the registry is untouched and teardown still removes everything.
"""
import json
import shutil

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.verify import runner
from tools.verify.recipes import RECIPES, validate_recipes
from tools.verify.recipes import writes_common as wc
from tools.verify.recipes import writes_tree_notes as wtn
from tools.verify.recipes import writes_tree_relationships as wtr

from _fake_tree import CAPS, COUPLES, PERSONS, RELATIONSHIPS, WriteTree

NOTE_OPS = {
    "writes.tree.person_notes": ("createPersonNote", "updatePersonNote", "deletePersonNote"),
    "writes.tree.couple_notes": ("createCoupleRelationshipNote",
                                 "updateCoupleRelationshipNote",
                                 "deleteCoupleRelationshipNote"),
    "writes.tree.child_and_parents_notes": ("createChildAndParentsRelationshipNote",
                                            "updateChildAndParentsRelationshipNote",
                                            "deleteChildAndParentsRelationshipNote"),
}
RELATIONSHIP_OPS = {
    "writes.tree.relationship_create": ("createRelationship",),
    "writes.tree.couple_update": ("updateCoupleRelationship",),
    "writes.tree.child_and_parents_update": ("updateChildAndParentsRelationship",),
    "writes.tree.couple_delete_restore": ("deleteCoupleRelationship",
                                          "restoreCoupleRelationship"),
    "writes.tree.child_and_parents_delete_restore": ("deleteChildAndParentsRelationship",
                                                     "restoreChildAndParentsRelationship"),
    "writes.tree.relationship_parent_delete": ("deleteChildAndParentsRelationshipParent",),
}
OPS = {**NOTE_OPS, **RELATIONSHIP_OPS}
READ_BACKS = {"readPersonNotes", "readPersonNote", "getCoupleRelationshipNotes",
              "readCoupleRelationshipNote", "getChildAndParentsRelationshipNotes",
              "readChildAndParentRelationshipNote", "readCoupleRelationship",
              "getChildAndParentsRelationship", "readPersonSpouses", "readPersonParents",
              "readPersonChildren"}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


@pytest.fixture
def slept(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(wc, "sleep", pauses.append)
    return pauses


def run(name, reg_path, mode=None, **kw):
    tree = WriteTree(mode, **kw).serve()
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-26")
    return result, tree, ctx


def posts(prefix):
    return [json.loads(c.request.body) for c in responses.calls
            if c.request.method == "POST" and c.request.url.split("?")[0].startswith(prefix)]


# ---------------------------------------------------------------- registration


def test_recipes_registered_claiming_only_their_writes():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in wtn.WRITES_TREE_NOTES} == set(NOTE_OPS)
    assert {r.name for r in wtr.WRITES_TREE_RELATIONSHIPS} == set(RELATIONSHIP_OPS)
    for recipe in (*wtn.WRITES_TREE_NOTES, *wtr.WRITES_TREE_RELATIONSHIPS):
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & READ_BACKS
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__


# ---------------------------------------------------------------- honest fake


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, reg_path, slept):
    result, tree, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert ctx.negative_controls >= 2 if name in NOTE_OPS else ctx.negative_controls >= 1
    assert tree.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
@pytest.mark.parametrize("name, envelope", [
    ("writes.tree.person_notes", "persons"),
    ("writes.tree.couple_notes", "relationships"),
    ("writes.tree.child_and_parents_notes", "childAndParentsRelationships")])
def test_note_recipes_post_in_the_owners_envelope_and_update_by_id(name, envelope, reg_path,
                                                                    slept):
    result, tree, ctx = run(name, reg_path)
    assert result["passed"], result["problems"]
    assert ctx.negative_controls == 3  # create, update, delete
    sent = [b for b in posts(PERSONS) + posts(COUPLES) + posts(CAPS) if envelope in b
            and "notes" in b[envelope][0]]
    assert len(sent) == 3  # two creates, one update
    creates, update = sent[:2], sent[2]
    assert all("id" not in b[envelope][0]["notes"][0] for b in creates)
    note = update[envelope][0]["notes"][0]
    assert note["id"] and note["text"].startswith("ZZSPIKE")
    updates = [c.request.url for c in responses.calls if c.request.method == "POST"
               and "/notes/" in c.request.url]
    assert len(updates) == 1 and updates[0].endswith("/notes/" + note["id"])
    deletes = [c.request.url for c in responses.calls if c.request.method == "DELETE"
               and "/notes/" in c.request.url]
    assert deletes == [updates[0]]  # the updated note, by its id


@responses.activate
def test_relationship_create_posts_a_couple_then_one_child_and_parents(reg_path, slept):
    result, tree, ctx = run("writes.tree.relationship_create", reg_path)
    assert result["passed"], result["problems"]
    couple, cap = posts(RELATIONSHIPS)
    assert list(couple) == ["relationships"]
    rel, = couple["relationships"]
    assert rel["type"] == "http://gedcomx.org/Couple"
    assert {rel["person1"]["resourceId"], rel["person2"]["resourceId"]} <= set(tree.persons)
    assert list(cap) == ["childAndParentsRelationships"]
    rel, = cap["childAndParentsRelationships"]
    assert set(rel) == {"parent1", "parent2", "child"}
    assert all(rel[k]["resource"] == f"{PERSONS}/{rel[k]['resourceId']}" for k in rel)
    # Exactly the two posted relationships existed: no derived couple was left.
    assert len(tree.relationships) == 2
    assert ctx.negative_controls == 2


@responses.activate
def test_relationship_create_tracks_and_reports_a_derived_couple(reg_path, slept):
    result, tree, _ctx = run("writes.tree.relationship_create", reg_path, "duplicate_couple")
    assert not result["passed"]
    assert any("not created by this recipe" in p for p in result["problems"]), result
    assert len([r for r in tree.relationships.values() if r["kind"] == "couple"]) == 2
    assert tree.live() == []  # the derived couple was tracked and torn down too


@responses.activate
@pytest.mark.parametrize("name", ["writes.tree.child_and_parents_update",
                                  "writes.tree.relationship_parent_delete"])
def test_two_parent_fixture_recipes_sweep_up_a_derived_couple(name, reg_path, slept):
    # Not the op under test, so it does not fail the recipe; it must not be left behind.
    result, tree, _ctx = run(name, reg_path, "duplicate_couple")
    assert result["passed"], result["problems"]
    assert any("nobody asked for" in n for n in result["notes"]), result
    assert len([r for r in tree.relationships.values() if r["kind"] == "couple"]) == 2
    assert tree.live() == []


@responses.activate
def test_parent_delete_removes_parent2_of_a_two_parent_relationship(reg_path, slept):
    result, tree, _ctx = run("writes.tree.relationship_parent_delete", reg_path)
    assert result["passed"], result["problems"]
    [delete] = [c.request.url for c in responses.calls if c.request.method == "DELETE"
                and c.request.url.split("?")[0].endswith("/parent2")]
    assert delete.startswith(CAPS + "/")


@responses.activate
@pytest.mark.parametrize("name", ["writes.tree.couple_delete_restore",
                                  "writes.tree.child_and_parents_delete_restore"])
def test_delete_restore_restores_with_a_bare_post_and_deletes_again(name, reg_path, slept):
    result, tree, _ctx = run(name, reg_path)
    assert result["passed"], result["problems"]
    restores = [c.request for c in responses.calls if c.request.url.endswith("/restore")]
    assert len(restores) == 1 and not restores[0].body
    deletes = [p for m, p in tree.calls if m == "DELETE" and "-relationships/" in p]
    assert len(deletes) == 2 and deletes[0] == deletes[1]  # the recipe's, then teardown's


DELETE_RESTORE = ["writes.tree.couple_delete_restore",
                  "writes.tree.child_and_parents_delete_restore"]


@responses.activate
@pytest.mark.parametrize("name", DELETE_RESTORE)
def test_delete_restore_polls_a_lagging_restore_but_not_forever(name, reg_path, slept):
    attempts, pause = wc.EVENTUAL
    result, tree, _ctx = run(name, reg_path, restore_lag=attempts - 1)
    assert result["passed"], result["problems"]
    assert slept == [pause] * (attempts - 1)
    assert tree.live() == []
    slept.clear()
    result, tree, _ctx = run(name, reg_path, restore_lag=attempts)
    assert not result["passed"]
    assert any("readable again after the restore: not seen after" in p
               for p in result["problems"]), result["problems"]
    assert any(n.startswith("restore answered 204") for n in result["notes"])


@responses.activate
@pytest.mark.parametrize("name", DELETE_RESTORE)
@pytest.mark.parametrize("mode", ["restore_new_id", "restore_new_id_silent"])
def test_delete_restore_follows_a_restore_under_a_new_id(name, mode, reg_path, slept):
    # Found from the restore's X-ENTITY-ID, or else from the relatives list; tracked at
    # once (teardown deletes it), the old id is left as deleted.
    result, tree, _ctx = run(name, reg_path, mode)
    assert result["passed"], result["problems"]
    assert any("restored under a new id" in n for n in result["notes"]), result
    assert len(tree.relationships) == 2 and tree.live() == []


# ---------------------------------------------------------------- the fake lies


NOTE_LIES = {
    "note_ignore_create": "negative control failed",
    "note_ignore_update": "negative control failed",
    "note_update_creates": "negative control failed",
    "note_update_replaces": "note ids",
    "note_sibling_changed": "the other note",
    "note_wrong_delete": "negative control failed",
    "note_delete_noop": "negative control failed",
}
LIES = {
    **{(name, mode): text for name in NOTE_OPS for mode, text in NOTE_LIES.items()},
    ("writes.tree.relationship_create", "cap_creates_couples"):
        "not a child-and-parents relationship",
    ("writes.tree.relationship_create", "duplicate_couple"): "not created by this recipe",
    ("writes.tree.couple_update", "ignore_rel_update"): "negative control failed",
    ("writes.tree.couple_update", "rel_update_replaces"): "the earlier facts",
    ("writes.tree.child_and_parents_update", "ignore_rel_update"): "negative control failed",
    ("writes.tree.child_and_parents_update", "rel_update_replaces"): "parent1Facts",
    ("writes.tree.couple_delete_restore", "rel_delete_noop"): "negative control failed",
    ("writes.tree.couple_delete_restore", "restore_noop"): "readable again after the restore",
    ("writes.tree.child_and_parents_delete_restore", "rel_delete_noop"):
        "negative control failed",
    ("writes.tree.child_and_parents_delete_restore", "restore_noop"):
        "readable again after the restore",
    ("writes.tree.relationship_parent_delete", "parent_delete_noop"):
        "negative control failed",
    ("writes.tree.relationship_parent_delete", "parent_delete_wrong"):
        "negative control failed",
}


@responses.activate
@pytest.mark.parametrize("name, mode", sorted(LIES))
def test_recipe_fails_when_the_fake_lies(name, mode, reg_path, slept):
    raw = reg_path.read_bytes()
    result, tree, _ctx = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    problems = "\n".join(result["problems"])
    assert LIES[(name, mode)] in problems, problems
    assert reg_path.read_bytes() == raw  # nothing recorded
    assert tree.live() == []  # and still nothing left behind


@responses.activate
def test_spouse_links_instead_of_parent_child_are_all_tracked_and_torn_down(reg_path, slept):
    # The original library bug: the child-and-parents POST made child-parent couples.
    result, tree, _ctx = run("writes.tree.relationship_create", reg_path,
                             "cap_creates_couples")
    assert not result["passed"]
    couples = [r for r in tree.relationships.values() if r["kind"] == "couple"]
    assert len(couples) == 3 and tree.live() == []
    assert not any(p.startswith("teardown") for p in result["problems"]), result["problems"]
