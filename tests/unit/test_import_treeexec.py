import hashlib
import json

import pytest

from familysearch.errors import FSHTTPError
from familysearch_import.execute import GateError, Ledger, RunLock
from familysearch_import.linking import FsPerson
from familysearch_import.prod_reader import ReadFailed, Relative
from familysearch_import.treeexec import (
    TreeServices, resolve_tree, rollback_tree, run_tree_plan, verify_tree_plan_for_execution,
)
from familysearch_import.treeplan import TreeAction, TreePlan, compute_tree_plan_hash, tree_plan_json

A, B, C, D, E, F = "AAAA-001", "BBBB-002", "CCCC-003", "DDDD-004", "EEEE-005", "FFFF-006"
LINKED = [A, B, C, D, E, F]


class FakeFS:
    """Fictional FamilySearch: persons, couples, child-and-parents relationships."""

    def __init__(self):
        self.people = {p: dict(name=f"Person {p}", living=False, status="ok") for p in LINKED}
        self.couples = {}   # rel id -> frozenset
        self.cap = {}       # rel id -> (child, tuple(parents))
        self.hits = []      # search_candidates results
        self.calls = []
        self.n = 0
        self.apply = True            # does a write take effect
        self.leak_to = None          # write also (wrongly) links this person
        self.write_error = None
        self.ret_id = None           # override the returned id
        self.delete_effect = True
        self.new_name = None         # name stored for created people (None: as requested)

    # reader
    def get_fs_person(self, pid):
        self.calls.append(("person", pid))
        p = self.people.get(pid)
        if p is None:
            return FsPerson(pid, "", None, None, None, "not-found")
        return FsPerson(pid, p["name"], None, None, p["living"], p["status"])

    def get_relatives(self, pid):
        out = []
        for cid, s in self.couples.items():
            if pid in s:
                (o,) = s - {pid}
                out.append(Relative(self.get_fs_person(o), "spouse"))
        for _, (child, parents) in self.cap.items():
            if pid == child:
                out += [Relative(self.get_fs_person(x), "parent") for x in parents]
            if pid in parents:
                out.append(Relative(self.get_fs_person(child), "child"))
        return out

    def search_candidates(self, person, limit=3, status=None):
        self.calls.append(("search", person.display_name, person.birth, person.death))
        return list(self.hits)

    # writer
    def _id(self, prefix):
        self.n += 1
        return self.ret_id or f"{prefix}{self.n}"

    def create_couple(self, p1, p2, *, reason):
        self.calls.append(("create_couple", p1, p2))
        if self.write_error:
            raise FSHTTPError(self.write_error, "x")
        rid = self._id("R")
        if self.apply:
            self.couples[rid] = frozenset((p1, p2))
        if self.leak_to:
            self.couples[rid + "x"] = frozenset((p1, self.leak_to))
        return rid

    def create_parent_child(self, *, child_id, parent_ids, reason):
        self.calls.append(("create_parent_child", child_id, tuple(parent_ids)))
        rid = self._id("P")
        if self.apply:
            self.cap[rid] = (child_id, tuple(parent_ids))
        if self.leak_to:
            self.cap[rid + "x"] = (self.leak_to, (parent_ids[0],))
        return rid

    def create_person(self, person, *, reason):
        self.calls.append(("create_person", json.dumps(person, sort_keys=True)))
        rid = self.ret_id or f"NNNN-{self.n + 1:03d}"; self.n += 1
        if self.apply:
            name = self.new_name or person["names"][0]["nameForms"][0]["fullText"]
            self.people[rid] = dict(name=name, living=False, status="ok")
        return rid

    def delete_couple(self, rid, *, reason):
        self.calls.append(("delete_couple", rid))
        if self.delete_effect:
            self.couples.pop(rid, None)

    def delete_child_and_parents(self, rid, *, reason):
        self.calls.append(("delete_cap", rid))
        if self.delete_effect:
            self.cap.pop(rid, None)

    def delete_person(self, pid, *, reason):
        self.calls.append(("delete_person", pid))
        if self.delete_effect:
            self.people.pop(pid, None)

    def writes(self):
        return [c for c in self.calls if c[0].startswith(("create_", "delete_"))]


