"""Offline tests for the read-case framework (no network, no cookies).

The fake service is ``responses``: each test scripts what beta answers for the fixture
ids and for the bogus ids the not-found control uses. ``FakeWorld`` stands in for the
fixture world (Task 2) with fixed ids and names.
"""
import dataclasses
import json
import re
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
from tools.verify.recipes import validate_recipes

BETA = Environment.BETA.base_url
PERSONS = BETA + "/platform/tree/persons"
FS_JSON = "application/x-fs-v1+json"
CHILD, DAD, OTHER, BOGUS = "KWZZ-CHD", "KWZZ-DAD", "KWZZ-OTH", "ZZZZ-ZZZ"
CHILD_NAME = "ZZSPIKE Child Testabc12345"
CHILD_ID = lambda w: w.ids["child"]  # noqa: E731 - the usual anchor
CHILD_NAMED = lambda w: w.names["child_name"]  # noqa: E731


class FakeWorld:
    """Fixed fixture ids and names; records which groups were asked for."""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.client = "a client the cases must not reach"
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
    return {"persons": [{"id": pid, "gender": {"type": "http://gedcomx.org/Male"},
                         "display": {"name": name}}]}


def serve_person(pid=CHILD, *, body=None, status=200, content_type=FS_JSON, bogus=404,
                 headers=None):
    body = person_body(pid, CHILD_NAME) if body is None else body
    responses.add(responses.GET, f"{PERSONS}/{pid}", status=status, headers=headers,
                  body=json.dumps(body), content_type=content_type)
    if bogus is not None:
        bogus_body = {} if bogus < 400 else {"errors": [{"code": bogus}]}
        responses.add(responses.GET, f"{PERSONS}/{BOGUS}", status=bogus,
                      body=json.dumps(bogus_body), content_type=FS_JSON)


def expect_child(result, world):
    assert json_path(result.body, "persons[0].id") == world.ids["child"], (
        f"persons[0].id is not the child {world.ids['child']}")


def read_child(**kw):
    kw.setdefault("expect", expect_child)
    kw.setdefault("missing", {"pid": BOGUS})
    kw.setdefault("anchors", (CHILD_ID, CHILD_NAMED))
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
    with pytest.raises(AssertionError, match="not in the response"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), read_child())


@responses.activate
def test_expect_failure_message_reaches_the_caller(reg_path):
    # The anchors are present (in a sibling list), but persons[0] is someone else.
    body = {"persons": [{"id": OTHER}, {"id": CHILD, "display": {"name": CHILD_NAME}}]}
    serve_person(body=body)
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
        assert json_path(result.body, "persons[0].id") == world.ids["child"], "first child"

    if not kw.get("allow_empty"):
        kw.setdefault("anchors", (CHILD_ID,))
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
                 children_case("child", anchors=(CHILD_NAMED,)))


@responses.activate
def test_204_allowed_only_after_a_populated_case_of_the_same_op(reg_path):
    serve_children()
    ctx = ctx_for("readPersonChildren", reg_path=reg_path)
    empty = children_case("child", allow_empty=True)
    # Standalone, nothing vouches that the operation can return content: fail closed.
    with pytest.raises(AssertionError, match="populated"):
        run_case(ctx, FakeWorld(), empty)
    run_case(ctx, FakeWorld(), empty, populated={"readPersonChildren": ({"pid": DAD}, None)})


@responses.activate
def test_allow_empty_variant_must_differ_from_the_populated_case(reg_path):
    serve_children()
    ctx = ctx_for("readPersonChildren", reg_path=reg_path)
    with pytest.raises(AssertionError, match="same"):
        run_case(ctx, FakeWorld(), children_case("child", allow_empty=True),
                 populated={"readPersonChildren": ({"pid": CHILD}, None)})
    # In a recipe: both cases read the dad's children.
    recipe = reads_recipe("t.children", [children_case("dad"),
                                         children_case("dad", allow_empty=True)],
                          world_factory=FakeWorld)
    with pytest.raises(AssertionError, match="same"):
        recipe.run(make_ctx(recipe, reg_path))


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


# ------------------------------------------------------------------ anchors


@responses.activate
@pytest.mark.parametrize("anchor", [
    lambda w: OTHER,                         # a literal that is no fixture of this world
    lambda w: w.ids["child"][:2],            # fixture-derived but not a fixture value
    lambda w: "ZZSPIKE",                     # the shared prefix of every fixture
    lambda w: 42,                            # not even a string
])
def test_anchor_must_be_a_world_id_or_name(reg_path, anchor):
    serve_person(body=person_body(OTHER, "ZZSPIKE " + CHILD))
    with pytest.raises(AssertionError, match="not a world id or name"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(anchors=(anchor,), expect=lambda r, w: contains(r, OTHER)))


@responses.activate
def test_framework_checks_anchors_even_when_expect_does_not(reg_path):
    # expect is tied to the response (fails on mutants) but not to this fixture.
    serve_person(body=person_body(OTHER, "Somebody Else"))

    def expect(result, world):
        assert json_path(result.body, "persons[0].display.name") == "Somebody Else"

    with pytest.raises(AssertionError, match=f"{CHILD_NAME}.*not in the response"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(expect=expect, anchors=(CHILD_NAMED,)))


@responses.activate
def test_anchor_only_in_a_key_fails(reg_path):
    # Mutants keep dict keys: an anchor that survives the scrub is not response content.
    serve_person(body={"persons": [{"id": "X"}], "index": {CHILD_NAME: 1}})
    with pytest.raises(AssertionError, match="survives"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(anchors=(CHILD_NAMED,), expect=lambda r, w: contains(r, "X")))


