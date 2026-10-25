"""Offline tests for the read-case framework (no network, no cookies).

The fake service is ``responses``: each test scripts what beta answers for the fixture
ids and for the bogus ids the not-found control uses. ``FakeWorld`` stands in for the
fixture world (Task 2) with fixed ids and names.
"""
import json
import shutil

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError
from tools.verify import reads, runner
from tools.verify.core import Recipe, VerifyApi
from tools.verify.reads import ReadCase, contains, json_path, reads_recipe, run_case

BETA = Environment.BETA.base_url
PERSONS = BETA + "/platform/tree/persons"
FS_JSON = "application/x-fs-v1+json"
CHILD, DAD, OTHER, BOGUS = "KWZZ-CHD", "KWZZ-DAD", "KWZZ-OTH", "ZZZZ-ZZZ"
CHILD_NAME = "ZZSPIKE Child Testabc12345"


class FakeWorld:
    """Fixed fixture ids and names; records which groups were asked for."""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.ids = {"child": CHILD, "dad": DAD}
        self.names = {"child_name": CHILD_NAME}
        self.needed: list[str] = []

    def need(self, *groups):
        self.needed.extend(groups)


class Fixtures:
    def teardown(self):
        return []


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


def make_ctx(recipe, reg_path):
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    ctx.fixtures = Fixtures()
    return ctx


def ctx_for(*ops, reg_path):
    return make_ctx(Recipe("t", ops, "read-content", lambda ctx: None), reg_path)


def person_body(pid, name):
    return {"persons": [{"id": pid, "display": {"name": name}}]}


def serve_person(pid=CHILD, *, body=None, status=200, content_type=FS_JSON, bogus=404):
    body = person_body(pid, CHILD_NAME) if body is None else body
    responses.add(responses.GET, f"{PERSONS}/{pid}", status=status,
                  body=json.dumps(body), content_type=content_type)
    if bogus is not None:
        bogus_body = {} if bogus < 400 else {"errors": [{"code": bogus}]}
        responses.add(responses.GET, f"{PERSONS}/{BOGUS}", status=bogus,
                      body=json.dumps(bogus_body), content_type=FS_JSON)


def expect_child(result, world):
    assert json_path(result.body, "persons[0].id") == world.ids["child"], (
        f"persons[0].id is not the child {world.ids['child']}")
    contains(result, world.names["child_name"])


def read_child(**kw):
    kw.setdefault("expect", expect_child)
    kw.setdefault("missing", {"pid": BOGUS})
    return ReadCase(op="readPerson", path_args=lambda w: {"pid": w.ids["child"]}, **kw)


# ------------------------------------------------------------------ run_case


@responses.activate
def test_positive_case_passes(reg_path):
    serve_person()
    ctx = ctx_for("readPerson", reg_path=reg_path)
    run_case(ctx, FakeWorld(), read_child())
    assert ctx.api.called == {"readPerson"}
    # Positive then the not-found control, same operation.
    assert [c.request.url for c in responses.calls] == [f"{PERSONS}/{CHILD}", f"{PERSONS}/{BOGUS}"]


@responses.activate
def test_wrong_content_fails_with_message(reg_path):
    serve_person(body=person_body(OTHER, "Somebody Else"))
    with pytest.raises(AssertionError, match="is not the child"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), read_child())


@responses.activate
def test_catch_all_200_for_bogus_ids_fails_control(reg_path):
    serve_person(bogus=200)
    with pytest.raises(AssertionError, match="control.*answered 200"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), read_child())


@responses.activate
def test_control_with_unexpected_error_status_fails(reg_path):
    serve_person(bogus=500)
    with pytest.raises(AssertionError, match="control.*500"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), read_child())


@responses.activate
@pytest.mark.parametrize("status", [404, 403, 500])
def test_error_on_positive_call_fails(reg_path, status):
    serve_person(status=status, body={"errors": []}, bogus=None)
    with pytest.raises(AssertionError, match=f"positive call.*{status}"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), read_child())


