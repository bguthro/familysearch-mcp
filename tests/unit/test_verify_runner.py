"""Offline tests for the live-verification runner (no network, no cookies)."""
import json
import shutil

import pytest
import requests
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError
from tools.verify import record, runner
from tools.verify.core import Recipe, VerifyApi, VerifyContext, expect_changed
from tools.verify.fixtures import FixtureSet
from tools.verify.recipes import validate_recipes

BETA = Environment.BETA.base_url
PROD = Environment.PRODUCTION.base_url
PERSONS = BETA + "/platform/tree/persons"
TOKEN = "tok-SECRET-do-not-print"


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


def make_ctx(recipe, reg_path):
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    return runner.make_context(client, recipe, registry_path=reg_path)


def run(recipe, reg_path):
    return runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                             commit="abc123", today="2026-10-25")


class Fake:
    """Teardown-free fixtures stand-in that can report problems."""

    def __init__(self, problems=()):
        self.problems = list(problems)
        self.torn_down = False

    def teardown(self):
        self.torn_down = True
        return self.problems


def ctx_with(recipe, reg_path, fixtures):
    ctx = make_ctx(recipe, reg_path)
    ctx.fixtures = fixtures
    return ctx


def _person_notes(ctx, pid):
    return ctx.api.read_person_notes(pid).body


@responses.activate
def test_noop_operation_fails_negative_control(reg_path):
    # The "write" is accepted by the server but changes nothing.
    responses.add(responses.GET, PERSONS + "/P1/notes", json={"persons": [{"notes": []}]})
    responses.add(responses.POST, PERSONS + "/P1/notes", status=201)

    def body(ctx):
        before = _person_notes(ctx, "P1")
        ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})
        ctx.expect_changed(before, _person_notes(ctx, "P1"), "person notes")

    recipe = Recipe("noop", ("createPersonNote", "readPersonNotes"), "state-change", body)
    raw = reg_path.read_bytes()
    ctx = ctx_with(recipe, reg_path, Fake())
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc",
                               today="2026-10-25")
    assert result["passed"] is False
    assert any("negative control failed" in p for p in result["problems"])
    assert ctx.fixtures.torn_down
    assert reg_path.read_bytes() == raw


