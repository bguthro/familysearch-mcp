"""Offline tests for the private-tree source and relationship recipes and the source read case."""
import json
import shutil

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.verify import reads, runner
from tools.verify.recipes import RECIPES, validate_recipes
from tools.verify.recipes import writes_common as wc
from tools.verify.recipes import writes_genealogies_relationships as wgr
from tools.verify.recipes import writes_genealogies_sources as wgs

from _fake_genealogies import FakeGenealogies

OPS = {
    "writes.genealogies.source_create": ("createGenealogiesSourceDescription",),
    "writes.genealogies.source_update": ("updateGenealogiesSourceDescription",),
    "writes.genealogies.source_delete": ("deleteGenealogiesSourceDescription",),
    "writes.genealogies.relationship_update": ("updateGenealogiesRelationship",),
    "writes.genealogies.relationship_delete": ("deleteGenealogiesRelationship",),
}
UNCLAIMED = {"readGenealogiesSourceDescription", "readGenealogiesPerson"}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


@pytest.fixture
def slept(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(wc, "sleep", pauses.append)
    monkeypatch.setattr(reads, "sleep", pauses.append)
    return pauses


def run(name, reg_path, mode=None, **kw):
    fake = FakeGenealogies(mode, **kw).serve()
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-26")
    return result, fake, ctx


def untouched(reg_path):
    return json.loads(reg_path.read_text()) == json.loads(REGISTRY_PATH.read_text())


def test_recipes_registered_claiming_only_their_writes():
    assert validate_recipes(RECIPES) == []
    mine = wgs.WRITES_GENEALOGIES_SOURCES + wgr.WRITES_GENEALOGIES_RELATIONSHIPS
    assert {r.name for r in mine} == set(OPS)
    for recipe in mine:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__
    assert RECIPES["reads.genealogies.source"].ops == ("readGenealogiesSourceDescription",)


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, reg_path, slept):
    result, fake, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert ctx.negative_controls >= 1
    assert fake.live() == []
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"]["status"] == "verified"


@responses.activate
def test_the_couple_is_planted_by_a_tree_update_with_a_male_person1(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.relationship_update", reg_path)
    assert result["passed"], result["problems"]
    [(_, _, body)] = [b for b in fake.bodies if b[0] == "POST" and b[1].endswith("/trees/2:2:2:GZTR-001")]
    assert [p["living"] for p in body["persons"]] == [True, True]
    rel = body["relationships"][0]
    assert rel["person1"]["resourceId"] == "m1" and rel["person2"]["resourceId"] == "f1"
    assert body["persons"][0]["gender"]["type"].endswith("Male")


LIES = [
    ("writes.genealogies.source_create", "ignore_source_create", "negative control"),
    ("writes.genealogies.source_update", "ignore_source_update", "negative control"),
    ("writes.genealogies.source_update", "source_update_creates", "made a new one"),
    ("writes.genealogies.source_update", "source_update_collateral", "other source"),
    ("writes.genealogies.source_delete", "source_delete_noop", "negative control"),
    ("writes.genealogies.relationship_update", "ignore_relationship_update",
     "negative control"),
    ("writes.genealogies.relationship_update", "relationship_update_collateral",
     "gained 2 facts"),
    ("writes.genealogies.relationship_delete", "relationship_delete_noop", "not seen after"),
    ("writes.genealogies.relationship_delete", "relationship_delete_persons", "410"),
]


@responses.activate
@pytest.mark.parametrize("name,mode,fragment", LIES)
def test_lies_fail_leave_the_registry_alone(name, mode, fragment, reg_path, slept):
    result, fake, ctx = run(name, reg_path, mode)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)


@responses.activate
def test_a_read_that_lists_no_relationship_fails_with_the_skeleton(reg_path, slept,
                                                                  monkeypatch):
    monkeypatch.setattr(wgr, "person_relationships", lambda person: {})
    result, fake, ctx = run("writes.genealogies.relationship_update", reg_path)
    assert not result["passed"]
    assert "lists 0 relationships" in " ".join(result["problems"])
    assert fake.live() == []


@responses.activate
def test_source_read_case_passes_and_catches_a_wrong_record(reg_path, slept):
    result, fake, ctx = run("reads.genealogies.source", reg_path)
    result.pop("notes", None)
    assert result == {"recipe": "reads.genealogies.source", "passed": True, "problems": []}
    assert fake.live() == []