def couple(a=A, b=B):
    x, y = sorted((a, b))
    return TreeAction("add-couple", f"spouse:{x}:{y}", {"a": x, "b": y}, "planned", "r")


def child(c=C, parents=(A, B)):
    ps = sorted(parents)
    return TreeAction("add-child-parents", f"child-parents:{c}",
                      {"child": c, "parents": ps}, "planned", "r")


def newp(name="Zed Testname", birth=1850, death=1910):
    key = "create-person:" + hashlib.sha256(name.encode()).hexdigest()[:16]
    return TreeAction("create-person", key, {"name": name, "birth": birth, "death": death,
                                             "gender": "Unknown"}, "planned", "r")


def make(tmp_path, actions, env="beta", approve=True, links=LINKED):
    arch = tmp_path / "arch"
    imp = arch / "_import"
    imp.mkdir(parents=True, exist_ok=True)
    plan = TreePlan(env, tuple(actions), compute_tree_plan_hash(actions, env))
    path = imp / "TreePlan.json"
    path.write_text(json.dumps(tree_plan_json(plan)))
    (imp / "links.json").write_text(json.dumps({"people": [
        {"line": "L", "folder": f"f{i}", "verdict": "confirmed", "chosen_id": x} for i, x in enumerate(links)]}))
    appr = {"approved_tree_plan_hash": plan.plan_hash} if approve else {}
    (imp / "approvals.json").write_text(json.dumps(appr))
    return arch, path, plan


def pf_for(tmp_path, actions, env="beta", **kw):
    arch, path, plan = make(tmp_path, actions, env, **kw)
    return arch, verify_tree_plan_for_execution(arch, path, plan.plan_hash, env), plan


def run(tmp_path, fs, actions, env="beta", execute=True, **kw):
    arch, pf, _ = pf_for(tmp_path, actions, env)
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    svc = TreeServices(reader=fs, writer=fs, archive=arch, env=env)
    if execute:
        with RunLock(arch):
            return run_tree_plan(pf, svc, led, execute=True, **kw), led
    return run_tree_plan(pf, svc, led, execute=False, **kw), led


def statuses(summary):
    return [o.status for o in summary.outcomes]


# ------------------------------------------------------------------ verify gates


def test_verify_ok_and_gates(tmp_path):
    arch, path, plan = make(tmp_path, [couple()])
    pf = verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")
    assert pf.verified and len(pf.planned()) == 1
    with pytest.raises(GateError, match="--env|built against"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "production")
    with pytest.raises(GateError, match="confirm-hash"):
        verify_tree_plan_for_execution(arch, path, "0" * 64, "beta")
    with pytest.raises(GateError, match="64-character"):
        verify_tree_plan_for_execution(arch, path, "abc", "beta")


