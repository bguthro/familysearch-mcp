"""Offline tests for the private-tree person recipes and the person read cases."""
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
from tools.verify.recipes import writes_genealogies_persons as wgp

from _fake_genealogies import G, FakeGenealogies

OPS = {
    "writes.genealogies.person_create": ("createGenealogiesPerson",),
    "writes.genealogies.person_update": ("updateGenealogiesPerson",),
    "writes.genealogies.person_conclusions": ("deleteGenealogiesConclusion",),
    "writes.genealogies.person_delete_restore": ("deleteGenealogiesPerson",
                                                 "restoreGenealogiesPerson"),
}
UNCLAIMED = {"readGenealogiesTrees", "readGenealogiesPersons", "readGenealogiesPerson"}


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
    assert {r.name for r in wgp.WRITES_GENEALOGIES_PERSONS} == set(OPS)
    for recipe in wgp.WRITES_GENEALOGIES_PERSONS:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__
    assert RECIPES["reads.genealogies.persons"].ops == ("readGenealogiesPersons",)
    assert RECIPES["reads.genealogies.person"].ops == ("readGenealogiesPerson",)


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
        assert registry[op]["generated"]["recipe"] == name


@responses.activate
def test_create_posts_a_living_person_under_the_tree_only(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.person_create", reg_path)
    assert result["passed"], result["problems"]
    posts = [c.request for c in responses.calls if c.request.method == "POST"
             and c.request.url.endswith("/persons")]
    assert [p.url.split("/platform/")[1] for p in posts] == ["genealogies/trees/2%3A2%3A2%3AGZTR-001/persons"]
    [person] = json.loads(posts[0].body)["persons"]
    assert person["living"] is True
    assert not any("/platform/tree/" in c.request.url for c in responses.calls)


@responses.activate
def test_update_posts_to_the_first_person_by_conclusion_id(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.person_update", reg_path)
    assert result["passed"], result["problems"]
    posts = [(p, b) for m, p, b in fake.bodies if m == "POST" and "/persons/" in p]
    assert [p for p, _ in posts] == ["/platform/genealogies/persons/2:2:2:GZPE-001"] * 2
    assert posts[0][1]["persons"][0]["names"][0]["id"].startswith("2:2:2:GZCC")


@responses.activate
def test_delete_restore_restores_with_a_bare_post(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.person_delete_restore", reg_path)
    assert result["passed"], result["problems"]
    restores = [(m, p) for m, p in fake.calls if p.endswith("/restore")]
    assert restores == [("POST", "/platform/genealogies/persons/2:2:2:GZPE-001/restore")]


@responses.activate
def test_a_failed_restore_marks_the_gone_person_deleted_for_teardown(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.person_delete_restore", reg_path,
                            "person_restore_noop")
    assert not result["passed"]
    assert not any(p.startswith("teardown") for p in result["problems"]), result["problems"]


@responses.activate
def test_writes_only_tracked_persons(reg_path, slept, monkeypatch):
    monkeypatch.setattr(wgp, "_own_person", lambda ctx, pid: (_ for _ in ()).throw(
        AssertionError(f"refusing to write person {pid}")))
    for name in ("writes.genealogies.person_update", "writes.genealogies.person_conclusions",
                 "writes.genealogies.person_delete_restore"):
        result, fake, ctx = run(name, reg_path)
        assert "refusing to write person" in " ".join(result["problems"])
        responses.reset()


LIES = [
    ("writes.genealogies.person_create", "drop_living", "tree"),
    ("writes.genealogies.person_create", "ignore_person_create", "not seen after"),
    ("writes.genealogies.person_update", "ignore_person_update", "negative control"),
    ("writes.genealogies.person_update", "person_update_creates", "number of names"),
    ("writes.genealogies.person_update", "person_update_collateral", "facts"),
    ("writes.genealogies.person_update", "person_update_unlives", "living"),
    ("writes.genealogies.person_conclusions", "ignore_conclusion_delete", "negative control"),
    ("writes.genealogies.person_conclusions", "conclusion_collateral", "other facts"),
    ("writes.genealogies.person_delete_restore", "person_delete_noop", "did not change"),
    ("writes.genealogies.person_delete_restore", "person_restore_noop", "not seen after"),
]


@responses.activate
@pytest.mark.parametrize("name,mode,fragment", LIES)
def test_lies_fail_leave_the_registry_alone(name, mode, fragment, reg_path, slept):
    result, fake, ctx = run(name, reg_path, mode)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)


@responses.activate
@pytest.mark.parametrize("name", ["reads.genealogies.persons", "reads.genealogies.person"])
def test_person_read_cases_pass_and_catch_lies(name, reg_path, slept):
    result, fake, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert fake.live() == []
    responses.reset()
    bad, _fake, _ctx = run(name, reg_path, "drop_living")
    assert not bad["passed"] and "living" in " ".join(bad["problems"])
