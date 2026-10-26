"""Offline tests for the private-tree (Genealogies) tree recipes and the tree read case.

The registered recipes run through the real ``run_recipe`` (no network, no cookies)
against ``FakeGenealogies``. Honest, each passes, is recorded verified and leaves nothing
behind; in each lie mode the recipe fails for that reason, the registry is untouched and
teardown still removes everything. The read case runs with the real fixture ``World``
(its ``gtree`` group is built through the same fake).
"""
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
from tools.verify.recipes import reads_genealogies as rg
from tools.verify.recipes import writes_common as wc
from tools.verify.recipes import writes_genealogies_trees as wgt

from _fake_genealogies import G, FakeGenealogies

OPS = {
    "writes.genealogies.tree_create": ("createGenealogiesTree",),
    "writes.genealogies.tree_update": ("updateGenealogiesTree",),
    "writes.genealogies.tree_delete": ("deleteGenealogiesTree",),
}
# Reads the recipes use without claiming them.
UNCLAIMED = {"readGenealogiesTrees", "readGenealogiesTree", "readGenealogiesPersons",
             "readGenealogiesPerson"}


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


def requests_to(method, prefix):
    return [c.request for c in responses.calls if c.request.method == method
            and c.request.url.split("?")[0].startswith(prefix)]


def untouched(reg_path):
    return json.loads(reg_path.read_text()) == json.loads(REGISTRY_PATH.read_text())


# ---------------------------------------------------------------- registration


def test_recipes_registered_claiming_only_their_write():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in wgt.WRITES_GENEALOGIES_TREES} == set(OPS)
    for recipe in wgt.WRITES_GENEALOGIES_TREES:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__


def test_the_list_read_is_deferred_and_the_tree_read_has_a_case():
    assert "readGenealogiesTrees" in rg.DEFERRED
    assert RECIPES["reads.genealogies.tree"].ops == ("readGenealogiesTree",)


# ---------------------------------------------------------------- honest fake


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, reg_path, slept):
    result, fake, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert ctx.negative_controls >= 1
    assert fake.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
def test_create_posts_a_collection_title_and_notes_the_answer(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.tree_create", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", G + "/trees")
    [coll] = json.loads(post.body)["collections"]
    assert coll["title"].startswith("ZZSPIKET")
    assert any("collections body" in n and "Location=" in n for n in result["notes"])
    assert [d.url.split("?")[0] for d in requests_to("DELETE", G + "/trees/")] == [
        G + "/trees/GZTR-001"]


@responses.activate
def test_create_falls_back_to_the_source_description_shape(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.tree_create", reg_path, sd_trees=True)
    assert result["passed"], result["problems"]
    shapes = [list(json.loads(c.request.body)) for c in responses.calls
              if c.request.method == "POST" and c.request.url.endswith("/genealogies/trees")]
    assert shapes[:2] == [["collections"], ["sourceDescriptions"]]
    assert any("sourceDescriptions body" in n for n in result["notes"])
    assert fake.live() == []


@responses.activate
def test_create_takes_the_id_from_x_entity_id_too(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.tree_create", reg_path, entity_id=True)
    assert result["passed"], result["problems"]


@responses.activate
def test_update_adds_a_living_person_to_the_first_tree_only(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.tree_update", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", G + "/trees/GZTR-001")
    [person] = json.loads(post.body)["persons"]
    assert person["living"] is True and person["id"] == "zz1"
    assert person["names"][0]["nameForms"][0]["fullText"].startswith("ZZSPIKEN")
    assert ctx.fixtures.tracked("genealogies_person") == []  # torn down


@responses.activate
def test_delete_deletes_the_first_tree_and_teardown_skips_it(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.tree_delete", reg_path)
    assert result["passed"], result["problems"]
    deletes = [d.url.split("?")[0] for d in requests_to("DELETE", G + "/trees/")]
    assert deletes == [G + "/trees/GZTR-001", G + "/trees/GZTR-002"]


@responses.activate
def test_update_and_delete_write_only_tracked_trees(reg_path, slept, monkeypatch):
    monkeypatch.setattr(wgt, "_own", lambda ctx, tid: (_ for _ in ()).throw(
        AssertionError(f"refusing to write tree {tid}")))
    for name in ("writes.genealogies.tree_update", "writes.genealogies.tree_delete"):
        result, fake, ctx = run(name, reg_path)
        assert not result["passed"] and "refusing to write tree" in " ".join(result["problems"])
        assert [m for m, _ in fake.calls if m == "DELETE"].count("DELETE") == 2  # teardown only
        responses.reset()


# ---------------------------------------------------------------- lies

LIES = [
    ("writes.genealogies.tree_create", "ignore_tree_create", "not seen after"),
    ("writes.genealogies.tree_create", "wrong_tree_title", "titled"),
    ("writes.genealogies.tree_update", "tree_update_noop", "not seen after"),
    ("writes.genealogies.tree_update", "tree_update_other", "not seen after"),
    ("writes.genealogies.tree_delete", "tree_delete_noop", "not seen after"),
    ("writes.genealogies.tree_delete", "tree_delete_collateral", "no longer listed"),
]


@responses.activate
@pytest.mark.parametrize("name,mode,fragment", LIES)
def test_lies_fail_leave_the_registry_alone_and_still_tear_down(name, mode, fragment,
                                                                reg_path, slept):
    result, fake, ctx = run(name, reg_path, mode)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)


@responses.activate
def test_a_delete_that_deletes_nothing_is_reported_by_teardown_too(reg_path, slept):
    result, fake, ctx = run("writes.genealogies.tree_delete", reg_path, "tree_delete_noop")
    assert any(p.startswith("teardown: genealogies_tree") and "still readable" in p
               for p in result["problems"]), result["problems"]


# ---------------------------------------------------------------- the read case


@responses.activate
def test_tree_read_case_passes_with_the_real_world(reg_path, slept):
    result, fake, ctx = run("reads.genealogies.tree", reg_path)
    result.pop("notes", None)
    assert result == {"recipe": "reads.genealogies.tree", "passed": True, "problems": []}
    assert fake.live() == []
    entry = json.loads(reg_path.read_text())["operations"]["readGenealogiesTree"]["generated"]
    assert entry["status"] == "verified" and entry["evidence"] == "read-content"


@responses.activate
@pytest.mark.parametrize("mode,fragment", [("read_other", "anchor"),
                                           ("catch_all", "not-found control answered 200")])
def test_tree_read_case_catches_lies(mode, fragment, reg_path, slept):
    result, fake, ctx = run("reads.genealogies.tree", reg_path, mode)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)
