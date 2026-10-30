import json

import pytest

from familysearch_import import __main__ as cli
from familysearch_import.relgap import CreateCandidate, EdgeResult, RelGapResult, UnmappedNode
from familysearch_import.relgapreport import edge_key, relgap_json, render_relgap_report


def edge(status, a="Adam Example", b="Cal Example", kind="parent-child", fa="AAAA-1", fb="CCCC-1", note="",
         found=(), approved=()):
    return EdgeResult(kind, a, b, fa, fb, status, note, ("Line A",), approved, found)


def sample(partial=False):
    return RelGapResult(
        edges=(
            edge("conflict", note="FamilySearch wins", found=("Xavier Other (XXXX-1)", "(living) (YYYY-1)")),
            edge("missing-in-familysearch", a="Eve Sample", fa="EEEE-1", note="partial-parents: only other parent",
                 approved=("Cal Example",)),
            edge("missing-in-familysearch", a="Adam Example", b="Eve Sample", kind="spouse", fa="AAAA-1", fb="EEEE-1"),
            edge("blocked", a="Dan Example", b="Cal Example", fa=None, fb="CCCC-1", note="blocked: parent has no folder"),
            edge("present", a="Adam Example", b="Dan Example", fb="DDDD-1"),
            edge("skipped-living", a="Cal Example", b="(living)", fb=None),
        ),
        create_candidates=(CreateCandidate("Dan Example", 1827, 1900, "no folder", ("Line A",), ("Cal Example",)),),
        unmapped_nodes=(UnmappedNode("Dan Example", "unmapped", ("Line A",)),),
        reads=9, partial=partial, living_skipped=1,
        unparsed_lines=(("Line A", "((( garbled ((("), ("Line A", "Kay Livingston (b.2005) junk (((")),
        living_nodes=2, failed_reads=1 if partial else 0)


def test_edge_key_is_stable_and_spouse_keys_are_ordered():
    assert edge_key(edge("missing-in-familysearch")) == "parent-child:AAAA-1:CCCC-1"
    e = edge("missing-in-familysearch", kind="spouse", fa="EEEE-1", fb="AAAA-1")
    assert edge_key(e) == "spouse:AAAA-1:EEEE-1"


def test_render_totals_sections_and_approval_lines():
    text = render_relgap_report(sample())
    assert "- conflict: 1" in text and "- missing-in-familysearch: 2" in text and "- present: 1" in text
    assert "Adam Example -> Cal Example" in text
    assert "FamilySearch has: Xavier Other (XXXX-1), (living) (YYYY-1)" in text
    assert '"approved_relationships"' in text
    assert '"parent-child:EEEE-1:CCCC-1"' in text and '"spouse:AAAA-1:EEEE-1"' in text
    assert '"parent-child:AAAA-1:CCCC-1"' not in text.split("Approval lines")[1]  # conflicts never offered
    assert "link approved in approvals.json: Cal Example" in text
    assert "## Creation candidates (1)" in text and "Dan Example" in text and "no folder" in text
    assert "## Unmapped nodes (1)" in text
    assert "living people skipped (counted only, never listed): edges 1, nodes 2" in text
    assert "PARTIAL" not in text


def test_unparsed_lines_hide_text_that_may_name_a_living_person():
    text = render_relgap_report(sample())
    assert "garbled" in text
    assert "Livingston" not in text and "withheld" in text


def test_partial_warning_and_json():
    assert "PARTIAL RESULT" in render_relgap_report(sample(partial=True))
    data = relgap_json(sample(partial=True))
    assert data["partial"] is True and data["reads"] == 9 and data["living_skipped"] == 1
    assert data["edges"][0]["status"] == "conflict" and data["edges"][0]["fs_found"][0].startswith("Xavier")
    assert data["edges"][1]["key"] == "parent-child:EEEE-1:CCCC-1"
    assert data["create_candidates"][0]["label"] == "Dan Example"
    assert "Livingston" not in json.dumps(data)


