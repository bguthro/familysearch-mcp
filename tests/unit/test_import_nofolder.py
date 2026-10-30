import json

import pytest

from familysearch_import import __main__ as cli
from familysearch_import.linking import FsPerson
from familysearch_import.nofolder import find_nofolder_matches
from familysearch_import.nofolderreport import nofolder_json, render_nofolder_report
from familysearch_import.prod_reader import ReadFailed, Relative


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


def edge(kind, a, b, a_fs=None, b_fs=None, note="", status="blocked"):
    return {"kind": kind, "a": a, "b": b, "a_fs": a_fs, "b_fs": b_fs, "status": status, "note": note,
            "a_folder": None, "b_folder": None}


def rels(edges, cands=()):
    return {"edges": list(edges), "create_candidates": [
        {"label": n, "birth": b, "death": d, "reason": r, "lines": ["Line A"], "related_to": []}
        for n, b, d, r in cands]}


EVE = ("Eve Sample", 1802, 1880, "no folder")
# Eve is the parent of linked Cal (CCCC-1) and spouse of linked Adam (AAAA-1)
EDGES = [edge("parent-child", "Eve Sample", "Cal Example", None, "CCCC-1", "blocked: parent has no folder"),
         edge("spouse", "Adam Example", "Eve Sample", "AAAA-1", None, "blocked: second spouse has no folder")]


def test_confirmed_by_name_years_and_role_with_both_edges_as_evidence():
    tree = {"CCCC-1": [Relative(fsp("EEEE-1", "Eve Sample", 1802, 1880), "parent"),
                       Relative(fsp("XXXX-1", "Xavier Other", 1800, 1870), "parent")],
            "AAAA-1": [Relative(fsp("EEEE-1", "Eve Sample", 1802, 1880), "spouse")]}
    res = find_nofolder_matches(rels(EDGES, [EVE]), FakeReader(tree))
    (t,) = res.targets
    assert t.verdict == "confirmed" and [m.fs.id for m in t.matches] == ["EEEE-1"]
    assert {(e.role, e.via_fs_id) for e in t.matches[0].evidence} == {("parent", "CCCC-1"), ("spouse", "AAAA-1")}
    assert t.linked_edges == 2 and not res.partial


def test_role_must_be_consistent_with_the_edge():
    # Eve is a parent of Cal; a FamilySearch *child* of Cal named Eve Sample must not match
    tree = {"CCCC-1": [Relative(fsp("EEEE-9", "Eve Sample", 1802, 1880), "child")]}
    res = find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader(tree))
    assert res.targets[0].verdict == "not-found"


def test_child_role_for_the_other_direction():
    edges = [edge("parent-child", "Adam Example", "Cal Example", "AAAA-1", None, "blocked: child has no folder")]
    tree = {"AAAA-1": [Relative(fsp("CCCC-1", "Cal Example", 1825, 1890), "child")]}
    res = find_nofolder_matches(rels(edges, [("Cal Example", 1825, 1890, "no folder")]), FakeReader(tree))
    assert res.targets[0].verdict == "confirmed"


def test_probable_when_years_differ_slightly_or_name_is_fuzzy():
    tree = {"CCCC-1": [Relative(fsp("EEEE-1", "Eve Sample", 1803, 1880), "parent")]}
    res = find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader(tree))
    assert res.targets[0].verdict == "probable"


def test_ambiguous_with_several_and_not_found_with_none():
    tree = {"CCCC-1": [Relative(fsp("EEEE-1", "Eve Sample", 1802, 1880), "parent"),
                       Relative(fsp("EEEE-2", "Eve Sample", 1802, 1881), "parent")]}
    res = find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader(tree))
    assert res.targets[0].verdict == "ambiguous" and len(res.targets[0].matches) == 2
    res = find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader({}))
    assert res.targets[0].verdict == "not-found"


def test_birth_year_far_off_is_not_a_match():
    tree = {"CCCC-1": [Relative(fsp("EEEE-1", "Eve Sample", 1850, None), "parent")]}
    assert find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader(tree)).targets[0].verdict == "not-found"


def test_living_relatives_are_counted_never_matched():
    tree = {"CCCC-1": [Relative(fsp("LIVE-1", "Eve Sample", 1802, 1880, living=True), "parent"),
                       Relative(fsp("LIVE-2", "Eve Sample", 1802, 1880, living=None), "parent")]}
    res = find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader(tree))
    assert res.targets[0].verdict == "not-found" and res.living_skipped == 2


