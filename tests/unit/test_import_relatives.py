from pathlib import Path

from familysearch_import.linking import FsPerson
from familysearch_import.prod_reader import ReadFailed, Relative
from familysearch_import.relatives import resolve_through_relatives
from familysearch_import.scan import Person


def person(line, folder, name, birth, death):
    return Person(line, folder, Path("/nonexistent") / line / folder, birth, death, name, (), (), frozenset())


def fsp(id, name, b, d, living=False):
    return FsPerson(id, name, b, d, living, "ok")


class FakeReader:
    def __init__(self, tree, fail=()):
        self.tree = tree  # id -> list[Relative]
        self.fail = set(fail)
        self.calls = []

    def get_relatives(self, fs_id):
        self.calls.append(fs_id)
        if fs_id in self.fail:
            raise ReadFailed("boom")
        return self.tree.get(fs_id, [])


def status_of(overrides=None):
    overrides = overrides or {}
    return lambda p: (overrides.get(p.folder, "deceased"), "missing")


A = person("Line A", "Ann Example", "Ann Example", 1850, 1920)
B = person("Line B", "Bob Sample", "Bob Sample", 1880, 1950)
LINKS = {"Line A/Ann Example": "AAAA-111", "Line B/Bob Sample": "BBBB-222"}


def run(people, tree, links=LINKS, approved=None, **kw):
    reader = FakeReader(tree, kw.pop("fail", ()))
    rec = kw.pop("record_status", status_of())
    res = resolve_through_relatives(people, links, approved or {}, reader, record_status=rec, **kw)
    return res, reader


def test_child_found_with_evidence_and_ranking_confirmed_first():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    tree = {"AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, 1940), "child"),
                         Relative(fsp("CCCC-2", "Cal Example", 1876, None), "child")]}
    res, _ = run([A, t], {**tree, "AAAA-111": tree["AAAA-111"]}, links={"Line A/Ann Example": "AAAA-111"})
    (tr,) = res.targets
    assert [c.fs.id for c in tr.candidates] == ["CCCC-1", "CCCC-2"]
    assert [c.verdict for c in tr.candidates] == ["confirmed", "probable"]
    ev = tr.candidates[0].evidence[0]
    assert (ev.relationship, ev.via_fs_id, ev.via_pkey) == ("child", "AAAA-111", "Line A/Ann Example")
    assert not tr.ambiguous and res.reads == 3 and not res.partial


def test_two_hops_composed_relationship():
    t = person("Line A", "Gus Example", "Gus Example", 1900, 1970)
    tree = {
        "AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, 1940), "child")],
        "CCCC-1": [Relative(fsp("GGGG-1", "Gus Example", 1900, 1970), "child")],
    }
    res, reader = run([A, t], tree, links={"Line A/Ann Example": "AAAA-111"})
    ev = res.targets[0].candidates[0].evidence[0]
    assert (ev.relationship, ev.via_fs_id) == ("child-of-child", "CCCC-1")
    res1, _ = run([A, t], tree, links={"Line A/Ann Example": "AAAA-111"}, max_hops=1)
    assert res1.targets[0].candidates == ()


def test_same_person_via_several_paths_is_one_candidate():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    cal = fsp("CCCC-1", "Cal Example", 1875, 1940)
    tree = {"AAAA-111": [Relative(cal, "child")], "BBBB-222": [Relative(cal, "spouse")]}
    res, _ = run([A, B, t], tree)
    (c,) = res.targets[0].candidates
    assert {(e.relationship, e.via_pkey) for e in c.evidence} == {
        ("child", "Line A/Ann Example"), ("spouse", "Line B/Bob Sample")}


def test_conflicting_birth_year_is_not_a_candidate():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    tree = {"AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1890, 1940), "child")]}
    res, _ = run([A, t], tree, links={"Line A/Ann Example": "AAAA-111"})
    assert res.targets[0].candidates == ()


def test_living_relative_never_listed_or_expanded_only_counted():
    t = person("Line A", "Cal Example", "Cal Example", 1875, None)
    tree = {
        "AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, None, living=True), "child")],
        "CCCC-1": [Relative(fsp("DDDD-1", "Dee Example", 1900, 1980), "child")],
    }
    res, reader = run([A, t], tree, links={"Line A/Ann Example": "AAAA-111"})
    assert res.targets[0].candidates == ()
    assert res.living_skipped == 1
    assert "CCCC-1" not in reader.calls
    assert "Cal Example" not in repr(res.targets[0].candidates)


def test_living_archive_people_are_neither_start_nor_target():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    rec = status_of({"Ann Example": "living", "Cal Example": "living"})
    res, reader = run([A, t], {"AAAA-111": []}, links={"Line A/Ann Example": "AAAA-111"}, record_status=rec)
    assert res.targets == () and reader.calls == []


def test_taken_by_and_ambiguous_flags():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    other = person("Line A", "Cal Other", "Cal Example", 1875, 1940)
    tree = {"AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, 1940), "child"),
                         Relative(fsp("CCCC-2", "Cal Example", 1875, 1940), "child")]}
    links = {"Line A/Ann Example": "AAAA-111", "Line A/Cal Other": "CCCC-1"}
    res, _ = run([A, t, other], tree, links=links)
    tr = next(x for x in res.targets if x.pkey == "Line A/Cal Example")
    assert tr.ambiguous
    assert {c.fs.id: c.taken_by for c in tr.candidates} == {"CCCC-1": "Line A/Cal Other", "CCCC-2": None}


def test_same_line_relative_outranks_other_line():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    tree = {"BBBB-222": [Relative(fsp("CCCC-9", "Cal Example", 1875, 1940), "child")],
            "AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, 1940), "child")]}
    res, _ = run([A, B, t], tree)
    tr = res.targets[0]
    assert [c.fs.id for c in tr.candidates] == ["CCCC-1", "CCCC-9"]
    assert not tr.ambiguous


def test_read_cap_reports_partial_and_caches():
    tree = {"AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, 1940), "child")],
            "BBBB-222": []}
    t = person("Line A", "Zed Example", "Zed Example", 1900, 1960)
    res, reader = run([A, B, t], tree, max_reads=1)
    assert res.partial and res.reads == 1
    full, reader2 = run([A, B, t], tree)
    assert not full.partial and full.reads == 3 and len(reader2.calls) == 3  # 2 starts + 1 hop-2 node


def test_failed_read_marks_partial():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    res, _ = run([A, t], {}, links={"Line A/Ann Example": "AAAA-111"}, fail={"AAAA-111"})
    assert res.partial and res.failed_reads == 1


def test_new_approval_turns_target_into_start():
    t = person("Line A", "Cal Example", "Cal Example", 1875, 1940)
    tree = {"AAAA-111": [Relative(fsp("CCCC-1", "Cal Example", 1875, 1940), "child")],
            "CCCC-1": [Relative(fsp("GGGG-1", "Gus Example", 1900, 1970), "child")]}
    g = person("Line A", "Gus Example", "Gus Example", 1900, 1970)
    links = {"Line A/Ann Example": "AAAA-111"}
    before, _ = run([A, t, g], tree, links=links, max_hops=1)
    assert {x.pkey for x in before.targets} == {"Line A/Cal Example", "Line A/Gus Example"}
    assert before.targets[1].candidates == ()
    after, _ = run([A, t, g], tree, links=links, approved={"Line A/Cal Example": "CCCC-1"}, max_hops=1)
    assert [x.pkey for x in after.targets] == ["Line A/Gus Example"]
    assert after.targets[0].candidates[0].fs.id == "GGGG-1"