# ---- CLI ----

TREE = """# Line

## Tree diagram

```
Pat Example (1844–1915) m. 1870, Sue Sample (1846–1920)
 └─ Cal Example (1875–1940)
       └─ Liv Secret (b.1980)
```
"""


def make_archive(tmp_path):
    root = tmp_path / "arch"
    line = root / "Example Line"
    for name in ("1844-1915 Pat Example", "1875-1940 Cal Example", "1980 Liv Secret"):
        d = line / name
        d.mkdir(parents=True)
        (d / "ResearchSummary.md").write_text("notes")
    (line / "FamilyTree.md").write_text(TREE)
    imp = root / "_import"
    imp.mkdir()
    (imp / "links.json").write_text(json.dumps({"people": [
        {"line": "Example Line", "folder": "1844-1915 Pat Example", "verdict": "confirmed", "chosen_id": "AAAA-111"},
        {"line": "Example Line", "folder": "1875-1940 Cal Example", "verdict": "confirmed", "chosen_id": "CCCC-111"},
        {"line": "Example Line", "folder": "1980 Liv Secret", "verdict": "confirmed", "chosen_id": "LIVE-999"},
    ]}))
    return root


class FakeReader:
    def __init__(self):
        self.calls = []

    def get_relatives(self, fs_id):
        self.calls.append(fs_id)
        return []


@pytest.fixture
def fake_reader(monkeypatch):
    reader = FakeReader()
    monkeypatch.setattr(cli, "_make_reader", lambda env: reader)
    return reader


def test_cli_writes_report_and_json_without_living_names(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["relationships", "--archive", str(root), "--env", "production"]) == 0
    text = (root / "_import" / "RelationshipReport.md").read_text()
    data = json.loads((root / "_import" / "relationships.json").read_text())
    assert "Pat Example -> Cal Example" in text and "Liv Secret" not in text
    assert "Liv Secret" not in json.dumps(data) and data["living_skipped"] == 1
    assert "parent-child:AAAA-111:CCCC-111" in text
    assert fake_reader.calls == ["CCCC-111"]


def test_cli_max_reads_partial(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["relationships", "--archive", str(root), "--env", "production", "--max-reads", "0"]) == 0
    assert "PARTIAL RESULT" in (root / "_import" / "RelationshipReport.md").read_text()
    assert fake_reader.calls == []


def test_cli_requires_links_and_repo_guard_and_env(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["relationships", "--archive", str(root)])
    assert cli.main(["relationships", "--archive", str(cli.repo_root()), "--env", "production"]) == 2
    (root / "_import" / "links.json").unlink()
    assert cli.main(["relationships", "--archive", str(root), "--env", "production"]) == 2
    assert not (root / "_import" / "RelationshipReport.md").exists()


def test_cli_missing_family_tree_files_are_noted(tmp_path, fake_reader, capsys):
    root = make_archive(tmp_path)
    (root / "Example Line" / "FamilyTree.md").unlink()
    assert cli.main(["relationships", "--archive", str(root), "--env", "production"]) == 0
    assert "no FamilyTree.md" in capsys.readouterr().out


def test_cli_fs_error_returns_2(tmp_path, monkeypatch):
    from familysearch.errors import FSError

    def boom(env):
        raise FSError("not signed in")

    monkeypatch.setattr(cli, "_make_reader", boom)
    root = make_archive(tmp_path)
    assert cli.main(["relationships", "--archive", str(root), "--env", "production"]) == 2
    assert not (root / "_import" / "RelationshipReport.md").exists()


# ---- reviewed relations files ----

