"""Offline tests for the live-verification framework core (no network, no cookies)."""
import json
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH, OperationLockedError
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError
from tools.spec.bootstrap_registry import render
from tools.verify import record, teardown
from tools.verify.core import Recipe, VerifyApi, expect_changed
from tools.verify.fixtures import FixtureSet
from tools.verify.recipes import RECIPES, validate_recipes

BETA = Environment.BETA.base_url
PERSONS = BETA + "/platform/tree/persons"


def beta_client():
    return FSClient("tkn", Environment.BETA, min_write_interval=0)


def registry(**generated):
    return {"schema": 1, "operations": {
        oid: {"generated": g} for oid, g in generated.items()}}


# ---------------------------------------------------------------- VerifyApi


def test_verify_api_refuses_production_client():
    prod = FSClient("tkn", Environment.PRODUCTION, min_write_interval=0)
    with pytest.raises(FSError, match="beta"):
        VerifyApi(prod, ops=("createPerson",))


def test_verify_api_refuses_environment_lookalike():
    fake = SimpleNamespace(environment=SimpleNamespace(value=BETA, name="BETA"))
    with pytest.raises(FSError):
        VerifyApi(fake, ops=())


@responses.activate
def test_verify_api_injects_unlock_flags_and_records_calls():
    responses.add(responses.POST, PERSONS, status=201, headers={"X-ENTITY-ID": "P1"})
    responses.add(responses.DELETE, PERSONS + "/P1", status=204)
    responses.add(responses.GET, PERSONS + "/P1", json={"persons": [{"id": "P1"}]})
    api = VerifyApi(beta_client(), ops=("createPerson", "deletePerson", "readPerson"),
                    registry=registry(createPerson={"status": "unverified"},
                                      deletePerson={"status": "unverified"}))
    assert api.called == set()
    api.create_person(reason="r", body={"persons": []})
    api.delete_person("P1", reason="r")  # destructive: confirm injected
    api.read_person("P1")
    assert api.called == {"createPerson", "deletePerson", "readPerson"}


@responses.activate
def test_verify_api_refuses_undeclared_write():
    api = VerifyApi(beta_client(), ops=("readPerson",),
                    registry=registry(createPerson={"status": "unverified"}))
    with pytest.raises(OperationLockedError):
        api.create_person(reason="r", body={})
    assert api.called == set() and len(responses.calls) == 0


@responses.activate
@pytest.mark.parametrize("flags", [
    {}, {"allow_unverified": True, "confirm_destructive": True},
    {"allow_unverified": True, "confirm_destructive": True, "allow_production": True},
])
def test_verify_api_undeclared_write_ignores_caller_unlock_flags(flags):
    api = VerifyApi(beta_client(), ops=("readPerson",), registry=registry(
        deletePerson={"status": "unverified"}))
    with pytest.raises(OperationLockedError):
        api.delete_person("P1", reason="r", **flags)
    assert api.called == set() and len(responses.calls) == 0


@responses.activate
def test_verify_api_forces_flags_false_for_undeclared_writes(monkeypatch):
    import familysearch.api._runtime as runtime

    seen = []
    monkeypatch.setattr(runtime, "check_call", lambda *a, **kw: seen.append(kw))
    responses.add(responses.POST, PERSONS, status=201)
    api = VerifyApi(beta_client(), ops=("readPerson",), registry=registry())
    api.create_person(reason="r", body={}, allow_unverified=True, confirm_destructive=True,
                      allow_production=True)
    assert seen == [{"allow_unverified": False, "allow_production": False,
                     "confirm_destructive": False}]


@responses.activate
def test_verify_api_still_refuses_unverifiable_op():
    api = VerifyApi(beta_client(), ops=("updateVocabTranslation",), registry=registry(
        updateVocabTranslation={"status": "unverifiable", "reason": "shared data"}))
    spec = OPERATIONS["updateVocabTranslation"]
    args = {p: "x" for p in spec.path_params}
    with pytest.raises(OperationLockedError, match="unverifiable"):
        getattr(api, spec.method_name)(**args, reason="r")
    assert api.called == set() and len(responses.calls) == 0


