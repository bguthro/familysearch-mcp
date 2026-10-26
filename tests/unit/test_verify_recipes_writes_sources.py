"""Offline tests for the source description write recipes against the stateful fake.

As the other write recipe tests: the registered recipes run through the real
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
from tools.verify.recipes import writes_sources as ws

from _fake_tree import DESCRIPTIONS, WriteTree

OPS = {
    "writes.sources.description_create": ("createSourceDescription",),
    "writes.sources.description_update": ("updateSourceDescription",),
    "writes.sources.description_delete": ("deleteSourceDescription",),
}
# Reads the recipes use without claiming them.
UNCLAIMED = {"readSourceDescription", "readUserSourceDescriptions"}


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


def requests_to(method, prefix):
    return [c.request for c in responses.calls if c.request.method == method
            and c.request.url.split("?")[0].startswith(prefix)]


# ---------------------------------------------------------------- registration


def test_recipes_registered_claiming_only_their_write():
    assert validate_recipes(RECIPES) == []
    assert {r.name for r in ws.WRITES_SOURCES} == set(OPS)
    for recipe in ws.WRITES_SOURCES:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__


def test_read_back_is_the_verified_description_read():
    registry = json.loads(REGISTRY_PATH.read_text())["operations"]
    assert registry["readSourceDescription"]["generated"]["status"] == "verified"


# ---------------------------------------------------------------- honest fake


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipe_passes_records_and_leaves_nothing(name, reg_path, slept):
    result, tree, ctx = run(name, reg_path)
    result.pop("notes", None)
    assert result == {"recipe": name, "passed": True, "problems": []}
    assert ctx.negative_controls == 1
    assert tree.live() == []  # strict teardown removed every fixture
    registry = json.loads(reg_path.read_text())["operations"]
    for op in OPS[name]:
        assert registry[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-26"}


@responses.activate
def test_create_posts_the_library_body_and_tracks_the_new_id(reg_path, slept):
    result, tree, ctx = run("writes.sources.description_create", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", DESCRIPTIONS)
    [desc] = json.loads(post.body)["sourceDescriptions"]
    assert set(desc) == {"about", "titles", "citations"}
    title = desc["titles"][0]["value"]
    assert title.startswith("ZZSPIKET") and desc["citations"][0]["value"] == f"{title} citation"
    assert list(tree.descriptions) == ["SDWZ-001"]
    deletes = requests_to("DELETE", DESCRIPTIONS + "/")
    assert [d.url.split("?")[0] for d in deletes] == [f"{DESCRIPTIONS}/SDWZ-001"]


@responses.activate
def test_update_posts_the_whole_description_to_the_first_only(reg_path, slept):
    result, tree, ctx = run("writes.sources.description_update", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", DESCRIPTIONS + "/")
    assert post.url.split("?")[0] == f"{DESCRIPTIONS}/SDWZ-001"
    [desc] = json.loads(post.body)["sourceDescriptions"]
    assert desc["id"] == "SDWZ-001" and set(desc) == {"id", "about", "titles", "citations"}
    assert desc["about"].startswith("https://example.org/zzspikeu")
    assert ctx.fixtures.tracked("source") == []  # torn down


@responses.activate
def test_delete_deletes_the_first_only_and_teardown_skips_it(reg_path, slept):
    result, tree, ctx = run("writes.sources.description_delete", reg_path)
    assert result["passed"], result["problems"]
    deletes = [d.url.split("?")[0] for d in requests_to("DELETE", DESCRIPTIONS + "/")]
    # The recipe's delete, then teardown's of the sibling only.
    assert deletes == [f"{DESCRIPTIONS}/SDWZ-001", f"{DESCRIPTIONS}/SDWZ-002"]


@responses.activate
def test_update_and_delete_write_only_tracked_descriptions(reg_path, slept, monkeypatch):
    monkeypatch.setattr(ws, "_own", lambda ctx, sid: (_ for _ in ()).throw(
        AssertionError(f"refusing to write source description {sid}")))
    for name in ("writes.sources.description_update", "writes.sources.description_delete"):
        result, tree, _ctx = run(name, reg_path)
        assert not result["passed"] and "refusing to write" in "\n".join(result["problems"])
        assert tree.live() == []
    assert not [c for c in responses.calls if c.request.method == "POST"
                and c.request.url.split("?")[0].startswith(DESCRIPTIONS + "/")]


# ---------------------------------------------------------------- the fake lies


LIES = {
    ("writes.sources.description_update", "desc_update_ignored"): "negative control failed",
    ("writes.sources.description_update", "desc_update_creates"): "made a new one",
    ("writes.sources.description_update", "desc_update_sibling"):
        "the other source description",
    ("writes.sources.description_delete", "desc_delete_noop"): "negative control failed",
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
def test_create_fails_without_an_id(reg_path, slept, monkeypatch):
    tree = WriteTree()
    real = tree.create_description

    def no_id(request):
        status, _headers, body = real(request)
        return status, {}, body
    monkeypatch.setattr(tree, "create_description", no_id)
    tree.serve()
    recipe = RECIPES["writes.sources.description_create"]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-26")
    assert not result["passed"]
    assert "no X-ENTITY-ID, Entity-Id or Location" in "\n".join(result["problems"])
