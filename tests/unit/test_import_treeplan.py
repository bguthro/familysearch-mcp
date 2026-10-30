import json

from familysearch_import import __main__ as cli
from familysearch_import.treeplan import build_tree_plan, load_tree_approvals, write_tree_plan

NOPAR = "child has no parents on FamilySearch"
LINKS = {"Line A/alpha": "AAAA-001", "Line A/beta": "BBBB-002", "Line A/gamma": "CCCC-003",
         "Line A/delta": "DDDD-004"}


def edge(kind, a, b, fa, fb, status="missing-in-familysearch", note=""):
    key = f"{kind}:{fa}:{fb}"
    if kind == "spouse":
        key = f"spouse:{min(fa, fb)}:{max(fa, fb)}"
    return {"key": key, "kind": kind, "a": a, "b": b, "a_fs": fa, "b_fs": fb, "status": status,
            "note": note, "lines": ["Line A"], "approved_ends": [], "fs_found": []}


def rel(*edges, cands=()):
    return {"edges": list(edges), "create_candidates": list(cands)}


def appr(rels=(), creations=()):
    return {"approved_relationships": list(rels), "approved_creations": list(creations)}


def test_unapproved_edge_not_planned():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"))
    assert build_tree_plan(r, appr(), LINKS, "beta").actions == ()


def test_spouse_deduped_and_canonical():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"),
            edge("spouse", "Beta Testname", "Alpha Testname", "BBBB-002", "AAAA-001"))
    plan = build_tree_plan(r, appr(["spouse:AAAA-001:BBBB-002"]), LINKS, "beta")
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert (a.kind, a.state, a.key) == ("add-couple", "planned", "spouse:AAAA-001:BBBB-002")


def test_child_grouped_with_two_parents():
    r = rel(edge("parent-child", "Alpha Testname", "Gamma Testname", "AAAA-001", "CCCC-003", note=NOPAR),
            edge("parent-child", "Beta Testname", "Gamma Testname", "BBBB-002", "CCCC-003", note=NOPAR))
    ap = appr(["parent-child:AAAA-001:CCCC-003", "parent-child:BBBB-002:CCCC-003"])
    plan = build_tree_plan(r, ap, LINKS, "beta")
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.kind == "add-child-parents" and a.state == "planned"
    assert a.params["child"] == "CCCC-003" and a.params["parents"] == ["AAAA-001", "BBBB-002"]


def test_manual_when_child_already_has_a_parent():
    r = rel(edge("parent-child", "Alpha Testname", "Gamma Testname", "AAAA-001", "CCCC-003",
                 note="partial-parents: FamilySearch has only the other parent"))
    plan = build_tree_plan(r, appr(["parent-child:AAAA-001:CCCC-003"]), LINKS, "beta")
    assert plan.actions[0].state == "manual"
    assert "website" in plan.actions[0].reason


def test_link_mismatch_skipped():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "ZZZZ-999"))
    plan = build_tree_plan(r, appr(["spouse:AAAA-001:ZZZZ-999"]), LINKS, "beta")
    assert plan.actions[0].state == "skipped"
    assert "links.json" in plan.actions[0].reason


def test_non_missing_status_is_not_planned():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002", status="present"))
    plan = build_tree_plan(r, appr(["spouse:AAAA-001:BBBB-002"]), LINKS, "beta")
    assert [a.state for a in plan.actions] == ["skipped"]


def cand(label, birth, death):
    return {"label": label, "birth": birth, "death": death, "reason": "no folder", "lines": ["Line A"],
            "related_to": []}


def test_create_person_rules():
    cands = (cand("Epsilon Testname", 1850, 1910), cand("Zeta Testname", None, None),
             cand("Eta Testname", 1905, None), cand("Theta Testname", 1800, None),
             cand("Iota Testname", 1850, 1900))
    names = [c["label"] for c in cands]
    plan = build_tree_plan(rel(cands=cands), appr(creations=names[:-1]), LINKS, "beta")
    by = {a.params["name"]: a for a in plan.actions}
    assert set(by) == set(names[:-1])  # unapproved Iota absent
    assert by["Epsilon Testname"].state == "planned" and by["Epsilon Testname"].params["gender"] == "Unknown"
    assert by["Zeta Testname"].state == "skipped"
    assert by["Eta Testname"].state == "skipped" and "living" in by["Eta Testname"].reason
    assert by["Theta Testname"].state == "planned"


