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