def children_case(pid_key, **kw):
    def expect(result, world):
        contains(result, world.ids["child"])

    return ReadCase(op="readPersonChildren", path_args=lambda w: {"pid": w.ids[pid_key]},
                    expect=expect, missing={"pid": BOGUS}, **kw)


def serve_children(*, dad_status=200):
    body = {"persons": [{"id": CHILD}]} if dad_status == 200 else None
    responses.add(responses.GET, f"{PERSONS}/{DAD}/children", status=dad_status,
                  body=json.dumps(body) if body else "", content_type=FS_JSON)
    responses.add(responses.GET, f"{PERSONS}/{CHILD}/children", status=204)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/children", status=404)


@responses.activate
def test_204_on_positive_call_fails_without_allow_empty(reg_path):
    serve_children()
    with pytest.raises(AssertionError, match="204"):
        run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(),
                 children_case("child"))


@responses.activate
def test_204_allowed_only_after_a_populated_case_of_the_same_op(reg_path):
    serve_children()
    ctx = ctx_for("readPersonChildren", reg_path=reg_path)
    empty = children_case("child", allow_empty=True)
    # Standalone, nothing vouches that the operation can return content: fail closed.
    with pytest.raises(AssertionError, match="populated"):
        run_case(ctx, FakeWorld(), empty)
    run_case(ctx, FakeWorld(), empty, populated={"readPersonChildren"})


@responses.activate
def test_recipe_accepts_204_when_populated_sibling_passed(reg_path):
    serve_children()
    # Listed empty-first: the recipe still runs the populated case first.
    recipe = reads_recipe("t.children", [children_case("child", allow_empty=True),
                                         children_case("dad")], world_factory=FakeWorld)
    assert recipe.ops == ("readPersonChildren",)
    recipe.run(make_ctx(recipe, reg_path))


@responses.activate
def test_recipe_rejects_204_when_populated_sibling_failed(reg_path):
    serve_children(dad_status=204)
    recipe = reads_recipe("t.children", [children_case("dad"),
                                         children_case("child", allow_empty=True)],
                          world_factory=FakeWorld)
    with pytest.raises(AssertionError) as info:
        recipe.run(make_ctx(recipe, reg_path))
    assert "populated" in str(info.value) and "204" in str(info.value)


@responses.activate
@pytest.mark.parametrize("content_type, ok", [
    (FS_JSON, True),
    (FS_JSON + "; charset=UTF-8", True),
    ("application/json", True),  # in readPerson's accepts and the same (json) family
    ("application/xml", False),
    ("text/html", False),
])
def test_media_type_must_match_requested_accept(reg_path, content_type, ok):
    serve_person(content_type=content_type)
    ctx = ctx_for("readPerson", reg_path=reg_path)
    if ok:
        run_case(ctx, FakeWorld(), read_child())
    else:
        with pytest.raises(AssertionError, match="media type"):
            run_case(ctx, FakeWorld(), read_child())


@pytest.mark.parametrize("op, requested, got, ok", [
    ("readPerson", FS_JSON, "application/x-gedcomx-v1+json", True),
    ("readPerson", "application/json", FS_JSON, True),
    ("readVocabConceptV2", "application/ld-v2+json", "application/json", False),
    ("readVocabConceptV2", "application/ld-v2+json", "application/x-fs-v2+json", True),
    ("readPlaceAttributes", "application/ld+json", "application/rdf+xml", False),
    ("standardizeDate", "text/plain", "text/plain;charset=utf-8", True),
    ("standardizeDate", "text/plain", "application/json", False),
    ("readPerson", FS_JSON, "", False),
])
def test_media_type_rule(op, requested, got, ok):
    problem = reads.media_type_problem(OPERATIONS[op], requested, got)
    assert (problem is None) is ok, problem


