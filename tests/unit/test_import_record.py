import copy
import json
from pathlib import Path

import pytest

from familysearch_import.record import (
    RecordError,
    draft_record,
    load_record,
    record_status_for,
    repo_root,
    write_draft,
)
from familysearch_import.scan import Person

VALID = {
    "version": 1,
    "status": "deceased",
    "familysearch_id": "ABCD-123",
    "names": [{"given": "Pat", "surname": "Example", "note": "n"}],
    "gender": "Female",
    "facts": [{"type": "Birth", "date": "1844", "place": "Town", "sources": ["s1"]}],
    "relationships": [{"kind": "parent", "folder": "1810-1880 Sam Example", "line": "L"}],
    "sources": [{"title": "t", "citation": "c", "url": "https://example.org", "file": "a.pdf"}],
    "reviewed": True,
}


def person(path: Path, ids=(), birth=1844, death=1915, name="Pat Example"):
    return Person("L", f"{birth}-{death} {name}", path, birth, death, name, (), tuple(ids), frozenset())


def write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_valid_loads(tmp_path):
    assert load_record(write(tmp_path / "r.json", VALID)) == VALID


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda d: d.pop("version"), "version"),
        (lambda d: d.update(status="dead"), "status"),
        (lambda d: d.update(gender="X"), "gender"),
        (lambda d: d.update(familysearch_id="nope"), "familysearch_id"),
        (lambda d: d.update(facts={}), "facts"),
        (lambda d: d.update(reviewed="yes"), "reviewed"),
        (lambda d: d["facts"][0].update(type="Bogus"), "facts[0].type"),
        (lambda d: d["facts"][0].update(sources="s"), "facts[0].sources"),
        (lambda d: d["names"][0].update(given=3), "names[0].given"),
        (lambda d: d["relationships"][0].update(kind="cousin"), "relationships[0].kind"),
        (lambda d: d["sources"][0].pop("citation"), "sources[0].citation"),
        (lambda d: d.update(names=["x"]), "names[0]"),
    ],
)
def test_invalid_names_field(tmp_path, mutate, field):
    d = copy.deepcopy(VALID)
    mutate(d)
    with pytest.raises(RecordError) as e:
        load_record(write(tmp_path / "r.json", d))
    assert str(e.value).startswith(field)


def test_invalid_json(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{nope")
    with pytest.raises(RecordError):
        load_record(p)


def test_draft_record(tmp_path):
    d = draft_record(person(tmp_path, ids=("ABCD-123",), name="Pat Q Example (Alt Name)"))
    assert d["names"] == [{"given": "Pat Q", "surname": "Example", "note": "folder alternate: Alt Name"}]
    assert d["familysearch_id"] == "ABCD-123"
    assert [f["type"] for f in d["facts"]] == ["Birth", "Death"]
    assert d["facts"][0]["date"] == "1844"
    assert d["reviewed"] is False and d["relationships"] == [] and d["sources"] == []
    assert d["status"] is None


def test_draft_id_only_when_exactly_one(tmp_path):
    assert draft_record(person(tmp_path, ids=("ABCD-123", "WXYZ-456")))["familysearch_id"] is None
    assert draft_record(person(tmp_path))["familysearch_id"] is None


def test_draft_validates(tmp_path):
    p = tmp_path / "x.json"
    write(p, draft_record(person(tmp_path)))
    load_record(p)


def test_write_draft_and_refuse_overwrite(tmp_path):
    folder = tmp_path / "arch" / "1844-1915 Pat Example"
    folder.mkdir(parents=True)
    pr = person(folder)
    out = write_draft(pr, repo_root())
    assert out.name == "ImportRecord.draft.json" and out.parent == folder
    load_record(out)
    with pytest.raises(FileExistsError):
        write_draft(pr, repo_root())


def test_write_draft_refuses_reviewed_record(tmp_path):
    folder = tmp_path / "1844-1915 Pat Example"
    folder.mkdir()
    write(folder / "ImportRecord.json", VALID)
    with pytest.raises(FileExistsError):
        write_draft(person(folder), repo_root())
    assert not (folder / "ImportRecord.draft.json").exists()


def test_write_draft_allowed_over_unreviewed_record(tmp_path):
    folder = tmp_path / "1844-1915 Pat Example"
    folder.mkdir()
    write(folder / "ImportRecord.json", {**VALID, "reviewed": False})
    assert write_draft(person(folder), repo_root()).exists()


def test_write_draft_refuses_inside_repo():
    inside = repo_root() / "tests" / "zz_nonexistent_person"
    with pytest.raises(ValueError):
        write_draft(person(inside), repo_root())
    assert not inside.exists()


def test_record_status_for(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    p = person(folder, birth=1950, death=None)
    assert record_status_for(p) == "living"
    write(folder / "ImportRecord.json", {**VALID, "status": "deceased"})
    assert record_status_for(p) == "deceased"
    (folder / "ImportRecord.json").write_text("garbage")
    assert record_status_for(p) == "living"
