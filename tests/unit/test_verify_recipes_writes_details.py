"""Offline tests for the relationship detail write recipes against the stateful fake.

As test_verify_recipes_writes_relations.py: the registered recipes run through the real
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
from tools.verify.recipes import writes_tree_relationship_details as wtd

from _fake_tree import CAPS, COUPLES, DESCRIPTIONS, PERSONS, WriteTree

OPS = {
    "writes.tree.couple_source_references": ("createCoupleRelationshipSourceReference",
                                             "deleteCoupleRelationshipSourceReference"),
    "writes.tree.child_and_parents_source_references": (
        "createChildAndParentsRelationshipSourceReference",
        "deleteChildAndParentsRelationshipSourceReference"),
    "writes.tree.couple_conclusion_delete": ("deleteCoupleRelationshipConclusion",),
    "writes.tree.child_and_parents_conclusion_delete": (
        "deleteChildAndParentsRelationshipConclusion",),
    "writes.tree.couple_spouses_order": ("updateCoupleRelationshipSpousesOrder",),
    "writes.tree.child_and_parents_parents_order": (
        "updateChildAndParentsRelationshipParentsOrder",),
}
REFERENCES = ["writes.tree.couple_source_references",
              "writes.tree.child_and_parents_source_references"]
# Verified reads and writes the recipes use without claiming them.
UNCLAIMED = {"readSourceReferences", "readCoupleRelationship",
             "getChildAndParentsRelationship", "readPersonSpouses", "readPersonParents",
             "updateCoupleRelationship", "updateChildAndParentsRelationship",
             "readCoupleRelationshipSourceReferences", "readCoupleRelationshipSources",
             "getChildAndParentsRelationshipSourceReferences",
             "getChildAndParentsRelationshipSources"}


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


def requests_to(method, suffix):
    return [c.request for c in responses.calls if c.request.method == method
            and c.request.url.split("?")[0].endswith(suffix)]


# ---------------------------------------------------------------- registration


def test_recipes_registered_claiming_only_their_writes():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in wtd.WRITES_TREE_RELATIONSHIP_DETAILS} == set(OPS)
    for recipe in wtd.WRITES_TREE_RELATIONSHIP_DETAILS:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__
    assert wtd.DEFERRED == {}  # nothing deferred before a live run says so


def test_planting_writes_are_verified_so_unclaimed_calls_pass_the_lock():
    registry = json.loads(REGISTRY_PATH.read_text())["operations"]
    for op in ("updateCoupleRelationship", "updateChildAndParentsRelationship",
               "readSourceReferences", "readCoupleRelationship",
               "getChildAndParentsRelationship"):
        assert registry[op]["generated"]["status"] == "verified", op


# ---------------------------------------------------------------- honest fake


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, reg_path, slept):
    result, tree, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert ctx.negative_controls == (3 if name in REFERENCES else 1)
    assert tree.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
@pytest.mark.parametrize("name, base, envelope", [
    ("writes.tree.couple_source_references", COUPLES, "relationships"),
    ("writes.tree.child_and_parents_source_references", CAPS,
     "childAndParentsRelationships")])
def test_source_references_post_in_the_envelope_and_delete_the_first_by_srid(
        name, base, envelope, reg_path, slept):
    result, tree, _ctx = run(name, reg_path)
    assert result["passed"], result["problems"]
    posted = requests_to("POST", "/source-references")
    assert len(posted) == 2 and all(r.url.startswith(base + "/") for r in posted)
    bodies = [json.loads(r.body) for r in posted]
    for body in bodies:
        assert list(body) == [envelope]
        [ref] = body[envelope][0]["sources"]
        assert set(ref) == {"description"} and ref["description"].startswith(DESCRIPTIONS)
    [delete] = [r for r in responses.calls if r.request.method == "DELETE"
                and "/source-references/" in r.request.url]
    assert delete.request.url.split("?")[0].rsplit("/", 1)[-1] == "SRWZ-001"  # the first
    assert all(not url.startswith(PERSONS) for url in [r.url for r in posted])


@responses.activate
@pytest.mark.parametrize("name", REFERENCES)
def test_source_references_poll_a_lagging_citation_but_not_forever(name, reg_path, slept):
    attempts, pause = wc.EVENTUAL
    result, tree, _ctx = run(name, reg_path, reference_lag=attempts - 1,
                             resource_sources=False)
    assert result["passed"], result["problems"]
    assert slept and set(slept) == {pause}
    assert tree.live() == []
    slept.clear()
    result, tree, _ctx = run(name, reg_path, reference_lag=attempts, resource_sources=False)
    assert not result["passed"]
    assert any("not seen after" in p for p in result["problems"]), result["problems"]
    assert tree.live() == []


@responses.activate
@pytest.mark.parametrize("name", REFERENCES)
def test_source_references_read_back_from_readsourcereferences_alone(name, reg_path, slept):
    # Should beta's relationship resource not answer its sources, the citations carry it.
    result, _tree, _ctx = run(name, reg_path, resource_sources=False)
    assert result["passed"], result["problems"]


@responses.activate
@pytest.mark.parametrize("name", REFERENCES)
@pytest.mark.parametrize("resource_sources", [True, False])
def test_source_references_delete_the_reference_read_back_not_the_entity_id(
        name, resource_sources, reg_path, slept):
    # Should X-ENTITY-ID name the relationship, the srid still comes from the read-backs.
    result, tree, _ctx = run(name, reg_path, owner_entity_id=True,
                             resource_sources=resource_sources)
    assert result["passed"], result["problems"]
    [delete] = [c.request.url for c in responses.calls if c.request.method == "DELETE"
                and "/source-references/" in c.request.url]
    assert delete.split("?")[0].endswith("/source-references/SRWZ-001")
    assert any("Location=" in n and "X-ENTITY-ID=" in n for n in result["notes"])
    assert tree.live() == []


@responses.activate
@pytest.mark.parametrize("name", REFERENCES)
def test_source_references_fall_back_to_the_location_without_read_back_ids(
        name, reg_path, slept, monkeypatch):
    # Citations and resource name no reference ids: the Location's srid is deleted.
    monkeypatch.setattr(wc, "_reference_sdid", lambda ref: "not-the-description")
    result, _tree, _ctx = run(name, reg_path, owner_entity_id=True)
    assert result["passed"], result["problems"]
    [delete] = [c.request.url for c in responses.calls if c.request.method == "DELETE"
                and "/source-references/" in c.request.url]
    assert delete.split("?")[0].endswith("/source-references/SRWZ-001")


@responses.activate
@pytest.mark.parametrize("name", REFERENCES)
def test_source_references_read_back_from_the_resource_alone(name, reg_path, slept,
                                                             monkeypatch):
    # Should readSourceReferences list persons only, the relationship's resource carries it.
    monkeypatch.setattr(wc, "CITING", {"persons": "person"})
    result, _tree, _ctx = run(name, reg_path)
    assert result["passed"], result["problems"]


@responses.activate
def test_child_and_parents_conclusion_delete_uses_the_parent1_role(reg_path, slept):
    result, _tree, _ctx = run("writes.tree.child_and_parents_conclusion_delete", reg_path)
    assert result["passed"], result["problems"]
    [delete] = [c.request.url for c in responses.calls if c.request.method == "DELETE"
                and "/conclusions/" in c.request.url]
    assert delete.startswith(CAPS + "/") and "/parent1/conclusions/" in delete


@responses.activate
@pytest.mark.parametrize("name, suffix, roles", [
    ("writes.tree.couple_spouses_order", "/spouses/order", ("person1", "person2")),
    ("writes.tree.child_and_parents_parents_order", "/parents/order",
     ("parent1", "parent2"))])
def test_order_posts_the_relationships_own_persons_swapped(name, suffix, roles, reg_path,
                                                           slept):
    result, tree, _ctx = run(name, reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", suffix)
    [entry] = next(iter(json.loads(post.body).values()))
    assert set(entry) == set(roles)
    posted = [entry[r]["resourceId"] for r in roles]
    assert posted[0] != posted[1] and set(posted) <= set(tree.persons)


@responses.activate
@pytest.mark.parametrize("name", ["writes.tree.couple_spouses_order",
                                  "writes.tree.child_and_parents_parents_order"])
def test_order_fixtures_have_one_gender_so_no_traditional_order_applies(name, reg_path,
                                                                        slept):
    result, tree, _ctx = run(name, reg_path)
    assert result["passed"], result["problems"]
    [post] = [c.request for c in responses.calls if c.request.url.endswith("/order")]
    [entry] = next(iter(json.loads(post.body).values()))
    genders = {tree.persons[ref["resourceId"]]["gender"]["type"] for ref in entry.values()}
    assert len(genders) == 1


@responses.activate
@pytest.mark.parametrize("name", ["writes.tree.child_and_parents_conclusion_delete",
                                  "writes.tree.child_and_parents_parents_order"])
def test_two_parent_fixture_recipes_sweep_up_a_derived_couple(name, reg_path, slept):
    result, tree, _ctx = run(name, reg_path, "duplicate_couple")
    assert result["passed"], result["problems"]
    assert any("nobody asked for" in n for n in result["notes"]), result
    assert tree.live() == []


# ---------------------------------------------------------------- the fake lies


LIES = {
    **{(name, mode): text for name in REFERENCES for mode, text in {
        "rel_ref_ignored": "citing source",
        "rel_ref_to_person": "citing source",
        "rel_ref_delete_noop": "no longer citing source",
        "rel_ref_delete_wrong": "no longer citing source",
        "rel_ref_delete_all": "the other source",
    }.items()},
    ("writes.tree.couple_conclusion_delete", "rel_conclusion_noop"):
        "negative control failed",
    ("writes.tree.couple_conclusion_delete", "rel_conclusion_wrong"):
        "negative control failed",
    ("writes.tree.couple_conclusion_delete", "rel_conclusion_all"):
        "the couple's other fact",
    ("writes.tree.couple_conclusion_delete", "ignore_rel_update"): "expected the update",
    ("writes.tree.child_and_parents_conclusion_delete", "rel_conclusion_noop"):
        "negative control failed",
    ("writes.tree.child_and_parents_conclusion_delete", "rel_conclusion_wrong"):
        "negative control failed",
    ("writes.tree.child_and_parents_conclusion_delete", "rel_conclusion_all"):
        "parent2Facts",
    ("writes.tree.couple_spouses_order", "order_noop"): "negative control failed",
    ("writes.tree.couple_spouses_order", "order_drops"): "swapped",
    ("writes.tree.couple_spouses_order", "ignore_rel_update"): "planted Marriage",
    ("writes.tree.child_and_parents_parents_order", "order_noop"):
        "negative control failed",
    ("writes.tree.child_and_parents_parents_order", "order_drops"): "swapped",
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
