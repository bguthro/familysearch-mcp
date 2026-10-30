from pathlib import Path

from familysearch_import.linking import FsPerson
from familysearch_import.prod_reader import ReadFailed, Relative
from familysearch_import.relgap import evaluate_edges, map_nodes
from familysearch_import.scan import Person
from familysearch_import.treeparse import parse_family_tree


def person(line, name, birth, death, folder=None):
    folder = folder or name
    return Person(line, folder, Path("/nonexistent") / line / folder, birth, death, name, (), (), frozenset())


def fsp(id, name, b=None, d=None, living=False):
    return FsPerson(id, name, b, d, living, "ok")


class FakeReader:
    def __init__(self, tree=None, fail=()):
        self.tree = tree or {}
        self.fail = set(fail)
        self.calls = []

    def get_relatives(self, fs_id):
        self.calls.append(fs_id)
        if fs_id in self.fail:
            raise ReadFailed("boom")
        return self.tree.get(fs_id, [])


def diagram(body):
    return "## Tree diagram\n```\n" + body + "\n```\n"


ADAM = person("Line A", "Adam Example", 1800, 1860)
EVE = person("Line A", "Eve Sample", 1802, 1880)
CAL = person("Line A", "Cal Example", 1825, 1890)
FAMILY = diagram("Adam Example (1800–1860) m. 1822, Eve Sample (1802–1880)\n └─ Cal Example (1825–1890)")
LINKS = {"Line A/Adam Example": "AAAA-1", "Line A/Eve Sample": "EEEE-1", "Line A/Cal Example": "CCCC-1"}


def run(body, people, links, tree=None, approved=None, line="Line A", **kw):
    parsed = {line: parse_family_tree(line, body)}
    reader = FakeReader(tree, kw.pop("fail", ()))
    return evaluate_edges(parsed, people, links, approved or {}, reader, **kw), reader


def by_status(res, status):
    return [e for e in res.edges if e.status == status]


def pair(e):
    return (e.a_label, e.b_label)


# ---- mapping


def test_mapping_same_line_first_then_other_lines_ambiguous_and_unmapped():
    other_cal = person("Line B", "Cal Example", 1825, 1890)
    people = [CAL, other_cal, ADAM]
    parsed = {
        "Line A": parse_family_tree("Line A", diagram("Cal Example (1825–1890)\n └─ Zed Nobody (1850–1900)")),
        "Line C": parse_family_tree("Line C", diagram("Adam Example (1800–1860)\n └─ Cal Example (1825–1890)")),
    }
    m = map_nodes(parsed, people)
    assert m[("Line A", 0)].person is CAL and m[("Line A", 0)].status == "mapped"
    assert m[("Line A", 1)].status == "unmapped"
    assert m[("Line C", 0)].person is ADAM  # found in another line
    assert m[("Line C", 1)].status == "ambiguous" and m[("Line C", 1)].person is None


def test_two_folders_in_the_same_line_are_ambiguous_and_dateless_nodes_unmapped():
    twin = person("Line A", "Cal Example", 1826, 1890, folder="Cal Example Jr")
    parsed = {"Line A": parse_family_tree("Line A", diagram("Cal Example (1825–1890)\n └─ Nodate Person"))}
    m = map_nodes(parsed, [CAL, twin])
    assert m[("Line A", 0)].status == "ambiguous"
    assert m[("Line A", 1)].status == "unmapped"


def test_living_rule_and_folder_override():
    kid = person("Line A", "Kay Example", 2005, None)
    parsed = {"Line A": parse_family_tree("Line A", diagram("Cal Example (1825–1890)\n └─ Kay Example (b.2005)"))}
    m = map_nodes(parsed, [CAL, kid])
    assert m[("Line A", 1)].living and not m[("Line A", 0)].living
    m2 = map_nodes(parsed, [CAL, kid], record_status=lambda p: ("deceased", "reviewed"))
    assert not m2[("Line A", 1)].living


# ---- parent-child


def test_present_when_child_has_the_parents():
    tree = {"CCCC-1": [Relative(fsp("AAAA-1", "Adam Example"), "parent"),
                       Relative(fsp("EEEE-1", "Eve Sample"), "parent")]}
    res, _ = run(FAMILY, [ADAM, EVE, CAL], LINKS, tree)
    pcs = [e for e in res.edges if e.kind == "parent-child"]
    assert {e.status for e in pcs} == {"present"} and len(pcs) == 2


