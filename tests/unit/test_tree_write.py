import json

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.tree import TreeWriter

BASE = "https://beta.familysearch.org"
PERSONS = BASE + "/platform/tree/persons"
RELS = BASE + "/platform/tree/relationships"
Q = "?flag=fsh"
# Real discovery hrefs carry a query string.


class FakeDiscovery:
    def href(self, rel):
        return {"persons": PERSONS + Q, "relationships": RELS + Q}[rel]


def _writer():
    c = FSClient("tkn", Environment.BETA, min_write_interval=0)
    return TreeWriter(c, FakeDiscovery())


def _path_query(i):
    from urllib.parse import urlsplit
    u = urlsplit(responses.calls[i].request.url)
    return u.path, u.query


def _body(i):
    return json.loads(responses.calls[i].request.body)


@responses.activate
def test_create_person_posts_envelope_and_returns_id():
    responses.add(responses.POST, PERSONS + Q, status=201, headers={"X-ENTITY-ID": "BMXM-GRC"})
    person = {"gender": {"type": "http://gedcomx.org/Male"}}
    assert _writer().create_person(person, reason="r1") == "BMXM-GRC"
    req = responses.calls[0].request
    assert req.method == "POST" and req.url == PERSONS + Q
    assert _body(0) == {"persons": [person]}
    assert req.headers["Content-Type"] == "application/x-fs-v1+json"
    assert req.headers["X-Reason"] == "r1"


@responses.activate
def test_create_person_id_from_location_fallback():
    responses.add(responses.POST, PERSONS + Q, status=201, headers={"Location": PERSONS + "/ABCD-123"})
    assert _writer().create_person({}, reason="r") == "ABCD-123"


@responses.activate
def test_add_fact_posts_fact_envelope():
    responses.add(responses.POST, PERSONS + "/P1" + Q, status=200)
    fact = {"type": "http://gedcomx.org/Birth"}
    assert _writer().add_fact("P1", fact, reason="why") is None
    assert _body(0)["persons"][0]["facts"][0] == fact
    assert responses.calls[0].request.headers["Content-Type"] == "application/x-fs-v1+json"
    assert responses.calls[0].request.headers["X-Reason"] == "why"


@responses.activate
def test_add_fact_posts_to_person_resource():
    responses.add(responses.POST, PERSONS + "/P1" + Q, status=200)
    _writer().add_fact("P1", {"type": "x"}, reason="r")
    url = responses.calls[0].request.url
    assert _path_query(0) == ("/platform/tree/persons/P1", "flag=fsh")
    assert "/conclusions" not in url and "/sources" not in url


@responses.activate
def test_create_couple_body_shape():
    responses.add(responses.POST, RELS + Q, status=201, headers={"X-ENTITY-ID": "R-1"})
    assert _writer().create_couple("P1", "P2", reason="r") == "R-1"
    rel = _body(0)["relationships"][0]
    assert rel["type"] == "http://gedcomx.org/Couple"
    assert rel["person1"] == {"resource": PERSONS + "/P1", "resourceId": "P1"}
    assert rel["person2"] == {"resource": PERSONS + "/P2", "resourceId": "P2"}
    assert responses.calls[0].request.headers["X-Reason"] == "r"


CP = BASE + "/platform/tree/child-and-parents-relationships"


@responses.activate
def test_create_parent_child_one_post_with_both_parents():
    responses.add(responses.POST, RELS + Q, status=201, headers={
        "X-ENTITY-ID": "CP-1", "Location": CP + "/CP-1"})
    rid = _writer().create_parent_child(child_id="C", parent_ids=["DAD", "MOM"], reason="r")
    assert rid == "CP-1" and isinstance(rid, str)
    assert len(responses.calls) == 1
    req = responses.calls[0].request
    assert req.url == RELS + Q
    assert _body(0) == {"childAndParentsRelationships": [{
        "parent1": {"resource": PERSONS + "/DAD", "resourceId": "DAD"},
        "parent2": {"resource": PERSONS + "/MOM", "resourceId": "MOM"},
        "child": {"resource": PERSONS + "/C", "resourceId": "C"},
    }]}
    assert req.headers["Content-Type"] == "application/x-fs-v1+json"
    assert req.headers["X-Reason"] == "r"


@responses.activate
def test_create_parent_child_single_parent_omits_parent2():
    responses.add(responses.POST, RELS + Q, status=201, headers={"X-ENTITY-ID": "CP-2"})
    assert _writer().create_parent_child(child_id="C", parent_ids=["DAD"], reason="r") == "CP-2"
    assert _body(0) == {"childAndParentsRelationships": [{
        "parent1": {"resource": PERSONS + "/DAD", "resourceId": "DAD"},
        "child": {"resource": PERSONS + "/C", "resourceId": "C"},
    }]}


@pytest.mark.parametrize("parents", [[], ["A", "B", "C"], "AB", None])
@responses.activate
def test_create_parent_child_validates_parent_count(parents):
    with pytest.raises(ValueError):
        _writer().create_parent_child(child_id="C", parent_ids=parents, reason="r")
    assert len(responses.calls) == 0


COUPLE = BASE + "/platform/tree/couple-relationships"


@responses.activate
def test_delete_couple_uses_couple_relationships_resource():
    responses.add(responses.DELETE, COUPLE + "/R-1", status=204)
    assert _writer().delete_couple("R-1", reason="cleanup") is None
    req = responses.calls[0].request
    assert req.url == COUPLE + "/R-1" and req.method == "DELETE"
    assert "/platform/tree/relationships/" not in req.url
    assert req.headers["X-Reason"] == "cleanup"


@responses.activate
def test_delete_child_and_parents_uses_cp_path():
    responses.add(responses.DELETE, CP + "/CP-1", status=204)
    assert _writer().delete_child_and_parents("CP-1", reason="cleanup") is None
    req = responses.calls[0].request
    assert req.url == CP + "/CP-1" and req.method == "DELETE"
    assert req.headers["X-Reason"] == "cleanup"


@responses.activate
def test_delete_relationship_ids_are_quoted():
    responses.add(responses.DELETE, CP + "/a%2Fb", status=204)
    _writer().delete_child_and_parents("a/b", reason="r")
    assert responses.calls[0].request.url == CP + "/a%2Fb"


@responses.activate
def test_delete_person_204_ok():
    responses.add(responses.DELETE, PERSONS + "/P1" + Q, status=204)
    assert _writer().delete_person("P1", reason="dup") is None
    assert _path_query(0) == ("/platform/tree/persons/P1", "flag=fsh")
    assert responses.calls[0].request.headers["X-Reason"] == "dup"


def test_relationship_url_helpers_match_delete_urls():
    from familysearch.tree import child_and_parents_url, couple_url
    assert couple_url(BASE, "R-1") == COUPLE + "/R-1"
    assert child_and_parents_url(BASE, "a/b") == CP + "/a%2Fb"
