"""Offline tests for the pilot recipes against a scripted fake beta (no network, no cookies).

The fake keeps real state (persons by id, deleted or not) so the recipes' read-backs see
what the writes did; each "ignore" mode makes one write a silent no-op (the server says
yes and changes nothing), which the recipes' negative controls must catch.
"""
import itertools
import json
import re
import shutil

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.verify import runner
from tools.verify.recipes import RECIPES, validate_recipes
from tools.verify.recipes import pilot

BETA = Environment.BETA.base_url
PERSONS = BETA + "/platform/tree/persons"
JSON = {"Content-Type": "application/x-fs-v1+json"}
PILOT_OPS = {
    "family_tree.person_create_read": ("createPerson", "readPerson"),
    "family_tree.person_delete_restore": ("deletePerson", "restorePerson"),
}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


class FakeBeta:
    """Persons as id -> {"name", "deleted"}.

    ``ignore`` names the write to no-op ("create", "delete", "restore"), or breaks a read:
    "search" hits for any surname, "unknown-id" reads any id as 200.
    """

    def __init__(self, ignore=None):
        self.ignore = ignore
        self.persons: dict[str, dict] = {}
        self._ids = (f"PL{n:02d}-ZZZ" for n in itertools.count(1))
        self.calls: list[str] = []

    def install(self):
        responses.add(responses.GET, BETA + "/platform/collections/tree", json={
            "collections": [{"links": {"persons": {"href": PERSONS}}}]})
        responses.add(responses.GET, BETA + "/platform/users/current",
                      json={"users": [{"id": "U"}]})
        responses.add_callback(responses.GET, BETA + "/platform/tree/search",
                               callback=self.search)
        responses.add_callback(responses.POST, PERSONS, callback=self.create)
        one = re.compile(re.escape(PERSONS) + r"/([^/?]+)$")
        responses.add_callback(responses.GET, one, callback=self.read)
        responses.add_callback(responses.DELETE, one, callback=self.delete)
        responses.add_callback(
            responses.POST, re.compile(re.escape(PERSONS) + r"/([^/?]+)/restore$"),
            callback=self.restore)
        return self

    @staticmethod
    def _pid(request):
        return request.path_url.split("?")[0].split("/")[4]

    def search(self, request):
        self.calls.append("search")
        surname = request.params.get("q.surname", "")
        if self.ignore == "search":  # search hits for anything: the marker "pre-exists"
            return 200, {"Content-Type": "application/x-gedcomx-atom+json"}, json.dumps(
                {"entries": [{"id": "X", "content": {"gedcomx": {"persons": [
                    {"id": "OLD1-ZZZ", "display": {"name": f"ZZSPIKE Pilot {surname}"}}]}}}]})
        hits = [{"id": pid, "content": {"gedcomx": {"persons": [
            {"id": pid, "display": {"name": p["name"]}}]}}}
            for pid, p in self.persons.items() if not p["deleted"] and surname in p["name"]]
        if not hits:
            return 204, {}, ""
        return 200, {"Content-Type": "application/x-gedcomx-atom+json"}, json.dumps(
            {"entries": hits})

    def create(self, request):
        self.calls.append("create")
        person = json.loads(request.body)["persons"][0]
        assert person["living"] is False
        name = person["names"][0]["nameForms"][0]["fullText"]
        assert name.startswith("ZZSPIKE")
        pid = next(self._ids)
        if self.ignore != "create":
            self.persons[pid] = {"name": name, "deleted": False}
        return 201, {"X-ENTITY-ID": pid, "Location": f"{PERSONS}/{pid}"}, ""

    def read(self, request):
        self.calls.append("read")
        pid = self._pid(request)
        person = self.persons.get(pid)
        if person is None and self.ignore == "unknown-id":
            person = {"name": "Somebody", "deleted": False}
        if person is None:
            return 404, {}, ""
        if person["deleted"]:
            return 410, {}, ""
        return 200, JSON, json.dumps({"persons": [
            {"id": pid, "living": False, "display": {"name": person["name"]}}]})

    def delete(self, request):
        self.calls.append("delete")
        person = self.persons.get(self._pid(request))
        if person is None:
            return 404, {}, ""
        if self.ignore != "delete":
            person["deleted"] = True
        return 204, {}, ""

    def restore(self, request):
        self.calls.append("restore")
        assert not request.body  # least-assuming: a bare POST
        person = self.persons.get(self._pid(request))
        if person is None:
            return 404, {}, ""
        if self.ignore != "restore":
            person["deleted"] = False
        return 204, {}, ""


def run(name, reg_path):
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    return runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                             today="2026-10-25"), ctx


def _entries(reg_path):
    return json.loads(reg_path.read_text())["operations"]


