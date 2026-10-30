"""Offline tests for the pure helpers of the beta import drill (no network)."""
import json
import plistlib

from familysearch_import.scan import scan_archive
from tools.verify import import_beta_drill as d


def fake_png():
    fake_png.n += 1
    return b"png%d" % fake_png.n


fake_png.n = 0


def test_build_archive_layout_and_unique_files(tmp_path):
    arch = d.build_archive(tmp_path, "ABCD-123", fake_png)
    assert arch["person_key"] == "zzline/1800-1870 Zzspike Drillperson"
    assert sorted(p.name for p in arch["notes"]) == sorted(d.NOTE_NAMES)
    assert len({p.read_bytes() for p in arch["notes"]}) == 3
    assert (arch["living"] / "Zzspike Living.png").is_file()
    loc = plistlib.loads((arch["dead"] / "Zzspike Drillperson.webloc").read_bytes())
    assert loc["URL"].startswith("https://beta.familysearch.org/") and loc["URL"].endswith("ABCD-123")


def test_archive_scans_as_expected(tmp_path):
    d.build_archive(tmp_path, "ABCD-123", fake_png)
    people = {p.folder: p for p in scan_archive(tmp_path)}
    assert set(people) == {d.DEAD_FOLDER, d.LIVING_FOLDER}
    assert people[d.DEAD_FOLDER].familysearch_ids == ("ABCD-123",)


def test_write_approvals(tmp_path):
    path = d.write_approvals(tmp_path / "_import", "a" * 64, "l/f", "ABCD-123")
    data = json.loads(path.read_text())
    assert data == {"approved_links": {"l/f": "ABCD-123"}, "approved_plan_hash": "a" * 64}


def test_ledger_helpers(tmp_path):
    p = tmp_path / "ledger.jsonl"
    assert d.read_ledger(p) == []
    lines = [{"type": "intent"}, {"type": "result", "status": "created", "memory_id": "m1"},
             {"type": "result", "status": "created", "memory_id": None},
             {"type": "rollback", "status": "rolled-back"}]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    recs = d.read_ledger(p)
    assert len(recs) == 4
    assert d.count_ledger(recs, "intent") == 1
    assert d.count_ledger(recs, "rollback", "rolled-back") == 1
    assert d.created_memory_ids(recs) == ["m1"]


def test_plan_stats():
    plan = {"env": "beta", "plan_hash": "h", "people": [
        {"folder": "1800-1870 X", "actions": [
            {"kind": "upload-document", "state": "planned"},
            {"kind": "upload-document", "state": "already-present"},
            {"kind": "add-source", "state": "planned"}]},
        {"folder": "1990 Y", "actions": [{"kind": "upload-document", "state": "planned"}]}]}
    s = d.plan_stats(plan)
    assert (s["planned_uploads"], s["already_present"], s["living_planned"]) == (2, 1, 1)
    assert s["env"] == "beta"


def test_parse_plan_output():
    h = "ab" * 32
    assert d.parse_plan_output(f"wrote /t/Plan.json and /t/PlanReport.md (plan hash {h})") == ("/t/Plan.json", h)
    assert d.parse_plan_output("error: nope") is None


def test_page_report():
    assert "no list" in d.page_report([])
    assert "single page" in d.page_report(["platform/x/artifacts"])
    rep = d.page_report(["platform/x/artifacts", "platform/x/artifacts?start=25&count=25"])
    assert "2 list request" in rep and "links.next followed" in rep and "count,start" in rep


def test_format_step_and_scrub():
    assert d.format_step("a", True, "3 ok") == "PASS a: 3 ok"
    assert d.format_step("a", False) == "FAIL a"
    secret = "Bearer abc.def " + "x" * 40
    assert "abc.def" not in d.format_step("a", False, secret) and "x" * 40 not in d.scrub(secret)


def test_describe_error_has_no_message():
    class E(Exception):
        status = 409

    assert d.describe_error(E("secret body")) == "E HTTP 409"


def test_poll_retries_then_returns_last():
    seq = iter([0, 1, 2, 3])
    sleeps = []
    assert d.poll(lambda: next(seq), lambda v: v == 2, tries=5, sleep=sleeps.append) == 2
    assert len(sleeps) == 2
    assert d.poll(lambda: 0, lambda v: False, tries=3, sleep=lambda s: None) == 0


def test_report_tracks_failures():
    out = []
    r = d.Report(out.append)
    assert r.step("ok", True) and r.exit_code == 0
    assert not r.step("bad", False, "why")
    assert r.exit_code == 1 and "bad" in r.summary() and out[-1] == "FAIL bad: why"