@responses.activate
def test_anchors_that_only_echo_the_request_fail(reg_path):
    # Review probe: another person's record that merely links back to the requested id.
    body = {"persons": [{"id": OTHER, "gender": {"type": "http://gedcomx.org/Male"}}],
            "links": {"self": {"href": f"https://beta/platform/tree/persons/{CHILD}"}}}
    serve_person(body=body)
    case = read_child(anchors=(CHILD_ID,), expect=lambda r, w: contains(r, "gedcomx.org"))
    with pytest.raises(AssertionError, match="echo"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 0  # refused before anything is sent


@responses.activate
@pytest.mark.parametrize("pids", [
    lambda w: f"{w.ids['child']},{w.ids['dad']}",   # review probe p6: joined
    lambda w: [w.ids["child"], w.ids["dad"]],        # review probe p6b: a list
])
def test_anchors_contained_in_what_was_sent_are_echoes(reg_path, pids):
    # A server that echoes the pids back passes both anchors: neither proves content.
    def expect(r, w):
        assert [p["id"] for p in r.body["persons"]] == [w.ids["child"], w.ids["dad"]]

    case = ReadCase(op="readPersons", path_args=lambda w: {},
                    query=lambda w: {"pids": pids(w)}, expect=expect, missing={},
                    missing_query={"pids": BOGUS}, anchors=(CHILD_ID, lambda w: w.ids["dad"]))
    with pytest.raises(AssertionError, match="echo"):
        run_case(ctx_for("readPersons", reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 0
    # Adding an anchor the request did not carry (the child's name) makes it a real check.
    ok = dataclasses.replace(case, anchors=(CHILD_ID, CHILD_NAMED))
    responses.add(responses.GET, PERSONS, body=json.dumps(
        {"persons": [{"id": CHILD, "display": {"name": CHILD_NAME}}, {"id": DAD}]}),
        content_type=FS_JSON, match=[responses.matchers.query_param_matcher(
            {"pids": f"{CHILD},{DAD}"} if isinstance(pids(FakeWorld()), str)
            else {"pids": [CHILD, DAD]})])
    responses.add(responses.GET, PERSONS, status=404,
                  match=[responses.matchers.query_param_matcher({"pids": BOGUS})])
    run_case(ctx_for("readPersons", reg_path=reg_path), FakeWorld(), ok)


@responses.activate
def test_echo_check_covers_query_and_read_post_body(reg_path):
    query_case = ReadCase(op="searchTreePersons", path_args=lambda w: {},
                          expect=lambda r, w: contains(r, w.ids["child"]), missing={},
                          query=lambda w: {"q.surname": w.names["child_name"]},
                          missing_query={"q.surname": "Nobody"}, anchors=(CHILD_NAMED,))
    body_case = ReadCase(op="createNameSegments", path_args=lambda w: {},
                         expect=lambda r, w: contains(r, w.names["child_name"]), missing={},
                         body={"names": [CHILD_NAME]}, missing_body={"bogus": True},
                         anchors=(CHILD_NAMED,))
    for op, case in (("searchTreePersons", query_case), ("createNameSegments", body_case)):
        with pytest.raises(AssertionError, match="echo"):
            run_case(ctx_for(op, reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 0


# Probes from review: expects that do not tie the case to the fixture.
def _is_dict(r, w):
    assert isinstance(r.body, dict)


def _try_except(r, w):
    try:
        contains(r, w.ids["child"])
    except AssertionError:
        pass


def _not_mutated(r, w):  # would game a fixed scrub marker, if there were one
    assert not json_path(r.body, "persons[0].id").startswith("ZZMUTATED")


def _status_only(result, world):
    assert result.status == 200, "status"


def _body_not_none(result, world):
    assert result.body is not None, "no body"


VACUOUS = {
    "noop": lambda r, w: None,
    "ignore": lambda r, w: None if r.status else None,
    "status-only": _status_only,
    "body-not-none": _body_not_none,
    "keys-only": lambda r, w: contains(r, "persons", "display"),
    "is-dict": _is_dict,
    "try-except": _try_except,
    "not-mutated": _not_mutated,
}
UNTIED = {  # depend on the response, but not on this fixture
    "const-gedcomx": lambda r, w: contains(r, "gedcomx.org"),
    "other-person-const": lambda r, w: contains(r, "http://gedcomx.org/Male"),
    "fake-constant": lambda r, w: contains(r, CHILD),
    "prefix": lambda r, w: contains(r, w.ids["child"][:2]),
}


@pytest.mark.parametrize("name", sorted(VACUOUS) + sorted(UNTIED))
def test_case_without_anchors_rejected(name):
    expect = {**VACUOUS, **UNTIED}[name]
    with pytest.raises(ValueError, match="anchors"):
        reads_recipe("t", [read_child(expect=expect, anchors=())])
    with pytest.raises(ValueError, match="anchors"):
        run_case(None, FakeWorld(), read_child(expect=expect, anchors=()))


@responses.activate
@pytest.mark.parametrize("name", sorted(VACUOUS))
def test_vacuous_expect_fails_even_on_the_right_content(reg_path, name):
    serve_person()
    with pytest.raises(AssertionError, match="does not depend on the response"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(expect=VACUOUS[name]))


@responses.activate
@pytest.mark.parametrize("name", sorted(VACUOUS) + sorted(UNTIED) + ["fixture-specific"])
def test_noop_expect_is_caught_by_mutation(reg_path, name):
    # The fixture response is changed (another person's data): every case must fail.
    expect = expect_child if name == "fixture-specific" else {**VACUOUS, **UNTIED}[name]
    serve_person(body=person_body(OTHER, "Somebody Else"))
    with pytest.raises(AssertionError):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(expect=expect))


def test_case_without_content_check_rejected():
    with pytest.raises(ValueError, match="expect"):
        reads_recipe("t", [read_child(expect=None)])
    with pytest.raises(ValueError, match="expect"):
        reads_recipe("t", [read_child(expect="not callable")])


@responses.activate
def test_expect_returning_a_value_fails(reg_path):
    # ``lambda r, w: r.body["persons"][0]["id"] == ...`` returns False instead of failing.
    serve_person()
    case = read_child(expect=lambda r, w: r.body["persons"][0]["id"] == "nobody")
    with pytest.raises(AssertionError, match="returned"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), case)


@responses.activate
def test_anchor_free_case_runs_on_its_expect(reg_path):
    url = BETA + "/platform/tree/search"

    def answer(request):
        if "q.surname=Nobody" in request.url:
            return 400, {}, ""
        return 200, {"Content-Type": "application/x-gedcomx-atom+json"}, json.dumps(
            {"entries": [{"id": "E1"}]})

    responses.add_callback(responses.GET, url, callback=answer)
    case = ReadCase(op="searchTreePersons", path_args=lambda w: {},
                    expect=lambda r, w: contains(r, "E1"), missing={},
                    query={"q.surname": "Test"}, missing_query={"q.surname": "Nobody"},
                    anchor_free_reason="discovered value; no fixture in the response",
                    literal_anchors=("E1",))
    run_case(ctx_for("searchTreePersons", reg_path=reg_path), FakeWorld(), case)


# ----------------------------------------------- anchor-free cases and literal anchors

PLACE_TYPE = "http://gedcomx.org/City"  # a spec constant a place-types list must carry


def place_types_case(**kw):
    kw.setdefault("literal_anchors", (PLACE_TYPE,))
    kw.setdefault("expect", lambda r, w: contains(r, PLACE_TYPE))
    return ReadCase(op="readPlaceTypes", path_args=lambda w: {}, missing={},
                    missing_query={"zzbogus": "1"}, anchor_free_reason="standards vocabulary",
                    **kw)


def serve_place_types(body=None, *, control_body=""):
    body = {"types": [{"uri": PLACE_TYPE}]} if body is None else body

    def answer(request):
        if "zzbogus" in request.url:
            return 400, {}, control_body
        return 200, {"Content-Type": "application/ld+json"}, json.dumps(body)

    responses.add_callback(responses.GET, BETA + "/platform/places/types", callback=answer)


@responses.activate
def test_anchor_free_case_passes_with_its_literal_anchor(reg_path):
    serve_place_types()
    run_case(ctx_for("readPlaceTypes", reg_path=reg_path), FakeWorld(), place_types_case())


def test_anchor_free_case_needs_literal_anchors():
    for bad in ((), ("",), (PLACE_TYPE, 3)):
        with pytest.raises(ValueError, match="literal_anchors"):
            reads_recipe("t", [place_types_case(literal_anchors=bad)])


def _distinct_strings(r, w):  # review probe p4: "two distinct strings" is no content check
    strings = [v for v in json.dumps(r.body or {}).split('"') if v.strip(" :,{}[]")]
    assert len(set(strings)) > 2, "need distinct strings"


@responses.activate
@pytest.mark.parametrize("expect", [_distinct_strings, lambda r, w: contains(r, "x")])
def test_anchor_free_vacuous_expect_fails_on_its_literal_anchor(reg_path, expect):
    # A catch-all answer with some constant strings: the literal anchor is not there.
    serve_place_types({"a": "x", "b": "y"})
    with pytest.raises(AssertionError, match=f"{PLACE_TYPE}.*not in the response"):
        run_case(ctx_for("readPlaceTypes", reg_path=reg_path), FakeWorld(),
                 place_types_case(expect=expect))


@responses.activate
def test_literal_anchor_only_in_a_key_fails(reg_path):
    serve_place_types({"types": {PLACE_TYPE: "City"}})
    with pytest.raises(AssertionError, match="survives"):
        run_case(ctx_for("readPlaceTypes", reg_path=reg_path), FakeWorld(),
                 place_types_case(expect=lambda r, w: contains(r, "City")))


@responses.activate
def test_literal_anchor_in_the_not_found_answer_fails(reg_path):
    # A server that answers the vocabulary even when it says 400 is not discriminating.
    serve_place_types(control_body=json.dumps({"types": [{"uri": PLACE_TYPE}]}))
    with pytest.raises(AssertionError, match="control.*" + re.escape(PLACE_TYPE)):
        run_case(ctx_for("readPlaceTypes", reg_path=reg_path), FakeWorld(), place_types_case())


def test_literal_anchor_sent_in_the_request_fails():
    case = place_types_case(query={"type": PLACE_TYPE})
    with pytest.raises(AssertionError, match="echo"):
        run_case(None, FakeWorld(), case)


def _ancestry_case(**kw):
    return ReadCase(op="readAncestry", path_args=lambda w: {},
                    query=lambda w: {"person": w.ids["child"]}, expect=_distinct_strings,
                    missing={}, missing_query={"person": BOGUS},
                    anchor_free_reason="ancestry is whatever it is",
                    literal_anchors=("http://gedcomx.org/Male",), **kw)


def serve_ancestry():
    def answer(request):
        if BOGUS in request.url:
            return 404, {}, "{}"
        return 200, {"Content-Type": FS_JSON}, json.dumps(
            {"persons": [{"id": "ANY", "gender": {"type": "http://gedcomx.org/Male"}}]})

    responses.add_callback(responses.GET, BETA + "/platform/tree/ancestry", callback=answer)


@responses.activate
@pytest.mark.parametrize("query", [
    lambda w: {"person": w.ids["child"]},                       # review probe p5b
    lambda w: {"person": f"{w.ids['child']},{w.ids['dad']}"},   # contained, not equal
    lambda w: {"person": [w.ids["dad"]]},                        # a list item
])
def test_anchor_free_case_may_not_send_a_fixture_value(reg_path, query):
    serve_ancestry()
    case = dataclasses.replace(_ancestry_case(), query=query)
    with pytest.raises(AssertionError, match="anchor-free.*sends"):
        run_case(ctx_for("readAncestry", reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 0


@responses.activate
def test_anchor_free_case_may_not_send_a_fixture_name_in_its_body(reg_path):
    case = ReadCase(op="createNameSegments", path_args=lambda w: {}, missing={},
                    expect=lambda r, w: contains(r, "x"), body={"names": [CHILD_NAME]},
                    missing_body={"bogus": True}, anchor_free_reason="r",
                    literal_anchors=("http://gedcomx.org/Given",))
    with pytest.raises(AssertionError, match="anchor-free.*sends"):
        run_case(ctx_for("createNameSegments", reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 0


@responses.activate
def test_anchor_free_case_borrowing_another_cases_fixture_fails_in_the_runner(reg_path):
    # Review probe p5: the case itself has no needs; another case's needs put the child
    # in the world, and the anchor-free case queries it.
    serve_ancestry()
    serve_person()
    recipe = reads_recipe("t.p5", [read_child(needs=("persons",)), _ancestry_case()],
                          world_factory=FakeWorld)
    with reads.allow_test_world_factories():
        result = runner.run_recipe(recipe, make_ctx(recipe, reg_path),
                                   registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False
    assert any("readAncestry" in p and "anchor-free" in p for p in result["problems"])


def test_mutants_scrub_every_string_and_empty_the_response():
    from familysearch.api import ApiResult

    result = ApiResult(200, {"ETag": "e1"}, FS_JSON, {"a": [{"b": "x", "n": 3}]}, '{"a": 1}')
    scrubbed, emptied = reads.mutants(result)
    marker = scrubbed.body["a"][0]["b"]
    assert not any("MUTATED" in name for name in dir(reads))  # no fixed marker to test for
    assert scrubbed.body == {"a": [{"b": marker, "n": 3}]}  # keys and numbers kept
    assert scrubbed.headers == {"ETag": marker} and "x" not in scrubbed.text
    assert emptied.body is None and emptied.text == "" and emptied.headers == {}
    assert result.body == {"a": [{"b": "x", "n": 3}]}  # the original is untouched
    others = [reads.mutants(result)[0].body["a"][0]["b"] for _ in range(20)]
    assert marker not in others and len(set(others)) == 20  # random per call
    # Not even a shared prefix to recognise it by.
    assert len({m[:4] for m in others + [marker]}) > 1


@pytest.mark.parametrize("kw", [
    {},                                        # an op with path params
    {"needs": ("persons",)},                   # parameterless, but it uses fixtures
])
def test_anchor_free_reason_only_for_parameterless_fixture_free_cases(kw):
    if not kw:
        case = read_child(anchors=(), anchor_free_reason="lazy",
                          expect=lambda r, w: contains(r, "gedcomx.org"))
    else:
        case = ReadCase(op="searchTreePersons", path_args=lambda w: {},
                        expect=lambda r, w: contains(r, "E1"), missing={},
                        missing_query={"q.surname": "Nobody"}, anchor_free_reason="r", **kw)
    with pytest.raises(ValueError, match="anchor_free_reason"):
        reads_recipe("t", [case])


# ------------------------------------------------------------- HEAD, OPTIONS, POST


REV = '"rev-9c1e44"'  # a revision hash: carries no pid


def serve_head(*, head_etag=REV, get_etag="W/" + REV[:-1] + '-gzip"'):
    responses.add(responses.HEAD, f"{PERSONS}/{CHILD}", status=200, headers={"ETag": head_etag})
    responses.add(responses.HEAD, f"{PERSONS}/{BOGUS}", status=404)
    serve_person(headers={"ETag": get_etag}, bogus=None)


def head_case(**kw):
    kw.setdefault("compare_with", "readPerson")
    kw.setdefault("anchors", (CHILD_ID, CHILD_NAMED))
    return ReadCase(op="headPerson", path_args=lambda w: {"pid": w.ids["child"]},
                    expect=expect_child, missing={"pid": BOGUS}, **kw)


@responses.activate
def test_head_case_matches_its_get_by_validator(reg_path):
    serve_head()
    ctx = ctx_for("headPerson", reg_path=reg_path)
    run_case(ctx, FakeWorld(), head_case())
    methods = [(c.request.method, c.request.url.rsplit("/", 1)[-1]) for c in responses.calls]
    assert methods == [("HEAD", CHILD), ("GET", CHILD), ("HEAD", BOGUS)]
    assert "headPerson" in ctx.api.called


@responses.activate
def test_head_case_fails_when_validators_differ(reg_path):
    serve_head(get_etag='"rev-other"')
    with pytest.raises(AssertionError, match="validator"):
        run_case(ctx_for("headPerson", reg_path=reg_path), FakeWorld(), head_case())


@responses.activate
def test_head_case_fails_when_get_lacks_the_fixture(reg_path):
    responses.add(responses.HEAD, f"{PERSONS}/{CHILD}", status=200, headers={"ETag": REV})
    serve_person(body=person_body(OTHER, "Somebody"), headers={"ETag": REV}, bogus=None)
    with pytest.raises(AssertionError, match="not in the response"):
        run_case(ctx_for("headPerson", reg_path=reg_path), FakeWorld(), head_case())


@responses.activate
def test_head_case_fails_when_its_get_fails(reg_path):
    responses.add(responses.HEAD, f"{PERSONS}/{CHILD}", status=200, headers={"ETag": REV})
    responses.add(responses.GET, f"{PERSONS}/{CHILD}", status=404)
    with pytest.raises(AssertionError, match="readPerson"):
        run_case(ctx_for("headPerson", reg_path=reg_path), FakeWorld(), head_case())


@responses.activate
def test_head_case_still_runs_its_not_found_control(reg_path):
    serve_head()
    responses.replace(responses.HEAD, f"{PERSONS}/{BOGUS}", status=200)
    with pytest.raises(AssertionError, match="control"):
        run_case(ctx_for("headPerson", reg_path=reg_path), FakeWorld(), head_case())


def merge_case(**kw):
    def expect(result, world):
        assert "POST" in result.headers.get("Allow", ""), "merge not allowed"

    kw.setdefault("contrast_args", lambda w: {"pid": w.ids["child"], "dpid": w.ids["child"]})
    # No anchors: Allow carries no fixture value; the contrast shows it depends on the ids.
    return ReadCase(op="allowPersonMerge",
                    path_args=lambda w: {"pid": w.ids["child"], "dpid": w.ids["dad"]},
                    expect=expect, missing={"dpid": BOGUS}, **kw)


def serve_options(*, contrast_allow="GET"):
    responses.add(responses.OPTIONS, f"{PERSONS}/{CHILD}/merges/{DAD}", status=200,
                  headers={"Allow": "GET,POST"})
    responses.add(responses.OPTIONS, f"{PERSONS}/{CHILD}/merges/{CHILD}", status=200,
                  headers={"Allow": contrast_allow})
    responses.add(responses.OPTIONS, f"{PERSONS}/{CHILD}/merges/{BOGUS}", status=404)


@responses.activate
def test_options_contrast_without_allow_fails(reg_path):
    serve_options(contrast_allow="")  # differs, but says nothing
    with pytest.raises(AssertionError, match="contrast.*no Allow"):
        run_case(ctx_for("allowPersonMerge", reg_path=reg_path), FakeWorld(), merge_case())


def test_options_case_may_not_waive_its_contrast():
    with pytest.raises(ValueError, match="anchor_free_reason"):
        reads_recipe("t", [merge_case(anchor_free_reason="no fixture value")])


@responses.activate
def test_options_case_passes_when_allow_depends_on_the_args(reg_path):
    serve_options()
    run_case(ctx_for("allowPersonMerge", reg_path=reg_path), FakeWorld(), merge_case())
    assert len(responses.calls) == 3


@responses.activate
def test_options_case_fails_when_allow_ignores_the_args(reg_path):
    serve_options(contrast_allow="POST, GET")  # same set, other order and spacing
    with pytest.raises(AssertionError, match="same Allow"):
        run_case(ctx_for("allowPersonMerge", reg_path=reg_path), FakeWorld(), merge_case())


SEGMENTS = {"names": [{"given": "ZZSPIKE Child", "surname": "Testabc12345"}]}


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
                    body=SEGMENTS, missing_body={"bogus": True}, anchors=(CHILD_NAMED,))
    run_case(ctx_for("createNameSegments", reg_path=reg_path), FakeWorld(), case)
    (positive, headers), (control, _) = sent
    assert positive == SEGMENTS and control == {"bogus": True}
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
                    missing_query={"q.surname": "Nobody", "flag": "fst"}, anchors=(CHILD_ID,))
    run_case(ctx_for("searchTreePersons", reg_path=reg_path), FakeWorld(), case)
    assert len(responses.calls) == 2


@responses.activate
def test_control_refused_client_side_does_not_count(reg_path):
    # A control that never reaches the server proves nothing about the server.
    serve_person()
    case = read_child(missing={"pid": "  "})  # empty after strip: _call raises ValueError
    with pytest.raises(AssertionError, match="control.*did not reach the server"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), case)


@responses.activate
def test_control_identical_to_positive_fails(reg_path):
    serve_person()
    with pytest.raises(AssertionError, match="fixture's own"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(missing={"pid": CHILD}))


@responses.activate
def test_control_keeping_the_fixture_id_but_changing_the_query_fails(reg_path):
    # Review probe: the "missing" pid is the fixture's own; only a bad query differs.
    serve_person()
    with pytest.raises(AssertionError, match="fixture's own"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(),
                 read_child(missing={"pid": CHILD}, missing_query={"x": "bad"}))
    assert len(responses.calls) == 0  # refused before anything is sent


# ------------------------------------------------------------- the sealed world


@responses.activate
@pytest.mark.parametrize("where, fragment", [
    ("expect-need", "sealed"),
    ("expect-client", "client"),
    ("path-args-ctx", "ctx"),
    ("expect-set", "sealed"),
])
def test_cases_see_a_sealed_read_only_world(reg_path, where, fragment):
    serve_person()

    def expect(result, world):
        expect_child(result, world)
        if where == "expect-need":
            world.need("memory")
        elif where == "expect-client":
            world.client.post("/platform/tree/persons")
        elif where == "expect-set":
            world.ids = {}

    def path_args(world):
        if where == "path-args-ctx":
            world.ctx.api.create_person(reason="r", body={})
        return {"pid": world.ids["child"]}

    case = ReadCase(op="readPerson", path_args=path_args, expect=expect,
                    missing={"pid": BOGUS}, anchors=(CHILD_NAMED,))
    built = []

    def factory(ctx):
        built.append(FakeWorld(ctx))
        return built[-1]

    recipe = reads_recipe("t.sealed", [case], world_factory=factory)
    with pytest.raises(AssertionError, match=fragment):
        recipe.run(make_ctx(recipe, reg_path))
    assert built[0].needed == []


def test_sealed_world_is_a_copy_with_only_ids_and_names():
    world = FakeWorld()
    view = reads.seal(world)
    assert dict(view.ids) == world.ids and dict(view.names) == world.names
    world.ids["child"] = "CHANGED"
    assert view.ids["child"] == CHILD
    with pytest.raises(TypeError):
        view.ids["child"] = "X"
    assert not hasattr(view, "client") and not hasattr(view, "ctx")
    with pytest.raises(AssertionError, match="sealed"):
        view.need("persons")


# ------------------------------------------------------------- reads_recipe


def _case(op, **kw):
    params = OPERATIONS[op].path_params if op in OPERATIONS else ()
    kw.setdefault("path_args", lambda w: {p: "X" for p in params})
    kw.setdefault("expect", expect_child)
    kw.setdefault("missing", {p: BOGUS for p in params[:1]})
    kw.setdefault("anchors", (CHILD_ID,))
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
    ([_case("readPerson", missing_statuses=(200,))], "missing_statuses"),
    ([_case("readPerson", missing_statuses=(500,))], "missing_statuses"),
    ([_case("readPerson", missing_statuses=(405,))], "missing_statuses"),
    ([_case("readPerson", missing_statuses=(406,))], "missing_statuses"),
    ([_case("readPerson", missing_statuses=(409,))], "missing_statuses"),
    ([_case("readPerson", missing_statuses=(400, 404, 410, 405))], "missing_statuses"),
    ([_case("readPerson", path_args={"pid": "X"})], "path_args"),
    ([_case("readPerson", needs=("",))], "needs"),
    ([_case("readPerson", anchors=())], "anchors"),
    ([_case("readPerson", anchors=(), anchor_free_reason="  ")], "anchor_free_reason"),
    ([_case("readPerson", anchors=("KWZZ-CHD",))], "anchors"),
    ([_case("headPerson")], "compare_with"),
    ([_case("headPerson", anchor_free_reason="lazy")], "anchor_free_reason"),
    ([_case("headPerson", compare_with="readPersonNotes", anchors=(CHILD_ID,))], "compare_with"),
    ([_case("headPerson", compare_with="headPerson")], "compare_with"),
    ([_case("headPerson", compare_with="readPersonChildren", anchors=()) ], "anchors"),
    ([_case("readPerson", compare_with="readPerson")], "compare_with"),
    ([_case("allowPersonMerge", anchors=(), anchor_free_reason="r")], "contrast_args"),
    ([_case("readPerson", contrast_args=lambda w: {"pid": "Y"})], "contrast_args"),
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
    assert reads.read_recipe_problem(recipe) is None


def test_head_sibling_get_is_not_a_declared_op():
    recipe = reads_recipe("t.h", [head_case()])
    assert recipe.ops == ("headPerson",)


def test_validate_recipes_rejects_read_content_recipes_not_built_by_reads_recipe():
    import dataclasses

    built = reads_recipe("built", [read_child()])
    forged = {
        "hand": Recipe("hand", ("readPerson",), "read-content", lambda ctx: None),
        # A copy of a genuine recipe with its run swapped is not that recipe.
        "swapped": dataclasses.replace(built, name="swapped", run=lambda ctx: None),
        "copied": dataclasses.replace(built, name="copied"),
    }
    for key, recipe in forged.items():
        assert any("reads_recipe" in p for p in validate_recipes({key: recipe})), key
    assert validate_recipes({"built": built}) == []
    assert not hasattr(Recipe, "reads")  # no flag anyone could set by hand


def test_custom_world_factory_admitted_only_in_test_mode():
    custom = reads_recipe("custom", [read_child()], world_factory=FakeWorld)
    assert any("world factory" in p for p in validate_recipes({"custom": custom}))
    with reads.allow_test_world_factories():
        assert validate_recipes({"custom": custom}) == []
    assert any("world factory" in p for p in validate_recipes({"custom": custom}))


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
        person_body(CHILD, CHILD_NAME)), content_type=FS_JSON)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/spouses", status=404)
    cases = [read_child(),
             ReadCase(op="readPersonNotes", path_args=lambda w: {"pid": w.ids["child"]},
                      expect=expect_child, missing={"pid": BOGUS}, anchors=(CHILD_NAMED,)),
             ReadCase(op="readPersonSpouses", path_args=lambda w: {"pid": w.ids["child"]},
                      expect=lambda r, w: contains(r, w.ids["child"]), missing={"pid": BOGUS},
                      anchors=(CHILD_NAMED,))]
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
    with reads.allow_test_world_factories():
        result = runner.run_recipe(recipe, make_ctx(recipe, reg_path),
                                   registry_path=reg_path, commit="abc123", today="2026-10-25")
    assert result == {"recipe": "reads.t.person", "passed": True, "problems": []}
    entry = json.loads(reg_path.read_text())["operations"]["readPerson"]["generated"]
    assert entry["status"] == "verified" and entry["evidence"] == "read-content"
    assert entry["recipe"] == "reads.t.person"


@responses.activate
def test_failing_reads_recipe_records_nothing(reg_path):
    serve_person(bogus=200)
    recipe = reads_recipe("reads.t.person", [read_child()], world_factory=FakeWorld)
    raw = reg_path.read_bytes()
    with reads.allow_test_world_factories():
        result = runner.run_recipe(recipe, make_ctx(recipe, reg_path),
                                   registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert any("readPerson" in p and "control" in p for p in result["problems"])


@responses.activate
def test_runner_refuses_custom_world_factory_before_running(reg_path):
    # Review probe: a factory gets the raw client and could write with it.
    made = []

    def factory(ctx):
        made.append(ctx)
        ctx.client.request("POST", "/platform/tree/persons", write=True, reason="r", json={})
        return FakeWorld(ctx)

    recipe = reads_recipe("reads.t", [read_child()], world_factory=factory)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                               commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert any("world factory" in p for p in result["problems"])
    assert made == [] and len(responses.calls) == 0


@responses.activate
def test_runner_refuses_forged_read_recipe_before_running(reg_path):
    import dataclasses

    ran = []
    built = reads_recipe("reads.t", [read_child()])
    forged = dataclasses.replace(built, run=lambda ctx: ran.append(ctx))
    result = runner.run_recipe(forged, make_ctx(forged, reg_path), registry_path=reg_path,
                               commit="abc", today="2026-10-25")
    assert result["passed"] is False and ran == []
    assert any("reads_recipe" in p for p in result["problems"])


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
    # A test-only factory stands in for "somehow": the recipe itself passes.
    serve_person()

    def factory(ctx):
        ctx.api.called.add("createPerson")
        return FakeWorld(ctx)

    recipe = reads_recipe("sneaky", [read_child()], world_factory=factory)
    raw = reg_path.read_bytes()
    with reads.allow_test_world_factories():
        result = runner.run_recipe(recipe, make_ctx(recipe, reg_path),
                                   registry_path=reg_path, commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert any("createPerson" in p and "write" in p for p in result["problems"])


@responses.activate
def test_runner_refuses_hand_built_read_content_recipe(reg_path):
    # Review probe: a hand-written read recipe can reach the raw client and write.
    responses.add(responses.POST, PERSONS, status=201, headers={"X-Entity-Id": "NEW1"})
    serve_person()

    def body(ctx):
        ctx.client.request("POST", "/platform/tree/persons", write=True, reason="r", json={})
        run_case(ctx, FakeWorld(), read_child())

    recipe = Recipe("reads.t", ("readPerson",), "read-content", body)
    raw = reg_path.read_bytes()
    result = runner.run_recipe(recipe, make_ctx(recipe, reg_path), registry_path=reg_path,
                               commit="abc", today="2026-10-25")
    assert result["passed"] is False and reg_path.read_bytes() == raw
    assert any("reads_recipe" in p for p in result["problems"])
    assert len(responses.calls) == 0  # refused before run: the POST was never sent


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


# ------------------------------------------------- live-run fixes: diagnostics


def test_skeleton_is_bounded_and_carries_no_secret():
    from familysearch.api import ApiResult

    secret = "Bearer s3cr3t-token-value"
    deep = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": "too deep to show"}}}}}}}}
    body = {"persons": [{"id": CHILD, "display": {"name": "N" * 500}}] * 300,
            "access_token": secret, "links": {"self": {"href": "x"}}, **deep,
            **{f"k{i}": "v" * 80 for i in range(400)}}
    result = ApiResult(200, {"Authorization": secret, "Set-Cookie": secret, "ETag": '"e1"'},
                       FS_JSON, body, json.dumps(body))
    shape = reads.skeleton(result)
    assert len(shape) <= reads.SKELETON_LIMIT
    assert "s3cr3t" not in shape
    assert "N" * 61 not in shape and "N" * 60 in shape  # strings cut to 60
    assert "list[300]" in shape and CHILD in shape        # length + first item
    assert "too deep" not in shape
    head = ApiResult(200, {"Authorization": secret, "ETag": '"e1"', "Content-Length": "9"},
                     "", None, "")
    shape = reads.skeleton(head, headers=True)
    assert "s3cr3t" not in shape
    assert "e1" in shape and "Content-Length" in shape and "Authorization" in shape


@responses.activate
def test_anchor_failure_shows_the_response_skeleton(reg_path):
    serve_person(body=person_body(OTHER, "Somebody Else"))
    recipe = reads_recipe("t.p", [read_child()], world_factory=FakeWorld)
    with pytest.raises(AssertionError) as info:
        recipe.run(make_ctx(recipe, reg_path))
    message = str(info.value)
    assert "not in the response" in message and "Somebody Else" in message
    assert OTHER in message and "response:" in message


@responses.activate
def test_expect_failure_shows_the_response_skeleton(reg_path):
    body = {"persons": [{"id": OTHER}, {"id": CHILD, "display": {"name": CHILD_NAME}}]}
    serve_person(body=body)
    with pytest.raises(AssertionError, match=r"(?s)is not the child.*response:.*KWZZ-OTH"):
        run_case(ctx_for("readPerson", reg_path=reg_path), FakeWorld(), read_child())


@responses.activate
def test_validator_failure_shows_both_validators(reg_path):
    serve_head(get_etag='"rev-other"')
    with pytest.raises(AssertionError, match="validator") as info:
        run_case(ctx_for("headPerson", reg_path=reg_path), FakeWorld(), head_case())
    assert "rev-other" in str(info.value) and "rev-9c1e44" in str(info.value)


# ------------------------------------------------- HEAD sends its GET's Accept

CHANGES_ACCEPT = "application/x-gedcomx-atom+json"


@responses.activate
def test_head_case_sends_the_sibling_gets_accept(reg_path):
    url = f"{PERSONS}/{CHILD}/changes"
    feed = {"entries": [{"id": "1", "title": CHILD_NAME}]}

    def answer(request):
        if request.headers.get("Accept") != CHANGES_ACCEPT:
            return 406, {}, ""
        body = "" if request.method == "HEAD" else json.dumps(feed)
        return 200, {"ETag": REV, "Content-Type": CHANGES_ACCEPT}, body

    for method in (responses.HEAD, responses.GET):
        responses.add_callback(method, url, callback=answer)
    responses.add(responses.HEAD, f"{PERSONS}/{BOGUS}/changes", status=404)
    case = ReadCase(op="headPersonChangeHistory", path_args=lambda w: {"pid": w.ids["child"]},
                    compare_with="readPersonChangeHistory", missing={"pid": BOGUS},
                    expect=lambda r, w: contains(r, w.names["child_name"]),
                    anchors=(CHILD_NAMED,))
    run_case(ctx_for("headPersonChangeHistory", reg_path=reg_path), FakeWorld(), case)
    assert {c.request.headers.get("Accept") for c in responses.calls} == {CHANGES_ACCEPT}


# ------------------------------------------------- an empty answer as the control

EMPTY_REASON = "beta answers an unknown id with 204 (nothing found)"


def serve_children_control(bogus_status=204, bogus_body=""):
    responses.add(responses.GET, f"{PERSONS}/{DAD}/children", status=200,
                  body=json.dumps({"persons": [{"id": CHILD}]}), content_type=FS_JSON)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/children", status=bogus_status,
                  body=bogus_body)


@responses.activate
def test_empty_control_accepted_with_reason_and_documented_204(reg_path):
    serve_children_control()
    ctx = ctx_for("readPersonChildren", reg_path=reg_path)
    run_case(ctx, FakeWorld(), children_case("dad", control_may_be_empty=EMPTY_REASON))
    with pytest.raises(AssertionError, match="control answered 204"):
        run_case(ctx, FakeWorld(), children_case("dad"))  # without the flag: not proof


@responses.activate
def test_empty_control_with_content_fails(reg_path):
    serve_children_control(bogus_status=200,
                           bogus_body=json.dumps({"persons": [{"id": CHILD}]}))
    with pytest.raises(AssertionError, match="control"):
        run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(),
                 children_case("dad", control_may_be_empty=EMPTY_REASON))


@responses.activate
def test_catch_all_204_server_still_fails_the_positive(reg_path):
    responses.add(responses.GET, f"{PERSONS}/{DAD}/children", status=204)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/children", status=204)
    with pytest.raises(AssertionError, match="positive call answered 204"):
        run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(),
                 children_case("dad", control_may_be_empty=EMPTY_REASON))


@responses.activate
def test_empty_control_with_a_body_fails(reg_path):
    serve_children_control(bogus_status=204, bogus_body="unexpected")
    # (The HTTP stack itself refuses a 204 with content; either way it is no proof.)
    with pytest.raises(AssertionError, match="not-found control"):
        run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(),
                 children_case("dad", control_may_be_empty=EMPTY_REASON))


@responses.activate
def test_empty_control_for_a_list_read_whose_spec_omits_204(reg_path):
    # readPersons documents no 204, yet beta answers one for an unknown pid (the known
    # "undocumented 204 for empty lists" defect): a collection read may still use it.
    assert 204 not in reads.documented_statuses("readPersons")
    responses.add(responses.GET, PERSONS, match=[
        responses.matchers.query_param_matcher({"pids": CHILD})], status=200,
        body=json.dumps(person_body(CHILD, CHILD_NAME)), content_type=FS_JSON)
    responses.add(responses.GET, PERSONS, match=[
        responses.matchers.query_param_matcher({"pids": BOGUS})], status=204)
    case = ReadCase(op="readPersons", path_args=lambda w: {},
                    query=lambda w: {"pids": w.ids["child"]}, missing={},
                    missing_query={"pids": BOGUS}, expect=expect_child,
                    anchors=(CHILD_NAMED,), control_may_be_empty=EMPTY_REASON)
    run_case(ctx_for("readPersons", reg_path=reg_path), FakeWorld(), case)


@responses.activate
def test_empty_control_for_a_one_record_read_with_reason_and_anchored_positive(reg_path):
    # Any GET read may use it (coordinator ruling, live run 3): the positive proved the
    # record's content first, so the empty answer for the unknown id is "nothing".
    serve_person(bogus=None)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}", status=204)
    ctx = ctx_for("readPerson", reg_path=reg_path)
    run_case(ctx, FakeWorld(), read_child(control_may_be_empty=EMPTY_REASON))
    with pytest.raises(AssertionError, match="control answered 204"):
        run_case(ctx, FakeWorld(), read_child())  # without the flag: not proof


