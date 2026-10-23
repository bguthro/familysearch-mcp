import json
from pathlib import Path

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError
from familysearch.tree import TreeReader

FIX = Path(__file__).parent / "fixtures"
BASE = "https://beta.familysearch.org"
PERSON = BASE + "/platform/tree/persons/BMXM-2HZ"


def _load(name):
    return json.loads((FIX / name).read_text())


def _reader():
    c = FSClient("tkn", Environment.BETA)
    return TreeReader(c, Discovery(c))


def _add(url, body):
    responses.add(responses.GET, url, json=body)


@responses.activate
def test_get_person_returns_single_person():
    _add(PERSON, _load("person_named.json"))
    assert _reader().get_person("BMXM-2HZ")["display"]["name"] == "Ben Guthro"


@responses.activate
@pytest.mark.parametrize("body", [{"persons": []}, {}])
def test_get_person_empty_list_raises(body):
    _add(PERSON, body)
    with pytest.raises(FSError):
        _reader().get_person("BMXM-2HZ")


@responses.activate
def test_get_relatives_shapes_sections():
    _add(PERSON, _load("person.json"))
    _add(PERSON + "/parents", {"persons": [{"id": "P1"}]})
    _add(PERSON + "/children", {"persons": [{"id": "C1"}, {"id": "C2"}]})
    _add(PERSON + "/spouses", {})
    rel = _reader().get_relatives("BMXM-2HZ")
    assert [p["id"] for p in rel["parents"]] == ["P1"]
    assert len(rel["children"]) == 2
    assert rel["spouses"] == []


@responses.activate
def test_get_relatives_missing_links_gives_empty_lists():
    _add(PERSON, {"persons": [{"id": "X", "links": {}}]})
    assert _reader().get_relatives("BMXM-2HZ") == {"parents": [], "children": [], "spouses": []}


@responses.activate
def test_get_memories_sources_changes_return_lists():
    _add(PERSON, _load("person.json"))
    _add(PERSON + "/memories", {"sourceDescriptions": [{"id": "M1"}]})
    _add(PERSON + "/sources", {"sourceDescriptions": [{"id": "S1"}, {"id": "S2"}]})
    _add(PERSON + "/changes", {"entries": [{"id": "E1"}]})
    r = _reader()
    assert r.get_memories("BMXM-2HZ") == [{"id": "M1"}]
    assert len(r.get_sources("BMXM-2HZ")) == 2
    assert r.get_changes("BMXM-2HZ") == [{"id": "E1"}]


@responses.activate
def test_empty_bodies_return_empty_lists():
    _add(PERSON, _load("person.json"))
    for tail in ("memories", "sources", "changes"):
        _add(f"{PERSON}/{tail}", {})
    r = _reader()
    assert r.get_memories("BMXM-2HZ") == []
    assert r.get_sources("BMXM-2HZ") == []
    assert r.get_changes("BMXM-2HZ") == []


@responses.activate
def test_get_matches_returns_candidates():
    _add(PERSON, _load("person.json"))
    _add(PERSON + "/matches", {"entries": [
        {"id": "BMXM-AAA", "score": 4.5, "content": {"gedcomx": {"persons": [{"id": "BMXM-AAA"}]}}},
        {"id": "BMXM-BBB", "score": 3.0},
    ]})
    out = _reader().get_matches("BMXM-2HZ")
    assert [e["id"] for e in out] == ["BMXM-AAA", "BMXM-BBB"]


@responses.activate
@pytest.mark.parametrize("body", [{}, {"entries": []}, [1, 2]])
def test_get_matches_empty(body):
    _add(PERSON, _load("person.json"))
    _add(PERSON + "/matches", body)
    assert _reader().get_matches("BMXM-2HZ") == []
