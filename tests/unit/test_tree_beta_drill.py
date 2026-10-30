"""Offline tests for the tree beta drill: refusal without a beta session, and its pure helpers."""
import json

import pytest

from familysearch.errors import FSError
from familysearch_import.treeexec import verify_tree_plan_for_execution
from familysearch_import.treeplan import build_tree_plan, write_tree_plan
from familysearch_import.planreport import load_links
from tools.verify import runner
from tools.verify import tree_beta_drill as d
from tools.verify.import_beta_drill import Report

IDS = {"p1": "AAAA-111", "p2": "BBBB-222", "child": "CCCC-333", "control": "DDDD-444"}


def test_refuses_without_a_session(monkeypatch, capsys):
    def no_token():
        raise FSError("no session")
    monkeypatch.setattr(runner, "extract_brave_token", no_token)
    assert d.run(Report(out=lambda s: None)) == 2
    assert "DRILL FAILED" in capsys.readouterr().out


def test_refuses_a_non_beta_session(monkeypatch):
    class Resp:
        status_code = 401

    class FakeClient:
        def __init__(self, token, env):
            self.environment = env

        def get(self, path):
            return Resp()

    monkeypatch.setattr(runner, "extract_brave_token", lambda: "x")
    monkeypatch.setattr(runner, "FSClient", FakeClient)
    assert d.run(Report(out=lambda s: None)) == 2


def test_scratch_inputs_plan_and_verify_offline(tmp_path):
    imp = tmp_path / "_import"
    d.write_inputs(imp, ids=IDS)
    plan = build_tree_plan(json.loads((imp / "relationships.json").read_text()),
                           json.loads((imp / "approvals.json").read_text()),
                           load_links(imp / "links.json"), "beta")
    assert sorted(a.kind for a in plan.actions if a.state == "planned") == \
        ["add-child-parents", "add-couple", "create-person"]
    path = write_tree_plan(tmp_path, plan)
    parsed = d.parse_tree_plan_output(f"wrote {path} and its report (plan hash {plan.plan_hash})")
    assert parsed == (str(path), plan.plan_hash)
    d.write_approvals(imp, json.loads((imp / "approvals.json").read_text())["approved_relationships"],
                      [d.NEW_PERSON["label"]], plan.plan_hash)
    assert verify_tree_plan_for_execution(tmp_path, path, plan.plan_hash, "beta").verified


def test_ledger_helpers():
    recs = [{"type": "progress", "created": {"couple_id": "X1"}},
            {"type": "result", "status": "created", "key": "k", "created": {"person_id": "P1"}},
            {"type": "result", "status": "failed", "key": "j", "created": {}}]
    assert d.created_by_key(recs) == {"k": {"person_id": "P1"}}
    assert d.all_created_ids(recs) == {"couple_id": ["X1"], "child_parents_id": [], "person_id": ["P1"]}
    assert d.count_outcomes("created: a\nskipped: b\ncreated: c", "created") == 2


@pytest.mark.parametrize("name", [n for n, *_ in d.PEOPLE.values()] + [d.NEW_PERSON["label"]])
def test_invented_names(name):
    assert name.startswith("Drill")