@pytest.mark.parametrize("make, why", [
    (lambda: [head_case(control_may_be_empty=EMPTY_REASON)], "HEAD"),
    (lambda: [merge_case(control_may_be_empty=EMPTY_REASON)], "OPTIONS"),
    (lambda: [_ancestry_case(control_may_be_empty=EMPTY_REASON)], "anchor-free"),
    (lambda: [children_case("dad", control_may_be_empty="  ")], "reason"),
    (lambda: [children_case("dad", control_may_be_empty=True)], "reason"),
    (lambda: [children_case("dad"), children_case("child", allow_empty=True,
                                                  control_may_be_empty=EMPTY_REASON)],
     "allow_empty"),
])
def test_empty_control_flag_rejected_when_unsound(make, why):
    with pytest.raises(ValueError, match="control_may_be_empty") as info:
        reads_recipe("t", make())
    assert why in str(info.value)


# ------------------------------------------------- eventual consistency


@pytest.fixture
def sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(reads, "sleep", slept.append)
    return slept


def eventual_children(**kw):
    return children_case("dad", eventual=(4, 2.5), **kw)


@responses.activate
def test_eventual_retries_the_positive_while_empty(reg_path, sleeps):
    url = f"{PERSONS}/{DAD}/children"
    responses.add(responses.GET, url, status=204)
    responses.add(responses.GET, url, status=200, content_type=FS_JSON,
                  body=json.dumps({"persons": [{"id": OTHER}]}))  # anchor still missing
    responses.add(responses.GET, url, status=200, content_type=FS_JSON,
                  body=json.dumps({"persons": [{"id": CHILD}]}))
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/children", status=404)
    run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(), eventual_children())
    assert sleeps == [2.5, 2.5]
    bogus = [c for c in responses.calls if BOGUS in c.request.url]
    assert len(bogus) == 1  # the control is never polled


