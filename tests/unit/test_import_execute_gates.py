import json

import pytest

from familysearch_import import execute
from familysearch_import.execute import (
    GateError, Ledger, RunLock, check_run_gates, verify_plan_for_execution,
)
from familysearch_import.planner import Action, Plan, PersonPlan, action_key, compute_plan_hash
from familysearch_import.planreport import load_ledger_keys, plan_json
from familysearch_import.record import repo_root

SHA = "a" * 64


def upload(fs_id="ZZ11-AAA", sha=SHA, state="planned", **kw):
    d = dict(key=action_key(fs_id, "upload-document", sha), kind="upload-document",
             person_line="Example Line", person_folder="1844 Pat Example", fs_id=fs_id,
             file="Census 1900.pdf", title="Census 1900", url=None, state=state, reason="r", sha256=sha)
    d.update(kw)
    return Action(**d)


def cite(fs_id="ZZ11-AAA", url="https://example.org/r/1"):
    return Action(action_key(fs_id, "add-source", url), "add-source", "Example Line",
                  "1844 Pat Example", fs_id, "cite.url", "cite", url, "planned", "r", None)


def write_plan(tmp_path, actions, approve=True, mutate=None, approved_hash=None):
    arch = tmp_path / "arch"
    imp = arch / "_import"
    imp.mkdir(parents=True, exist_ok=True)
    person = PersonPlan("Example Line", "1844 Pat Example", "ZZ11-AAA", "planned", tuple(actions), "")
    plan = Plan((person,), compute_plan_hash([person], "beta"), "beta")
    data = plan_json(plan)
    if mutate:
        mutate(data)
    pp = imp / "Plan.json"
    pp.write_text(json.dumps(data), encoding="utf-8")
    if approve:
        (imp / "approvals.json").write_text(json.dumps(
            {"approved_links": {}, "approved_plan_hash": approved_hash or plan.plan_hash}))
    return arch, pp, plan.plan_hash


def test_good_plan_verifies(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload(), cite()])
    pf = verify_plan_for_execution(arch, pp, h, "beta")
    assert pf.plan_hash == h and len(pf.planned()) == 2


def test_wrong_confirm_hash(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()])
    with pytest.raises(GateError, match="confirm-hash"):
        verify_plan_for_execution(arch, pp, "b" * 64, "beta")
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, "", "beta")
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, h.upper(), "beta")


def test_edited_plan_json_fails_recompute(tmp_path):
    # stored hash left alone but an action is edited after approval
    def edit(d):
        d["people"][0]["actions"][0]["fs_id"] = "ZZ22-BBB"
    arch, pp, h = write_plan(tmp_path, [upload()], mutate=edit)
    with pytest.raises(GateError, match="recomputed"):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_edited_plan_json_with_rewritten_hash_fails_approval(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()])
    data = json.loads(pp.read_text())
    data["people"][0]["actions"][0]["title"] = "Other"
    person = PersonPlan("Example Line", "1844 Pat Example", "ZZ11-AAA", "planned",
                        (Action(**data["people"][0]["actions"][0]),), "")
    new = compute_plan_hash([person], "beta")
    data["plan_hash"] = new
    pp.write_text(json.dumps(data))
    with pytest.raises(GateError, match="approved_plan_hash"):
        verify_plan_for_execution(arch, pp, new, "beta")


def test_state_flip_to_planned_is_caught(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload(state="skipped")])
    data = json.loads(pp.read_text())
    data["people"][0]["actions"][0]["state"] = "planned"
    pp.write_text(json.dumps(data))
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_edited_approvals_hash(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()], approved_hash="c" * 64)
    with pytest.raises(GateError, match="approved_plan_hash"):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_missing_or_malformed_approvals(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()], approve=False)
    with pytest.raises(GateError, match="approvals.json"):
        verify_plan_for_execution(arch, pp, h, "beta")
    (arch / "_import" / "approvals.json").write_text(json.dumps({"approved_links": {}}))
    with pytest.raises(GateError, match="approved_plan_hash"):
        verify_plan_for_execution(arch, pp, h, "beta")
    (arch / "_import" / "approvals.json").write_text("not json")
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_malformed_plan(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()])
    pp.write_text("{")
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, h, "beta")
    pp.write_text(json.dumps({"plan_hash": h}))
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_extra_action_field_rejected(tmp_path):
    def edit(d):
        d["people"][0]["actions"][0]["extra"] = 1
    arch, pp, h = write_plan(tmp_path, [upload()], mutate=edit)
    with pytest.raises(GateError, match="unexpected fields"):
        verify_plan_for_execution(arch, pp, h, "beta")


