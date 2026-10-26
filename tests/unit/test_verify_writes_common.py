"""Offline tests for the write recipes' shared helpers and the stateful fake tree."""
import json
import re

import pytest
import responses

from familysearch import FSHTTPError
from familysearch.api._runtime import ApiResult
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.models import FACT_BIRTH, GENDER_URI, build_fact
from tools.verify.core import VerifyApi, VerifyContext
from tools.verify.fixtures import FixtureSet
from tools.verify.recipes import writes_common as wc

from _fake_tree import DESCRIPTIONS, PERSONS, WRITE_LIES, WriteTree

REASON = "ZZSPIKE test"


def client():
    return FSClient("tkn", Environment.BETA, min_write_interval=0)


def ctx_for(ops=()):
    c = client()
    return VerifyContext(api=VerifyApi(c, ops=ops), fixtures=FixtureSet(c, reason=REASON),
                         client=c, reason=REASON, unique="ZZSPIKEtest")


def result(body, status=200):
    text = json.dumps(body) if body is not None else ""
    return ApiResult(status, {}, "application/x-fs-v1+json" if body is not None else "",
                     body, text)


# ---------------------------------------------------------------- markers, gone


def test_token_is_a_fresh_zzspike_marker():
    a, b = wc.token("n"), wc.token("n")
    assert a != b and re.fullmatch(r"ZZSPIKEN\d{12}", a)
    with pytest.raises(ValueError):
        wc.token("nn")


def test_status_of_and_gone():
    def raising(status):
        def read():
            raise FSHTTPError(status, "")
        return read

    assert wc.status_of(lambda: result({})) == 200
    assert wc.status_of(raising(410)) == 410
    assert wc.gone(raising(404)) and wc.gone(raising(410))
    assert not wc.gone(lambda: result({}))
    with pytest.raises(FSHTTPError):
        wc.gone(raising(500))  # not "gone": a problem to see


# ---------------------------------------------------------------- person view


PERSON = {"persons": [{
    "id": "P1", "gender": {"id": "G1", "type": GENDER_URI["Male"]},
    "names": [{"id": "N1", "nameForms": [{"fullText": "ZZSPIKE A Test"}]}],
    "facts": [{"id": "F1", "type": FACT_BIRTH, "date": {"original": "1850", "formal": "+1850"}},
              {"id": "F2", "type": wc.FACT_RESIDENCE, "place": {"original": "ZZSPIKEF1"}}]}]}


def test_person_view_keys_conclusions_by_id():
    view = wc.person_view(result(PERSON))
    assert view.names == {"N1": "ZZSPIKE A Test"}
    assert view.gender == ("G1", GENDER_URI["Male"])
    assert view.facts["F1"].date == "1850" and view.facts["F2"].place == "ZZSPIKEF1"
    assert view.facts_with("ZZSPIKEF1") == ["F2"] and view.name_has("A Test")
    assert "F2" in view.summary()
    # The read itself is kept for the skeleton, out of comparisons.
    assert wc.person_view(result(PERSON)) == view


@pytest.mark.parametrize("body, problem", [
    ({"persons": []}, "no person"),
    ({"persons": [{"facts": [{"type": FACT_BIRTH}]}]}, "fact without an id"),
    ({"persons": [{"names": [{"nameForms": [{}]}]}]}, "name without an id"),
])
def test_person_view_refuses_what_it_cannot_key(body, problem):
    with pytest.raises(AssertionError, match=problem) as e:
        wc.person_view(result(body))
    assert "response: 200" in str(e.value)  # the skeleton rides along


def test_expect_same():
    wc.expect_same({"a": 1}, {"a": 1}, "x")
    with pytest.raises(AssertionError, match="x changed"):
        wc.expect_same({"a": 1}, {}, "x")


# ---------------------------------------------------------------- bodies