def test_accepted_statuses_from_the_spec():
    def accepted(op, allow_empty=False):
        return reads.accepted_statuses(op, allow_empty=allow_empty)

    assert reads.documented_statuses("readPerson") == frozenset({200, 301})
    assert accepted("readPerson") == {200}  # 301 is followed by the client
    assert accepted("readPersonChildren") == {200}
    assert accepted("readPersonChildren", allow_empty=True) == {200, 204}
    assert accepted("readCurrentTreePerson") == {200}  # documents only 303
    assert accepted("readPartnerEligibility") == set()  # documents only 204
    assert accepted("readPartnerEligibility", allow_empty=True) == {204}


@responses.activate
def test_head_case_passes_with_headers_only(reg_path):
    responses.add(responses.HEAD, f"{PERSONS}/{CHILD}", status=200,
                  headers={"ETag": f'"7-{CHILD}"'})
    responses.add(responses.HEAD, f"{PERSONS}/{BOGUS}", status=404)
    seen = []

    def expect(result, world):
        seen.append(result)
        assert result.headers.get("ETag") == f'"7-{world.ids["child"]}"', "etag"

    case = ReadCase(op="headPerson", path_args=lambda w: {"pid": w.ids["child"]},
                    expect=expect, missing={"pid": BOGUS})
    run_case(ctx_for("headPerson", reg_path=reg_path), FakeWorld(), case)
    assert seen[0].body is None and seen[0].status == 200


@responses.activate
def test_options_case_passes_with_headers_only(reg_path):
    url = f"{PERSONS}/{CHILD}/merges/{DAD}"
    responses.add(responses.OPTIONS, url, status=200, headers={"Allow": "GET,POST"})
    responses.add(responses.OPTIONS, f"{PERSONS}/{CHILD}/merges/{BOGUS}", status=404)

    def expect(result, world):
        assert "POST" in result.headers.get("Allow", ""), "merge not allowed"

    case = ReadCase(op="allowPersonMerge",
                    path_args=lambda w: {"pid": w.ids["child"], "dpid": w.ids["dad"]},
                    expect=expect, missing={"dpid": BOGUS})
    run_case(ctx_for("allowPersonMerge", reg_path=reg_path), FakeWorld(), case)


@responses.activate
def test_read_post_sends_body_and_no_reason(reg_path):
    url = BETA + "/platform/names/segments"
    sent = []

    def answer(request):
        body = json.loads(request.body)
        sent.append((body, dict(request.headers)))
        if body.get("bogus"):
            return 400, {}, ""
        return 200, {"Content-Type": FS_JSON}, json.dumps({"names": [{"value": CHILD_NAME}]})

    responses.add_callback(responses.POST, url, callback=answer)
    case = ReadCase(op="createNameSegments", path_args=lambda w: {},
                    expect=lambda r, w: contains(r, w.names["child_name"]), missing={},
                    body={"names": [CHILD_NAME]}, missing_body={"bogus": True})
    run_case(ctx_for("createNameSegments", reg_path=reg_path), FakeWorld(), case)
    (positive, headers), (control, _) = sent
    assert positive == {"names": [CHILD_NAME]} and control == {"bogus": True}
    assert not any(k.lower() == "x-reason" for k in headers)


