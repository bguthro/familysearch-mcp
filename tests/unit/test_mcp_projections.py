import json
from pathlib import Path

import pytest

from familysearch_mcp.projections import (
    project_memory,
    project_person,
    project_record_hit,
    project_search_hit,
    project_source,
)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_project_person_fields():
    p = project_person(load("person_named.json")["persons"][0])
    assert p["id"] == "BMXM-2HZ"
    assert p["name"] == "Ben Guthro"
    assert p["lifespan"] == "1975-"
    assert p["gender"] == "Male"
    assert p["living"] is None
    assert p["facts"] == [{"type": "Birth", "date": "1975", "place": None}]
    assert p["links_summary"] == ["person"]


def test_fact_type_unknown_passthrough_and_fallbacks():
    p = project_person({
        "living": False,
        "gender": {"type": "http://gedcomx.org/Female"},
        "facts": [
            {"type": "data:,Custom", "date": {"formal": "+1850-01-01"}, "place": {"original": "X"}},
            {},
        ],
        "links": {"parents": {}, "ancestry": {}, "children": {}},
    })
    assert p["gender"] == "Female"
    assert p["living"] is False
    assert p["facts"][0] == {"type": "data:,Custom", "date": "+1850-01-01", "place": "X"}
    assert p["facts"][1] == {"type": None, "date": None, "place": None}
    assert p["links_summary"] == ["ancestry", "children", "parents"]


def test_project_search_hit():
    h = project_search_hit(load("search_entry.json"))
    assert h["id"] == "KW1"
    assert h["confidence"] == 4 and isinstance(h["confidence"], int)
    assert h["name"] == "John Anson Smith"
    assert h["gender"] == "Male"
    assert h["birth"] == {"date": "25 December 1846", "place": "Ontario, Canada"}
    assert h["death"] == {"date": "28 February 1891", "place": "Walters Falls, Grey, Ontario, Canada"}


def test_search_hit_id_from_ark_and_confidence_coercion():
    e = {"confidence": "3", "content": {"gedcomx": {"id": "ark:/61903/4:1:LDQY-5WC"}}}
    h = project_search_hit(e)
    assert h["id"] == "LDQY-5WC"
    assert h["confidence"] == 3
    assert project_search_hit({"confidence": "high"})["confidence"] is None
    assert project_search_hit({})["birth"] == {"date": None, "place": None}


def test_project_record_hit_uses_principal():
    h = project_record_hit(load("record_entry.json"))
    assert h["name"] == "August Anderson"
    assert h["confidence"] == 5
    assert h["id"] is None
    assert h["facts"] == [{"type": "Birth", "date": "1 May 1880", "place": "Sweden"}]


def test_record_hit_first_person_when_no_principal():
    e = {"content": {"gedcomx": {"persons": [{"names": [{"nameForms": [{"fullText": "A B"}]}]}]}}}
    assert project_record_hit(e)["name"] == "A B"


def test_project_source():
    s = project_source({
        "id": "71FD-SL2",
        "titles": [{"value": "T"}],
        "citations": [{"value": "C"}],
        "about": "http://x",
    })
    assert s == {"id": "71FD-SL2", "title": "T", "citation": "C", "about": "http://x"}


def test_project_memory_description_forms():
    base = {"id": "1", "titles": [{"value": "T"}], "links": {"description": {"href": "h"}}}
    m = project_memory({**base, "description": "D"})
    assert m == {"id": "1", "title": "T", "description": "D", "link": "h", "media_type": None}
    assert project_memory({**base, "description": [{"value": "D2"}]})["description"] == "D2"
    assert project_memory({"id": "2"})["link"] is None


LIVE_MEMORY = {
    "about": "https://cdn.example.org/img.png",
    "attribution": {"contributor": {"resourceId": "X"}},
    "created": 1761300000000,
    "id": "238367374",
    "links": {
        "artifact": {"href": "https://beta.familysearch.org/platform/memories/artifacts/1"},
        "memory": {"href": "https://beta.familysearch.org/platform/memories/memories/238367374"},
        "persons": {"href": "https://beta.familysearch.org/platform/memories/memories/238367374/personas"},
    },
    "mediaType": "image/png",
    "resourceType": "http://gedcomx.org/DigitalArtifact",
    "titles": [{"value": "Birth cert"}],
    "artifactMetadata": [{"filename": "a.png"}],
}


def test_project_memory_live_shape():
    assert project_memory(LIVE_MEMORY) == {
        "id": "238367374", "title": "Birth cert", "description": None,
        "link": "https://beta.familysearch.org/platform/memories/memories/238367374",
        "media_type": "image/png",
    }


def test_project_memory_tolerant_titles_and_link_fallback():
    assert project_memory({"titles": ["plain"]})["title"] == "plain"
    assert project_memory({"titles": [None, {"value": "second"}]})["title"] == "second"
    assert project_memory({"titles": [{"value": 5}]})["title"] is None
    old = {"links": {"description": {"href": "old"}}}
    assert project_memory(old)["link"] == "old"
    assert project_memory({"mediaType": 5})["media_type"] is None


def test_int_ids_become_strings():
    assert project_memory({"id": 238367374})["id"] == "238367374"
    assert project_person({"id": 238367374})["id"] == "238367374"
    assert project_source({"id": 238367374})["id"] == "238367374"
    e = {"content": {"gedcomx": {"persons": [{"id": 238367374}]}}}
    assert project_search_hit(e)["id"] == "238367374"
    assert project_record_hit({"id": 238367374})["id"] == "238367374"


def test_bool_and_empty_ids_are_none():
    assert project_person({"id": True})["id"] is None
    assert project_memory({"id": ""})["id"] is None


def test_ark_ending_in_colon_gives_none_id():
    e = {"content": {"gedcomx": {"id": "ark:/61903/4:1:", "persons": [{}]}}}
    assert project_search_hit(e)["id"] is None


ALL = [project_person, project_search_hit, project_record_hit, project_source, project_memory]
BAD = [
    {}, None, [], "x", 5,
    {"display": [], "facts": "x", "links": [], "gender": "m", "id": 5},
    {"titles": {}, "citations": "x", "content": [], "description": 5, "links": "x"},
    {"content": {"gedcomx": {"persons": "x"}}, "confidence": [1]},
    {"content": {"gedcomx": {"persons": [None, 3, {"names": "x", "facts": [None, 1]}]}}},
    {"facts": [None, "x", {"date": "d", "place": []}], "display": {"name": 3}},
]


@pytest.mark.parametrize("fn", ALL)
@pytest.mark.parametrize("bad", BAD)
def test_never_raises(fn, bad):
    assert isinstance(fn(bad), dict)
