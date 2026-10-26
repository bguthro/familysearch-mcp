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