def test_verify_requires_approval_hash(tmp_path):
    arch, path, plan = make(tmp_path, [couple()], approve=False)
    with pytest.raises(GateError, match="approved_tree_plan_hash"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_approved_hash_must_match(tmp_path):
    arch, path, plan = make(tmp_path, [couple()])
    (arch / "_import" / "approvals.json").write_text(json.dumps({"approved_tree_plan_hash": "1" * 64}))
    with pytest.raises(GateError, match="approved_tree_plan_hash"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_detects_tampered_plan(tmp_path):
    arch, path, plan = make(tmp_path, [couple()])
    data = json.loads(path.read_text())
    data["actions"][0]["params"]["b"] = C
    path.write_text(json.dumps(data))
    with pytest.raises(GateError, match="recomputed hash"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_env_is_in_hash(tmp_path):
    arch, path, plan = make(tmp_path, [couple()])
    data = json.loads(path.read_text())
    data["env"] = "production"
    path.write_text(json.dumps(data))
    with pytest.raises(GateError, match="recomputed hash"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "production")


def test_verify_rejects_malformed_runnable(tmp_path):
    bad = TreeAction("add-couple", "spouse:AAAA-001:BBBB-002", {"a": A, "b": "nope"}, "planned", "r")
    arch, path, plan = make(tmp_path, [bad])
    with pytest.raises(GateError, match="malformed"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_rejects_living_creation(tmp_path):
    arch, path, plan = make(tmp_path, [newp(birth=1950, death=None)])
    with pytest.raises(GateError, match="deceased"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


# ------------------------------------------------------------------- dry run


def test_dry_run_writes_nothing(tmp_path):
    fs = FakeFS()
    s, led = run(tmp_path, fs, [couple(), child(D, (A, B)), newp()], execute=False)
    assert statuses(s) == ["would-create"] * 3
    assert fs.writes() == []
    assert not led.path.exists()


def test_run_requires_lock_when_executing(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    svc = TreeServices(fs, fs, arch, "beta")
    with pytest.raises(GateError, match="lock"):
        run_tree_plan(pf, svc, Ledger(arch / "_import" / "tree-ledger.jsonl"), execute=True)
    assert fs.calls == []


def test_unverified_plan_refused(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    import dataclasses
    raw = dataclasses.replace(pf, verified=False)
    with pytest.raises(GateError, match="not verified"):
        run_tree_plan(raw, TreeServices(fs, fs, arch, "beta"), Ledger(arch / "_import" / "tree-ledger.jsonl"))


def test_env_mismatch_refused(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    with pytest.raises(GateError, match="services are"):
        run_tree_plan(pf, TreeServices(fs, fs, arch, "production"), Ledger(arch / "_import" / "x.jsonl"),
                      only=[pf.planned()[0].key], allow_production=True)


# ------------------------------------------------------------- production gates


def test_production_gates(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()], env="production")
    svc = TreeServices(fs, fs, arch, "production")
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    key = pf.planned()[0].key
    with pytest.raises(GateError, match="allow-production"):
        run_tree_plan(pf, svc, led, only=[key])
    with pytest.raises(GateError, match="--only"):
        run_tree_plan(pf, svc, led, allow_production=True)
    with pytest.raises(GateError, match="25"):
        run_tree_plan(pf, svc, led, only=[key], max_actions=26, allow_production=True)
    with pytest.raises(GateError, match="not a planned action"):
        run_tree_plan(pf, svc, led, only=["spouse:ZZZZ-001:ZZZZ-002"], allow_production=True)
    assert fs.calls == []
    s = run_tree_plan(pf, svc, led, only=[key], max_actions=10, allow_production=True)
    assert statuses(s) == ["would-create"]


def test_only_selects_by_key(tmp_path):
    fs = FakeFS()
    c1, c2 = couple(A, B), couple(C, D)
    s, _ = run(tmp_path, fs, [c1, c2], only=[c2.key])
    assert statuses(s) == ["created"]
    assert fs.writes() == [("create_couple", C, D)]


def test_max_actions_truncates(tmp_path):
    fs = FakeFS()
    s, _ = run(tmp_path, fs, [couple(A, B), couple(C, D)], max_actions=1)
    assert statuses(s) == ["created"] and s.truncated


# ------------------------------------------------------------------ successes


def test_couple_created_with_readback(tmp_path):
    fs = FakeFS()
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["created"] and not s.stopped
    assert fs.writes() == [("create_couple", A, B)]
    types = [r["type"] for r in led.records()]
    assert types == ["intent", "progress", "result"]
    res = led.records()[-1]
    assert res["status"] == "created" and res["env"] == "beta" and res["kind"] == "add-couple"
    assert res["created"] == {"couple_id": "R1"}
    assert led.records()[1]["created"] == {"couple_id": "R1"}


def test_child_parents_created(tmp_path):
    fs = FakeFS()
    s, led = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["created"]
    assert fs.writes() == [("create_parent_child", C, (A, B))]
    assert led.records()[-1]["created"] == {"child_parents_id": "P1"}


def test_create_person_created(tmp_path):
    fs = FakeFS()
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["created"]
    assert [c[0] for c in fs.calls].count("search") == 1
    body = json.loads(next(c for c in fs.calls if c[0] == "create_person")[1])
    assert body["gender"]["type"].endswith("Unknown") and body["living"] is False
    assert led.records()[-1]["created"] == {"person_id": "NNNN-001"}


def test_rerun_is_noop(tmp_path):
    fs = FakeFS()
    actions = [couple()]
    run(tmp_path, fs, actions)
    n = len(fs.writes())
    arch = tmp_path / "arch"
    pf = verify_tree_plan_for_execution(arch, arch / "_import" / "TreePlan.json",
                                        json.loads((arch / "_import" / "TreePlan.json").read_text())["plan_hash"],
                                        "beta")
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    with RunLock(arch):
        s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), led, execute=True)
    assert statuses(s) == ["skipped"] and len(fs.writes()) == n


# ----------------------------------------------------------------- pre-checks


@pytest.mark.parametrize("mutate,reason", [
    (lambda fs: fs.people[A].update(living=True), "living"),
    (lambda fs: fs.people[A].update(living=None), "living"),
    (lambda fs: fs.people[B].update(status="redirected"), "redirected"),
    (lambda fs: fs.people.pop(B), "not-found"),
    (lambda fs: fs.couples.update(X=frozenset((A, B))), "already exists"),
])
def test_couple_prechecks(tmp_path, mutate, reason):
    fs = FakeFS()
    mutate(fs)
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["skipped"] and reason in s.outcomes[0].reason
    assert fs.writes() == [] and not led.path.exists()


def test_couple_refused_when_link_no_longer_confirmed(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()], links=[A, C, D, E])
    s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), Ledger(arch / "_import" / "tree-ledger.jsonl"))
    assert statuses(s) == ["skipped"] and "links.json" in s.outcomes[0].reason


def test_couple_refused_without_negative_control(tmp_path):
    fs = FakeFS()
    s, _ = run(tmp_path, fs, [couple()], execute=False)  # sanity: controls exist
    assert statuses(s) == ["would-create"]
    fs2 = FakeFS()
    arch, pf, _ = pf_for(tmp_path / "two", [couple()], links=[A, B])
    s2 = run_tree_plan(pf, TreeServices(fs2, fs2, arch, "beta"), Ledger(arch / "_import" / "tree-ledger.jsonl"))
    assert statuses(s2) == ["skipped"] and "negative control" in s2.outcomes[0].reason


@pytest.mark.parametrize("mutate,reason", [
    (lambda fs: fs.cap.update(X=(C, (D,))), "already has a parent"),
    (lambda fs: fs.people[C].update(living=True), "living"),
    (lambda fs: fs.people[A].update(living=True), "living"),
    (lambda fs: fs.people[B].update(status="deleted"), "deleted"),
])
def test_child_prechecks(tmp_path, mutate, reason):
    fs = FakeFS()
    mutate(fs)
    s, _ = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["skipped"] and reason in s.outcomes[0].reason
    assert fs.writes() == []


def test_create_person_refuses_plausible_match(tmp_path):
    fs = FakeFS()
    fs.hits = [FsPerson("MMMM-111", "Zed Testname", 1850, 1910, False, "ok")]
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["skipped"] and "plausible match MMMM-111" in s.outcomes[0].reason
    assert fs.writes() == [] and not led.path.exists()


def test_read_failure_stops_without_write(tmp_path):
    fs = FakeFS()

    def boom(pid):
        raise ReadFailed("boom")
    fs.get_relatives = boom
    s, _ = run(tmp_path, fs, [couple()])
    assert s.stopped and fs.writes() == []


# ------------------------------------------------------------- read-back / failure


def test_couple_not_visible_is_failed_readback_and_blocks(tmp_path):
    fs = FakeFS()
    fs.apply = False
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"] and s.stopped
    assert led.records()[-1]["created"] == {"couple_id": "R1"}  # id kept for rollback
    assert led.blocked_keys("beta") == {couple().key: "failed-readback"}
    n = len(fs.writes())
    fs.apply = True
    s2, _ = run(tmp_path, fs, [couple()])
    assert s2.stopped and "resolve" in s2.stop_reason and len(fs.writes()) == n


def test_negative_control_failure(tmp_path):
    fs = FakeFS()
    fs.leak_to = E  # control picked is the first unrelated linked person (CCCC-003 here); leak into it
    fs.leak_to = C
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"] and "negative control" in s.outcomes[0].reason
    assert led.blocked_keys("beta")


def test_child_extra_parent_fails_readback(tmp_path):
    fs = FakeFS()
    orig = fs.create_parent_child

    def extra(**kw):
        rid = orig(**kw)
        fs.cap[rid] = (kw["child_id"], (*kw["parent_ids"], D))
        return rid
    fs.create_parent_child = extra
    s, _ = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["failed-readback"]


def test_person_name_mismatch_fails_readback(tmp_path):
    fs = FakeFS()
    fs.new_name = "Someone Else"
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["failed-readback"]
    assert led.records()[-1]["created"] == {"person_id": "NNNN-001"}


def test_write_error_is_failed_and_blocks(tmp_path):
    fs = FakeFS()
    fs.write_error = 500
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed"] and s.stopped
    assert led.records()[-1]["created"] == {}
    assert couple().key in led.blocked_keys("beta")


def test_invalid_returned_id_not_recorded(tmp_path):
    fs = FakeFS()
    fs.ret_id = "../etc"
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"]
    assert led.records()[-1]["created"] == {}
    assert all(r["type"] != "progress" for r in led.records())


def test_keyboard_interrupt_records_partial(tmp_path):
    fs = FakeFS()

    def boom(pid):
        raise KeyboardInterrupt
    orig = fs.get_relatives
    calls = {"n": 0}

    def rels(pid):
        # pre-check reads succeed; interrupt during read-back (after the write)
        if fs.couples:
            raise KeyboardInterrupt
        return orig(pid)
    fs.get_relatives = rels
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, fs, [couple()])
    led = Ledger(tmp_path / "arch" / "_import" / "tree-ledger.jsonl")
    last = led.records()[-1]
    assert last["status"] == "partial" and last["created"] == {"couple_id": "R1"}


def test_crash_after_progress_reconciles_as_partial(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    led._append({"type": "intent", "intent_id": "i1", "ts": "2020-01-01T00:00:00Z", "key": couple().key,
                 "kind": "add-couple", "env": "beta", "params": couple().params})
    led._append({"type": "progress", "intent_id": "i1", "key": couple().key, "env": "beta",
                 "created": {"couple_id": "R9"}})
    with RunLock(arch):
        s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), led, execute=True)
    assert s.stopped and fs.writes() == []
    assert led.records()[-1]["status"] == "partial"
    assert led.blocked_keys("beta") == {couple().key: "partial"}


