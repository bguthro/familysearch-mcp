"""Offline tests for the memory write recipes against the stateful fake.

As the other write recipe tests: the registered recipes run through the real
``run_recipe`` (no network, no cookies) against ``WriteTree``. Honest, each passes, is
recorded verified and leaves nothing behind; in each lie mode the recipe fails for that
reason, the registry is untouched and teardown still removes everything. No report ever
carries the signed-in user's id or agent id.
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
from tools.verify.recipes import writes_memories as wm

from _fake_tree import AGENT, MEMORIES, UID, WriteTree

OPS = {
    "writes.memories.memory_create": ("createMemories",),
    "writes.memories.memory_update": ("updateMemory",),
    "writes.memories.memory_delete": ("deleteMemory",),
    "writes.memories.comment_create": ("createMemoryComments",),
    "writes.memories.comment_delete": ("deleteMemoryComment",),
}
# Reads the recipes use without claiming them.
UNCLAIMED = {"readMemory", "readMemoryComments", "getPersonMemories"}


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
    assert {r.name for r in wm.WRITES_MEMORIES} == set(OPS)
    for recipe in wm.WRITES_MEMORIES:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__


def test_the_deferred_artifact_ops_are_unclaimed_and_explained():
    claimed = {op for r in wm.WRITES_MEMORIES for op in r.ops}
    assert set(wm.DEFERRED) == {"updateMemoryArtifact", "deleteMemoryArtifactCoverage"}
    assert not claimed & set(wm.DEFERRED)
    for op, reason in wm.DEFERRED.items():
        assert OPERATIONS[op].kind == "write" and len(reason.split()) >= 20, op


# ---------------------------------------------------------------- honest fake


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, reg_path, slept):
    result, tree, ctx = run(name, reg_path)
    notes = result.pop("notes", [])
    assert result == {"recipe": name, "passed": True, "problems": []}
    no_account_ids(notes)
    assert ctx.negative_controls >= 1
    assert tree.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
def test_create_uploads_the_png_floating_with_title_and_description(reg_path, slept):
    result, tree, ctx = run("writes.memories.memory_create", reg_path)
    assert result["passed"], result["problems"]
    [post] = [r for r in requests_to("POST", MEMORIES) if r.url.split("?")[0] == MEMORIES
              and b'name="artifact"' in r.body and "ZZSPIKEM" in r.url]
    assert post.headers["Content-Type"].startswith("multipart/form-data")
    assert "filename=zzspike.png" in post.url and "description=ZZSPIKED" in post.url
    # The recipe's memory and the sibling fixture are both deleted through the library.
    deletes = [d.url.split("?")[0] for d in requests_to("DELETE", MEMORIES + "/")]
    assert sorted(deletes) == [f"{MEMORIES}/4100001", f"{MEMORIES}/4100002"]


@responses.activate
def test_update_posts_the_description_by_the_id_readmemory_answered(reg_path, slept):
    result, tree, _ctx = run("writes.memories.memory_update", reg_path, memory_lag=2)
    assert result["passed"], result["problems"]
    [post] = [r for r in requests_to("POST", MEMORIES + "/")]
    assert post.url.split("?")[0] == f"{MEMORIES}/4100001"
    [description] = json.loads(post.body)["sourceDescriptions"]
    assert description["id"] == "4100001"
    assert description["titles"][0]["value"].startswith("ZZSPIKET")
    assert description["descriptions"][0]["value"].startswith("ZZSPIKEE")
    assert slept  # the person's memory list lagged and was polled


@responses.activate
def test_delete_deletes_the_first_only_and_teardown_skips_it(reg_path, slept):
    result, tree, _ctx = run("writes.memories.memory_delete", reg_path)
    assert result["passed"], result["problems"]
    deletes = [d.url.split("?")[0] for d in requests_to("DELETE", MEMORIES + "/")]
    assert deletes == [f"{MEMORIES}/4100001", f"{MEMORIES}/4100002"]


@responses.activate
def test_comment_create_posts_one_text_and_keeps_the_planted_comment(reg_path, slept):
    result, tree, _ctx = run("writes.memories.comment_create", reg_path)
    assert result["passed"], result["problems"]
    planting, post = requests_to("POST", MEMORIES + "/4100001/comments")
    assert json.loads(planting.body)["discussions"][0]["comments"][0]["text"].startswith(
        "ZZSPIKEP")
    assert json.loads(post.body)["discussions"][0]["comments"][0]["text"].startswith("ZZSPIKEX")
    assert post.headers["Content-Type"] == "application/x-fs-v1+json"


@responses.activate
def test_comment_delete_names_the_comment_by_the_id_the_read_answered(reg_path, slept):
    result, tree, _ctx = run("writes.memories.comment_delete", reg_path)
    assert result["passed"], result["problems"]
    [delete] = requests_to("DELETE", MEMORIES + "/4100001/comments/")
    assert delete.url.split("?")[0] == f"{MEMORIES}/4100001/comments/CM001"


@responses.activate
@pytest.mark.parametrize("name", ["writes.memories.memory_update",
                                  "writes.memories.memory_delete"])
def test_person_memory_lists_are_polled_but_not_forever(name, reg_path, slept):
    attempts, pause = wc.EVENTUAL
    result, tree, _ctx = run(name, reg_path, memory_lag=attempts - 1)
    assert result["passed"], result["problems"]
    assert slept and set(slept) == {pause}
    slept.clear()
    result, tree, _ctx = run(name, reg_path, memory_lag=attempts)
    assert not result["passed"]
    assert any("not seen after" in p for p in result["problems"]), result["problems"]
    assert tree.live() == []


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipes_write_only_tracked_fixtures(name, reg_path, slept, monkeypatch):
    monkeypatch.setattr(wm, "own", lambda ctx, kind, oid: (_ for _ in ()).throw(
        AssertionError(f"refusing to write {kind} {oid}")))
    result, tree, ctx = run(name, reg_path)
    if name == "writes.memories.memory_create":
        assert result["passed"]  # writes only what it creates
        return
    assert not result["passed"] and "refusing to write" in "\n".join(result["problems"])
    assert not set(OPS[name]) & ctx.api.called
    assert tree.live() == []


@responses.activate
def test_a_create_without_an_id_is_reported_and_the_leftover_is_named(reg_path, slept):
    tree = WriteTree()
    original = tree.create_memory

    def anonymous(request):  # only the claimed create (it sends a description) loses its id
        status, headers, body = original(request)
        return status, ({} if "description=" in request.url else headers), body

    tree.create_memory = anonymous
    tree.serve()
    recipe = RECIPES["writes.memories.memory_create"]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-26")
    assert not result["passed"]
    assert "may be left on beta" in "\n".join(result["problems"])


# ---------------------------------------------------------------- the fake lies


LIES = {
    ("writes.memories.memory_create", "memory_create_ignored"): "404",
    ("writes.memories.memory_create", "memory_create_attached"): "(a created memory is floating)",
    ("writes.memories.memory_create", "memory_create_no_description"): "not",
    ("writes.memories.memory_update", "memory_update_ignored"): "not seen after",
    ("writes.memories.memory_update", "memory_update_sibling"): "should not touch it",
    ("writes.memories.memory_update", "memory_update_unlinks"): "not seen after",
    ("writes.memories.memory_delete", "memory_delete_noop"): "not seen after",
    ("writes.memories.memory_delete", "memory_delete_wrong"): "not seen after",
    ("writes.memories.comment_create", "comment_create_ignored"): "not seen after",
    ("writes.memories.comment_create", "comment_create_replaces"): "not exactly",
    ("writes.memories.comment_delete", "comment_delete_noop"): "not seen after",
    ("writes.memories.comment_delete", "comment_delete_wrong"): "not seen after",
    ("writes.memories.comment_delete", "comment_delete_all"): "not exactly",
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
def test_failures_carry_a_response_skeleton_without_the_contributor(reg_path, slept):
    result, _tree, _ctx = run("writes.memories.comment_delete", reg_path,
                              "comment_delete_all")
    problems = "\n".join(result["problems"])
    assert "response: 200" in problems and "contributor" not in problems
    no_account_ids(result)
