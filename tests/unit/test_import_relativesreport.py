import json

import pytest

from familysearch_import import __main__ as cli
from familysearch_import.linking import FsPerson
from familysearch_import.prod_reader import Relative
from familysearch_import.relatives import (
    Candidate, Evidence, RelativesResult, TargetResult,
)
from familysearch_import.relativesreport import relatives_json, render_relatives_report


def fsp(id, name, b, d, living=False):
    return FsPerson(id, name, b, d, living, "ok")


def sample(partial=False):
    c1 = Candidate(fsp("CCCC-111", "Cal Example", 1875, 1940), "confirmed",
                   (Evidence("child", "AAAA-111", "Line A/Ann Example"),
                    Evidence("spouse-of-child", "BBBB-222", "Line B/Bob Sample")), None)
    c2 = Candidate(fsp("CCCC-222", "Cal Example", 1876, None), "probable",
                   (Evidence("child", "AAAA-111", "Line A/Ann Example"),), "Line A/Cal Other")
    return RelativesResult(
        (TargetResult("Line A/Cal Example", (c1, c2), True), TargetResult("Line A/Zed Example", (), False)),
        reads=7, partial=partial, living_skipped=3, failed_reads=1 if partial else 0, starts=2)


def test_render_contains_flags_evidence_approval_and_no_candidate():
    text = render_relatives_report(sample())
    assert "ambiguous" in text
    assert "taken by Line A/Cal Other" in text
    assert "child of Ann Example (Line A / Ann Example) via AAAA-111" in text
    assert "spouse-of-child of Bob Sample (Line B / Bob Sample) via BBBB-222" in text
    assert '"Line A/Cal Example": "CCCC-111"' in text
    assert "## No candidate (1)" in text and "Line A/Zed Example" in text
    assert "living relatives skipped (counted only, never listed): 3" in text
    assert "PARTIAL" not in text


def test_partial_warning():
    assert "PARTIAL RESULT" in render_relatives_report(sample(partial=True))
    assert relatives_json(sample(partial=True))["partial"] is True


def test_json_shape():
    data = relatives_json(sample())
    t = data["targets"][0]
    assert t["person"] == "Line A/Cal Example" and t["ambiguous"] is True
    assert t["candidates"][0]["evidence"][0] == {
        "relationship": "child", "via_fs_id": "AAAA-111", "via_person": "Line A/Ann Example"}
    assert t["candidates"][1]["taken_by"] == "Line A/Cal Other"
    assert data["living_skipped"] == 3


# ---- CLI ----

def make_archive(tmp_path):
    root = tmp_path / "arch"
    line = root / "Example Line"
    for name in ("1844-1915 Pat Example", "1875-1940 Cal Example", "1980 Living Example"):
        d = line / name
        d.mkdir(parents=True)
        (d / "ResearchSummary.md").write_text("notes")
    imp = root / "_import"
    imp.mkdir()
    (imp / "links.json").write_text(json.dumps({"people": [
        {"line": "Example Line", "folder": "1844-1915 Pat Example", "verdict": "confirmed", "chosen_id": "AAAA-111"},
        {"line": "Example Line", "folder": "1875-1940 Cal Example", "verdict": "needs-search", "chosen_id": None},
    ]}))
    return root


class FakeReader:
    def __init__(self):
        self.calls = []

    def get_relatives(self, fs_id):
        self.calls.append(fs_id)
        if fs_id == "AAAA-111":
            return [Relative(fsp("CCCC-111", "Cal Example", 1875, 1940), "child"),
                    Relative(fsp("LIVE-999", "Liv Secret", 1980, None, living=True), "child")]
        return []


@pytest.fixture
def fake_reader(monkeypatch):
    reader = FakeReader()
    monkeypatch.setattr(cli, "_make_reader", lambda env: reader)
    return reader


def test_cli_writes_report_and_json_without_living_names(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["relatives", "--archive", str(root), "--env", "production"]) == 0
    text = (root / "_import" / "RelativesReport.md").read_text()
    data = json.loads((root / "_import" / "relatives.json").read_text())
    assert "CCCC-111" in text and "Liv Secret" not in text
    assert "Liv Secret" not in json.dumps(data) and data["living_skipped"] == 1
    assert data["targets"][0]["candidates"][0]["id"] == "CCCC-111"


def test_cli_max_reads_partial(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["relatives", "--archive", str(root), "--env", "production", "--max-reads", "0"]) == 0
    assert "PARTIAL RESULT" in (root / "_import" / "RelativesReport.md").read_text()
    assert fake_reader.calls == []


def test_cli_approval_turns_target_into_start(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    (root / "_import" / "approvals.json").write_text(json.dumps(
        {"approved_links": {"Example Line/1875-1940 Cal Example": "CCCC-111"}}))
    assert cli.main(["relatives", "--archive", str(root), "--env", "production"]) == 0
    assert "CCCC-111" in fake_reader.calls
    assert json.loads((root / "_import" / "relatives.json").read_text())["targets"] == []


def test_cli_requires_links_and_env_and_repo_guard(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    (root / "_import" / "links.json").unlink()
    assert cli.main(["relatives", "--archive", str(root), "--env", "production"]) == 2
    assert not (root / "_import" / "RelativesReport.md").exists()
    with pytest.raises(SystemExit):
        cli.main(["relatives", "--archive", str(root)])
    assert cli.main(["relatives", "--archive", str(cli.repo_root()), "--env", "production"]) == 2


def test_cli_fs_error_returns_2(tmp_path, monkeypatch):
    from familysearch.errors import FSError

    def boom(env):
        raise FSError("not signed in")

    monkeypatch.setattr(cli, "_make_reader", boom)
    root = make_archive(tmp_path)
    assert cli.main(["relatives", "--archive", str(root), "--env", "production"]) == 2
    assert not (root / "_import" / "RelativesReport.md").exists()