def test_crash_without_progress_is_ambiguous(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    led._append({"type": "intent", "intent_id": "i1", "ts": "2020-01-01T00:00:00Z", "key": couple().key,
                 "kind": "add-couple", "env": "beta", "params": couple().params})
    with RunLock(arch):
        s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), led, execute=True)
    assert s.stopped and fs.writes() == []
    assert led.blocked_keys("beta") == {couple().key: "ambiguous"}


# ----------------------------------------------------------------------- resolve


def test_resolve_absent_lifts_block(tmp_path):
    fs = FakeFS()
    fs.write_error = 500
    run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    # make the intent old enough to count as settled
    lines = [json.loads(x) for x in led.path.read_text().splitlines()]
    lines[0]["ts"] = "2020-01-01T00:00:00Z"
    led.path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    svc = TreeServices(fs, fs, arch, "beta")
    with RunLock(arch):
        s = resolve_tree(led, svc, [couple().key], execute=True)
    assert statuses(s) == ["resolved"] and led.blocked_keys("beta") == {}


def test_resolve_present_records_created(tmp_path):
    fs = FakeFS()
    fs.write_error = 500
    run(tmp_path, fs, [couple()])
    fs.couples["Q"] = frozenset((A, B))
    arch = tmp_path / "arch"
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    with RunLock(arch):
        s = resolve_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key], execute=True)
    assert statuses(s) == ["resolved-created"]
    assert couple().key in led.executed_keys("beta")