@responses.activate
def test_eventual_gives_up_after_max_attempts(reg_path, sleeps):
    responses.add(responses.GET, f"{PERSONS}/{DAD}/children", status=204)
    responses.add(responses.GET, f"{PERSONS}/{BOGUS}/children", status=404)
    with pytest.raises(AssertionError, match="204"):
        run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(),
                 eventual_children())
    assert sleeps == [2.5, 2.5, 2.5]
    assert sum(DAD in c.request.url for c in responses.calls) == 4


@responses.activate
def test_eventual_does_not_retry_errors_or_a_good_answer(reg_path, sleeps):
    serve_children_control(bogus_status=404)
    run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(), eventual_children())
    assert sleeps == []
    responses.replace(responses.GET, f"{PERSONS}/{DAD}/children", status=500)
    with pytest.raises(AssertionError, match="positive call failed"):
        run_case(ctx_for("readPersonChildren", reg_path=reg_path), FakeWorld(),
                 eventual_children())
    assert sleeps == []


@pytest.mark.parametrize("eventual", [(0, 1.0), (13, 1.0), (3, -1.0), (3, 61.0), (3,),
                                      ("3", 1.0), [3, 1.0], (True, 1.0)])
def test_eventual_rejects_unbounded_polling(eventual):
    with pytest.raises(ValueError, match="eventual"):
        reads_recipe("t", [children_case("dad", eventual=eventual)])


def test_eventual_not_for_an_allow_empty_case():
    with pytest.raises(ValueError, match="eventual"):
        reads_recipe("t", [children_case("dad"),
                           children_case("child", allow_empty=True, eventual=(3, 1.0))])