def test_only_no_folder_candidates_and_edge_only_nodes_included():
    cands = [EVE, ("Folder Haver", 1800, 1850, "folder has no link")]
    edges = EDGES + [edge("spouse", "Adam Example", "Dot Nodates", "AAAA-1", None, "blocked: second spouse has no folder"),
                     edge("spouse", "Adam Example", "(living)", None, None, "an end is living", "skipped-living")]
    res = find_nofolder_matches(rels(edges, cands), FakeReader())
    assert [t.label for t in res.targets] == ["Dot Nodates", "Eve Sample"]
    assert res.targets[0].birth is None


def test_reads_are_cached_and_budgeted_and_failures_are_partial():
    reader = FakeReader()
    find_nofolder_matches(rels(EDGES, [EVE]), reader)
    assert sorted(reader.calls) == ["AAAA-1", "CCCC-1"]
    res = find_nofolder_matches(rels(EDGES, [EVE]), FakeReader(), max_reads=0)
    assert res.partial and res.reads == 0
    res = find_nofolder_matches(rels(EDGES, [EVE]), FakeReader(fail={"CCCC-1"}))
    assert res.partial and res.failed_reads == 1


def test_candidate_without_linked_edges_is_not_found_and_approval_is_echoed():
    res = find_nofolder_matches(rels([], [EVE]), FakeReader(), {"Eve Sample": "EEEE-1"})
    t = res.targets[0]
    assert t.verdict == "not-found" and t.linked_edges == 0 and t.approved_id == "EEEE-1"


def test_report_and_json_never_name_living():
    tree = {"CCCC-1": [Relative(fsp("EEEE-1", "Eve Sample", 1802, 1880), "parent"),
                       Relative(fsp("LIVE-1", "Liv Secret", 1980, None, living=True), "parent")]}
    res = find_nofolder_matches(rels(EDGES[:1], [EVE]), FakeReader(tree))
    text = render_nofolder_report(res)
    assert '"Eve Sample": "EEEE-1"' in text and "approved_nofolder" in text
    assert "Liv Secret" not in text + json.dumps(nofolder_json(res))
    assert "living relatives skipped (counted only, never listed): 1" in text
    assert nofolder_json(res)["targets"][0]["matches"][0]["id"] == "EEEE-1"


# ---- CLI


def make_archive(tmp_path, env="production", approvals=None):
    root = tmp_path / "archive"
    imp = root / "_import"
    imp.mkdir(parents=True)
    data = rels(EDGES, [EVE])
    data["env"] = env
    (imp / "relationships.json").write_text(json.dumps(data))
    if approvals is not None:
        (imp / "approvals.json").write_text(json.dumps(approvals))
    return root


@pytest.fixture
def fake_reader(monkeypatch):
    reader = FakeReader({"CCCC-1": [Relative(fsp("EEEE-1", "Eve Sample", 1802, 1880), "parent")]})
    monkeypatch.setattr(cli, "_make_reader", lambda env: reader)
    return reader


def test_cli_writes_report_and_json(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["nofolder", "--archive", str(root), "--env", "production"]) == 0
    assert "EEEE-1" in (root / "_import" / "NoFolderReport.md").read_text()
    data = json.loads((root / "_import" / "nofolder.json").read_text())
    assert data["targets"][0]["verdict"] == "confirmed"


def test_cli_guards(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["nofolder", "--archive", str(root), "--env", "beta"]) == 2  # env mismatch
    assert cli.main(["nofolder", "--archive", str(cli.repo_root()), "--env", "production"]) == 2
    (root / "_import" / "relationships.json").unlink()
    assert cli.main(["nofolder", "--archive", str(root), "--env", "production"]) == 2
    assert not (root / "_import" / "NoFolderReport.md").exists()
    with pytest.raises(SystemExit):
        cli.main(["nofolder", "--archive", str(root)])


def test_cli_rejects_bad_approved_nofolder(tmp_path, fake_reader):
    root = make_archive(tmp_path, approvals={"approved_links": {}, "approved_nofolder": {"Eve Sample": ""}})
    assert cli.main(["nofolder", "--archive", str(root), "--env", "production"]) == 2
