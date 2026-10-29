import json

import pytest

from familysearch_import import __main__ as cli
from familysearch_import.linking import FsPerson
from familysearch_import.linkreport import (
    build_link_results,
    links_json,
    render_link_report,
    split_by_status,
)
from familysearch_import.scan import scan_archive


def fs(id, name="Pat Example", b=1844, d=1915, status="ok", survivor=None):
    return FsPerson(id, name, b, d, False, status, survivor)


def make_archive(tmp_path):
    root = tmp_path / "arch"
    line = root / "Example Line"
    folders = {
        "1844-1915 Pat Example": "FamilySearch ID: ABCD-123",
        "1860-1930 Sam Example": "Father ABCD-123; his own id WXYZ-9AB",
        "1850-1920 Lee Example": "no ids here",
        "1855-1925 Kim Example": "ID QQQQ-4R5",
        "1980 Living Example": "ID LIVE-111",
        "?-? Unknown Example": "ID UNKN-222",
    }
    for name, note in folders.items():
        d = line / name
        d.mkdir(parents=True)
        (d / "ResearchSummary.md").write_text(note)
    return root


DB = {
    "ABCD-123": fs("ABCD-123"),
    "WXYZ-9AB": fs("WXYZ-9AB", "Sam Example", 1860, 1930),
    "QQQQ-4R5": fs("QQQQ-4R5", status="error"),
}


def run(tmp_path):
    people = scan_archive(make_archive(tmp_path))
    looked, searched = [], []

    def lookup(i):
        looked.append(i)
        return DB.get(i, fs(i, status="not-found"))

    def search(p):
        searched.append(p.folder)
        return [fs("SRCH-111", "Lee Example", 1850, 1920)]

    results = build_link_results(people, lookup, search)
    return people, results, looked, searched


def test_build_results_deceased_only_and_verdicts(tmp_path):
    people, results, looked, searched = run(tmp_path)
    assert not any("LIVE-111" in i or "UNKN-222" in i for i in looked)
    assert searched == ["1850-1920 Lee Example"]
    by = {r.person.display_name: r for r in results}
    assert set(by) == {"Pat Example", "Sam Example", "Lee Example", "Kim Example"}
    assert (by["Pat Example"].verdict, by["Pat Example"].chosen_id) == ("confirmed", "ABCD-123")
    assert (by["Sam Example"].verdict, by["Sam Example"].chosen_id) == ("confirmed", "WXYZ-9AB")
    assert by["Kim Example"].verdict == "unlinked"
    assert by["Lee Example"].verdict == "needs-search"
    assert [s.id for s in by["Lee Example"].search] == ["SRCH-111"]
    _, counts = split_by_status(people)
    assert (counts["deceased"], counts["living"], counts["unknown"]) == (4, 1, 1)


def test_search_failure_is_flagged_not_empty(tmp_path):
    people = scan_archive(make_archive(tmp_path))

    def boom(p):
        raise RuntimeError("down")

    results = build_link_results(people, lambda i: DB.get(i, fs(i, status="not-found")), boom)
    lee = next(r for r in results if r.person.display_name == "Lee Example")
    assert lee.search_error and lee.search == ()
    assert "search failed" in render_link_report(results, {})


def test_render_and_json_hide_living(tmp_path):
    people, results, _, _ = run(tmp_path)
    _, counts = split_by_status(people)
    md = render_link_report(results, counts)
    assert "Example Line / 1844-1915 Pat Example -> ABCD-123" in md
    assert "found in ResearchSummary.md (note)" in md
    assert "search candidate SRCH-111: Lee Example 1850-1920" in md
    assert "living (not looked up, not listed): 1" in md
    assert "unknown status (not looked up, not listed): 1" in md
    assert "Living Example" not in md and "Unknown Example" not in md
    data = links_json(results)
    text = json.dumps(data)
    assert "Living Example" not in text and "LIVE-111" not in text and "UNKN-222" not in text
    assert data["summary"]["confirmed"] == 2
    assert data["summary"]["needs-search"] == 1


class FakeReader:
    def __init__(self):
        self.gets = []

    def get_fs_person(self, i):
        self.gets.append(i)
        return DB.get(i, fs(i, status="not-found"))

    def search_candidates(self, p, limit=3, status=None):
        assert status == "deceased"
        return []


@pytest.fixture
def fake_reader(monkeypatch):
    reader = FakeReader()
    monkeypatch.setattr(cli, "_make_reader", lambda env: reader)
    return reader


def test_cli_writes_outputs(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["links", "--archive", str(root), "--env", "production"]) == 0
    assert (root / "_import" / "LinkReport.md").is_file()
    data = json.loads((root / "_import" / "links.json").read_text())
    assert data["summary"]["confirmed"] == 2
    assert "LIVE-111" not in fake_reader.gets
    # default location may be overwritten on rerun
    assert cli.main(["links", "--archive", str(root), "--env", "beta"]) == 0


def test_cli_limit(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    assert cli.main(["links", "--archive", str(root), "--env", "production", "--limit", "1"]) == 0
    assert len(json.loads((root / "_import" / "links.json").read_text())["people"]) == 1


def test_cli_env_required_and_validated(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["links", "--archive", str(root)])
    with pytest.raises(SystemExit):
        cli.main(["links", "--archive", str(root), "--env", "staging"])


def test_cli_refuses_repo_outputs_and_overwrites(tmp_path, fake_reader):
    root = make_archive(tmp_path)
    repo = cli.repo_root()
    assert cli.main(["links", "--archive", str(root), "--env", "production", "--out", str(repo / "x")]) == 2
    assert not (repo / "x").exists()
    out = tmp_path / "outside"
    assert cli.main(["links", "--archive", str(root), "--env", "production", "--out", str(out)]) == 0
    assert cli.main(["links", "--archive", str(root), "--env", "production", "--out", str(out)]) == 2
    assert cli.main(["links", "--archive", str(root), "--env", "production", "--out", str(root / "Example Line")]) == 2
    assert cli.main(["links", "--archive", str(repo), "--env", "production"]) == 2
    assert fake_reader.gets  # lookups happened only for the successful run


def test_cli_fs_error_returns_2(tmp_path, monkeypatch):
    from familysearch.errors import FSError

    def boom(env):
        raise FSError("not signed in")

    monkeypatch.setattr(cli, "_make_reader", boom)
    root = make_archive(tmp_path)
    assert cli.main(["links", "--archive", str(root), "--env", "production"]) == 2
    assert not (root / "_import").exists()