@responses.activate
def test_parameterless_control_uses_missing_query(reg_path):
    url = BETA + "/platform/tree/search"

    def answer(request):
        if "q.surname=Nobody" in request.url:
            return 400, {}, ""
        entries = [{"content": {"gedcomx": {"persons": [{"id": CHILD}]}}}]
        return 200, {"Content-Type": "application/x-gedcomx-atom+json"}, json.dumps(
            {"entries": entries})

    responses.add_callback(responses.GET, url, callback=answer)
    case = ReadCase(op="searchTreePersons", path_args=lambda w: {},
                    expect=lambda r, w: contains(r, w.ids["child"]), missing={},
                    query=lambda w: {"q.surname": "Test", "flag": "fst"},
                    missing_query={"q.surname": "Nobody", "flag": "fst"})
    run_case(ctx_for("searchTreePersons", reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 2


@responses.activate
def test_control_refused_client_side_does_not_count(reg_path):
    # A control that never reaches the server proves nothing about the server.
    serve_person()
    ctx = ctx_for("readPerson", reg_path=reg_path)
    case = read_child(missing={"pid": "  "})  # empty after strip: _call raises ValueError
    with pytest.raises(AssertionError, match="control.*did not reach the server"):
        run_case(ctx, FakeWorld(), case)


@responses.activate
def test_control_identical_to_positive_fails(reg_path):
    serve_person()
    with pytest.raises(AssertionError, match="identical"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(missing={"pid": CHILD}))


@responses.activate
def test_expect_returning_a_value_fails(reg_path):
    # ``lambda r, w: r.body["persons"][0]["id"] == ...`` returns False instead of failing.
    serve_person(body=person_body(OTHER, "Somebody Else"))
    case = read_child(expect=lambda r, w: r.body["persons"][0]["id"] == w.ids["child"])
    with pytest.raises(AssertionError, match="returned"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), case)


# ------------------------------------------------- vacuous checks (Review Focus 1)


def test_case_without_content_check_rejected():
    with pytest.raises(ValueError, match="expect"):
        reads_recipe("t", [read_child(expect=None)])
    with pytest.raises(ValueError, match="expect"):
        reads_recipe("t", [read_child(expect="not callable")])


def _status_only(result, world):
    assert result.status == 200, "status"


def _body_not_none(result, world):
    assert result.body is not None, "no body"


VACUOUS = {
    "noop": lambda r, w: None,
    "status-only": _status_only,
    "body-not-none": _body_not_none,
    "keys-only": lambda r, w: contains(r, "persons", "display"),
}


@responses.activate
@pytest.mark.parametrize("name", sorted(VACUOUS))
def test_vacuous_expect_fails_even_on_the_right_content(reg_path, name):
    serve_person()
    with pytest.raises(AssertionError, match="does not depend on the response"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(expect=VACUOUS[name]))


@responses.activate
@pytest.mark.parametrize("name", sorted(VACUOUS) + ["fixture-specific"])
def test_noop_expect_is_caught_by_mutation(reg_path, name):
    # The fixture response is changed (another person's data): every case must fail.
    expect = expect_child if name == "fixture-specific" else VACUOUS[name]
    serve_person(body=person_body(OTHER, "Somebody Else"))
    with pytest.raises(AssertionError):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(expect=expect))


def test_mutants_scrub_every_string_and_empty_the_response():
    from familysearch.api import ApiResult

    result = ApiResult(200, {"ETag": "e1"}, FS_JSON, {"a": [{"b": "x", "n": 3}]}, '{"a": 1}')
    scrubbed, emptied = reads.mutants(result)
    assert scrubbed.body == {"a": [{"b": reads.MUTATED, "n": 3}]}
    assert scrubbed.headers == {"ETag": reads.MUTATED} and "x" not in scrubbed.text
    assert emptied.body is None and emptied.text == "" and emptied.headers == {}
    assert result.body == {"a": [{"b": "x", "n": 3}]}  # the original is untouched


# ------------------------------------------------------------- reads_recipe


def _case(op, **kw):
    params = OPERATIONS[op].path_params if op in OPERATIONS else ()
    kw.setdefault("path_args", lambda w: {p: "X" for p in params})
    kw.setdefault("expect", expect_child)
    kw.setdefault("missing", {p: BOGUS for p in params[:1]})
    return ReadCase(op=op, **kw)


