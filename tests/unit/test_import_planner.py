import pytest

from familysearch_import import planner
from familysearch_import.classify import DocumentVerdict
from familysearch_import.linking import FsPerson
from familysearch_import.planner import build_plan
from familysearch_import.prod_reader import ReadFailed, SourceRef
from familysearch_import.scan import scan_archive


class FakeReader:
    def __init__(self, memories=(), sources=(), person=None, fail=None):
        self.memories = list(memories)
        self.sources = list(sources)
        self.person = person
        self.fail = fail
        self.calls = []

    def get_fs_person(self, pid):
        self.calls.append(("person", pid))
        return self.person or FsPerson(pid, "Pat Example", 1844, 1915, False, "ok")

    def get_memory_titles(self, pid):
        self.calls.append(("memories", pid))
        if self.fail == "memories":
            raise ReadFailed("boom")
        return self.memories

    def get_source_refs(self, pid):
        self.calls.append(("sources", pid))
        if self.fail == "sources":
            raise ReadFailed("boom")
        return self.sources


def make_archive(tmp_path, folders):
    root = tmp_path / "arch"
    for folder, files in folders.items():
        d = root / "Example Line" / folder
        d.mkdir(parents=True)
        for name, data in files.items():
            sub = d / name
            sub.parent.mkdir(parents=True, exist_ok=True)
            sub.write_bytes(data)
    return scan_archive(root)


@pytest.fixture(autouse=True)
def verdicts(monkeypatch):
    """Classifier stub: file name decides the verdict; no embedded-file inspection."""

    def fake(person, path, status, banned=()):
        n = path.name.lower()
        if status != "deceased":
            return DocumentVerdict(path, "other", None, None, "skip", "living")
        if n.endswith((".md", ".txt")):
            return DocumentVerdict(path, "other", None, None, "skip", "not a document file")
        if n.startswith("cite"):
            return DocumentVerdict(path, "other", "ancestry", "https://example.org/r/1", "cite-only", "banned provider")
        if n.startswith("skip"):
            return DocumentVerdict(path, "other", None, None, "skip", "unreadable, provenance unverifiable")
        return DocumentVerdict(path, "other", None, None, "include", "ok")

    monkeypatch.setattr(planner, "classify_document", fake)


PAT = "1844-1915 Pat Example"
KEY = "Example Line/" + PAT


def one(tmp_path, files, **kw):
    people = make_archive(tmp_path, {PAT: files})
    reader = kw.pop("reader", FakeReader())
    links = kw.pop("links", {KEY: "ABCD-123"})
    plan = build_plan(people, links, kw.pop("approved", {}), reader, **kw)
    return plan, plan.people[0], reader


def test_upload_and_cite_and_skip(tmp_path):
    plan, pp, _ = one(tmp_path, {"Census 1900.pdf": b"a", "cite one.pdf": b"b", "skip me.pdf": b"c", "notes.md": b"n"})
    kinds = sorted((a.kind, a.state) for a in pp.actions)
    assert kinds == [("add-source", "planned"), ("skip", "skipped"), ("upload-document", "planned")]
    up = next(a for a in pp.actions if a.kind == "upload-document")
    assert up.title == "Census 1900" and up.sha256 and up.fs_id == "ABCD-123"
    assert pp.status == "planned" and len(plan.plan_hash) == 64


def test_already_present_title_case_and_space_insensitive(tmp_path):
    _, pp, _ = one(tmp_path, {"Census 1900.pdf": b"a"}, reader=FakeReader(memories=["  census   1900 "]))
    assert pp.actions[0].state == "already-present"
    assert pp.status == "nothing-to-do"


def test_ledger_key_makes_already_present(tmp_path):
    _, first, _ = one(tmp_path / "a", {"Doc.pdf": b"a"})
    key = first.actions[0].key
    _, pp, _ = one(tmp_path / "b", {"Doc.pdf": b"a"}, ledger_keys=frozenset({key}))
    assert pp.actions[0].state == "already-present" and pp.actions[0].reason == "in ledger"