@pytest.mark.parametrize("bad", [
    dict(file="../../etc/x.pdf"), dict(file="/abs/x.pdf"), dict(file="a/./b.pdf"),
    dict(person_folder="../x"), dict(person_line="a/b"), dict(title=" "),
    dict(fs_id="bad"), dict(sha256="zz"),
])
def test_unsafe_planned_action_rejected(tmp_path, bad):
    a = upload(**bad) if "sha256" not in bad else upload(**bad)
    arch, pp, h = write_plan(tmp_path, [a])
    with pytest.raises(GateError):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_key_must_match_content(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload(key="f" * 64)])
    with pytest.raises(GateError, match="key"):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_source_needs_url(tmp_path):
    a = cite(url="ftp://x")
    arch, pp, h = write_plan(tmp_path, [a])
    with pytest.raises(GateError, match="url"):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_plan_must_live_in_import_dir(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()])
    other = tmp_path / "Plan.json"
    other.write_text(pp.read_text())
    with pytest.raises(GateError, match="inside"):
        verify_plan_for_execution(arch, other, h, "beta")


def test_plan_env_must_match_cli_env(tmp_path):
    arch, pp, h = write_plan(tmp_path, [upload()])
    with pytest.raises(GateError, match="built against beta"):
        verify_plan_for_execution(arch, pp, h, "production")
    with pytest.raises(GateError, match="--env"):
        verify_plan_for_execution(arch, pp, h, "mars")


def test_old_plan_without_env_refused(tmp_path):
    def drop(d):
        d.pop("env")
    arch, pp, h = write_plan(tmp_path, [upload()], mutate=drop)
    with pytest.raises(GateError, match="no environment"):
        verify_plan_for_execution(arch, pp, h, "beta")


def test_env_edit_breaks_the_hash(tmp_path):
    def flip(d):
        d["env"] = "production"
    arch, pp, h = write_plan(tmp_path, [upload()], mutate=flip)
    with pytest.raises(GateError, match="recomputed"):
        verify_plan_for_execution(arch, pp, h, "production")


def test_archive_and_plan_inside_repo_refused():
    repo = repo_root()
    with pytest.raises(GateError, match="repo"):
        verify_plan_for_execution(repo, repo / "_import" / "Plan.json", "a" * 64, "beta")


# ---- run gates

def test_beta_ok_without_only():
    check_run_gates("beta", False, [], 10)
    check_run_gates("beta", False, ["L/F"], 1000)


def test_bad_env_and_max():
    for env in ("prod", "Production", "", None):
        with pytest.raises(GateError):
            check_run_gates(env, True, ["L/F"], 1)
    for m in (0, -1, True, "5", 2.5):
        with pytest.raises(GateError):
            check_run_gates("beta", False, [], m)


def test_production_requires_flag_only_and_cap():
    check_run_gates("production", True, ["L/F"], 25)
    with pytest.raises(GateError, match="allow-production"):
        check_run_gates("production", False, ["L/F"], 5)
    with pytest.raises(GateError, match="allow-production"):
        check_run_gates("production", 1, ["L/F"], 5)  # truthy is not True
    with pytest.raises(GateError, match="--only"):
        check_run_gates("production", True, [], 5)
    with pytest.raises(GateError, match="exceed"):
        check_run_gates("production", True, ["L/F"], 26)


@pytest.mark.parametrize("bad", ["", "L", "L/F/G", "/F", "L/", "../F", "L/..", "."])
def test_only_format(bad):
    with pytest.raises(GateError):
        check_run_gates("beta", False, [bad], 1)


# ---- ledger

def test_ledger_in_repo_refused():
    with pytest.raises(GateError, match="repo"):
        Ledger(repo_root() / "ledger.jsonl")