@pytest.mark.parametrize("cases, fragment", [
    ([_case("createPerson", missing={})], "not a read"),
    ([_case("readPerson"), _case("readPerson")], "more than once"),
    ([_case("noSuchOp", path_args=lambda w: {}, missing={})], "unknown operation"),
    ([_case("readPerson", accept="text/csv")], "accept"),
    ([_case("readPerson", missing={"nope": "x"})], "not path parameters"),
    ([_case("readPerson", missing={})], "missing"),
    ([_case("readCurrentTreePerson", missing={})], "missing_query"),
    ([_case("getNameSegments", missing={}, query={"fullName": "A B"},
            missing_query={"x": "1"})], "fullName"),
    ([_case("readPerson", body={"a": 1})], "body"),
    ([_case("readPerson", missing_body={"a": 1})], "body"),
    ([_case("readPersonChildren", allow_empty=True)], "populated"),
    ([_case("readPerson", missing_statuses=(403,))], "missing_statuses"),
    ([_case("readPerson", missing_statuses=())], "missing_statuses"),
    ([_case("readPerson", path_args={"pid": "X"})], "path_args"),
    ([_case("readPerson", needs=("",))], "needs"),
    ([], "no cases"),
    ([_case(op) for op in sorted(op for op, s in OPERATIONS.items()
                                  if s.kind == "read" and s.path_params)[:13]], "at most"),
])
def test_reads_recipe_rejects_bad_cases(cases, fragment):
    with pytest.raises(ValueError, match=fragment):
        reads_recipe("t", cases)


def test_reads_recipe_shape():
    recipe = reads_recipe("t.p", [_case("readPerson", needs=("persons",)),
                                  _case("readPersonChildren", needs=("persons", "couple"))])
    assert recipe.name == "t.p" and recipe.evidence == "read-content"
    assert recipe.ops == ("readPerson", "readPersonChildren")


@responses.activate
def test_reads_recipe_builds_world_from_union_of_needs(reg_path):
    serve_person()
    worlds = []

    def factory(ctx):
        worlds.append(FakeWorld(ctx))
        return worlds[-1]

    recipe = reads_recipe("t.p", [read_child(needs=("persons", "couple"))],
                          world_factory=factory)
    ctx = make_ctx(recipe, reg_path)
    recipe.run(ctx)
    assert worlds[0].ctx is ctx and worlds[0].needed == ["persons", "couple"]


@responses.activate
def test_reads_recipe_aggregates_every_failing_case(reg_path):
    serve_person(bogus=200)  # readPerson's control fails
    responses.add(responses.GET, f"{PERSONS}/{CHILD}/notes", status=500)
    responses.add(responses.GET, f"{PERSONS}/{CHILD}/spouses", body=json.dumps(
        {"persons": [{"id": CHILD}]}), content_type=FS_JSON)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/spouses", status=404)
    cases = [read_child(),
             ReadCase(op="readPersonNotes", path_args=lambda w: {"pid": w.ids["child"]},
                      expect=expect_child, missing={"pid": BOGUS}),
             ReadCase(op="readPersonSpouses", path_args=lambda w: {"pid": w.ids["child"]},
                      expect=lambda r, w: contains(r, w.ids["child"]), missing={"pid": BOGUS})]
    recipe = reads_recipe("t.agg", cases, world_factory=FakeWorld)
    with pytest.raises(AssertionError) as info:
        recipe.run(make_ctx(recipe, reg_path))
    text = str(info.value)
    assert "2 of 3 read cases failed" in text
    assert "readPerson:" in text and "readPersonNotes:" in text and "500" in text
    assert "readPersonSpouses" not in text


def test_default_world_factory_uses_task2_signature(monkeypatch):
    import sys
    import types

    made = []
    module = types.ModuleType("tools.verify.world")
    module.World = lambda *args: made.append(args) or FakeWorld()
    monkeypatch.setitem(sys.modules, "tools.verify.world", module)

    class Ctx:
        client, fixtures, reason, unique = "C", "F", "R", "U"

    reads.default_world(Ctx())
    assert made == [("C", "F", "R", "U")]