def test_bodies_are_the_proven_person_resource_shapes():
    name = wc.name_update("ZZSPIKEN1", "ZZSPIKEUpdated", name_id="N1")["persons"][0]["names"][0]
    assert name["id"] == "N1" and name["nameForms"][0]["parts"]  # beta requires parts
    assert "id" not in wc.name_update("ZZSPIKEN1", "S")["persons"][0]["names"][0]
    fact = build_fact(wc.FACT_RESIDENCE, place="ZZSPIKEF1")
    assert wc.fact_update(fact) == {"persons": [{"facts": [fact]}]}
    with pytest.raises(ValueError):
        wc.fact_update({**fact, "id": "F1"})
    assert wc.gender_update("Female", gender_id="G1") == {
        "persons": [{"gender": {"type": GENDER_URI["Female"], "id": "G1"}}]}
    with pytest.raises(ValueError):
        wc.gender_update("Other")
    assert wc.source_reference_update("u") == {"persons": [{"sources": [{"description": "u"}]}]}
    assert wc.note_body("persons", "s", "t") == {
        "persons": [{"notes": [{"subject": "s", "text": "t"}]}]}
    assert wc.other_gender(GENDER_URI["Male"]) == "Female"
    assert wc.other_gender(GENDER_URI["Female"]) == "Male"


# ---------------------------------------------------------------- polling


def test_poll_waits_bounded_and_notes_the_attempts(monkeypatch):
    slept = []
    monkeypatch.setattr(wc, "sleep", slept.append)
    ctx = ctx_for()
    values = iter([1, 2, 3])
    assert wc.poll(ctx, lambda: next(values), lambda v: v == 3, "thing",
                   eventual=(3, 2.0), result_of=lambda v: None) == 3
    assert slept == [2.0, 2.0] and ctx.notes == ["thing: after 3 of 3 attempts"]
    with pytest.raises(AssertionError, match="not seen after 2 attempts"):
        wc.poll(ctx, lambda: 0, lambda v: False, "other", eventual=(2, 1.0),
                result_of=lambda v: None)


# ---------------------------------------------------------------- the fake + fixtures


@responses.activate
def test_fake_updates_by_id_and_creates_without_and_reads_back():
    tree = WriteTree().serve()
    ctx = ctx_for(("updatePerson",))
    pid = ctx.fixtures.person(given="Fake", facts=[build_fact(wc.FACT_RESIDENCE, place="X")])
    v0 = wc.read_person_view(ctx, pid)
    assert [f.type for f in v0.facts.values()] == [FACT_BIRTH, wc.FACT_RESIDENCE]
    [nid] = v0.names
    ctx.api.update_person(pid, reason=REASON, body=wc.name_update("ZZSPIKEN", "S", name_id=nid))
    ctx.api.update_person(pid, reason=REASON, body=wc.fact_update(build_fact(FACT_BIRTH)))
    v1 = wc.read_person_view(ctx, pid)
    assert v1.names == {nid: "ZZSPIKEN S"} and len(v1.facts) == 3
    # Beta refuses a name without parts.
    with pytest.raises(FSHTTPError) as e:
        ctx.api.update_person(pid, reason=REASON, body={"persons": [{"names": [
            {"nameForms": [{"fullText": "ZZSPIKE x"}]}]}]})
    assert e.value.status == 400
    assert ctx.fixtures.teardown() == [] and tree.live() == []


@responses.activate
def test_fixture_sources_are_tracked_attached_and_torn_down():
    tree = WriteTree().serve()
    ctx = ctx_for()
    pid = ctx.fixtures.person()
    sid = ctx.fixtures.source("ZZSPIKES1")
    assert ctx.fixtures.tracked("source") == [sid]
    assert ctx.fixtures.source_url(sid) == f"{DESCRIPTIONS}/{sid}"
    ctx.fixtures.attach_source(pid, sid)
    assert set(wc.person_sources(ctx, pid).items.values()) == {sid}
    with pytest.raises(ValueError):
        ctx.fixtures.source("not a fixture title")
    with pytest.raises(ValueError):
        ctx.fixtures.attach_source("KWQ7-ABC", sid)  # never a person this run did not make
    assert ctx.fixtures.teardown() == [] and tree.live() == []
    assert ("POST", f"/platform/tree/persons/{pid}") in tree.calls


@responses.activate
def test_person_sources_and_memories_answer_empty_on_204():
    WriteTree().serve()
    ctx = ctx_for()
    pid = ctx.fixtures.person()
    assert wc.person_sources(ctx, pid).items == {}
    assert wc.person_memories(ctx, pid).items == {}
    assert ctx.fixtures.teardown() == []