@responses.activate
def test_recipe_claiming_uncalled_op_fails(reg_path):
    responses.add(responses.POST, PERSONS, status=201, headers={"X-ENTITY-ID": "P1"})

    def body(ctx):
        ctx.api.create_person(reason=ctx.reason, body={"persons": []})
        ctx.expect_changed(None, "P1", "person")

    recipe = Recipe("liar", ("createPerson", "readPerson"), "state-change", body)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, ctx_with(recipe, reg_path, Fake()),
                               registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False
    assert any("readPerson" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw


@responses.activate
def test_teardown_problem_blocks_verification(reg_path):
    responses.add(responses.GET, PERSONS + "/P1", json={"persons": [{"id": "P1"}]})

    def body(ctx):
        assert ctx.api.read_person("P1").body["persons"][0]["id"] == "P1"

    recipe = Recipe("leaky", ("readPerson",), "read-content", body)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, ctx_with(recipe, reg_path, Fake(["person P9: leftover"])),
                               registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False
    assert any("leftover" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw


@responses.activate
def test_teardown_exception_blocks_verification(reg_path):
    responses.add(responses.GET, PERSONS + "/P1", json={})

    class Boom(Fake):
        def teardown(self):
            raise RuntimeError("teardown exploded")

    recipe = Recipe("boom", ("readPerson",), "read-content",
                    lambda ctx: ctx.api.read_person("P1"))
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, ctx_with(recipe, reg_path, Boom()),
                               registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw


@responses.activate
def test_state_change_recipe_without_negative_control_fails(reg_path):
    responses.add(responses.POST, PERSONS, status=201, headers={"X-ENTITY-ID": "P1"})
    recipe = Recipe("unproven", ("createPerson",), "state-change",
                    lambda ctx: ctx.api.create_person(reason=ctx.reason, body={}))
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, ctx_with(recipe, reg_path, Fake()),
                               registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False
    assert any("negative control" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw


@responses.activate
def test_passing_recipe_records_every_op_once(reg_path):
    notes = {"persons": [{"notes": []}]}
    responses.add_callback(responses.GET, PERSONS + "/P1/notes",
                           callback=lambda r: (200, {"Content-Type": "application/json"},
                                                    json.dumps(notes)))

    def post(request):
        notes["persons"][0]["notes"].append({"id": "N1"})
        return 201, {"X-ENTITY-ID": "N1"}, ""

    responses.add_callback(responses.POST, PERSONS + "/P1/notes", callback=post)

    def body(ctx):
        before = _person_notes(ctx, "P1")
        ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})
        ctx.expect_changed(before, _person_notes(ctx, "P1"), "person notes")

    ops = ("createPersonNote", "readPersonNotes")
    recipe = Recipe("person-note", ops, "state-change", body)
    result = runner.run_recipe(recipe, ctx_with(recipe, reg_path, Fake()),
                               registry_path=reg_path, commit="abc123", today="2026-10-25")
    assert result == {"recipe": "person-note", "passed": True, "problems": []}
    entries = json.loads(reg_path.read_text())["operations"]
    for op in ops:
        assert entries[op]["generated"] == {
            "status": "verified", "fingerprint": OPERATIONS[op].fingerprint,
            "recipe": "person-note", "evidence": "state-change", "commit": "abc123",
            "verified_on": "2026-10-25"}


@responses.activate
def test_failed_multi_op_record_writes_nothing(reg_path, monkeypatch):
    """If any op can't be recorded, none are (no half-written registry)."""
    responses.add(responses.GET, PERSONS + "/P1", json={})
    responses.add(responses.GET, PERSONS + "/P1/notes", json={})
    data = json.loads(reg_path.read_text())
    del data["operations"]["readPersonNotes"]
    reg_path.write_text(json.dumps(data, sort_keys=True, indent=1) + "\n")
    raw = reg_path.read_bytes()

    def body(ctx):
        ctx.api.read_person("P1")
        ctx.api.read_person_notes("P1")

    recipe = Recipe("two", ("readPerson", "readPersonNotes"), "read-content", body)
    result = runner.run_recipe(recipe, ctx_with(recipe, reg_path, Fake()),
                               registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw


@responses.activate
def test_real_fixtures_torn_down_after_recipe_failure(reg_path):
    responses.add(responses.GET, BETA + "/platform/collections/tree", json={"collections": [
        {"links": {"persons": {"href": PERSONS}}}]})
    live = set()

    def create(request):
        live.add("P1")
        return 201, {"X-ENTITY-ID": "P1"}, ""

    responses.add_callback(responses.POST, PERSONS, callback=create)
    responses.add_callback(responses.GET, PERSONS + "/P1",
                           callback=lambda r: (200, {}, "{}") if live else (404, {}, ""))

    def delete(request):
        live.discard("P1")
        return 204, {}, ""

    responses.add_callback(responses.DELETE, PERSONS + "/P1", callback=delete)

    def body(ctx):
        ctx.fixtures.person()
        raise AssertionError("recipe failed after creating a fixture")

    recipe = Recipe("fails", ("readPerson",), "read-content", body)
    result = run(recipe, reg_path)
    assert result["passed"] is False and live == set()


def test_make_context_builds_fresh_beta_objects(reg_path):
    recipe = Recipe("r", ("readPerson",), "read-content", lambda ctx: None)
    a, b = make_ctx(recipe, reg_path), make_ctx(recipe, reg_path)
    assert isinstance(a.api, VerifyApi) and isinstance(a.fixtures, FixtureSet)
    assert isinstance(a, VerifyContext)
    assert a.api is not b.api and a.api.called is not b.api.called
    assert a.unique.startswith("ZZSPIKE") and len(a.unique) == len("ZZSPIKE") + 8
    assert a.unique != b.unique and "ZZSPIKE" in a.reason


def test_expect_changed_on_context_counts_controls(reg_path):
    recipe = Recipe("r", ("readPerson",), "read-content", lambda ctx: None)
    ctx = make_ctx(recipe, reg_path)
    ctx.expect_changed(1, 2, "x")
    assert ctx.negative_controls == 1
    with pytest.raises(AssertionError):
        ctx.expect_changed(1, 1, "x")
    assert ctx.negative_controls == 1
    expect_changed("a", "b", "y")


# ---------------------------------------------------------------- main


@pytest.fixture
def fake_recipes(monkeypatch):
    ran = []
    recipe = Recipe("read-user", ("readCurrentUser",), "read-content",
                    lambda ctx: ran.append(ctx))
    monkeypatch.setattr(runner, "RECIPES", {"read-user": recipe})
    return ran


@pytest.fixture
def no_cookies(monkeypatch):
    calls = []

    def extract():
        calls.append(1)
        return TOKEN

    monkeypatch.setattr(runner, "extract_brave_token", extract)
    return calls


@pytest.fixture
def clean_tree(monkeypatch):
    monkeypatch.setattr(runner, "git_status_porcelain", lambda: "")


def _assert_production_untouched():
    assert all(not c.request.url.startswith(PROD) for c in responses.calls)


@responses.activate
@pytest.mark.parametrize("beta_status", [401, 403, 204, 500, "down"])
def test_runner_main_refuses_when_beta_check_fails(beta_status, reg_path, fake_recipes,
                                                   no_cookies, clean_tree, capsys):
    # Production is registered nowhere: any request to it would raise ConnectionError.
    if beta_status == "down":
        responses.add(responses.GET, BETA + "/platform/users/current",
                      body=requests.ConnectionError("down"))
    else:
        responses.add(responses.GET, BETA + "/platform/users/current", status=beta_status,
                      json={"users": [{"id": "U"}]})
    raw = reg_path.read_bytes()
    code = runner.main(["run", "--all", "--registry", str(reg_path)])
    assert code == 2
    assert fake_recipes == []
    assert reg_path.read_bytes() == raw
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert "abort" in out.err.lower()
    assert "not a beta session (are you signed into production?)" in out.err
    assert len(responses.calls) == 1
    _assert_production_untouched()


@responses.activate
def test_runner_main_runs_on_verified_beta(reg_path, fake_recipes, no_cookies, clean_tree,
                                           capsys, monkeypatch):
    monkeypatch.setattr(runner, "current_commit", lambda: "abc123")
    responses.add(responses.GET, BETA + "/platform/users/current",
                  json={"users": [{"id": "U"}]})

    def body(ctx):
        fake_recipes.append(ctx)
        assert ctx.api.read_current_user().status == 200

    monkeypatch.setitem(runner.RECIPES, "read-user", Recipe(
        "read-user", ("readCurrentUser",), "read-content", body))
    code = runner.main(["run", "--recipe", "read-user", "--registry", str(reg_path)])
    out = capsys.readouterr()
    assert code == 0, out
    assert TOKEN not in out.out + out.err
    ctx = fake_recipes[0]
    assert ctx.client.environment is Environment.BETA
    entry = json.loads(reg_path.read_text())["operations"]["readCurrentUser"]["generated"]
    assert entry["status"] == "verified" and entry["commit"] == "abc123"
    assert all(c.request.url.startswith(BETA) for c in responses.calls)
    _assert_production_untouched()


@responses.activate
def test_runner_main_dry_run_touches_nothing(reg_path, fake_recipes, no_cookies, capsys):
    raw = reg_path.read_bytes()
    assert runner.main(["run", "--all", "--dry-run", "--registry", str(reg_path)]) == 0
    out = capsys.readouterr().out
    assert "read-user" in out and "readCurrentUser" in out
    assert no_cookies == [] and fake_recipes == [] and len(responses.calls) == 0
    assert reg_path.read_bytes() == raw


@responses.activate
def test_runner_main_unknown_recipe_or_no_selection(reg_path, fake_recipes, no_cookies):
    assert runner.main(["run", "--recipe", "nope", "--registry", str(reg_path)]) == 2
    assert runner.main(["run", "--registry", str(reg_path)]) == 2
    assert no_cookies == [] and len(responses.calls) == 0


def test_runner_main_list(fake_recipes, no_cookies, capsys):
    assert runner.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "read-user" in out and "readCurrentUser" in out and no_cookies == []


def test_classify_requires_reason(reg_path, no_cookies, capsys):
    raw = reg_path.read_bytes()
    with pytest.raises(SystemExit):
        runner.main(["classify", "createPersonNote", "--status", "unverifiable",
                     "--registry", str(reg_path)])
    assert runner.main(["classify", "createPersonNote", "--status", "unverifiable",
                        "--reason", "   ", "--registry", str(reg_path)]) == 2
    with pytest.raises(SystemExit):
        runner.main(["classify", "createPersonNote", "--status", "verified",
                     "--reason", "x", "--registry", str(reg_path)])
    assert reg_path.read_bytes() == raw
    assert runner.main(["classify", "createPersonNote", "--status", "unverifiable",
                        "--reason", "needs a partner account", "--registry",
                        str(reg_path)]) == 0
    entry = json.loads(reg_path.read_text())["operations"]["createPersonNote"]
    assert entry == {"generated": {"status": "unverifiable",
                                   "reason": "needs a partner account"}}
    assert runner.main(["classify", "noSuchOp", "--status", "unverifiable",
                        "--reason", "x", "--registry", str(reg_path)]) == 2
    assert no_cookies == []


@responses.activate
def test_build_beta_client_probes_beta_only(monkeypatch):
    monkeypatch.setattr(runner, "extract_brave_token", lambda: TOKEN)
    responses.add(responses.GET, BETA + "/platform/users/current", status=401)
    with pytest.raises(FSError, match="signed into production"):
        runner.build_beta_client()
    assert not hasattr(runner, "detect_environment")
    _assert_production_untouched()


@pytest.mark.parametrize("porcelain, ok", [
    ("", True),
    (" M src/familysearch/api/registry.json\n", True),
    (" M tools/verify/runner.py\n", False),
    ("?? scratch.py\n", False),
    (" M src/familysearch/api/registry.json\n M src/familysearch/api/_ops.py\n", False),
    ("R  src/familysearch/api/registry.json -> elsewhere.json\n", False),
])
def test_dirty_tree_check(porcelain, ok):
    assert (runner.dirty_paths(porcelain) == []) is ok


@responses.activate
def test_runner_main_refuses_dirty_tree(reg_path, fake_recipes, no_cookies, monkeypatch,
                                        capsys):
    monkeypatch.setattr(runner, "git_status_porcelain", lambda: " M tools/verify/core.py\n")
    raw = reg_path.read_bytes()
    assert runner.main(["run", "--all", "--registry", str(reg_path)]) == 2
    err = capsys.readouterr().err
    assert "uncommitted" in err and "tools/verify/core.py" in err
    assert fake_recipes == [] and no_cookies == [] and len(responses.calls) == 0
    assert reg_path.read_bytes() == raw


@responses.activate
def test_runner_main_refuses_when_git_status_fails(reg_path, fake_recipes, no_cookies,
                                                   monkeypatch):
    def broken():
        raise OSError("no git")

    monkeypatch.setattr(runner, "git_status_porcelain", broken)
    assert runner.main(["run", "--all", "--registry", str(reg_path)]) == 2
    assert fake_recipes == [] and no_cookies == [] and len(responses.calls) == 0


@responses.activate
def test_runner_main_dry_run_ignores_dirty_tree(reg_path, fake_recipes, no_cookies, monkeypatch):
    monkeypatch.setattr(runner, "git_status_porcelain", lambda: " M tools/verify/core.py\n")
    assert runner.main(["run", "--all", "--dry-run", "--registry", str(reg_path)]) == 0


def _set_generated(reg_path, op, generated):
    data = json.loads(reg_path.read_text())
    data["operations"][op]["generated"] = generated
    reg_path.write_text(json.dumps(data, sort_keys=True, indent=1) + "\n")


def test_classify_refuses_verified_without_force(reg_path, capsys):
    fp = OPERATIONS["createPersonNote"].fingerprint
    _set_generated(reg_path, "createPersonNote", {"status": "verified", "fingerprint": fp})
    raw = reg_path.read_bytes()
    args = ["classify", "createPersonNote", "--status", "unverifiable", "--reason", "x",
            "--registry", str(reg_path)]
    assert runner.main(args) == 2
    assert "--force-reclassify" in capsys.readouterr().err
    assert reg_path.read_bytes() == raw
    assert runner.main(args + ["--force-reclassify"]) == 0
    entry = json.loads(reg_path.read_text())["operations"]["createPersonNote"]["generated"]
    assert entry == {"status": "unverifiable", "reason": "x"}


def test_classify_never_overwrites_excluded(reg_path):
    _set_generated(reg_path, "createPersonNote", {"status": "excluded", "reason": "out"})
    raw = reg_path.read_bytes()
    for extra in ([], ["--force-reclassify"]):
        assert runner.main(["classify", "createPersonNote", "--status", "unverifiable",
                            "--reason", "x", "--registry", str(reg_path)] + extra) == 2
    assert reg_path.read_bytes() == raw


# ---- ported from the adversarial review ----

def _go(recipe, reg_path):
    return runner.run_recipe(recipe, ctx_with(recipe, reg_path, Fake()),
                             registry_path=reg_path, commit="c", today="2026-10-25")


def _mock_notes():
    responses.add(responses.GET, PERSONS + "/P1/notes", json={"persons": [{"notes": []}]})
    responses.add(responses.POST, PERSONS + "/P1/notes", status=201)


NOTE_OPS = ("createPersonNote", "readPersonNotes")


@responses.activate
def test_write_op_labelled_read_content_is_rejected_and_not_recorded(reg_path):
    _mock_notes()

    def body(ctx):
        ctx.api.read_person_notes("P1")
        ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})

    recipe = Recipe("A2", NOTE_OPS, "read-content", body)
    raw = reg_path.read_bytes()
    result = _go(recipe, reg_path)
    assert result["passed"] is False
    assert any("state-change" in p for p in result["problems"])
    assert reg_path.read_bytes() == raw
    assert any("state-change" in p for p in validate_recipes({"A2": recipe}))