@responses.activate
def test_verify_api_never_passes_allow_production(monkeypatch):
    import familysearch.api._runtime as runtime

    seen = []
    monkeypatch.setattr(runtime, "check_call", lambda *a, **kw: seen.append(kw))
    responses.add(responses.POST, PERSONS, status=201)
    api = VerifyApi(beta_client(), ops=("createPerson",), registry=registry())
    api.create_person(reason="r", body={}, allow_production=True)
    assert seen == [{"allow_unverified": True, "allow_production": False,
                     "confirm_destructive": True}]


@responses.activate
def test_failed_call_is_not_recorded_as_called():
    responses.add(responses.GET, PERSONS + "/P1", status=500)
    api = VerifyApi(beta_client(), ops=("readPerson",), registry=registry())
    with pytest.raises(FSError):
        api.read_person("P1")
    assert api.called == set()


def test_expect_changed():
    expect_changed(1, 2, "x")
    with pytest.raises(AssertionError, match="negative control failed: notes did not change"):
        expect_changed([1], [1], "notes")


# ---------------------------------------------------------------- teardown move


def test_verified_delete_moved_and_live_teardown_tests_still_pass():
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    try:
        import _tracker
    finally:
        sys.path.pop(0)
    assert _tracker.verified_delete is teardown.verified_delete
    errors = []
    teardown.verified_delete("t", "u", lambda: None, lambda: None, errors)
    assert errors == ["t: still readable at u after delete"]


# ---------------------------------------------------------------- FixtureSet


class FakeTree:
    """A scripted beta tree over `responses`: persons and relationships by URL."""

    def __init__(self, rsps):
        self.rsps = rsps
        self.live: set[str] = set()
        self.created: list[dict] = []
        self.next_id = 0
        rsps.add(responses.GET, BETA + "/platform/collections/tree", json={"collections": [{
            "links": {
                "persons": {"href": PERSONS + "?flag=fsh"},
                "relationships": {"href": BETA + "/platform/tree/relationships"},
                "artifacts": {"href": BETA + "/platform/memories/memories"},
                "source-descriptions": {"href": BETA + "/platform/sources/descriptions"},
            }}]})
        rsps.add_callback(responses.POST, PERSONS, callback=self._create)
        pat = re.compile(r"https://beta\.familysearch\.org/platform/(tree|memories|sources)/.+")
        rsps.add_callback(responses.GET, pat, callback=self._get)
        rsps.add_callback(responses.DELETE, pat, callback=self._delete)

    @staticmethod
    def _key(url):
        return url.split("?")[0]

    def add(self, url):
        self.live.add(self._key(url))

    def _create(self, request):
        self.next_id += 1
        pid = f"P{self.next_id}"
        self.created.append(json.loads(request.body))
        self.add(PERSONS + "/" + pid)
        return 201, {"X-ENTITY-ID": pid}, ""

    def _get(self, request):
        return (200, {}, "{}") if self._key(request.url) in self.live else (404, {}, "")

    def _delete(self, request):
        k = self._key(request.url)
        if k not in self.live:
            return 404, {}, ""
        self.live.discard(k)
        return 204, {}, ""


@pytest.fixture
def tree():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeTree(rsps)


def test_fixtureset_person_is_tracked_before_assertions(tree):
    fx = FixtureSet(beta_client(), reason="ZZSPIKE test")
    pid = fx.person(given="Ann", gender="Female", birth_year="1850")
    assert fx.tracked("person") == [pid]  # tracked the moment it exists
    body = tree.created[0]["persons"][0]
    assert body["living"] is False
    assert body["names"][0]["nameForms"][0]["fullText"].count("ZZSPIKE") == 1
    assert "ZZSPIKE" in json.dumps(body["names"])
    assert any("1850" in json.dumps(f) for f in body["facts"])
    # No call happens between the create and the track: discovery, then the POST only.
    assert [c.request.method for c in tree.rsps.calls] == ["GET", "POST"]
    assert fx.teardown() == [] and tree.live == set()


