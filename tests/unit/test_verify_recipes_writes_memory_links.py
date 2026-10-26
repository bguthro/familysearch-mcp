"""Offline tests for the memory persona, reference and portrait write recipes.

As the other write recipe tests: the registered recipes run through the real
``run_recipe`` (no network, no cookies) against ``WriteTree``. Honest, each passes, is
recorded verified and leaves nothing behind; in each lie mode (a link not made, made for
the wrong person, not removed, the wrong or every link removed, a portrait on the wrong
person...) the recipe fails for that reason, the registry is untouched and teardown still
removes everything.
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
from tools.verify.recipes import writes_memory_links as wml

from _fake_tree import AGENT, BETA, MEMORIES, PERSONS, UID, WriteTree

OPS = {
    "writes.memories.persona_create": ("createMemoryPersona",),
    "writes.memories.persona_update": ("updateMemoryPersona",),
    "writes.memories.persona_delete": ("deleteMemoryPersona",),
    "writes.memories.portraits_update": ("updatePersonPortraits",),
    "writes.memories.portrait_delete": ("deletePersonPortrait",),
}
UNCLAIMED = {"readMemory", "readMemoryPersonas", "readMemoryPersona", "getPersonMemories",
             "readPersonPortraits", "readPerson"}


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
    assert {r.name for r in wml.WRITES_MEMORY_LINKS} == set(OPS)
    for recipe in wml.WRITES_MEMORY_LINKS:
        assert RECIPES[recipe.name] is recipe
        assert recipe.ops == OPS[recipe.name] and recipe.evidence == "state-change"
        assert not set(recipe.ops) & UNCLAIMED
        assert all(OPERATIONS[op].kind == "write" for op in recipe.ops)
        assert recipe.run.__doc__ and "Negative control" in recipe.run.__doc__


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
def test_persona_create_posts_the_proven_link_body_for_the_second_person(reg_path, slept):
    result, tree, ctx = run("writes.memories.persona_create", reg_path, memory_lag=2)
    assert result["passed"], result["problems"]
    # The second person is WZ02: the claimed link is the last POST to the personas.
    *_planted, post = requests_to("POST", MEMORIES + "/4100001/personas")
    [person] = json.loads(post.body)["persons"]
    assert person["media"] == [{"description": f"{MEMORIES}/4100001"}]
    assert person["identifiers"] == {
        "http://familysearch.org/v1/FamilyTreePerson": [f"{BETA}/ark:/61903/4:1:WZ02-ZZZ"]}
    assert slept  # the lagging memory lists were polled


@responses.activate
def test_persona_update_posts_the_owners_persona_by_id_with_name_parts(reg_path, slept):
    result, tree, _ctx = run("writes.memories.persona_update", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", MEMORIES + "/4100001/personas/")
    assert post.url.split("?")[0] == f"{MEMORIES}/4100001/personas/4100001-1"
    [person] = json.loads(post.body)["persons"]
    assert person["id"] == "4100001-1"
    form = person["names"][0]["nameForms"][0]
    assert form["parts"] and form["fullText"].startswith("ZZSPIKEN")


@responses.activate
def test_persona_delete_removes_the_second_persons_persona_by_id(reg_path, slept):
    result, tree, _ctx = run("writes.memories.persona_delete", reg_path)
    assert result["passed"], result["problems"]
    [delete] = requests_to("DELETE", MEMORIES + "/4100001/personas/")
    assert delete.url.split("?")[0] == f"{MEMORIES}/4100001/personas/4100001-2"


@responses.activate
def test_portraits_update_posts_the_memory_description_for_the_first_person(reg_path, slept):
    result, tree, _ctx = run("writes.memories.portraits_update", reg_path)
    assert result["passed"], result["problems"]
    [post] = requests_to("POST", PERSONS + "/WZ01-ZZZ/portraits")
    assert json.loads(post.body) == {"persons": [{"id": "WZ01-ZZZ", "media": [{
        "description": f"{MEMORIES}/4100001", "qualifiers": [{
            "name": "http://gedcomx.org/RectangleRegion", "value": "0.0,0.0,1.0,1.0"}]}]}]}


@responses.activate
def test_portrait_delete_clears_only_the_first_persons_portrait(reg_path, slept):
    result, tree, _ctx = run("writes.memories.portrait_delete", reg_path)
    assert result["passed"], result["problems"]
    [delete] = requests_to("DELETE", PERSONS + "/WZ01-ZZZ/portrait")
    assert delete.url.split("?")[0] == f"{PERSONS}/WZ01-ZZZ/portrait"
    # Both portraits were planted by the fixtures (two POSTs), none by the recipe.
    assert len(requests_to("POST", PERSONS + "/WZ01-ZZZ/portraits")) == 1


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_lagging_lists_are_polled_but_not_forever(name, reg_path, slept):
    attempts, pause = wc.EVENTUAL
    result, tree, _ctx = run(name, reg_path, memory_lag=attempts - 1)
    assert result["passed"], result["problems"]
    slept.clear()
    result, tree, _ctx = run(name, reg_path, memory_lag=attempts)
    assert not result["passed"]
    assert any("not seen after" in p for p in result["problems"]), result["problems"]
    assert tree.live() == []


@responses.activate
@pytest.mark.parametrize("name", sorted(OPS))
def test_recipes_write_only_tracked_memories(name, reg_path, slept, monkeypatch):
    monkeypatch.setattr(wml, "own", lambda ctx, kind, oid: (_ for _ in ()).throw(
        AssertionError(f"refusing to write {kind} {oid}")))
    result, tree, ctx = run(name, reg_path)
    if name.endswith(("portrait_delete",)):
        assert result["passed"]  # writes only the person's portrait of fixtures it made
        return
    assert not result["passed"] and "refusing to write" in "\n".join(result["problems"])
    assert not set(OPS[name]) & ctx.api.called
    assert tree.live() == []


# ---------------------------------------------------------------- the fake lies


LIES = {
    ("writes.memories.persona_create", "persona_create_ignored"): "not seen after",
    ("writes.memories.persona_create", "persona_wrong_person"): "not seen after",
    ("writes.memories.persona_create", "persona_create_replaces"): "not seen after",
    ("writes.memories.persona_update", "persona_update_ignored"): "not seen after",
    ("writes.memories.persona_update", "persona_update_creates"): "not seen after",
    ("writes.memories.persona_update", "persona_update_sibling"): "the other persona",
    ("writes.memories.persona_update", "persona_update_person"): "tree names",
    ("writes.memories.persona_delete", "persona_delete_noop"): "not seen after",
    ("writes.memories.persona_delete", "persona_delete_wrong"): "not seen after",
    ("writes.memories.persona_delete", "persona_delete_all"): "not exactly",
    ("writes.memories.persona_delete", "persona_delete_memory"): "410",
    ("writes.memories.portraits_update", "portrait_ignored"): "not seen after",
    ("writes.memories.portraits_update", "portrait_wrong_person"): "not seen after",
    ("writes.memories.portraits_update", "portrait_auto"): "already",
    ("writes.memories.portraits_update", "portrait_both"): "another person too",
    ("writes.memories.portrait_delete", "portrait_delete_noop"): "not seen after",
    ("writes.memories.portrait_delete", "portrait_delete_wrong"): "not seen after",
    ("writes.memories.portrait_delete", "portrait_delete_all"): "should not touch it",
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


def test_deferred_reference_delete_is_explained():
    assert set(wml.DEFERRED) == {"deletePersonMemoriesPersonaReference"}
    assert "entityId must not be empty" in wml.DEFERRED["deletePersonMemoriesPersonaReference"]


def test_every_fixture_png_is_valid_and_unique():
    import zlib
    from tools.verify.fixtures import unique_png
    pngs = {unique_png() for _ in range(20)}
    assert len(pngs) == 20
    for png in pngs:
        assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")
        pos = 8
        while pos < len(png):  # every chunk's CRC checks out
            n = int.from_bytes(png[pos:pos + 4], "big")
            body = png[pos + 4:pos + 8 + n]
            assert int.from_bytes(png[pos + 8 + n:pos + 12 + n], "big") == zlib.crc32(body)
            pos += 12 + n


@responses.activate
def test_a_duplicate_artifact_is_refused_409_like_beta(reg_path, slept):
    from familysearch.client import FSClient as C
    from familysearch.discovery import Discovery
    from familysearch.errors import FSHTTPError
    from familysearch.memories import Memories
    from tools.verify.fixtures import unique_png
    WriteTree().serve()
    client = C("tkn", Environment.BETA, min_write_interval=0)
    memories = Memories(client, Discovery(client))
    data = unique_png()
    memories.upload(data, filename="a.png", content_type="image/png", reason="r")
    with pytest.raises(FSHTTPError) as e:
        memories.upload(data, filename="a.png", content_type="image/png", reason="r")
    assert e.value.status == 409