def test_person_sources_parses_description_urls_and_ids():
    class Api:
        def read_person_sources(self, pid):
            return result({"persons": [
                {"id": "P1", "sources": [
                    {"id": "R1", "description": f"{DESCRIPTIONS}/SD1?flag=fsh"},
                    {"id": "R2", "descriptionId": "SD2", "description": "#sd2"}]},
                {"id": "OTHER", "sources": [{"id": "R9", "description": "x/SD9"}]}]})

    listing = wc.person_sources(type("C", (), {"api": Api()})(), "P1")
    assert listing.items == {"R1": "SD1", "R2": "SD2"}


def test_write_lies_are_named():
    with pytest.raises(ValueError):
        WriteTree("no_such_lie")
    assert "ignore_update" in WRITE_LIES and PERSONS.endswith("/platform/tree/persons")


# ---------------------------------------------------------------- notes, relationships


def headers_result(headers):
    return ApiResult(201, headers, "", None, "")


def test_new_note_id_takes_the_location_first_then_the_entity_header():
    loc = f"{PERSONS}/P1/notes/N-LOC?flag=fsh"
    assert wc.new_note_id(headers_result({"Location": loc, "X-ENTITY-ID": "N-ENT"})) == "N-LOC"
    assert wc.new_note_id(headers_result({"x-entity-id": "N-ENT"})) == "N-ENT"
    with pytest.raises(AssertionError, match="no Location or X-ENTITY-ID"):
        wc.new_note_id(headers_result({}))


def test_note_reads_parse_lists_and_single_notes():
    listed = wc.note_listing(result({"relationships": [{"id": "R", "notes": [
        {"id": "N1", "subject": "s1"}, {"id": "N2", "subject": "s2"}]}]}), "relationships")
    assert listed.items == {"N1": "s1", "N2": "s2"}
    assert wc.note_listing(result(None, 204), "relationships").items == {}
    with pytest.raises(AssertionError, match="not in relationships"):
        wc.note_listing(result({"persons": [{"notes": []}]}), "relationships")
    one = wc.single_note(result({"persons": [{"notes": [
        {"id": "N1", "subject": "ZZSPIKEA1 subject", "text": "ZZSPIKEA1"}]}]}), "persons", "N1")
    assert (one.subject, one.text) == ("ZZSPIKEA1 subject", "ZZSPIKEA1") and one.has("A1")
    with pytest.raises(AssertionError, match="answered notes"):
        wc.single_note(result({"persons": [{"notes": [{"id": "N2"}]}]}), "persons", "N1")


def test_relationship_views_tell_couples_from_child_and_parents_and_other_types():
    body = {"relationships": [
        {"id": "C1", "type": wc.COUPLE, "person1": {"resourceId": "A"},
         "person2": {"resource": f"{PERSONS}/B?flag=fsh"},
         "facts": [{"id": "F1", "type": "http://gedcomx.org/Marriage",
                    "place": {"original": "ZZSPIKEW1"}}]},
        {"id": "PC", "type": "http://gedcomx.org/ParentChild",
         "person1": {"resourceId": "A"}, "person2": {"resourceId": "K"}}],
        "childAndParentsRelationships": [
            {"id": "X1", "parent1": {"resourceId": "A"}, "child": {"resourceId": "K"},
             "parent1Facts": [{"id": "L1", "type": "http://gedcomx.org/AdoptiveParent"}]}]}
    couple = wc.relationship_view(result(body), "C1")
    assert couple.kind == "couple" and couple.roles == {"person1": "A", "person2": "B"}
    assert couple.facts_with("ZZSPIKEW1") == ["F1"]
    cap = wc.relationship_view(result(body), "X1")
    assert cap.kind == "child_and_parents" and cap.persons == {"A", "K"}
    assert list(cap.facts["parent1Facts"]) == ["L1"] and cap.facts["parent2Facts"] == {}
    assert wc.relationship_view(result(body), "PC").kind == "http://gedcomx.org/ParentChild"
    with pytest.raises(AssertionError, match="does not hold it"):
        wc.relationship_view(result(body), "ZZ")
    with pytest.raises(AssertionError, match="are not facts with ids"):
        wc.relationship_view(result({"relationships": [
            {"id": "C1", "type": wc.COUPLE, "facts": [{"type": "x"}]}]}), "C1")