def test_hash_changes_with_env_and_action():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"))
    ap = appr(["spouse:AAAA-001:BBBB-002"])
    p1 = build_tree_plan(r, ap, LINKS, "beta")
    assert p1.plan_hash == build_tree_plan(r, ap, LINKS, "beta").plan_hash
    assert p1.plan_hash != build_tree_plan(r, ap, LINKS, "production").plan_hash
    r2 = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"),
             edge("spouse", "Gamma Testname", "Delta Testname", "CCCC-003", "DDDD-004"))
    p2 = build_tree_plan(r2, appr(["spouse:AAAA-001:BBBB-002", "spouse:CCCC-003:DDDD-004"]), LINKS, "beta")
    assert p1.plan_hash != p2.plan_hash


def test_write_and_report_content(tmp_path):
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"),
            edge("parent-child", "Alpha Testname", "Gamma Testname", "AAAA-001", "CCCC-003", note="x"),
            cands=(cand("Epsilon Testname", 1850, 1910),))
    ap = appr(["spouse:AAAA-001:BBBB-002", "parent-child:AAAA-001:CCCC-003"], ["Epsilon Testname"])
    plan = build_tree_plan(r, ap, LINKS, "beta")
    path = write_tree_plan(tmp_path, plan)
    tag = plan.plan_hash[:8]
    assert path == tmp_path / "_import" / f"TreePlan.{tag}.json"
    data = json.loads(path.read_text())
    assert data["env"] == "beta" and data["plan_hash"] == plan.plan_hash
    report = (tmp_path / "_import" / f"TreePlanReport.{tag}.md").read_text()
    assert "Planned (2)" in report and "Manual on the website (1)" in report
    low = report.lower()
    for bad in ("token", "bearer", "access_token", "account", "session"):
        assert bad not in low


def test_cli_tree_plan_writes_files(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    imp = archive / "_import"
    imp.mkdir(parents=True)
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"))
    (imp / "relationships.json").write_text(json.dumps(r))
    (imp / "approvals.json").write_text(json.dumps(
        {"approved_links": {}, "approved_relationships": ["spouse:AAAA-001:BBBB-002"]}))
    (imp / "links.json").write_text(json.dumps({"people": [
        {"line": "Line A", "folder": "alpha", "verdict": "confirmed", "chosen_id": "AAAA-001"},
        {"line": "Line A", "folder": "beta", "verdict": "confirmed", "chosen_id": "BBBB-002"}]}))
    monkeypatch.setattr(cli, "check_archive", lambda a, r: None)
    assert cli.main(["tree-plan", "--archive", str(archive), "--env", "beta"]) == 0
    assert list(imp.glob("TreePlan.*.json")) and list(imp.glob("TreePlanReport.*.md"))


def test_load_tree_approvals_missing_file(tmp_path):
    assert load_tree_approvals(tmp_path / "none.json")["approved_relationships"] == []


def test_unmatched_approvals_warn_in_report():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"))
    plan = build_tree_plan(r, appr(["spouse:AAAA-001:ZZZZ-999"], ["Nobody Here"]), LINKS, "beta")
    assert plan.actions == ()
    assert len(plan.warnings) == 2
    from familysearch_import.treeplan import render_tree_plan_report
    text = render_tree_plan_report(plan)
    assert "spouse:AAAA-001:ZZZZ-999" in text and "Nobody Here" in text


def test_matched_approvals_do_not_warn():
    r = rel(edge("spouse", "Alpha Testname", "Beta Testname", "AAAA-001", "BBBB-002"))
    assert build_tree_plan(r, appr(["spouse:AAAA-001:BBBB-002"]), LINKS, "beta").warnings == ()
