import json
from pathlib import Path

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import LinkNotFoundError

FIX = Path(__file__).parent / "fixtures"
BASE = "https://beta.familysearch.org"


def _load(name):
    return json.loads((FIX / name).read_text())


def _disc():
    return Discovery(FSClient("tkn", Environment.BETA))


@responses.activate
def test_links_parsed_from_collection():
    responses.add(responses.GET, BASE + "/platform/collections/tree", json=_load("collections_tree.json"))
    d = _disc()
    assert d.href("relationships").split("?")[0].endswith("/platform/tree/relationships")
    assert "{" in d.href("person-search")


@responses.activate
def test_links_top_level_shape():
    responses.add(responses.GET, BASE + "/platform/collections/tree",
                  json={"links": {"persons": {"href": BASE + "/p"}}})
    assert _disc().href("persons") == BASE + "/p"


@responses.activate
def test_links_cached():
    responses.add(responses.GET, BASE + "/platform/collections/tree", json=_load("collections_tree.json"))
    d = _disc()
    d.href("persons")
    d.href("relationships")
    assert len(responses.calls) == 1


@responses.activate
def test_missing_rel_raises_named():
    responses.add(responses.GET, BASE + "/platform/collections/tree", json=_load("collections_tree.json"))
    with pytest.raises(LinkNotFoundError, match="nope"):
        _disc().href("nope")


@responses.activate
def test_person_links_returns_person_rels():
    responses.add(responses.GET, BASE + "/platform/tree/persons/BMXM-2HZ", json=_load("person.json"))
    links = _disc().person_links("BMXM-2HZ")
    assert links["artifacts"].split("?")[0].endswith("/persons/BMXM-2HZ/memories")
    assert "merge" in links