def rel_doc(reviewed=True, nodes=None):
    return {"version": 1, "line": "Example Line", "reviewed": reviewed,
            "nodes": nodes or [
                {"id": "p", "name": "Pat E.", "birth": 1844, "death": 1915, "status": "deceased",
                 "folder": "Example Line/1844-1915 Pat Example", "source": "Pat Example (1844-1915)"},
                {"id": "c", "name": "Cal E.", "birth": 1875, "death": 1940, "status": "deceased",
                 "folder": "Example Line/1875-1940 Cal Example", "source": "Cal Example (1875-1940)"}],
            "edges": [{"kind": "parent-child", "a": "p", "b": "c", "order": None, "marriage_year": None}]}


def write_rel(root, doc):
    d = root / "_import" / "relations"
    d.mkdir(exist_ok=True)
    (d / "Example Line.json").write_text(json.dumps(doc))


def run_rel(root):
    assert cli.main(["relationships", "--archive", str(root), "--env", "production"]) == 0
    return (root / "_import" / "RelationshipReport.md").read_text(), json.loads(
        (root / "_import" / "relationships.json").read_text())


def test_report_lists_sources():
    from dataclasses import replace
    r = replace(sample(), sources=(("Line A", "reviewed relations file"), ("Line B", "awaiting review"),
                                   ("Line C", "regex parser (fallback)")))
    text = render_relgap_report(r)
    assert "- Line A: reviewed relations file" in text and "- Line C: regex parser (fallback)" in text
    assert "Awaiting review (the regex parser was used meanwhile): Line B" in text
    assert relgap_json(r)["sources"]["Line B"] == "awaiting review"