def test_resolve_refuses_key_with_id(tmp_path):
    fs = FakeFS()
    fs.apply = False
    run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    s = resolve_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key])
    assert statuses(s) == ["still-blocked"] and "rollback" in s.outcomes[0].reason


def test_resolve_unblocked_key_refused(tmp_path):
    fs = FakeFS()
    run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    with pytest.raises(GateError, match="not blocked"):
        resolve_tree(Ledger(arch / "_import" / "tree-ledger.jsonl"), TreeServices(fs, fs, arch, "beta"),
                     [couple().key])


# ---------------------------------------------------------------------- rollback


def test_rollback_deletes_exactly_ledger_ids(tmp_path):
    fs = FakeFS()
    fs.couples["FOREIGN"] = frozenset((C, D))  # not ours, must survive
    s, led = run(tmp_path, fs, [couple(A, B), child(E, (C, D))])
    assert statuses(s) == ["created", "created"]
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    dry = rollback_tree(led, svc, [], False)
    assert [o.status for o in dry.outcomes] == ["would-rollback"] * 2
    assert [c for c in fs.writes() if c[0].startswith("delete")] == []
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back"] * 2
    deletes = [c for c in fs.writes() if c[0].startswith("delete")]
    assert sorted(deletes) == [("delete_cap", "P2"), ("delete_couple", "R1")]
    assert "FOREIGN" in fs.couples and not fs.cap
    # idempotent: nothing left to roll back
    with RunLock(arch):
        again = rollback_tree(led, svc, [], True)
    assert again.outcomes == []