def test_ledger_append_flush_and_executed_keys(tmp_path):
    led = Ledger(tmp_path / "_import" / "ledger.jsonl")
    a = upload()
    i = led.intent(action=a, env="beta", plan_hash="h")
    assert led.executed_keys() == frozenset()
    # intent already on disk before any result
    first = json.loads(led.path.read_text().splitlines()[0])
    assert first["type"] == "intent" and first["key"] == a.key and first["sha256"] == SHA
    assert first["env"] == "beta" and first["plan_hash"] == "h" and first["ts"].endswith("Z")
    assert [r["intent_id"] for r in led.unresolved_intents()] == [i]
    led.result(intent_id=i, key=a.key, status="created", reason="ok", memory_id="M1")
    assert led.executed_keys() == frozenset({a.key})
    assert led.unresolved_intents() == []


def test_only_created_counts_as_executed(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    for st in ("skipped", "failed", "failed-readback"):
        a = upload(sha=st[0] * 64)
        i = led.intent(action=a, env="beta", plan_hash="h")
        led.result(intent_id=i, key=a.key, status=st, reason="x")
    assert led.executed_keys() == frozenset()
    with pytest.raises(ValueError):
        led.result(intent_id="i", key="k", status="created (reconciled)", reason="x")


def test_planner_still_reads_keys(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    a = upload()
    i = led.intent(action=a, env="beta", plan_hash="h")
    assert load_ledger_keys(led.path, "beta") == frozenset()  # intent alone is not "executed"
    led.result(intent_id=i, key=a.key, status="created", reason="ok", env="beta", memory_id="M1")
    assert load_ledger_keys(led.path, "beta") == frozenset({a.key})
    assert load_ledger_keys(led.path, "production") == frozenset()


def test_torn_line_tolerated_and_not_executed(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    a = upload()
    i = led.intent(action=a, env="beta", plan_hash="h")
    with open(led.path, "a") as f:
        f.write('{"type": "result", "intent_id": "' + i + '", "status": "crea')  # no newline
    assert led.executed_keys() == frozenset()
    assert len(led.unresolved_intents()) == 1 and led.corrupt_lines == 1
    led.result(intent_id=i, key=a.key, status="created", reason="reconciled", reconciled=True)
    assert led.executed_keys() == frozenset({a.key})
    assert led.unresolved_intents() == []


def test_rollback_line_and_no_secrets(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.rollback_result(key="k", status="rolled-back", reason="ok", env="beta", memory_id="M1")
    rec = led.records()[0]
    assert rec["type"] == "rollback" and rec["key"] == "k"
    with pytest.raises(ValueError):
        led.rollback_result(key="k", status="nope", reason="x", env="beta")
    assert led.executed_keys() == frozenset()


# ---- lock

def test_lock_exclusive_and_released(tmp_path):
    arch = tmp_path / "arch"
    with RunLock(arch):
        assert (arch / "_import" / "execute.lock").exists()
        with pytest.raises(GateError, match="holds"):
            with RunLock(arch):
                pass
        # failed second acquisition must not have removed the first lock
        assert (arch / "_import" / "execute.lock").exists()
    assert not (arch / "_import" / "execute.lock").exists()


def test_lock_released_on_error(tmp_path):
    arch = tmp_path / "arch"
    with pytest.raises(RuntimeError):
        with RunLock(arch):
            raise RuntimeError("boom")
    assert not (arch / "_import" / "execute.lock").exists()


def test_break_lock_prints_contents(tmp_path):
    arch = tmp_path / "arch"
    (arch / "_import").mkdir(parents=True)
    (arch / "_import" / "execute.lock").write_text("stale pid 1")
    with pytest.raises(GateError):
        RunLock(arch).__enter__()
    said = []
    with RunLock(arch, break_lock=True, announce=said.append):
        assert (arch / "_import" / "execute.lock").exists()
    assert said and "stale pid 1" in said[0]
    assert not (arch / "_import" / "execute.lock").exists()


def test_lock_in_repo_refused():
    with pytest.raises(GateError, match="repo"):
        RunLock(repo_root() / "x")
