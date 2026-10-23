import json

import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.tree import TreeWriter

BASE = "https://beta.familysearch.org"
PERSONS = BASE + "/platform/tree/persons"
RELS = BASE + "/platform/tree/relationships"


class FakeDiscovery:
    def href(self, rel):
        return {"persons": PERSONS, "relationships": RELS}[rel]


def _writer():
    c = FSClient("tkn", Environment.BETA, min_write_interval=0)
    return TreeWriter(c, FakeDiscovery())


def _body(i):
    return json.loads(responses.calls[i].request.body)


@responses.activate
def test_create_person_posts_envelope_and_returns_id():
    responses.add(responses.POST, PERSONS, status=201, headers={"X-ENTITY-ID": "BMXM-GRC"})
    person = {"gender": {"type": "http://gedcomx.org/Male"}}
    assert _writer().create_person(person, reason="r1") == "BMXM-GRC"
    req = responses.calls[0].request
    assert req.method == "POST" and req.url == PERSONS
    assert _body(0) == {"persons": [person]}
    assert req.headers["X-Reason"] == "r1"


@responses.activate
def test_create_person_id_from_location_fallback():
    responses.add(responses.POST, PERSONS, status=201, headers={"Location": PERSONS + "/ABCD-123"})
    assert _writer().create_person({}, reason="r") == "ABCD-123"


@responses.activate
def test_add_fact_posts_fact_envelope():
    responses.add(responses.POST, PERSONS + "/P1", status=200)
    fact = {"type": "http://gedcomx.org/Birth"}
    assert _writer().add_fact("P1", fact, reason="why") is None
    assert _body(0)["persons"][0]["facts"][0] == fact
    assert responses.calls[0].request.headers["X-Reason"] == "why"


@responses.activate
def test_add_fact_posts_to_person_resource():
    responses.add(responses.POST, PERSONS + "/P1", status=200)
    _writer().add_fact("P1", {"type": "x"}, reason="r")
    url = responses.calls[0].request.url
    assert url == PERSONS + "/P1"
    assert "/conclusions" not in url and "/sources" not in url


@responses.activate
def test_create_couple_body_shape():
    responses.add(responses.POST, RELS, status=201, headers={"X-ENTITY-ID": "R-1"})
    assert _writer().create_couple("P1", "P2", reason="r") == "R-1"
    rel = _body(0)["relationships"][0]
    assert rel["type"] == "http://gedcomx.org/Couple"
    assert rel["person1"] == {"resource": PERSONS + "/P1", "resourceId": "P1"}
    assert rel["person2"] == {"resource": PERSONS + "/P2", "resourceId": "P2"}
    assert responses.calls[0].request.headers["X-Reason"] == "r"


@responses.activate
def test_create_parent_child_posts_parentchild_per_parent():
    responses.add(responses.POST, RELS, status=201, headers={"X-ENTITY-ID": "R-A"})
    responses.add(responses.POST, RELS, status=201, headers={"X-ENTITY-ID": "R-B"})
    ids = _writer().create_parent_child(child_id="C", parent_ids=["DAD", "MOM"], reason="r")
    assert ids == ["R-A", "R-B"]
    assert len(responses.calls) == 2
    for i, parent in enumerate(["DAD", "MOM"]):
        assert responses.calls[i].request.url == RELS
        rel = _body(i)["relationships"][0]
        assert rel["type"] == "http://gedcomx.org/ParentChild"
        assert rel["person1"]["resourceId"] == parent
        assert rel["person2"] == {"resource": PERSONS + "/C", "resourceId": "C"}
        assert responses.calls[i].request.headers["X-Reason"] == "r"


@responses.activate
def test_delete_person_204_ok():
    responses.add(responses.DELETE, PERSONS + "/P1", status=204)
    assert _writer().delete_person("P1", reason="dup") is None
    assert responses.calls[0].request.headers["X-Reason"] == "dup"