def test_missing_when_child_has_no_parents_and_spouse_missing():
    res, _ = run(FAMILY, [ADAM, EVE, CAL], LINKS, {})
    assert {pair(e) for e in by_status(res, "missing-in-familysearch")} == {
        ("Adam Example", "Cal Example"), ("Eve Sample", "Cal Example"), ("Adam Example", "Eve Sample")}


def test_spouse_present_when_fs_lists_each_other():
    tree = {"AAAA-1": [Relative(fsp("EEEE-1", "Eve Sample"), "spouse")]}
    res, _ = run(FAMILY, [ADAM, EVE, CAL], LINKS, tree)
    sp = [e for e in res.edges if e.kind == "spouse"][0]
    assert sp.status == "present" and sp.a_fs == "AAAA-1" and sp.b_fs == "EEEE-1"


def test_conflict_when_familysearch_has_other_parents_and_is_never_proposed():
    tree = {"CCCC-1": [Relative(fsp("XXXX-1", "Xavier Other"), "parent"),
                       Relative(fsp("YYYY-1", "Private Person", living=None), "parent")]}
    res, _ = run(FAMILY, [ADAM, EVE, CAL], LINKS, tree)
    conflicts = by_status(res, "conflict")
    assert {pair(e) for e in conflicts} == {("Adam Example", "Cal Example"), ("Eve Sample", "Cal Example")}
    found = " ".join(conflicts[0].fs_found)
    assert "Xavier Other (XXXX-1)" in found and "(living) (YYYY-1)" in found
    assert "Private Person" not in found
    assert not any(e.status == "missing-in-familysearch" and e.kind == "parent-child" for e in res.edges)


def test_one_fs_parent_plus_archive_adding_the_other_is_missing_but_partial():
    tree = {"CCCC-1": [Relative(fsp("AAAA-1", "Adam Example"), "parent")]}
    res, _ = run(FAMILY, [ADAM, EVE, CAL], LINKS, tree)
    eve = [e for e in res.edges if pair(e) == ("Eve Sample", "Cal Example")][0]
    assert eve.status == "missing-in-familysearch" and "partial-parents" in eve.note
    adam = [e for e in res.edges if pair(e) == ("Adam Example", "Cal Example")][0]
    assert adam.status == "present"


def test_single_archive_parent_differing_from_the_one_fs_parent_is_a_conflict():
    body = diagram("Adam Example (1800–1860)\n └─ Cal Example (1825–1890)")
    tree = {"CCCC-1": [Relative(fsp("XXXX-1", "Xavier Other"), "parent")]}
    res, _ = run(body, [ADAM, CAL], LINKS, tree)
    assert [e.status for e in res.edges] == ["conflict"]


def test_blocked_without_link_or_folder():
    links = {"Line A/Adam Example": "AAAA-1", "Line A/Cal Example": None}
    res, _ = run(FAMILY, [ADAM, CAL], links, {})  # Eve has no folder
    notes = {pair(e): e.note for e in by_status(res, "blocked")}
    assert "child has no link" in notes[("Adam Example", "Cal Example")]
    assert "parent has no folder" in notes[("Eve Sample", "Cal Example")]
    assert "second spouse has no folder" in notes[("Adam Example", "Eve Sample")]


def test_approved_link_counts_and_is_reported():
    links = {"Line A/Adam Example": "AAAA-1", "Line A/Cal Example": None}
    res, _ = run(FAMILY, [ADAM, CAL], links, {}, approved={"Line A/Cal Example": "CCCC-1"})
    e = [e for e in res.edges if pair(e) == ("Adam Example", "Cal Example")][0]
    assert e.status == "missing-in-familysearch" and e.approved_ends == ("Cal Example",)


def test_living_edges_skipped_and_names_never_appear():
    kid = person("Line A", "Livia Livingston", 2005, None)
    body = diagram("Cal Example (1825–1890)\n └─ Livia Livingston (b.2005)")
    res, reader = run(body, [CAL, kid], {"Line A/Cal Example": "CCCC-1", "Line A/Livia Livingston": "LLLL-1"}, {})
    assert res.living_skipped == 1 and res.living_nodes == 1
    assert [e.status for e in res.edges] == ["skipped-living"]
    assert reader.calls == []
    blob = repr(res)
    assert "Livia" not in blob and "Livingston" not in blob
    assert all("Livia" not in c.label for c in res.create_candidates)


