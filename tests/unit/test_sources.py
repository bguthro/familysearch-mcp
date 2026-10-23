import json
from urllib.parse import urlsplit

import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.sources import Sources

BASE = "https://beta.familysearch.org"
DESC = BASE + "/platform/sources/descriptions"
PERSONS = BASE + "/platform/tree/persons"
Q = "?flag=fsh"


class FakeDiscovery:
    def href(self, rel):
        return {"source-descriptions": DESC, "persons": PERSONS + Q}[rel]


def _src():
    return Sources(FSClient("tkn", Environment.BETA, min_write_interval=0), FakeDiscovery())


@responses.activate
def test_source_create_posts_descriptions():
    responses.add(responses.POST, DESC, status=201, headers={"X-ENTITY-ID": "SD-1"})
    sid = _src().create_description("https://example.com/x", "A title", "A citation", reason="r1")
    assert sid == "SD-1"
    req = responses.calls[0].request
    assert json.loads(req.body) == {"sourceDescriptions": [{
        "about": "https://example.com/x",
        "titles": [{"value": "A title"}],
        "citations": [{"value": "A citation"}],
    }]}
    assert req.headers["X-Reason"] == "r1"


@responses.activate
def test_source_attach_posts_to_person_resource():
    responses.add(responses.POST, PERSONS + "/PID-1" + Q, status=200)
    _src().attach_to_person("PID-1", DESC + "/SD-1", reason="r2")
    req = responses.calls[0].request
    assert req.url == PERSONS + "/PID-1" + Q
    u = urlsplit(req.url)
    assert u.path.endswith("/PID-1") and u.query == "flag=fsh"
    assert json.loads(req.body) == {"persons": [{"sources": [{"description": DESC + "/SD-1"}]}]}
    assert req.headers["X-Reason"] == "r2"