def test_cli_uses_reviewed_relations_file(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    write_rel(root, rel_doc())
    text, data = run_rel(root)
    assert "- Example Line: reviewed relations file" in text and data["sources"] == {
        "Example Line": "reviewed relations file"}
    assert "Pat E. -> Cal E." in text and "Sue Sample" not in text  # names come from the file, not the diagram


def test_cli_falls_back_without_file_and_waits_for_unreviewed(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    text, _ = run_rel(root)
    assert "- Example Line: regex parser (fallback)" in text
    write_rel(root, rel_doc(reviewed=False))
    text, data = run_rel(root)
    assert data["sources"] == {"Example Line": "awaiting review"}
    assert "Pat Example -> Cal Example" in text  # the regex parser was used meanwhile


def test_cli_invalid_relations_file_names_the_file(tmp_path, fake_reader, capsys):
    root = make_archive(tmp_path)
    bad = rel_doc()
    bad["nodes"][0]["folder"] = "Example Line/Nobody"
    write_rel(root, bad)
    assert cli.main(["relationships", "--archive", str(root), "--env", "production"]) == 2
    err = capsys.readouterr().err
    assert "Example Line.json" in err and "nodes[0]" in err and "folder" in err
    assert not (root / "_import" / "RelationshipReport.md").exists()


def test_relations_check_validates_and_cross_checks(tmp_path, capsys):
    root = make_archive(tmp_path)
    write_rel(root, rel_doc())
    assert cli.main(["relations-check", "--archive", str(root)]) == 0
    out = capsys.readouterr().out
    assert "ok: Example Line.json: 2 nodes, 1 edges, reviewed" in out
    assert "deceased Sue Sample (1846-1920) that the file lacks" in out
    assert "Liv Secret" not in out
    bad = rel_doc(reviewed=False)
    bad["edges"][0]["b"] = "zz"
    write_rel(root, bad)
    assert cli.main(["relations-check", "--archive", str(root)]) == 2
    assert "edges[0]: b does not reference" in capsys.readouterr().out


def rn(id, name, status="deceased", birth=None, death=None, folder=None, **kw):
    d = {"id": id, "name": name, "birth": birth, "death": death, "status": status, "folder": folder,
         "source": "s"}
    d.update(kw)
    return d


def re_(kind, a, b, order=None, year=None):
    return {"kind": kind, "a": a, "b": b, "order": order, "marriage_year": year}


REVIEW_DOC = {
    "version": 1, "line": "Example Line", "reviewed": False,
    "nodes": [
        rn("adam", "Adam Example", birth=1800, death=1860, folder="Example Line/1844-1915 Pat Example"),
        rn("eve", "Eve Sample", birth=1802, death=1880),
        rn("una", "Una Second", birth=1810, death=1890),
        rn("cal", "Cal Example", birth=1825, death=1890),
        rn("dan", "Dan Example", birth=1827, death=1900),
        rn("kay", "Kay Example", birth=1840, death=1900),
        rn("fay", "Fay Solo", birth=1850, death=1920),
        rn("moe", "Moe Modern", birth=1950, death=None, deceased_note="obit"),
        rn("tri", "Tri Parent", birth=1860, death=1930),
        rn("liv", "Secretname Livingston", status="living", birth=2005),
        rn("unk", "Mystery Hidden", status="unknown"),
        rn("odd", "Odd Loner", birth=1700, death=1750),
        rn("liv2", "Livspouse Hidden", status="living"),
    ],
    "edges": [
        re_("spouse", "adam", "eve", 1, 1822), re_("spouse", "adam", "una"),
        re_("parent-child", "adam", "cal"), re_("parent-child", "eve", "cal"),
        re_("parent-child", "adam", "dan"), re_("parent-child", "eve", "dan"),
        re_("parent-child", "adam", "kay"), re_("parent-child", "una", "kay"),
        re_("parent-child", "cal", "fay"),
        re_("parent-child", "cal", "liv"), re_("parent-child", "cal", "tri"),
        re_("parent-child", "dan", "tri"), re_("parent-child", "fay", "tri"),
        re_("spouse", "cal", "liv2"), re_("parent-child", "unk", "moe"),
        re_("parent-child", "adam", "moe"),
    ],
}


def review_text():
    from familysearch_import.relations import load_relations
    from familysearch_import.relationsreview import render_review
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "Example Line.json"
    p.write_text(json.dumps(REVIEW_DOC))
    rel = load_relations(p, {"Example Line/1844-1915 Pat Example"})
    return render_review([("Example Line", rel)], {"Example Line/1844-1915 Pat Example": (1844, 1915)})


def test_review_never_names_living_or_unknown_people():
    text = review_text()
    for secret in ("Secretname", "Livingston", "Mystery", "Hidden", "Livspouse"):
        assert secret not in text
    assert "(living person)" in text and "(unknown-status person)" in text
    assert "reviewed: NO" in text and "people: 13 (deceased 10," in text


def test_review_families_children_and_marriage_labels():
    text = review_text()
    assert ("- Adam Example (1800-1860) + Eve Sample (1802-1880) (m. 1822, 1st marriage) -> children: "
            "Cal Example (1825-1890), Dan Example (1827-1900)") in text
    assert "Adam Example (1800-1860) + Una Second (1810-1890) -> children: Kay Example (1840-1900)" in text
    assert "Fay Solo (1850-1920) (only one parent shown: Cal Example (1825-1890))" in text
    assert "| Adam Example | 1800-1860 | deceased | Example Line/1844-1915 Pat Example |" in text
    assert "| Eve Sample | 1802-1880 | deceased | no folder |" in text


def test_review_not_in_folders_and_checklist():
    text = review_text()
    assert "### Not in your archive folders" in text.split("### Review checklist")[0]
    assert "- Odd Loner (1700-1750)" in text
    check = text.split("### Review checklist")[1]
    assert "Adam Example: years differ from the folder name (birth 1800 vs folder 1844" in check
    assert "Moe Modern: marked deceased but born 1950 (obit)" in check
    assert "1 (unknown-status person) node(s) have status unknown" in check
    assert "Tri Parent (1860-1930): has 3 parents" in check
    assert "Adam Example (1800-1860): 2 spouses but some marriages have no order" in check
    assert "Odd Loner: has no relationships" in check
    assert "living or unknown-status node(s) have no relationships" not in check


def test_review_checklist_is_quiet_for_dated_deaths_and_approximate_years():
    import tempfile
    from pathlib import Path
    from familysearch_import.relations import load_relations
    from familysearch_import.relationsreview import render_review
    folders = {"Example Line/A": (1928, 2013), "Example Line/B": (1850, 1920), "Example Line/C": (1850, 1920)}
    doc = {"version": 1, "line": "Example Line", "reviewed": False, "edges": [],
           "nodes": [rn("a", "Alma Recent", birth=1928, death=2013, folder="Example Line/A"),
                     rn("b", "Bo Approx", birth=1851, death=1920, folder="Example Line/B", source="Bo (c.1851-1920)"),
                     rn("c", "Cy Exact", birth=1851, death=1920, folder="Example Line/C", source="Cy (1851-1920)")]}
    p = Path(tempfile.mkdtemp()) / "Example Line.json"
    p.write_text(json.dumps(doc))
    check = render_review([("Example Line", load_relations(p, set(folders)))], folders).split("### Review checklist")[1]
    assert "Alma Recent: marked deceased" not in check and "Bo Approx: years differ" not in check
    assert "Cy Exact: years differ from the folder name (birth 1851 vs folder 1850)" in check


def test_review_invalid_file_and_empty():
    from familysearch_import.relationsreview import render_review
    assert "NOT VALID" in render_review([("X", ["X.json: nodes[0]: bad"])], {})
    assert "No relations files found" in render_review([], {})


def test_cli_relations_review_writes_only_its_file(tmp_path, capsys):
    root = make_archive(tmp_path)
    write_rel(root, rel_doc())
    assert cli.main(["relations-review", "--archive", str(root)]) == 0
    out = capsys.readouterr().out
    path = root / "_import" / "RelationsReview.md"
    assert str(path) in out and "1 file(s), 0 invalid, 1 reviewed" in out
    assert "## Example Line" in path.read_text()
    assert cli.main(["relations-review", "--archive", str(root)]) == 0  # overwrite is fine
    assert cli.main(["relations-review", "--archive", str(cli.repo_root())]) == 2


def test_cli_relations_review_marks_invalid_files(tmp_path, capsys):
    root = make_archive(tmp_path)
    bad = rel_doc()
    bad["nodes"][0]["folder"] = "Example Line/Nobody"
    write_rel(root, bad)
    assert cli.main(["relations-review", "--archive", str(root)]) == 0
    assert "NOT VALID" in (root / "_import" / "RelationsReview.md").read_text()
    assert "1 invalid" in capsys.readouterr().out


def test_cli_relations_approve_requires_yes_and_valid_file(tmp_path, capsys):
    root = make_archive(tmp_path)
    write_rel(root, rel_doc(reviewed=False))
    f = root / "_import" / "relations" / "Example Line.json"
    assert cli.main(["relations-approve", "--archive", str(root), "--line", "Example Line"]) == 2
    assert json.loads(f.read_text())["reviewed"] is False
    assert "RelationsReview.md" in capsys.readouterr().out  # the reminder
    assert cli.main(["relations-approve", "--archive", str(root), "--line", "Example Line", "--yes"]) == 0
    assert json.loads(f.read_text())["reviewed"] is True
    assert cli.main(["relations-approve", "--archive", str(root), "--line", "Nope", "--yes"]) == 2
    bad = rel_doc(reviewed=False)
    bad["edges"][0]["b"] = "zz"
    write_rel(root, bad)
    assert cli.main(["relations-approve", "--archive", str(root), "--line", "Example Line", "--yes"]) == 2
    assert json.loads(f.read_text())["reviewed"] is False


def test_relations_check_reports_unreviewed_and_empty(tmp_path, capsys):
    root = make_archive(tmp_path)
    assert cli.main(["relations-check", "--archive", str(root)]) == 0
    assert "no relations files" in capsys.readouterr().out
    write_rel(root, rel_doc(reviewed=False))
    assert cli.main(["relations-check", "--archive", str(root)]) == 0
    assert "awaiting review" in capsys.readouterr().out
