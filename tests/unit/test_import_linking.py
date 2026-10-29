import plistlib
import unicodedata

from familysearch_import.linking import FsPerson, compare, name_matches, resolve_links
from familysearch_import.scan import scan_archive


def person(tmp_path, folder="1844-1915 Pat Example", files=None):
    d = tmp_path / "a" / "L" / folder
    d.mkdir(parents=True)
    for name, content in (files or {}).items():
        (d / name).write_text(content, encoding="utf-8")
    (p,) = scan_archive(tmp_path / "a")
    return p


def fs(id="ABCD-123", name="Pat Example", b=1844, d=1915, status="ok", survivor=None, living=False):
    return FsPerson(id, name, b, d, living, status, survivor)


def test_compare_confirmed_probable_mismatch(tmp_path):
    p = person(tmp_path)
    assert compare(p, fs()) == "confirmed"
    assert compare(p, fs(b=None)) == "confirmed"  # absent on one side is fine
    assert compare(p, fs(b=1846)) == "probable"  # off by 2
    assert compare(p, fs(b=1847)) == "mismatch"  # off by 3
    assert compare(p, fs(b=None, d=None)) == "probable"  # no years to compare
    assert compare(p, fs(name="Pat Other")) == "mismatch"


def test_parent_id_in_child_note_is_mismatch(tmp_path):
    child = person(
        tmp_path,
        "1880-1950 Sam Example",
        {"ResearchSummary.md": "Father: ABCD-123 (Pat Example 1844-1915)"},
    )
    parent = fs("ABCD-123", "Pat Example", 1844, 1915)
    r = resolve_links(child, {"ABCD-123": parent})
    assert compare(child, parent) == "mismatch"
    assert r.verdict == "needs-search"
    assert r.chosen_id is None
    assert r.candidates[0][2] == "mismatch"


def test_name_normalization():
    assert name_matches("Pat Example (Alt)", "Pat Example")
    assert name_matches("Mary Smith (Jones)", "Mary Jones")  # maiden name in parens
    assert name_matches("Pat Example", "Pat Quincy Example")  # middle name
    assert name_matches("Pat Example", "PAT  EXAMPLE.")
    composed = "José Example"
    decomposed = unicodedata.normalize("NFD", composed)
    assert name_matches(composed, decomposed)
    assert name_matches("José Example", "Jose Example")
    assert not name_matches("Pat Example", "Pat Other")
    assert not name_matches("Pat Example", "")


def test_question_mark_dates(tmp_path):
    p = person(tmp_path, "?-1915 Pat Example", {})
    assert p.birth is None
    assert compare(p, fs(b=1844, d=1915)) == "confirmed"
    p2 = person(tmp_path / "x", "1815-? Pat Example", {})
    assert compare(p2, fs(b=1815, d=None)) == "confirmed"


def test_redirected_uses_survivor(tmp_path):
    p = person(tmp_path, files={"n.md": "ID ABCD-123"})
    old = fs("ABCD-123", "Pat Example", 1844, 1915, status="redirected", survivor="WXYZ-9AB")
    new = fs("WXYZ-9AB", "Pat Example", 1844, 1915)
    r = resolve_links(p, {"ABCD-123": old, "WXYZ-9AB": new})
    assert r.verdict == "confirmed"
    assert r.chosen_id == "WXYZ-9AB"
    # survivor data absent from lookups: compare the redirect's own data, still the survivor id
    r2 = resolve_links(p, {"ABCD-123": old})
    assert (r2.verdict, r2.chosen_id) == ("confirmed", "WXYZ-9AB")
    # a redirect with a wrong-person survivor is not linked
    bad = fs("WXYZ-9AB", "Someone Else", 1700, 1760)
    r3 = resolve_links(p, {"ABCD-123": old, "WXYZ-9AB": bad})
    assert (r3.verdict, r3.chosen_id) == ("needs-search", None)


def test_redirect_without_survivor_and_deleted_not_confirmed(tmp_path):
    p = person(tmp_path, files={"n.md": "ID ABCD-123 and WXYZ-9AB"})
    r = resolve_links(p, {
        "ABCD-123": fs("ABCD-123", status="redirected", survivor=None),
        "WXYZ-9AB": fs("WXYZ-9AB", status="deleted"),
    })
    assert r.verdict == "needs-search"
    assert {o for _, _, o in r.candidates} == {"redirected", "deleted"}


def test_errors_are_not_missing(tmp_path):
    p = person(tmp_path, files={"n.md": "ID ABCD-123"})
    r = resolve_links(p, {"ABCD-123": fs(status="error")})
    assert r.verdict == "unlinked"
    assert r.candidates[0][2] == "error"
    r2 = resolve_links(p, {"ABCD-123": fs(status="not-found")})
    assert r2.verdict == "needs-search"


def test_two_confirmed_ids_conflict(tmp_path):
    p = person(tmp_path, files={"n.md": "ABCD-123 and WXYZ-9AB"})
    r = resolve_links(p, {"ABCD-123": fs("ABCD-123"), "WXYZ-9AB": fs("WXYZ-9AB")})
    assert r.verdict == "conflict"
    assert r.chosen_id is None
    assert len(r.candidates) == 2


def test_same_id_in_many_sources_is_one_confirmed(tmp_path):
    p = person(tmp_path, files={"a.md": "ABCD-123", "b.txt": "ABCD-123"})
    r = resolve_links(p, {"ABCD-123": fs()})
    assert (r.verdict, r.chosen_id) == ("confirmed", "ABCD-123")


def test_confirmed_beats_probable_and_probable_alone(tmp_path):
    p = person(tmp_path, files={"n.md": "ABCD-123 and WXYZ-9AB"})
    r = resolve_links(p, {"ABCD-123": fs("ABCD-123", b=1846), "WXYZ-9AB": fs("WXYZ-9AB")})
    assert (r.verdict, r.chosen_id) == ("confirmed", "WXYZ-9AB")
    r2 = resolve_links(p, {"ABCD-123": fs("ABCD-123", b=1846)})
    assert (r2.verdict, r2.chosen_id) == ("probable", "ABCD-123")


def test_no_candidates_needs_search(tmp_path):
    p = person(tmp_path)
    assert resolve_links(p, {}).verdict == "needs-search"


def test_webloc_candidate(tmp_path):
    p = person(tmp_path)
    (p.path / "FS.webloc").write_bytes(
        plistlib.dumps({"URL": "https://www.familysearch.org/tree/person/details/ABCD-123"})
    )
    (p2,) = scan_archive(tmp_path / "a")
    assert resolve_links(p2, {"ABCD-123": fs()}).verdict == "confirmed"