def test_rollback_failed_absence_readback(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    fs.delete_effect = False
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert rb.stopped and rb.outcomes[-1].status == "rollback-failed"


def test_rollback_failed_readback_key_by_name(tmp_path):
    fs = FakeFS()
    fs.apply = False
    _, led = run(tmp_path, fs, [couple()])
    fs.couples["R1"] = frozenset((A, B))  # the write was actually there
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    with RunLock(arch):
        assert rollback_tree(led, svc, [], True).outcomes == []  # bulk only touches created
        rb = rollback_tree(led, svc, [couple().key], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back"]
    assert led.blocked_keys("beta") == {}


def test_rollback_refuses_unknown_key_and_bad_id(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    with pytest.raises(GateError, match="not in the tree ledger"):
        rollback_tree(led, svc, ["spouse:ZZZZ-001:ZZZZ-002"], False)
    led._append({"type": "result", "intent_id": "x", "key": "child-parents:" + C, "kind": "add-child-parents",
                 "env": "beta", "status": "created", "reason": "", "created": {"child_parents_id": "../x"},
                 "params": {"child": C, "parents": [A]}})
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert "cannot-rollback" in [o.status for o in rb.outcomes]
    assert ("delete_cap", "../x") not in fs.calls


def test_rollback_person_alone_ok_and_foreign_relationship_refused(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [newp()])
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    # someone else links the new person: refuse
    fs.couples["F1"] = frozenset(("NNNN-001", D))
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert rb.outcomes[-1].status == "rollback-failed" and "not created by this ledger" in rb.outcomes[-1].reason
    assert fs.writes()[-1][0] == "create_person"  # nothing deleted
    del fs.couples["F1"]
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back"]
    assert "NNNN-001" not in fs.people


def test_rollback_person_with_our_relationship_requires_relationship_first(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [newp()])
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    # second plan: a couple involving the new person, linked now
    led._append({"type": "result", "intent_id": "c", "key": "spouse:" + D + ":NNNN-001", "kind": "add-couple",
                 "env": "beta", "status": "created", "reason": "", "created": {"couple_id": "RX"},
                 "params": {"a": D, "b": "NNNN-001"}})
    fs.couples["RX"] = frozenset((D, "NNNN-001"))
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back", "rolled-back"]  # newest first: couple, then person
    assert [c[0] for c in fs.writes() if c[0].startswith("delete")] == ["delete_couple", "delete_person"]


def test_rollback_production_gates(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()], env="production")
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    svc = TreeServices(fs, fs, arch, "production")
    with pytest.raises(GateError, match="allow-production"):
        rollback_tree(led, svc, [], False)
    with pytest.raises(GateError, match="--only"):
        rollback_tree(led, svc, [], False, allow_production=True)
