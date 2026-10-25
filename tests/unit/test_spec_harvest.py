import json
import re
from pathlib import Path

import pytest

from tools.spec import harvest as h

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "spec"
SNAPSHOT = Path(__file__).resolve().parents[2] / "tools" / "spec" / "snapshot.json"
BASE = "https://developers.familysearch.org/main/reference/"

AREAS = {
    "Authentication Endpoints",
    "Family Tree Endpoints",
    "Genealogies Endpoints",
    "Memories Endpoints",
    "Standards Endpoints",
    "User Endpoints",
    "User Tree Endpoints",
    "Utilities Endpoints",
}


def _read(name):
    return (FIX / name).read_text()


def _pages(**overrides):
    pages = {
        h.INDEX_URL: _read("index_mini.txt"),
        BASE + "createperson.md": _read("createperson.md"),
        BASE + "readperson.md": _read("readperson.md"),
    }
    pages.update(overrides)
    return pages


def test_parse_index_skips_guide_and_non_reference():
    entries = h.parse_index(_read("index_mini.txt"))
    assert entries == [
        ("Family Tree Endpoints", "Create Person", BASE + "createperson.md"),
        ("Family Tree Endpoints", "Read Person", BASE + "readperson.md"),
    ]


def test_parse_readperson():
    ops = h.parse_reference_page(_read("readperson.md"), "Family Tree Endpoints")
    assert len(ops) == 1
    op = ops[0]
    assert op["operation_id"] == "readPerson"
    assert op["method"] == "GET"
    assert op["path"] == "/tree/persons/{pid}"
    assert op["area"] == "Family Tree Endpoints"
    pid = next(p for p in op["params"] if p["name"] == "pid")
    assert pid["in"] == "path" and pid["required"] is True
    assert pid["example"] == "LZJW-C31"
    accept = next(p for p in op["params"] if p["name"] == "Accept")
    assert "application/x-fs-v1+json" in accept["enum"]
    assert "404" in op["responses"] and "410" in op["responses"]


def test_parse_createperson_request_media():
    (op,) = h.parse_reference_page(_read("createperson.md"), "Family Tree Endpoints")
    assert op["method"] == "POST"
    assert op["request_media"] == ["application/x-gedcomx-v1+json"]


def test_normalize_sorted_and_idempotent():
    a = h.parse_reference_page(_read("readperson.md"), "Z")[0]
    b = h.parse_reference_page(_read("createperson.md"), "A")[0]
    out = h.normalize([a, b])
    assert out["schema"] == 1 and out["source"] == h.INDEX_URL
    assert [o["area"] for o in out["operations"]] == ["A", "Z"]
    assert h.normalize(out["operations"]) == out
    assert h.normalize(list(reversed(out["operations"]))) == out


def test_harvest_with_fake_fetch():
    pages = _pages()
    snap = h.harvest(pages.__getitem__)
    assert {o["operation_id"] for o in snap["operations"]} == {"createPerson", "readPerson"}
    keys = [(o["area"], o["path"], o["method"]) for o in snap["operations"]]
    assert keys == sorted(keys)


def test_harvest_raises_on_page_without_block():
    pages = _pages(**{BASE + "createperson.md": "# Create Person\n\nno spec"})
    with pytest.raises(RuntimeError, match="createperson"):
        h.harvest(pages.__getitem__)


def test_harvest_raises_on_invalid_json():
    pages = _pages(**{BASE + "createperson.md": "# OpenAPI definition\n\n```json\n{bad\n```\n"})
    with pytest.raises(RuntimeError, match="createperson"):
        h.harvest(pages.__getitem__)


def test_committed_snapshot_shape():
    snap = json.loads(SNAPSHOT.read_text())
    ops = snap["operations"]
    assert len(ops) == 205
    assert len({o["operation_id"] for o in ops}) == 205
    assert {o["area"] for o in ops} == AREAS
    for o in ops:
        names = set(re.findall(r"\{([^}]+)\}", o["path"]))
        have = {p["name"] for p in o["params"] if p["in"] == "path"}
        assert names <= have, (o["operation_id"], names - have)