def test_source_already_present_by_url_or_title(tmp_path):
    by_url = FakeReader(sources=[SourceRef("Other", "https://example.org/r/1")])
    _, pp, _ = one(tmp_path / "a", {"cite one.pdf": b"b"}, reader=by_url)
    assert pp.actions[0].state == "already-present"
    by_title = FakeReader(sources=[SourceRef("CITE one", None)])
    _, pp, _ = one(tmp_path / "b", {"cite one.pdf": b"b"}, reader=by_title)
    assert pp.actions[0].state == "already-present"


def test_rerun_after_upload_is_not_planned_again(tmp_path):
    _, pp, _ = one(tmp_path / "a", {"Doc.pdf": b"a"})
    assert pp.actions[0].state == "planned"
    _, pp2, _ = one(tmp_path / "b", {"Doc.pdf": b"a"}, reader=FakeReader(memories=["Doc"]))
    assert all(a.state != "planned" for a in pp2.actions)


def test_living_person_zero_actions_even_when_approved(tmp_path):
    people = make_archive(tmp_path, {"1980 Living Example": {"a.pdf": b"a"}})
    key = "Example Line/1980 Living Example"
    reader = FakeReader()
    plan = build_plan(people, {}, {key: "ABCD-123"}, reader)
    pp = plan.people[0]
    assert pp.actions == () and pp.note == "living" and reader.calls == []


def test_unknown_status_skipped(tmp_path):
    people = make_archive(tmp_path, {"?-? Unknown Example": {"a.pdf": b"a"}})
    plan = build_plan(people, {"Example Line/?-? Unknown Example": "ABCD-123"}, {}, FakeReader())
    assert plan.people[0].note == "unknown status" and plan.people[0].actions == ()


def test_fs_says_living_skipped(tmp_path):
    reader = FakeReader(person=FsPerson("ABCD-123", "x", None, None, True, "ok"))
    _, pp, _ = one(tmp_path, {"a.pdf": b"a"}, reader=reader)
    assert pp.actions == () and pp.note == "living"


def test_no_confirmed_link_skipped(tmp_path):
    _, pp, reader = one(tmp_path / "a", {"a.pdf": b"a"}, links={KEY: None})
    assert pp.note == "no confirmed link" and pp.status == "skipped" and reader.calls == []
    _, pp, _ = one(tmp_path / "x", {"a.pdf": b"a"}, links={})
    assert pp.note == "no confirmed link"


def test_approved_link_is_planned(tmp_path):
    _, pp, _ = one(tmp_path, {"a.pdf": b"a"}, links={}, approved={KEY: "WXYZ-999"})
    assert pp.fs_id == "WXYZ-999" and pp.status == "planned"


def test_title_collision_is_disambiguated_and_flagged(tmp_path):
    _, pp, _ = one(tmp_path, {"Will.pdf": b"one", "sub/Will.png": b"two"})
    ups = [a for a in pp.actions if a.kind == "upload-document"]
    assert sorted(a.title for a in ups) == ["Will", "Will-2"]
    assert all(a.state == "planned" for a in ups)
    assert any("collision" in a.reason for a in ups)
    assert len({a.key for a in ups}) == 2


def test_identical_content_not_duplicated(tmp_path):
    _, pp, _ = one(tmp_path, {"Will.pdf": b"same", "sub/Will copy.pdf": b"same"})
    ups = [a for a in pp.actions if a.kind == "upload-document"]
    assert sorted(a.state for a in ups) == ["planned", "skipped"]


@pytest.mark.parametrize("fail", ["memories", "sources"])
def test_read_error_is_unplanned_not_empty(tmp_path, fail):
    _, pp, _ = one(tmp_path, {"a.pdf": b"a"}, reader=FakeReader(fail=fail))
    assert pp.status == "unplanned (read error)" and pp.actions == ()


@pytest.mark.parametrize("status", ["redirected", "deleted", "not-found"])
def test_unresolved_approved_id(tmp_path, status):
    reader = FakeReader(person=FsPerson("ABCD-123", "", None, None, None, status, "WXYZ-999"))
    _, pp, _ = one(tmp_path, {"a.pdf": b"a"}, reader=reader, links={}, approved={KEY: "ABCD-123"})
    assert pp.actions == () and status in pp.note and pp.status == "skipped"


def test_person_lookup_error_is_unplanned(tmp_path):
    reader = FakeReader(person=FsPerson("ABCD-123", "", None, None, None, "error"))
    _, pp, _ = one(tmp_path, {"a.pdf": b"a"}, reader=reader)
    assert pp.status == "unplanned (read error)"