def test_relatives_take_an_untyped_entry_as_a_couple_but_a_resource_read_does_not():
    body = {"persons": [{"id": "A"}, {"id": "B"}], "relationships": [
        {"id": "C1", "person1": {"resourceId": "A"}, "person2": {"resourceId": "B"}}]}

    class Api:
        def read_person_spouses(self, pid):
            return result(body)

    listed = wc.relatives(type("C", (), {"api": Api()})(), "spouses", "A")
    assert listed.persons == {"A", "B"} and set(listed.of_kind("couple")) == {"C1"}
    assert wc.relationship_view(result(body), "C1").kind == "None"  # type checked strictly


def test_restore_is_bare_and_retries_with_an_empty_body_only_on_415():
    sent = []

    def call(status):
        def send(body):
            sent.append(body)
            if status and len(sent) == 1:
                raise FSHTTPError(status, "")
            return result(None, 204)
        return send

    wc.restore(call(None))
    assert sent == [None]
    sent.clear()
    wc.restore(call(415))
    assert sent == [None, {}]
    sent.clear()
    with pytest.raises(FSHTTPError):
        wc.restore(call(400))
    assert sent == [None]


@responses.activate
def test_relationship_fixtures_are_tracked_read_back_and_torn_down():
    tree = WriteTree().serve()
    ctx = ctx_for()
    dad, mom = ctx.fixtures.person(), ctx.fixtures.person(gender="Female")
    child = ctx.fixtures.person(birth_year="1880")
    crid = ctx.fixtures.couple(dad, mom)
    caprid = ctx.fixtures.child_and_parents(child, [dad, mom])
    assert ctx.fixtures.tracked("couple") == [crid]
    assert ctx.fixtures.tracked("child_and_parents") == [caprid]
    assert ctx.fixtures.person_ref(dad) == {"resource": f"{PERSONS}/{dad}", "resourceId": dad}
    spouses = wc.relatives(ctx, "spouses", dad)
    assert set(spouses.of_kind("couple")) == {crid} and mom in spouses.persons
    assert set(wc.relatives(ctx, "parents", child).of_kind("child_and_parents")) == {caprid}
    assert wc.relatives(ctx, "spouses", child).relationships == {}  # 204
    assert wc.read_couple(ctx, crid).persons == {dad, mom}
    assert wc.read_child_and_parents(ctx, caprid).roles == {
        "parent1": dad, "parent2": mom, "child": child}
    assert len(tree.relationships) == 2  # their couple existed: none derived
    for bad in (lambda: ctx.fixtures.couple(dad, "KWQ7-ABC"),
                lambda: ctx.fixtures.child_and_parents("KWQ7-ABC", [dad]),
                lambda: ctx.fixtures.person_ref("KWQ7-ABC")):
        with pytest.raises(ValueError):
            bad()  # never a person this run did not make
    with pytest.raises(ValueError):
        wc.relatives(ctx, "cousins", dad)
    assert ctx.fixtures.teardown() == [] and tree.live() == []


@responses.activate
def test_fake_derives_a_couple_for_two_parents_without_one():
    tree = WriteTree().serve()
    ctx = ctx_for()
    dad, mom = ctx.fixtures.person(), ctx.fixtures.person(gender="Female")
    child = ctx.fixtures.person(birth_year="1880")
    ctx.fixtures.child_and_parents(child, [dad, mom])
    [derived] = tree.couples_between(dad, mom)
    assert derived in tree.live()  # untracked: left behind unless a recipe tracks it
    ctx.fixtures.track("couple", derived)
    assert ctx.fixtures.teardown() == [] and tree.live() == []


# ---------------------------------------------------------------- sources, source box


def test_redact_account_hides_the_users_ids():
    from tools.verify.core import redact_account
    text = ("GET /platform/sources/cis.user.MZZZ-USR1/collections/descriptions owner "
            "cis.user.MZZZ-USR1 by /platform/users/agents/AZZZ-AGT1; "
            "/platform/sources/collections/UDWZ-001 /platform/sources/descriptions/SD-1")
    out = redact_account(text)
    assert "MZZZ-USR1" not in out and "AZZZ-AGT1" not in out
    assert "/platform/sources/{uid}/collections/descriptions" in out
    # Collection and description paths are not account ids.
    assert "/platform/sources/collections/UDWZ-001" in out
    assert "/platform/sources/descriptions/SD-1" in out


