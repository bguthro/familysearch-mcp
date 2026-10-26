"""Offline tests for the source box write recipes against the stateful fake.

As the other write recipe tests: the registered recipes run through the real
``run_recipe`` (no network, no cookies) against ``WriteTree``. Honest, each passes, is
recorded verified and leaves nothing behind; in each lie mode the recipe fails for
that reason, the registry is untouched and teardown still removes everything. No
report ever carries the signed-in user's id or agent id.
"""
import json
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.verify import runner
from tools.verify.recipes import RECIPES, validate_recipes
from tools.verify.recipes import writes_common as wc
from tools.verify.recipes import writes_source_box as wsb

from _fake_tree import AGENT, BETA, COLLECTIONS, UID, WriteTree

OPS = {
    "writes.sources.collection_create": ("createSourceFolder",),
    "writes.sources.collection_update": ("updateUserDefinedCollection",),
    "writes.sources.collection_add": ("updateSourceDescriptionsToCollection",),
    "writes.sources.collection_remove": ("deleteSourceDescriptionsFromCollections",),
    "writes.sources.collection_delete": ("deleteUserDefinedCollection",),
}
MEMBERSHIP = ["writes.sources.collection_add", "writes.sources.collection_remove"]
# Reads the recipes use without claiming them.
UNCLAIMED = {"readUserDefinedCollection", "readUserDefinedCollectionSourceDescriptions",
             "readSourceFolders", "readSourceDescription"}


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


def no_account_ids(result):
    text = json.dumps(result)
    assert UID not in text and "MZZZ-USR1" not in text and AGENT not in text, text


def requests_to(method, prefix):
    return [c.request for c in responses.calls if c.request.method == method
            and c.request.url.split("?")[0].startswith(prefix)]


# ---------------------------------------------------------------- registration


def test_recipes_registered_claiming_only_their_write():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in wsb.WRITES_SOURCE_BOX} == set(OPS)
    for recipe in wsb.WRITES_SOURCE_BOX:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__


# ---------------------------------------------------------------- honest fake


@responses.activate
@pytest.mark.parametrize("gone_folder", [400, 404])
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, gone_folder, reg_path, slept):
    result, tree, ctx = run(name, reg_path, gone_folder=gone_folder)
    notes = result.pop("notes", [])
    assert result == {"recipe": name, "passed": True, "problems": []}
    no_account_ids(notes)
    assert ctx.negative_controls == 1
    assert tree.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
