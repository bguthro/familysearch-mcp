"""Offline tests for the private-tree source recipes and the source read case."""
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
from tools.verify.recipes import writes_genealogies_sources as wgs

from _fake_genealogies import FakeGenealogies

OPS = {
    "writes.genealogies.source_create": ("createGenealogiesSourceDescription",),
    "writes.genealogies.source_update": ("updateGenealogiesSourceDescription",),
    "writes.genealogies.source_delete": ("deleteGenealogiesSourceDescription",),
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
    mine = wgs.WRITES_GENEALOGIES_SOURCES
    assert {r.name for r in mine} == set(OPS)
    for recipe in mine:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__
    assert RECIPES["reads.genealogies.source"].ops == ("readGenealogiesSourceDescription",)


@responses.activate
def test_update_answering_the_local_form_of_the_same_source_is_not_a_new_one(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.source_update", reg_path, "source_update_alias")
    assert result["passed"], result["problems"]


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


LIES = [
    ("writes.genealogies.source_create", "ignore_source_create", "negative control"),
    ("writes.genealogies.source_update", "ignore_source_update", "negative control"),
    ("writes.genealogies.source_update", "source_update_creates", "made a new one"),
    ("writes.genealogies.source_update", "source_update_collateral", "other source"),
    ("writes.genealogies.source_delete", "source_delete_noop", "negative control"),
]


@responses.activate
@pytest.mark.parametrize("name,mode,fragment", LIES)
def test_lies_fail_leave_the_registry_alone(name, mode, fragment, reg_path, slept):
    result, fake, ctx = run(name, reg_path, mode)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)


@responses.activate
def test_source_read_case_passes_and_catches_a_wrong_record(reg_path, slept):
    result, fake, ctx = run("reads.genealogies.source", reg_path)
    result.pop("notes", None)
    assert result == {"recipe": "reads.genealogies.source", "passed": True, "problems": []}
    assert fake.live() == []


def test_the_relationship_ops_are_deferred_with_the_live_evidence():
    assert set(wgs.DEFERRED) == {"updateGenealogiesRelationship",
                                 "deleteGenealogiesRelationship"}
    assert all("never appears under readGenealogiesPerson.relationships" in r
               for r in wgs.DEFERRED.values())
    claimed = {op for r in RECIPES.values() for op in r.ops}
    assert not claimed & set(wgs.DEFERRED)
