import plistlib
import unicodedata
from pathlib import Path

import pytest

from familysearch_import.scan import (
    familysearch_person_id,
    parse_folder_name,
    scan_archive,
    unparsed_folders,
)


def test_parse_folder_name_cases():
    assert parse_folder_name("1844-1915 Pat Example (Alt)") == (1844, 1915, "Pat Example (Alt)")
    assert parse_folder_name("1815-? Pat Example") == (1815, None, "Pat Example")
    assert parse_folder_name("1952 Pat Example") == (1952, None, "Pat Example")
    assert parse_folder_name("1614 - 1708 Pat Example") == (1614, 1708, "Pat Example")
    assert parse_folder_name("?-1900 Pat Example") == (None, 1900, "Pat Example")
    assert parse_folder_name("No Dates") is None
    assert parse_folder_name("") is None


def test_parse_folder_name_nfc():
    composed = "1800-1870 José Example"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    assert parse_folder_name(decomposed) == parse_folder_name(composed)
    assert parse_folder_name(decomposed)[2] == "José Example"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.familysearch.org/tree/person/details/ABCD-123", "ABCD-123"),
        ("https://www.familysearch.org/en/tree/person/details/ABCD-123", "ABCD-123"),
        ("https://www.familysearch.org/en/tree/person/sources/ABCD-123", "ABCD-123"),
        ("https://ancestors.familysearch.org/en/ABCD-123/pat-example-1800-1870", "ABCD-123"),
        ("https://www.familysearch.org/search/record/results?q=x", None),
        ("https://www.familysearch.org/memories/gallery", None),
        ("https://www.familysearch.org/tree/", None),
        ("https://example.com/tree/person/details/ABCD-123", None),
        ("https://www.familysearch.org/tree/person/details/abcd-1", None),
    ],
)
def test_person_id(url, expected):
    assert familysearch_person_id(url) == expected


def _webloc(path: Path, url: str):
    path.write_bytes(plistlib.dumps({"URL": url}))


def make_archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    line = root / "Example Line"
    p = line / "1844-1915 Pat Example (Alt)"
    p.mkdir(parents=True)
    (p / "ResearchSummary.md").write_text("x")
    (p / ".DS_Store").write_text("x")
    (p / "Gmail - hello.pdf").write_text("x")
    (p / "Family Migration Map.html").write_text("x")
    _webloc(p / "FS.webloc", "https://www.familysearch.org/tree/person/details/ABCD-123")
    (p / "BookExclude.txt").write_text("# comment\n\ncert.pdf\n  other.pdf  \n")
    (p / "Questionable").mkdir()
    (p / "Questionable" / "bad.pdf").write_text("x")
    (p / "sub").mkdir()
    (p / "sub" / "doc.pdf").write_text("x")
    (line / "questionable" / "1900-1950 Hidden Person").mkdir(parents=True)
    (line / "No Dates").mkdir()
    (line / "1952 Kid Example").mkdir()
    (root / "books").mkdir()
    (root / "books" / "1900-1950 Skip Me").mkdir()
    (root / "0000_meta" / "1900-1950 Skip Me").mkdir(parents=True)
    (root / "_tools").mkdir()
    (root / ".hidden" / "1900-1950 Nope").mkdir(parents=True)
    return root


def test_scan_archive(tmp_path):
    root = make_archive(tmp_path)
    people = scan_archive(root)
    assert sorted(p.folder for p in people) == [
        "1844-1915 Pat Example (Alt)",
        "1952 Kid Example",
    ]
    pat = next(p for p in people if p.birth == 1844)
    assert pat.line == "Example Line"
    assert pat.death == 1915
    assert pat.display_name == "Pat Example (Alt)"
    names = sorted(f.name for f in pat.files)
    assert names == ["BookExclude.txt", "FS.webloc", "ResearchSummary.md", "doc.pdf"]
    assert pat.familysearch_ids == ("ABCD-123",)
    assert pat.book_excluded == frozenset({"cert.pdf", "other.pdf"})
    assert unparsed_folders(root) == [("Example Line", "No Dates")]


def test_scan_lines_filter_and_missing(tmp_path):
    root = make_archive(tmp_path)
    assert scan_archive(root, ["nonexistent"]) == []
    assert len(scan_archive(root, ["Example Line"])) == 2
    with pytest.raises(FileNotFoundError):
        scan_archive(tmp_path / "missing")


def test_webloc_regex_fallback(tmp_path):
    root = tmp_path / "a" / "L" / "1800-1870 Pat Example"
    root.mkdir(parents=True)
    (root / "x.webloc").write_text(
        "<broken><string>https://www.familysearch.org/tree/person/details/WXYZ-9AB</string>"
    )
    (root / "memories.webloc").write_bytes(
        plistlib.dumps({"URL": "https://www.familysearch.org/memories/gallery"})
    )
    (root / "dup.webloc").write_bytes(
        plistlib.dumps({"URL": "https://www.familysearch.org/tree/person/details/WXYZ-9AB"})
    )
    (p,) = scan_archive(tmp_path / "a")
    assert p.familysearch_ids == ("WXYZ-9AB",)


def test_symlinked_dirs_not_followed(tmp_path):
    outside = tmp_path / "outside"
    (outside / "1800-1870 Linked Person").mkdir(parents=True)
    root = tmp_path / "a"
    person = root / "L" / "1800-1870 Pat Example"
    person.mkdir(parents=True)
    (person / "real.pdf").write_text("x")
    (person / "link").symlink_to(outside, target_is_directory=True)
    (root / "L" / "1900-1950 Link Person").symlink_to(outside, target_is_directory=True)
    (root / "LinkLine").symlink_to(outside, target_is_directory=True)
    people = scan_archive(root)
    assert [p.folder for p in people] == ["1800-1870 Pat Example"]
    assert [f.name for f in people[0].files] == ["real.pdf"]