def test_create_posts_a_titled_collection_and_tracks_the_entity_id(reg_path, slept):
    result, tree, ctx = run("writes.sources.collection_create", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", COLLECTIONS)
    assert post.url.split("?")[0] == COLLECTIONS
    [collection] = json.loads(post.body)["collections"]
    assert set(collection) == {"title"} and collection["title"].startswith("ZZSPIKEF")
    assert any("X-ENTITY-ID='UDWZ-001'" in n for n in result["notes"])
    deletes = [d.url.split("?")[0] for d in requests_to("DELETE", COLLECTIONS)]
    assert deletes == [f"{COLLECTIONS}/UDWZ-001"]  # teardown's


@responses.activate
def test_update_posts_the_collection_by_id_and_renames_the_first_only(reg_path, slept):
    result, tree, _ctx = run("writes.sources.collection_update", reg_path)
    assert result["passed"], result["problems"]
    posts = [r for r in requests_to("POST", COLLECTIONS + "/")]
    [post] = posts
    assert post.url.split("?")[0] == f"{COLLECTIONS}/UDWZ-001"
    [collection] = json.loads(post.body)["collections"]
    assert collection["id"] == "UDWZ-001" and collection["title"].startswith("ZZSPIKER")


@responses.activate
def test_add_posts_both_ids_in_one_request(reg_path, slept):
    result, tree, _ctx = run("writes.sources.collection_add", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", COLLECTIONS + "/")
    assert post.url.split("?")[0] == f"{COLLECTIONS}/UDWZ-001/descriptions"
    assert json.loads(post.body) == {"sourceDescriptions": [{"id": "SDWZ-001"},
                                                           {"id": "SDWZ-002"}]}


@responses.activate
def test_remove_names_the_current_user_and_one_id(reg_path, slept):
    result, tree, _ctx = run("writes.sources.collection_remove", reg_path)
    assert result["passed"], result["problems"]
    [delete] = requests_to("DELETE", f"{BETA}/platform/sources/current/")
    url = urlsplit(delete.url)
    assert url.path == "/platform/sources/current/collections/descriptions"
    assert parse_qs(url.query) == {"id": ["SDWZ-001"]}
    # The uid is only ever in the URL beta's 307 redirects the folder list to.
    assert {c.request.url.split("?")[0] for c in responses.calls if UID in c.request.url} \
        == {f"{BETA}/platform/sources/{UID}/collections"}
    assert all(c.request.method == "GET" for c in responses.calls if UID in c.request.url)


@responses.activate
def test_delete_deletes_the_first_only_and_teardown_skips_it(reg_path, slept):
    result, tree, _ctx = run("writes.sources.collection_delete", reg_path)
    assert result["passed"], result["problems"]
    deletes = [d.url.split("?")[0] for d in requests_to("DELETE", COLLECTIONS + "/")]
    assert deletes == [f"{COLLECTIONS}/UDWZ-001", f"{COLLECTIONS}/UDWZ-002"]


@responses.activate
@pytest.mark.parametrize("name", MEMBERSHIP)
def test_membership_polls_a_lagging_read_but_not_forever(name, reg_path, slept):
    attempts, pause = wc.EVENTUAL
    result, tree, _ctx = run(name, reg_path, member_lag=attempts - 1)
    assert result["passed"], result["problems"]
    assert slept and set(slept) == {pause}
    assert tree.live() == []
    slept.clear()
    result, tree, _ctx = run(name, reg_path, member_lag=attempts)
    assert not result["passed"]
    assert any("not seen after" in p for p in result["problems"]), result["problems"]
    no_account_ids(result)
    assert tree.live() == []


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipes_write_only_tracked_fixtures(name, reg_path, slept, monkeypatch):
    monkeypatch.setattr(wsb, "_own", lambda ctx, kind, oid: (_ for _ in ()).throw(
        AssertionError(f"refusing to write {kind} {oid}")))
    result, tree, _ctx = run(name, reg_path)
    if name == "writes.sources.collection_create":
        assert result["passed"]  # writes only what it creates
        return
    assert not result["passed"] and "refusing to write" in "\n".join(result["problems"])
    assert not set(OPS[name]) & _ctx.api.called
    assert tree.live() == []


# ---------------------------------------------------------------- the fake lies


LIES = {
    ("writes.sources.collection_create", "folder_create_ignored"): "not seen after",
    ("writes.sources.collection_update", "folder_rename_ignored"): "negative control failed",
    ("writes.sources.collection_update", "folder_update_creates"): "appeared besides",
    ("writes.sources.collection_add", "members_add_ignored"): "not seen after",
    ("writes.sources.collection_add", "members_add_one"): "not seen after",
    ("writes.sources.collection_remove", "members_remove_noop"): "not seen after",
    ("writes.sources.collection_remove", "members_remove_wrong"): "not seen after",
    ("writes.sources.collection_remove", "members_remove_all"): "not exactly",
    ("writes.sources.collection_remove", "members_remove_deletes"):
        "deleted the description itself",
    ("writes.sources.collection_delete", "folder_delete_noop"): "not seen after",
}


@responses.activate
@pytest.mark.parametrize("name, mode", sorted(LIES))
def test_recipe_fails_when_the_fake_lies(name, mode, reg_path, slept):
    raw = reg_path.read_bytes()
    result, tree, _ctx = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    problems = "\n".join(result["problems"])
    assert LIES[(name, mode)] in problems, problems
    no_account_ids(result)
    assert reg_path.read_bytes() == raw  # nothing recorded
    assert tree.live() == []  # and still nothing left behind


@responses.activate
@pytest.mark.parametrize("mode", ["folder_create_no_id", "folder_create_entity_id"])
@pytest.mark.parametrize("name", sorted(OPS))
def test_a_create_without_x_entity_id_is_still_tracked(name, mode, reg_path, slept):
    # Every collection (the recipe's own and the fixtures') is found and torn down.
    result, tree, _ctx = run(name, reg_path, mode)
    assert result["passed"], result["problems"]
    assert tree.live() == []
    if name == "writes.sources.collection_create" and mode == "folder_create_no_id":
        assert any("answered no id" in n for n in result["notes"])


@responses.activate
def test_an_error_body_naming_the_user_is_redacted(reg_path, slept, monkeypatch):
    tree = WriteTree()
    monkeypatch.setattr(tree, "box_route", lambda request: (
        400, {}, f"Unable to remove: {UID} does not own /platform/sources/{UID}/collections"))
    tree.serve()
    recipe = RECIPES["writes.sources.collection_remove"]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-26")
    assert not result["passed"]
    assert "FSHTTPError" in "\n".join(result["problems"])
    no_account_ids(result)
    assert tree.live() == []