def _assert_verified(reg_path, name):
    entries = _entries(reg_path)
    for op in PILOT_OPS[name]:
        assert entries[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": name, "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-25"}


# ---------------------------------------------------------------- registration


def test_recipes_are_registered_and_reference_real_operations():
    assert validate_recipes(RECIPES) == []
    names = [r.name for r in RECIPES.values()]
    assert len(names) == len(set(names))
    for name, ops in PILOT_OPS.items():
        recipe = RECIPES[name]
        assert recipe.ops == ops and recipe.evidence == "state-change"
        assert all(op in OPERATIONS for op in recipe.ops)
        assert recipe.run.__doc__ and "negative control" in recipe.run.__doc__
    assert {r.name for r in pilot.PILOT} <= set(RECIPES)


# ---------------------------------------------------------------- create / read


@responses.activate
def test_person_create_read_passes_and_records(reg_path):
    fake = FakeBeta().install()
    result, ctx = run("family_tree.person_create_read", reg_path)
    assert result == {"recipe": "family_tree.person_create_read", "passed": True,
                      "problems": []}
    assert ctx.negative_controls >= 1
    # The marker was searched for BEFORE the create; the person was torn down.
    assert fake.calls.index("search") < fake.calls.index("create")
    assert [p["deleted"] for p in fake.persons.values()] == [True]
    name = next(iter(fake.persons.values()))["name"]
    assert name.startswith("ZZSPIKE") and ctx.unique in name
    _assert_verified(reg_path, "family_tree.person_create_read")


@responses.activate
def test_person_create_read_fails_when_create_is_ignored(reg_path):
    FakeBeta(ignore="create").install()
    raw = reg_path.read_bytes()
    result, _ = run("family_tree.person_create_read", reg_path)
    assert result["passed"] is False
    assert any("404" in p for p in result["problems"] if p.startswith("run:"))
    assert reg_path.read_bytes() == raw


@responses.activate
def test_person_create_read_fails_when_marker_already_present(reg_path):
    """A server whose search already shows the marker leaves the create nothing to prove."""
    FakeBeta(ignore="search").install()
    raw = reg_path.read_bytes()
    result, _ = run("family_tree.person_create_read", reg_path)
    assert result["passed"] is False
    assert any("negative control failed" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw


@responses.activate
def test_person_create_read_fails_when_unknown_ids_read_ok(reg_path):
    """A catch-all server (any id reads 200) is refused."""
    FakeBeta(ignore="unknown-id").install()
    raw = reg_path.read_bytes()
    result, _ = run("family_tree.person_create_read", reg_path)
    assert result["passed"] is False
    assert any("ZZZZ-ZZZ" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw


# ---------------------------------------------------------------- delete / restore


@responses.activate
def test_person_delete_restore_passes_and_records(reg_path):
    fake = FakeBeta().install()
    result, ctx = run("family_tree.person_delete_restore", reg_path)
    assert result == {"recipe": "family_tree.person_delete_restore", "passed": True,
                      "problems": []}
    assert ctx.negative_controls >= 2
    # delete, restore, then strict teardown deletes it again.
    assert fake.calls.count("delete") == 2 and fake.calls.count("restore") == 1
    assert [p["deleted"] for p in fake.persons.values()] == [True]
    _assert_verified(reg_path, "family_tree.person_delete_restore")


@pytest.mark.parametrize("ignore", ["delete", "restore"])
@responses.activate
def test_person_delete_restore_fails_when_write_is_ignored(reg_path, ignore):
    FakeBeta(ignore=ignore).install()
    raw = reg_path.read_bytes()
    result, _ = run("family_tree.person_delete_restore", reg_path)
    assert result["passed"] is False
    assert any("negative control failed" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw


# ---------------------------------------------------------------- through the runner CLI


@pytest.fixture
def offline_runner(monkeypatch):
    """runner.main with a fake token, clean tree, fixed commit and no write throttle."""
    monkeypatch.setattr(runner, "extract_brave_token", lambda: "tkn")
    monkeypatch.setattr(runner, "git_status_porcelain", lambda: "")
    monkeypatch.setattr(runner, "current_commit", lambda: "abc123")
    monkeypatch.setattr(runner, "FSClient",
                        lambda token, env: FSClient(token, env, min_write_interval=0))


@responses.activate
def test_runner_all_records_pilot_ops_verified(reg_path, offline_runner, capsys, monkeypatch):
    monkeypatch.setattr(runner, "RECIPES", {r.name: r for r in pilot.PILOT})
    FakeBeta().install()
    code = runner.main(["run", "--all", "--registry", str(reg_path)])
    out = capsys.readouterr()
    assert code == 0, out
    entries = _entries(reg_path)
    for name, ops in PILOT_OPS.items():
        for op in ops:
            gen = entries[op]["generated"]
            assert gen["status"] == "verified" and gen["recipe"] == name
            assert gen["fingerprint"] == OPERATIONS[op].fingerprint
            assert gen["evidence"] == "state-change" and gen["commit"] == "abc123"
    # handwritten provenance is preserved
    assert entries["createPerson"]["handwritten"]["status"] == "verified"
    assert all(c.request.url.startswith(BETA) for c in responses.calls)


@pytest.mark.parametrize("ignore", ["create", "delete", "restore"])
@responses.activate
def test_runner_all_records_nothing_when_a_write_is_ignored(reg_path, offline_runner, ignore,
                                                            capsys, monkeypatch):
    monkeypatch.setattr(runner, "RECIPES", {r.name: r for r in pilot.PILOT})
    FakeBeta(ignore=ignore).install()
    before = _entries(reg_path)
    assert runner.main(["run", "--all", "--registry", str(reg_path)]) == 1
    after = _entries(reg_path)
    failed = "family_tree.person_create_read" if ignore == "create" \
        else "family_tree.person_delete_restore"
    for op in PILOT_OPS[failed]:
        assert after[op] == before[op]
    assert f"FAIL {failed}" in capsys.readouterr().out
