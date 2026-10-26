"""Offline tests for the person write recipes against the stateful fake tree.

The registered recipe objects run through the real ``run_recipe`` (no network, no
cookies) against ``WriteTree``: honest, each passes, is recorded verified and leaves
nothing behind; in each lie mode (the server says yes and does something else) the
recipe that should catch it fails for that reason and the registry is untouched.
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
from tools.verify.recipes import writes_tree_persons as wtp

from _fake_tree import PERSONS, WriteTree

OPS = {
    "writes.tree.person_update": ("updatePerson",),
    "writes.tree.person_conclusions": ("deletePersonConclusion",),
    "writes.tree.person_source_reference": ("deletePersonSourceReference",),
    "writes.tree.person_memory": ("createPersonMemory",),
}
# Reads used for read-back are verified already and never claimed here.
READ_BACKS = {"readPerson", "readPersonSources", "getPersonMemories"}


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


# ---------------------------------------------------------------- registration


def test_recipes_registered_claiming_only_their_write():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in wtp.WRITES_TREE_PERSONS} == set(OPS)
    for recipe in wtp.WRITES_TREE_PERSONS:
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
    assert ctx.negative_controls >= 1
    assert tree.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
def test_person_update_sends_three_updates_with_the_conclusion_ids(reg_path, slept):
    result, tree, ctx = run("writes.tree.person_update", reg_path)
    assert result["passed"], result["problems"]
    assert ctx.negative_controls == 3
    updates = [json.loads(c.request.body)["persons"][0] for c in responses.calls
               if c.request.method == "POST" and "/tree/persons/" in c.request.url]
    assert [list(u) for u in updates] == [["names"], ["facts"], ["gender"]]
    name, = updates[0]["names"]
    assert name["id"] and name["nameForms"][0]["parts"]  # an update by id, with parts
    assert "id" not in updates[1]["facts"][0]  # a create
    assert updates[2]["gender"]["id"] and updates[2]["gender"]["type"].endswith("Female")
    # Only the recipe's own fixture is written.
    [pid] = tree.persons
    assert {c.request.url.split("?")[0] for c in responses.calls
            if c.request.method == "POST" and "/tree/persons/" in c.request.url} == {
        f"{PERSONS}/{pid}"}


@responses.activate
def test_person_conclusions_deletes_the_marked_fact_by_its_id(reg_path, slept):
    result, tree, _ctx = run("writes.tree.person_conclusions", reg_path)
    assert result["passed"], result["problems"]
    [delete] = [c.request.url for c in responses.calls if "/conclusions/" in c.request.url]
    [pid] = tree.persons
    assert delete.startswith(f"{PERSONS}/{pid}/conclusions/C.")


@responses.activate
def test_source_reference_polls_both_ways_but_not_forever(reg_path, slept):
    attempts, pause = wc.EVENTUAL
    result, _tree, _ctx = run("writes.tree.person_source_reference", reg_path,
                              source_lag=attempts - 1)
    assert result["passed"], result["problems"]
    assert slept == [pause] * (2 * (attempts - 1))  # the attach and the delete both lag
    assert len(result["notes"]) == 2
    slept.clear()
    result, tree, _ctx = run("writes.tree.person_source_reference", reg_path,
                             source_lag=attempts)
    assert not result["passed"]
    assert any("not seen after" in p for p in result["problems"]), result["problems"]
    assert tree.live() == []


@responses.activate
def test_person_memory_uploads_the_png_with_its_title_and_waits(reg_path, slept):
    result, tree, ctx = run("writes.tree.person_memory", reg_path, memory_lag=2)
    assert result["passed"], result["problems"]
    [upload] = [c.request for c in responses.calls if c.request.method == "POST"
                and c.request.url.split("?")[0].endswith("/memories")]
    assert b'name="artifact"' in upload.body and upload.headers["Content-Type"].startswith(
        "multipart/form-data")
    assert "filename=zzspike.png" in upload.url and "title=ZZSPIKEM" in upload.url
    assert slept == [wc.EVENTUAL[1]] * 2
    assert tree.live() == []  # deleted through Memories.delete at teardown
    assert any(m == "DELETE" and p.startswith("/platform/memories/memories/")
               for m, p in tree.calls)


@responses.activate
def test_person_memory_without_entity_id_finds_the_new_titled_memory(reg_path, slept):
    # The Location names the persona reference ({mid}-1), never taken as the memory id.
    result, tree, _ctx = run("writes.tree.person_memory", reg_path, "no_entity_id")
    assert result["passed"], result["problems"]
    assert tree.live() == []


# ---------------------------------------------------------------- the fake lies


LIES = {
    ("writes.tree.person_update", "ignore_update"): "negative control failed",
    ("writes.tree.person_update", "ignore_names"): "negative control failed",
    ("writes.tree.person_update", "ignore_facts"): "negative control failed",
    ("writes.tree.person_update", "ignore_gender"): "negative control failed",
    ("writes.tree.person_update", "replace_facts"): "the earlier facts (on adding one) changed",
    ("writes.tree.person_update", "update_creates"): "the number of names",
    ("writes.tree.person_update", "collateral"): "facts (on a name update) changed",
    ("writes.tree.person_conclusions", "ignore_conclusion_delete"): "negative control failed",
    ("writes.tree.person_conclusions", "wrong_conclusion"): "negative control failed",
    ("writes.tree.person_source_reference", "ref_not_removed"): "not seen after",
    ("writes.tree.person_source_reference", "ref_removes_all"): "other source's reference",
    ("writes.tree.person_source_reference", "ignore_update"): "not seen after",
    ("writes.tree.person_memory", "memory_not_attached"): "not seen after",
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
def test_failures_carry_the_response_skeleton(reg_path, slept):
    result, _tree, _ctx = run("writes.tree.person_source_reference", reg_path,
                              "ref_not_removed")
    assert "response: 200" in "\n".join(result["problems"])