@responses.activate
def test_fixtureset_refuses_production_client():
    prod = FSClient("tkn", Environment.PRODUCTION, min_write_interval=0)
    with pytest.raises(FSError, match="beta"):
        FixtureSet(prod, reason="ZZSPIKE test")
    assert len(responses.calls) == 0


def test_fixtureset_teardown_order_and_kinds(tree):
    fx = FixtureSet(beta_client(), reason="ZZSPIKE test")
    pid = fx.person()
    urls = {
        "couple": BETA + "/platform/tree/couple-relationships/R1",
        "child_and_parents": BETA + "/platform/tree/child-and-parents-relationships/C1",
        "memory": BETA + "/platform/memories/memories/M1",
        "source": BETA + "/platform/sources/descriptions/S1",
    }
    for url in urls.values():
        tree.add(url)
    fx.track("source", "S1")
    fx.track("memory", "M1")
    fx.track("couple", "R1")
    fx.track("child_and_parents", "C1")
    assert fx.teardown() == [] and tree.live == set()
    deletes = [c.request.url.split("?")[0] for c in tree.rsps.calls
               if c.request.method == "DELETE"]
    assert deletes == [urls["child_and_parents"], urls["couple"], urls["source"],
                       urls["memory"], PERSONS + "/" + pid]
    assert all(c.request.headers.get("X-Reason") for c in tree.rsps.calls
               if c.request.method == "DELETE")
    assert fx.teardown() == []  # nothing left to do


def test_fixtureset_teardown_reports_problems_and_continues(tree):
    fx = FixtureSet(beta_client(), reason="ZZSPIKE test")
    pid = fx.person()
    fx.track("memory", "GHOST")
    problems = fx.teardown()
    assert len(problems) == 1 and "GHOST" in problems[0]
    assert PERSONS + "/" + pid not in tree.live


def test_fixtureset_mark_deleted_and_bad_kind(tree):
    fx = FixtureSet(beta_client(), reason="ZZSPIKE test")
    fx.track("person", "GONE")
    fx.mark_deleted("person", "GONE")
    assert fx.teardown() == []
    with pytest.raises(ValueError):
        fx.track("note", "N1")


def test_fixtureset_requires_reason():
    with pytest.raises(ValueError):
        FixtureSet(beta_client(), reason="  ")


# ---------------------------------------------------------------- record


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


def _record(path, op="createPerson", **kw):
    args = dict(recipe="person-roundtrip", evidence="state-change",
                fingerprint=OPERATIONS[op].fingerprint if op in OPERATIONS else "x", commit="abc123",
                verified_on="2026-10-25", path=path)
    args.update(kw)
    record.record_verified(op, **args)


def test_real_registry_is_in_canonical_format():
    assert REGISTRY_PATH.read_text() == render(json.loads(REGISTRY_PATH.read_text()))


def test_record_preserves_other_entries_and_formatting(reg_path):
    before = json.loads(reg_path.read_text())
    _record(reg_path)
    text = reg_path.read_text()
    after = json.loads(text)
    assert text == render(after) and text.endswith("}\n")
    assert after["operations"]["createPerson"]["handwritten"] == \
        before["operations"]["createPerson"]["handwritten"]
    for oid in before["operations"]:
        if oid != "createPerson":
            assert after["operations"][oid] == before["operations"][oid]
    assert {k: v for k, v in after.items() if k != "operations"} == \
        {k: v for k, v in before.items() if k != "operations"}


def test_record_many_preserves_handwritten_for_read_op(reg_path):
    before = json.loads(reg_path.read_text())["operations"]["readPerson"]["handwritten"]
    record.record_many_verified(["readPerson"], "r", "read-content", "c", "d", path=reg_path)
    assert json.loads(reg_path.read_text())["operations"]["readPerson"]["handwritten"] == before


def test_record_keeps_file_mode(reg_path):
    reg_path.chmod(0o644)
    _record(reg_path)
    assert reg_path.stat().st_mode & 0o777 == 0o644