# ---------------------------------------------------------------- the runner


@responses.activate
def test_reads_recipe_verifies_through_the_runner(reg_path):
    serve_person()
    recipe = reads_recipe("reads.t.person", [read_child(needs=("persons",))],
                          world_factory=FakeWorld)
    result = runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                               commit="abc123", today="2026-10-25")
    assert result == {"recipe": "reads.t.person", "passed": True, "problems": []}
    entry = json.loads(reg_path.read_text())["operations"]["readPerson"]["generated"]
    assert entry["status"] == "verified" and entry["evidence"] == "read-content"
    assert entry["recipe"] == "reads.t.person"


@responses.activate
def test_failing_reads_recipe_records_nothing(reg_path):
    serve_person(bogus=200)
    recipe = reads_recipe("reads.t.person", [read_child()], world_factory=FakeWorld)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                               commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert any("readPerson" in p and "control" in p for p in result["problems"])


@responses.activate
def test_read_only_verify_api_refuses_even_a_verified_write(reg_path):
    # createPerson is verified, so the lock alone would let it through.
    assert json.loads(reg_path.read_text())["operations"]["createPerson"]["generated"][
        "status"] == "verified"
    ctx = ctx_for("readPerson", reg_path=reg_path)
    with pytest.raises(FSError, match="read-only"):
        ctx.api.create_person(reason="r", body={"persons": []})
    assert len(responses.calls) == 0 and ctx.api.called == set()


@responses.activate
def test_read_recipe_that_calls_a_write_is_refused(reg_path):
    def body(ctx):
        ctx.api.create_person(reason=ctx.reason, body={"persons": []})

    recipe = Recipe("sneaky", ("readPerson",), "read-content", body)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                               commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert len(responses.calls) == 0


@responses.activate
def test_runner_refuses_read_recipe_whose_api_called_a_write(reg_path):
    # However the write got through (a non-read-only API, a bug), the result is refused.
    serve_person()

    def body(ctx):
        ctx.api.read_person(CHILD)
        ctx.api.called.add("createPerson")

    recipe = Recipe("sneaky", ("readPerson",), "read-content", body)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                               commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert any("createPerson" in p and "write" in p for p in result["problems"])


def test_make_context_read_only_only_for_all_read_recipes(reg_path):
    reads_only = make_ctx(Recipe("r", ("readPerson",), "read-content", None), reg_path)
    mixed = make_ctx(Recipe("w", ("createPerson", "readPerson"), "state-change", None),
                     reg_path)
    assert isinstance(reads_only.api, VerifyApi)
    assert reads_only.api.read_only is True and mixed.api.read_only is False


# ------------------------------------------------------------------- helpers


def _result(body, text=None):
    from familysearch.api import ApiResult

    return ApiResult(200, {}, FS_JSON, body, json.dumps(body) if text is None else text)


def test_contains():
    contains(_result({"persons": [{"id": CHILD}]}), CHILD, "persons")
    with pytest.raises(AssertionError, match="ZZNOPE"):
        contains(_result({"persons": [{"id": CHILD}]}), CHILD, "ZZNOPE")
    contains(_result(None, text="plain " + CHILD), CHILD)  # non-JSON: the text
    with pytest.raises(AssertionError):
        contains(_result(None, text=""), CHILD)
    with pytest.raises(ValueError):
        contains(_result({}))  # no needles is no check


def test_json_path():
    body = {"a": {"b": [{"c": 1}, {"c": "two"}]}, "x": None}
    assert json_path(body, "a.b[1].c") == "two"
    assert json_path(body, "a.b[0]") == {"c": 1}
    assert json_path(body, "x") is None
    for bad in ("a.z", "a.b[5].c", "a.b.c", "a[0]", "nope"):
        with pytest.raises(AssertionError, match="no "):
            json_path(body, bad)
    with pytest.raises(AssertionError):
        json_path(None, "a")