def test_same_edge_in_two_lines_is_one_result_with_both_lines():
    parsed = {"Line A": parse_family_tree("Line A", FAMILY), "Line B": parse_family_tree("Line B", FAMILY)}
    res = evaluate_edges(parsed, [ADAM, EVE, CAL], LINKS, {}, FakeReader())
    assert len(res.edges) == 3
    assert all(e.lines == ("Line A", "Line B") for e in res.edges)


def test_second_marriage_children_attach_only_to_that_pair():
    una = person("Line A", "Una First", 1830, 1900)
    bess = person("Line A", "Bess Second", 1919, 1993)
    wayne = person("Line A", "Wayne Example", 1852, 1930)
    body = diagram('Cal Example (1825–1890) m. 1850, Dee Other (1828–1900)\n'
                   ' ├─ m1. 1845, Una First (1830–1900)\n'
                   ' └─ m2. ~1948, Bess Second (1919–1993)\n'
                   '    └─ Wayne Example (1852–1930)')
    links = {f"Line A/{p.folder}": f"{p.folder[:4].upper()}-1" for p in (CAL, una, bess, wayne)}
    res, _ = run(body, [CAL, una, bess, wayne], links, {})
    pcs = {pair(e) for e in res.edges if e.kind == "parent-child"}
    assert pcs == {("Cal Example", "Wayne Example"), ("Bess Second", "Wayne Example")}


def test_child_under_person_with_two_marriages_has_only_that_parent():
    una = person("Line A", "Una First", 1830, 1900)
    eli = person("Line A", "Eli Example", 1855, 1920)
    body = diagram('Cal Example (1825–1890) m. 1850, Dee Other (1828–1900)\n'
                   ' ├─ m2. 1880, Una First (1830–1900)\n'
                   ' └─ Eli Example (1855–1920)')
    links = {"Line A/Cal Example": "CCCC-1", "Line A/Eli Example": "ELIE-1", "Line A/Una First": "UNAF-1"}
    res, _ = run(body, [CAL, una, eli], links, {})
    pcs = [e for e in res.edges if e.kind == "parent-child"]
    assert [pair(e) for e in pcs] == [("Cal Example", "Eli Example")]
    assert "ambiguous" in pcs[0].note


# ---- budget, failures, candidates


def test_budget_makes_result_partial_and_unread_edges_blocked():
    body = diagram("Adam Example (1800–1860)\n ├─ Cal Example (1825–1890)\n └─ Dan Example (1827–1900)")
    dan = person("Line A", "Dan Example", 1827, 1900)
    links = {**LINKS, "Line A/Dan Example": "DDDD-1"}
    res, reader = run(body, [ADAM, CAL, dan], links, {}, max_reads=1)
    assert res.partial and res.reads == 1 and len(reader.calls) == 1
    assert [e.status for e in res.edges].count("blocked") == 1
    assert "budget" in by_status(res, "blocked")[0].note


def test_read_failure_is_blocked_and_partial():
    res, _ = run(FAMILY, [ADAM, EVE, CAL], LINKS, {}, fail={"CCCC-1"})
    assert res.partial and res.failed_reads == 1
    assert all("read failed" in e.note for e in res.edges if e.kind == "parent-child")


def test_each_fs_person_read_once():
    res, reader = run(FAMILY, [ADAM, EVE, CAL], LINKS, {})
    assert sorted(reader.calls) == sorted(set(reader.calls))


def test_creation_candidates_and_unmapped_report():
    res, _ = run(FAMILY, [ADAM, CAL], {"Line A/Adam Example": "AAAA-1", "Line A/Cal Example": "CCCC-1"}, {})
    eve = [c for c in res.create_candidates if c.label == "Eve Sample"]
    assert len(eve) == 1 and eve[0].reason == "no folder" and eve[0].lines == ("Line A",)
    assert "Adam Example" in eve[0].related_to
    assert [u.label for u in res.unmapped_nodes] == ["Eve Sample"]


def test_unparsed_lines_are_carried():
    body = diagram("Adam Example (1800–1860)\n └─ ((( garbled (((")
    res, _ = run(body, [ADAM], {}, {})
    assert res.unparsed_lines and res.unparsed_lines[0][0] == "Line A"