def test_pass_records_fingerprint_commit_date_and_evidence(reg_path):
    _record(reg_path, evidence="read-content", op="readPerson", recipe="read-person")
    entry = json.loads(reg_path.read_text())["operations"]["readPerson"]["generated"]
    assert entry == {"status": "verified", "fingerprint": OPERATIONS["readPerson"].fingerprint,
                     "recipe": "read-person", "evidence": "read-content",
                     "commit": "abc123", "verified_on": "2026-10-25"}


@pytest.mark.parametrize("kw", [
    {"op": "noSuchOperation"},
    {"evidence": "vibes"},
    {"recipe": " "},
    {"commit": ""},
    {"verified_on": ""},
    {"fingerprint": "deadbeef"},
])
def test_record_verified_validates(reg_path, kw):
    raw = reg_path.read_bytes()
    with pytest.raises(ValueError):
        _record(reg_path, **kw)
    assert reg_path.read_bytes() == raw


def test_record_verified_refuses_missing_entry(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(render({"schema": 1, "operations": {}}))
    with pytest.raises(ValueError):
        _record(p)


def test_record_unverifiable_into_missing_entry_creates_generated_only(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(render({"schema": 1, "operations": {}}))
    record.record_unverifiable("createPersonNote", "edits shared data", path=p)
    ops = json.loads(p.read_text())["operations"]
    assert ops == {"createPersonNote": {"generated": {
        "status": "unverifiable", "reason": "edits shared data"}}}


@pytest.mark.parametrize("reason", ["", "   ", None, 5])
def test_record_unverifiable_requires_reason(reg_path, reason):
    raw = reg_path.read_bytes()
    with pytest.raises(ValueError):
        record.record_unverifiable("createPersonNote", reason, path=reg_path)
    assert reg_path.read_bytes() == raw


def test_record_unverifiable_requires_known_operation(reg_path):
    with pytest.raises(ValueError):
        record.record_unverifiable("noSuchOperation", "x", path=reg_path)


def test_record_unverifiable_replaces_generated_and_keeps_handwritten(reg_path):
    record.record_unverifiable("createPerson", "cannot prove it", path=reg_path)
    entry = json.loads(reg_path.read_text())["operations"]["createPerson"]
    assert entry["generated"] == {"status": "unverifiable", "reason": "cannot prove it"}
    assert entry["handwritten"]["status"] == "verified"


def test_record_never_leaves_flat_or_extra_keys(reg_path):
    _record(reg_path)
    record.record_unverifiable("createPersonNote", "x", path=reg_path)
    ops = json.loads(reg_path.read_text())["operations"]
    assert all(set(e) <= {"generated", "handwritten"} and "generated" in e
               for e in ops.values())


def test_record_refuses_flat_entry(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(render({"schema": 1, "operations": {"createPerson": {"status": "unverified"}}}))
    raw = p.read_bytes()
    with pytest.raises(ValueError):
        _record(p)
    with pytest.raises(ValueError):
        record.record_unverifiable("createPerson", "x", path=p)
    assert p.read_bytes() == raw


# ---------------------------------------------------------------- recipes


def _noop(ctx):
    pass


def test_recipes_registry_is_consistent():
    assert validate_recipes(RECIPES) == []


@pytest.mark.parametrize("recipes, fragment", [
    ({"a": Recipe("a", ("noSuchOperation",), "state-change", _noop)}, "noSuchOperation"),
    ({"a": Recipe("b", ("readPerson",), "read-content", _noop)}, "key"),
    ({"a": Recipe("a", ("readPerson",), "vibes", _noop)}, "evidence"),
    ({"a": Recipe("a", (), "read-content", _noop)}, "no ops"),
])
def test_validate_recipes_rejects_bad_recipes(recipes, fragment):
    problems = validate_recipes(recipes)
    assert problems and any(fragment in p for p in problems)


def test_validate_recipes_rejects_duplicate_names():
    r = Recipe("a", ("readPerson",), "read-content", _noop)
    assert any("duplicate" in p for p in validate_recipes({"a": r, "b": r}))
