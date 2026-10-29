"""Offline tests for the User Tree (groups and trees) recipes and read cases (Task C)."""
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
from tools.verify.recipes import reads_user_trees as rut
from tools.verify.recipes import writes_common as wc
from tools.verify.recipes import writes_user_trees as wut

from _fake_usertree import P, FakeUserTree

OPS = {
    "writes.usertree.group_create": ("createGroup",),
    "writes.usertree.group_update": ("updateGroup",),
    "writes.usertree.group_delete": ("deleteGroup",),
    "writes.usertree.tree_create": ("createTree",),
    "writes.usertree.tree_update": ("updateTree",),
    "writes.usertree.tree_delete": ("deleteTree",),
}
READS = {"reads.usertree.group": ("readGroup",), "reads.usertree.tree": ("readTree",)}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    monkeypatch.setattr(wc, "sleep", lambda s: None)
    monkeypatch.setattr(reads, "sleep", lambda s: None)


def run(name, reg_path, mode=None, **kw):
    fake = FakeUserTree(mode, **kw).serve()
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-29")
    return result, fake, ctx


def untouched(reg_path):
    return json.loads(reg_path.read_text()) == json.loads(REGISTRY_PATH.read_text())


def test_registration_and_deferrals():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in wut.WRITES_USER_TREES} == set(OPS)
    for recipe in wut.WRITES_USER_TREES:
        assert RECIPES[recipe.name] is recipe and recipe.ops == OPS[recipe.name]
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert "Negative control" in recipe.run.__doc__
    for name, ops in READS.items():
        assert RECIPES[name].ops == ops
    claimed = {op for r in RECIPES.values() for op in r.ops}
    assert set(wut.DEFERRED) == {"setCurrentTree"} and not claimed & set(wut.DEFERRED)
    assert set(rut.DEFERRED) == {"getGroups", "readCurrentTree", "readUserTreePersons",
                                 "readTreeChanges", "readTreeMatches"}
    assert not claimed & set(rut.DEFERRED)


@responses.activate
@pytest.mark.parametrize("name", sorted({**OPS, **READS}))
def test_recipe_passes_records_and_never_touches_the_current_tree(name, reg_path):
    result, fake, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert fake.live() == []
    assert fake.current_posts == [] and fake.current == "GLOBAL"
    assert not [p for m, p in fake.calls if p.endswith("/trees/current")]
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        assert registry[op]["generated"]["status"] == "verified"


@responses.activate
def test_create_sends_a_private_hidden_tree_and_a_plain_group(reg_path):
    run("writes.usertree.tree_create", reg_path)
    run("writes.usertree.group_create", reg_path)
    posts = [(c.request.url.split("/platform/")[1], json.loads(c.request.body))
             for c in responses.calls if c.request.method == "POST"]
    tree = [b for p, b in posts if p == "trees"][-1]["trees"][0]
    assert tree["private"] is True and tree["hidden"] is True
    assert tree["name"].startswith("ZZSPIKE") and tree["groupAccess"]["groupId"]
    group = [b for p, b in posts if p == "groups"][-1]["groups"][0]
    assert set(group) == {"name", "description"}


@responses.activate
def test_a_tree_is_created_for_its_own_group_trying_the_group_fields_in_turn(reg_path):
    result, fake, ctx = run("writes.usertree.tree_create", reg_path)
    assert result["passed"], result["problems"]
    posts = [(c.request.url.split("/platform/")[1], json.loads(c.request.body))
             for c in responses.calls if c.request.method == "POST"]
    assert [p for p, _ in posts] == ["groups", "trees", "trees"]  # groupAccess{groupId}: 2nd
    first, second = posts[1][1]["trees"][0], posts[2][1]["trees"][0]
    assert isinstance(first["groupAccess"], list) and first["private"] is True
    assert second["groupAccess"]["groupId"].startswith("UG")
    [note] = [n for n in result["notes"] if "refused before" in n]
    assert "groupAccess[{groupId,access}]" in note and "one and only one group" in note
    # the tree is deleted before its group
    assert [p for m, p in fake.calls if m == "DELETE"] == [
        "/platform/trees/UT002-ZZZ", "/platform/groups/UG001-ZZZ"]