def test_record_refuses_read_content_for_a_write(reg_path):
    raw = reg_path.read_bytes()
    with pytest.raises(ValueError, match="state-change"):
        record.record_many_verified(["createPersonNote"], "r", "read-content", "c", "d",
                                    path=reg_path)
    assert reg_path.read_bytes() == raw


@responses.activate
def test_calls_ops_but_never_reads_back_fails(reg_path):
    _mock_notes()

    def body(ctx):
        ctx.api.read_person_notes("P1")
        ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})

    assert _go(Recipe("A", NOTE_OPS, "state-change", body), reg_path)["passed"] is False


@responses.activate
def test_swallowed_failed_write_is_not_counted(reg_path):
    responses.add(responses.GET, PERSONS + "/P1/notes", json={})
    responses.add(responses.POST, PERSONS + "/P1/notes", status=500)

    def body(ctx):
        ctx.api.read_person_notes("P1")
        try:
            ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})
        except Exception:  # noqa: BLE001
            pass
        ctx.expect_changed(1, 2, "x")

    assert _go(Recipe("B", NOTE_OPS, "state-change", body), reg_path)["passed"] is False


@responses.activate
def test_caught_negative_control_failure_is_not_counted(reg_path):
    _mock_notes()

    def body(ctx):
        before = ctx.api.read_person_notes("P1").body
        ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})
        try:
            ctx.expect_changed(before, ctx.api.read_person_notes("P1").body, "notes")
        except AssertionError:
            pass

    assert _go(Recipe("C", NOTE_OPS, "state-change", body), reg_path)["passed"] is False


@responses.activate
def test_module_level_expect_changed_is_not_counted(reg_path):
    _mock_notes()

    def body(ctx):
        before = ctx.api.read_person_notes("P1").body
        ctx.api.create_person_note("P1", reason=ctx.reason, body={"persons": []})
        expect_changed(before, {"x": 1}, "module-level")

    assert _go(Recipe("D3", NOTE_OPS, "state-change", body), reg_path)["passed"] is False


@responses.activate
def test_raw_client_call_does_not_count_as_calling_the_op(reg_path):
    responses.add(responses.DELETE, PERSONS + "/P1", status=204)

    def body(ctx):
        ctx.client.delete(PERSONS + "/P1", reason=ctx.reason)
        ctx.expect_changed(1, 2, "x")

    assert _go(Recipe("E2b", ("deletePerson",), "state-change", body),
               reg_path)["passed"] is False