def test_unsupported_extension_and_size(tmp_path):
    _, pp, _ = one(tmp_path, {"scan.tif": b"a", "big.pdf": b"x" * 20, "ok.jpeg": b"y"}, max_bytes=10)
    by = {a.file: a for a in pp.actions}
    assert by["scan.tif"].reason == "unsupported" and by["scan.tif"].state == "skipped"
    assert by["big.pdf"].reason == "unsupported"
    assert by["ok.jpeg"].state == "planned"


def test_plan_hash_stable_and_content_sensitive(tmp_path):
    a, _, _ = one(tmp_path / "a", {"d.pdf": b"a"})
    b, _, _ = one(tmp_path / "b", {"d.pdf": b"a"})
    c, _, _ = one(tmp_path / "c", {"d.pdf": b"changed"})
    d, _, _ = one(tmp_path / "d", {"d.pdf": b"a"}, reader=FakeReader(memories=["d"]))
    assert a.plan_hash == b.plan_hash != c.plan_hash
    assert d.plan_hash != a.plan_hash  # nothing planned


def test_reader_only_used_through_read_methods(tmp_path):
    _, _, reader = one(tmp_path, {"a.pdf": b"a"})
    assert {c[0] for c in reader.calls} <= {"person", "memories", "sources"}


# ---- likely-covered heuristic ----

def covered(tmp_path, fname, titles, force=frozenset()):
    reader = FakeReader(sources=[SourceRef(t, None) for t in titles])
    _, pp, _ = one(tmp_path, {fname: b"a"}, reader=reader, force_upload=force)
    return pp.actions[0], pp


def test_census_same_year_is_likely_covered(tmp_path):
    a, pp = covered(tmp_path, "Census 1900.pdf", ['"United States, Census, 1900"'])
    assert a.state == "likely-covered"
    assert a.reason == 'FamilySearch already has source: "United States, Census, 1900"'
    assert pp.status == "nothing-to-do"


def test_different_year_stays_planned(tmp_path):
    a, _ = covered(tmp_path, "Census 1910.pdf", ['"United States, Census, 1900"'])
    assert a.state == "planned"


def test_no_year_in_file_matches_on_kind(tmp_path):
    a, _ = covered(tmp_path, "World War I Draft Registration.pdf", ["World War I Draft Registration Cards"])
    assert a.state == "likely-covered"
    a, _ = covered(tmp_path / "b", "Passenger manifest.jpg", ["Passenger Lists, 1900"])
    assert a.state == "likely-covered"
    a, _ = covered(tmp_path / "c", "Household Examination Books.pdf", ["Household Examination Books, 1850"])
    assert a.state == "likely-covered"


def test_unreliable_kinds_stay_planned(tmp_path):
    a, _ = covered(tmp_path, "Photo 1900.jpg", ["Photo 1900 census"])
    assert a.state == "planned"
    a, _ = covered(tmp_path / "b", "Letter.pdf", ["Letter"])
    assert a.state == "planned"


def test_kind_must_match(tmp_path):
    a, _ = covered(tmp_path, "Birth 1900.pdf", ['"United States, Census, 1900"'])
    assert a.state == "planned"


def test_reason_title_truncated_to_80(tmp_path):
    a, _ = covered(tmp_path, "Census 1900.pdf", ["Census 1900 " + "x" * 200])
    assert a.reason == "FamilySearch already has source: " + ("Census 1900 " + "x" * 200)[:80]


def test_force_upload_returns_to_planned(tmp_path):
    a, pp = covered(tmp_path, "Census 1900.pdf", ["Census 1900"], force=frozenset({KEY + "/Census 1900.pdf"}))
    assert a.state == "planned" and pp.status == "planned"


def test_hash_covers_likely_covered(tmp_path):
    p1, _, _ = one(tmp_path / "a", {"Census 1900.pdf": b"a"}, reader=FakeReader(sources=[SourceRef("Census 1900", None)]))
    p2, _, _ = one(tmp_path / "b", {"Census 1900.pdf": b"b"}, reader=FakeReader(sources=[SourceRef("Census 1900", None)]))
    assert p1.plan_hash != p2.plan_hash