@responses.activate
def test_a_fixture_tree_gets_its_own_group_and_both_are_torn_down(reg_path):
    result, fake, ctx = run("writes.usertree.tree_update", reg_path)
    assert result["passed"], result["problems"]
    assert len(fake.objects["groups"]) == 2 and len(fake.objects["trees"]) == 2
    assert fake.live() == []


@responses.activate
def test_a_tree_is_deleted_by_its_own_id_and_its_group_only_at_teardown(reg_path):
    result, fake, ctx = run("writes.usertree.tree_delete", reg_path)
    assert result["passed"], result["problems"]
    deletes = [p for m, p in fake.calls if m == "DELETE"]
    assert deletes[0].startswith("/platform/trees/")  # the recipe's own delete first
    assert sum("/trees/" in d for d in deletes) == 2 and sum("/groups/" in d for d in deletes) == 2
    assert fake.live() == []


LIES = [
    ("writes.usertree.group_create", "ignore_create", "404"),
    ("writes.usertree.group_create", "wrong_name", "reads back named"),
    ("writes.usertree.tree_create", "ignore_create", "404"),
    ("writes.usertree.tree_create", "wrong_name", "reads back named"),
    ("writes.usertree.group_update", "ignore_update", "did not change"),
    ("writes.usertree.group_update", "update_creates", "made a new one"),
    ("writes.usertree.group_update", "update_collateral", "the other group"),
    ("writes.usertree.tree_update", "update_drops_description", "other fields"),
    ("writes.usertree.tree_update", "update_collateral", "the other user tree"),
    ("writes.usertree.tree_create", "drop_group", "without its group"),
    ("writes.usertree.group_delete", "delete_noop", "did not change"),
    ("writes.usertree.tree_delete", "delete_collateral", "410"),
]


@responses.activate
@pytest.mark.parametrize("name,mode,fragment", LIES)
def test_lies_fail_leave_the_registry_alone(name, mode, fragment, reg_path):
    kw = {"unknown_status": 404} if mode == "ignore_create" else {}
    result, fake, ctx = run(name, reg_path, mode, **kw)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)
    assert fake.current_posts == []


@responses.activate
@pytest.mark.parametrize("name", sorted(READS))
@pytest.mark.parametrize("mode,fragment", [("read_other", "anchor"),
                                           ("catch_all", "not-found control answered 200")])
def test_read_cases_catch_lies(name, mode, fragment, reg_path):
    result, fake, ctx = run(name, reg_path, mode)
    assert not result["passed"]
    assert fragment in " ".join(result["problems"]), result["problems"]
    assert untouched(reg_path)


@responses.activate
def test_set_current_tree_is_locked_even_for_a_recipe_that_does_not_claim_it(reg_path):
    from familysearch.errors import FSError
    fake = FakeUserTree().serve()
    recipe = RECIPES["writes.usertree.tree_create"]
    ctx = runner.make_context(FSClient("tkn", Environment.BETA, min_write_interval=0),
                              recipe, registry_path=reg_path)
    with pytest.raises(FSError):
        ctx.api.set_current_tree(reason="ZZSPIKE", body={"trees": [{"id": "X"}]})
    assert fake.current_posts == []


@responses.activate
def test_when_no_group_shape_is_accepted_the_failure_lists_every_shape_tried(reg_path):
    from tools.verify.fixtures import TREE_GROUP_SHAPES
    result, fake, ctx = run_with_group_of(reg_path, lambda item: None)
    assert not result["passed"]
    text = " ".join(result["problems"])
    for label, _fields, _bare in TREE_GROUP_SHAPES:
        assert label in text, label
    assert "one and only one group" in text
    assert fake.live() == []  # the fixture group is torn down


def run_with_group_of(reg_path, group_of):
    fake = FakeUserTree(group_of=group_of).serve()
    recipe = RECIPES["writes.usertree.tree_create"]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    return runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                             today="2026-10-29"), fake, ctx


@responses.activate
def test_bare_shapes_omit_the_private_and_hidden_flags(reg_path):
    from tools.verify.fixtures import TREE_GROUP_SHAPES
    assert [bare for _l, _f, bare in TREE_GROUP_SHAPES].count(True) >= 3
    result, fake, ctx = run_with_group_of(reg_path, lambda item: None)
    bodies = [json.loads(c.request.body)["trees"][0] for c in responses.calls
              if c.request.method == "POST" and c.request.url.endswith("/trees")]
    assert any("private" not in b for b in bodies) and any(b.get("private") for b in bodies)
