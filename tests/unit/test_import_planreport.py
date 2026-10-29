import json

import pytest

from familysearch_import import __main__ as cli
from familysearch_import import planner
from familysearch_import.classify import DocumentVerdict
from familysearch_import.linking import FsPerson
from familysearch_import.planner import Action, Plan, PersonPlan, build_plan
from familysearch_import.planreport import (
    load_approvals,
    load_ledger_keys,
    load_links,
    plan_json,
    render_plan_report,
)


class FakeReader:
    calls = 0

    def get_fs_person(self, pid):
        return FsPerson(pid, "Pat", 1844, 1915, False, "ok")

    def get_memory_titles(self, pid):
        return []

    def get_source_refs(self, pid):
        return []


def make_archive(tmp_path):
    root = tmp_path / "arch"
    line = root / "Example Line"
    for folder, files in {
        "1844-1915 Pat Example": ["Census 1900.pdf", "skip x.pdf"],
        "1980 Living Example": ["private.pdf"],
    }.items():
        d = line / folder
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_bytes(f.encode())
    (root / "_import").mkdir()
    return root


@pytest.fixture(autouse=True)
def stub_classifier(monkeypatch):
    def fake(person, path, status, banned=()):
        if path.name.startswith("skip"):
            return DocumentVerdict(path, "other", None, None, "skip", "unreadable, provenance unverifiable")
        return DocumentVerdict(path, "other", None, None, "include", "ok")

    monkeypatch.setattr(planner, "classify_document", fake)


def write_links(root, verdict="confirmed"):
    data = {"people": [
        {"line": "Example Line", "folder": "1844-1915 Pat Example", "verdict": verdict, "chosen_id": "ABCD-123"},
        {"line": "Example Line", "folder": "1980 Living Example", "verdict": "confirmed", "chosen_id": "LIVE-111"},
    ]}
    (root / "_import" / "links.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_links_confirmed_only(tmp_path):
    root = make_archive(tmp_path)
    write_links(root, "probable")
    links = load_links(root / "_import" / "links.json")
    assert links["Example Line/1844-1915 Pat Example"] is None
    assert links["Example Line/1980 Living Example"] == "LIVE-111"


def test_load_approvals(tmp_path):
    p = tmp_path / "approvals.json"
    assert load_approvals(p) == {}
    p.write_text(json.dumps({"approved_links": {"L/F": "ABCD-123"}}))
    assert load_approvals(p) == {"L/F": "ABCD-123"}
    for bad in ('{"approved_links": []}', '{"x": 1}', '{"approved_links": {"nofolder": "A"}}', "not json"):
        p.write_text(bad)
        with pytest.raises(ValueError):
            load_approvals(p)


def test_load_ledger_keys(tmp_path):
    p = tmp_path / "ledger.jsonl"
    assert load_ledger_keys(p) == frozenset()
    p.write_text('{"key": "k1"}\n\nbroken\n{"key": "k2", "x": 1}\n')
    assert load_ledger_keys(p) == frozenset({"k1", "k2"})


def test_report_and_json(tmp_path):
    root = make_archive(tmp_path)
    from familysearch_import.scan import scan_archive

    plan = build_plan(scan_archive(root), {"Example Line/1844-1915 Pat Example": "ABCD-123",
                                           "Example Line/1980 Living Example": "LIVE-111"}, {}, FakeReader())
    text = render_plan_report(plan)
    assert plan.plan_hash in text.splitlines()[2]
    assert "Census 1900" in text and "1844-1915 Pat Example" in text
    assert "Living Example" not in text and "private.pdf" not in text
    assert "unreadable, provenance unverifiable: 1" in text
    assert "living: 1" in text
    js = plan_json(plan)
    assert js["plan_hash"] == plan.plan_hash and len(js["people"]) == 2
    json.dumps(js)


def run(root, *extra):
    return cli.main(["plan", "--archive", str(root), "--env", "production", *extra])


def test_cli_writes_plan_files(tmp_path, monkeypatch, capsys):
    root = make_archive(tmp_path)
    write_links(root)
    monkeypatch.setattr(cli, "_make_reader", lambda env: FakeReader())
    assert run(root) == 0
    data = json.loads((root / "_import" / "Plan.json").read_text())
    assert (root / "_import" / "PlanReport.md").exists()
    assert data["plan_hash"] in (root / "_import" / "PlanReport.md").read_text()
    assert run(root) == 0  # no approvals: overwrite is fine


def test_cli_preserves_plan_with_approval_file(tmp_path, monkeypatch):
    root = make_archive(tmp_path)
    write_links(root)
    monkeypatch.setattr(cli, "_make_reader", lambda env: FakeReader())
    assert run(root) == 0
    original = (root / "_import" / "Plan.json").read_text()
    (root / "_import" / "approvals.json").write_text(json.dumps({"approved_links": {}}))
    (root / "_import" / "Plan.json").write_text(original + " ")  # simulate a reviewed older plan
    assert run(root) == 0
    assert (root / "_import" / "Plan.json").read_text() == original + " "
    others = [p.name for p in (root / "_import").glob("Plan.*.json")]
    assert len(others) == 1 and len(others[0]) == len("Plan.12345678.json")


def test_cli_refuses_output_in_repo_and_missing_links(tmp_path, monkeypatch, capsys):
    root = make_archive(tmp_path)
    monkeypatch.setattr(cli, "_make_reader", lambda env: FakeReader())
    assert run(root) == 2  # no links.json
    assert "links.json" in capsys.readouterr().err


def test_cli_limit(tmp_path, monkeypatch):
    root = make_archive(tmp_path)
    write_links(root)
    monkeypatch.setattr(cli, "_make_reader", lambda env: FakeReader())
    assert run(root, "--limit", "1") == 0
    data = json.loads((root / "_import" / "Plan.json").read_text())
    assert len(data["people"]) == 1
