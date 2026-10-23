import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.search import Search

FIX = Path(__file__).parent / "fixtures"
BASE = "https://beta.familysearch.org"


def _load(name):
    return json.loads((FIX / name).read_text())


def _search():
    responses.add(responses.GET, BASE + "/platform/collections/tree",
                  json=_load("collections_tree.json"))
    c = FSClient("tkn", Environment.BETA)
    return Search(c, Discovery(c))


@responses.activate
def test_tree_search_returns_entries_with_confidence():
    s = _search()
    responses.add(responses.GET, BASE + "/platform/tree/search", json=_load("tree_search.json"))
    out = s.tree_search(given="John", surname="Smith")
    assert out and isinstance(out[0]["confidence"], int)


@responses.activate
def test_tree_search_sends_q_params():
    s = _search()
    responses.add(responses.GET, BASE + "/platform/tree/search", json=_load("tree_search.json"))
    s.tree_search(given="August", surname="Anderson")
    req = responses.calls[-1].request
    q = parse_qs(urlparse(req.url).query)
    assert q["q.surname"] == ["Anderson"] and q["q.givenName"] == ["August"]
    assert "{" not in req.url and "%7B" not in req.url
    assert req.headers["Accept"] == "application/x-gedcomx-atom+json"


@responses.activate
def test_tree_search_empty():
    s = _search()
    responses.add(responses.GET, BASE + "/platform/tree/search", json={})
    assert s.tree_search(given="X") == []


@responses.activate
def test_record_search_parses_total_and_next():
    s = _search()
    responses.add(responses.GET, BASE + "/service/search/hr/v2/personas",
                  json=_load("record_personas.json"))
    out = s.record_search(given="August", surname="Anderson", count=2)
    assert out["total"] == 12676
    assert out["next"].endswith("offset=2&count=2")
    assert out["entries"]
    q = parse_qs(urlparse(responses.calls[-1].request.url).query)
    assert q["q.surname"] == ["Anderson"] and q["count"] == ["2"]


@responses.activate
def test_record_search_no_next_when_absent():
    s = _search()
    data = _load("record_personas.json")
    del data["links"]
    responses.add(responses.GET, BASE + "/service/search/hr/v2/personas", json=data)
    assert s.record_search(surname="Anderson")["next"] is None