def test_describe_box_drops_links_and_attribution():
    body = {"collections": [{"id": "UD-1", "title": "ZZSPIKEF000000000001",
                             "attribution": {"contributor": {"resourceId": "AZZZ-AGT1"}},
                             "links": {"owner": {"href": "/sources/cis.user.X-1/collections"}}}],
            "note": "cis.user.MZZZ-USR1"}
    shown = wc.describe_box(result(body))
    assert "ZZSPIKEF000000000001" in shown and "UD-1" in shown
    assert "AZZZ-AGT1" not in shown and "X-1" not in shown and "MZZZ-USR1" not in shown
    assert "attribution" not in shown and "links" not in shown
    assert wc.describe_box(None) == "response: (no response)"


def test_description_view_is_exactly_the_description_read():
    body = {"sourceDescriptions": [{"id": "SD-1", "about": "https://example.org/a",
                                    "titles": [{"value": "T"}], "citations": [{"value": "C"}]}]}
    d = wc.description_view(result(body), "SD-1")
    assert (d.id, d.about, d.title, d.citation) == ("SD-1", "https://example.org/a", "T", "C")
    with pytest.raises(AssertionError, match="does not hold it once"):
        wc.description_view(result(body), "SD-2")
    assert wc.description_body("a", "t", "c", sid="SD-1") == {"sourceDescriptions": [
        {"id": "SD-1", "about": "a", "titles": [{"value": "t"}], "citations": [{"value": "c"}]}]}
    with pytest.raises(AssertionError, match="no X-ENTITY-ID, Entity-Id or Location"):
        wc.require_created_id(ApiResult(201, {}, "", None, ""), "source description")
    assert wc.created_id(ApiResult(201, {}, "", None, ""), "x") is None
    located = ApiResult(201, {"Location": f"{DESCRIPTIONS}/SD-9"}, "", None, "")
    assert wc.require_created_id(located, "source description") == "SD-9"
    entity = ApiResult(201, {"Entity-Id": "UD-7"}, "", None, "")
    assert wc.created_id(entity, "collection") == "UD-7"


@responses.activate
@pytest.mark.parametrize("gone_folder", [400, 404])
def test_fixture_collections_are_tracked_filled_and_torn_down(gone_folder):
    tree = WriteTree(gone_folder=gone_folder).serve()
    ctx = ctx_for()
    udcid = ctx.fixtures.collection("ZZSPIKEF000000000001")
    a = ctx.fixtures.source("ZZSPIKES000000000001")
    b = ctx.fixtures.source("ZZSPIKES000000000002")
    ctx.fixtures.add_to_collection(udcid, [a, b])
    assert ctx.fixtures.tracked("collection") == [udcid]
    assert set(wc.members(ctx, udcid).items) == {a, b}
    assert wc.read_folder(ctx, udcid).title == "ZZSPIKEF000000000001"
    listed = wc.folders(ctx)
    assert listed.items[udcid] == "ZZSPIKEF000000000001" and listed.items["UDZZ-DEF"] is None
    for bad in (lambda: ctx.fixtures.collection("not a fixture"),
                lambda: ctx.fixtures.add_to_collection(udcid, ["SD-NOT-OURS"]),
                lambda: ctx.fixtures.add_to_collection("UD-NOT-OURS", [a])):
        with pytest.raises(ValueError):
            bad()
    # Sources go first, so the collection is empty when its turn comes.
    assert ctx.fixtures.teardown() == [] and tree.live() == []


@responses.activate
def test_a_400_counts_as_gone_only_when_the_folder_list_agrees(monkeypatch):
    WriteTree().serve()  # a deleted collection answers 400, as beta's unknown ids
    ctx = ctx_for()
    udcid = ctx.fixtures.collection("ZZSPIKEF000000000002")
    monkeypatch.setattr(ctx.fixtures, "folder_listed", lambda _udcid: True)
    problems = ctx.fixtures.teardown()
    assert len(problems) == 1 and f"collection {udcid}" in problems[0], problems
    assert "400" in problems[0]


@responses.activate
def test_a_non_empty_collection_fails_teardown_loudly():
    tree = WriteTree().serve()
    ctx = ctx_for()
    udcid = ctx.fixtures.collection("ZZSPIKEF000000000003")
    _status, headers, _body = tree._new_description({
        "about": "a", "titles": [{"value": "ZZSPIKES3"}], "citations": [{"value": "c"}]})
    tree._set_folder(headers["X-ENTITY-ID"], udcid)  # a member this run does not track
    problems = ctx.fixtures.teardown()
    assert len(problems) == 1 and "400" in problems[0], problems
