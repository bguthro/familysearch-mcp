from pathlib import Path

import pytest

from familysearch_import.classify import classify_document, document_kind, living_status
from familysearch_import.scan import Person


def person(birth=1850, death=1920):
    return Person("L", "f", Path("/nonexistent"), birth, death, "Pat Example", (), (), frozenset())


@pytest.mark.parametrize(
    "birth,death,rec,expected",
    [
        (1850, 1920, None, "deceased"),
        (1950, None, None, "living"),
        (1900, None, None, "living"),
        (1899, None, None, "unknown"),
        (None, None, None, "unknown"),
        (1950, None, "deceased", "deceased"),
        (1850, 1920, "living", "living"),
        (None, 1900, None, "deceased"),
    ],
)
def test_living_status(birth, death, rec, expected):
    assert living_status(person(birth, death), rec) == expected


def test_living_never_gets_documents(tmp_path):
    p = person(1950, None)
    for name in ("Obituary.pdf", "1950 census.pdf", "birth certificate.jpg"):
        v = classify_document(p, tmp_path / name, living_status(p))
        assert v.action == "skip" and v.reason == "living"
    v = classify_document(p, tmp_path / "x.pdf", "unknown")
    assert v.action == "skip" and v.reason == "unknown status"


def test_basic_include_and_kinds(tmp_path):
    p = person()
    v = classify_document(p, tmp_path / "1900 Census Pat.pdf", "deceased")
    assert (v.action, v.kind) == ("include", "census")
    assert document_kind("Obituary.pdf") == "obituary"
    assert document_kind("Marriage license.jpg") == "marriage"
    assert document_kind("Will and probate.pdf") == "will/probate"
    assert document_kind("random.pdf") == "other"
    assert document_kind("Death cert.pdf") == "death"


@pytest.mark.parametrize(
    "name",
    ["Citation: census.pdf", "SOURCE INFORMATION.pdf", "notes.md", "x.json", "l.webloc", "t.txt", "p.html"],
)
def test_non_documents_skipped(tmp_path, name):
    v = classify_document(person(), tmp_path / name, "deceased")
    assert v.action == "skip"


@pytest.mark.parametrize(
    "name",
    ["Fold3 draft card.pdf", "ancestry census.pdf", "Newspapers.com clip.jpg",
     "MyHeritage record.pdf", "Find a Grave memorial.png", "findagrave stone.jpg"],
)
def test_banned_without_source_is_skipped(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"junk")
    v = classify_document(person(), f, "deceased")
    assert v.action == "skip" and v.reason == "banned provider, no original source"
    assert v.provider


def test_banned_with_embedded_url_is_cite_only(tmp_path):
    pytest.importorskip("pypdf")
    from tests.unit.test_import_embedded import make_pdf

    f = tmp_path / "ancestry record.pdf"
    make_pdf(f, link="https://example.org/original/77")
    v = classify_document(person(), f, "deceased")
    assert v.action == "cite-only"
    assert v.source_url == "https://example.org/original/77"


def test_banned_detected_from_embedded_host(tmp_path):
    pytest.importorskip("pypdf")
    from tests.unit.test_import_embedded import make_pdf

    f = tmp_path / "record.pdf"
    make_pdf(f, link="https://www.fold3.com/image/1")
    v = classify_document(person(), f, "deceased")
    assert v.action == "cite-only" and v.provider == "fold3"


def test_unreadable_pdf_included_when_not_banned(tmp_path):
    f = tmp_path / "corrupt.pdf"
    f.write_bytes(b"garbage")
    v = classify_document(person(), f, "deceased")
    assert v.action == "include" and v.source_url is None


def test_custom_banned_list(tmp_path):
    v = classify_document(person(), tmp_path / "foo site.pdf", "deceased", banned=("foo site",))
    assert v.action == "skip"
