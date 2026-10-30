import dataclasses
import json

import pytest

from familysearch.api.registry import OperationLockedError
from familysearch.auth import Environment
from familysearch.errors import FSHTTPError
from familysearch_import.execute import GateError, Ledger, RunLock
from familysearch_import.linking import FsPerson
from familysearch_import.prod_reader import ReadFailed, Relative
from familysearch_import.treeexec import (
    GuardedTreeWriter, TreeServices, resolve_tree, rollback_tree, run_tree_plan, verify_tree_plan_for_execution,
)
from familysearch_import.treeplan import TreeAction, TreePlan, compute_tree_plan_hash, creation_key, tree_plan_json

A, B, C, D, E, F = "AAAA-001", "BBBB-002", "CCCC-003", "DDDD-004", "EEEE-005", "FFFF-006"
LINKED = [A, B, C, D, E, F]
LINE = "Line A"
FOLDER = {A: "1850-1910 Alpha Testname", B: "1852-1912 Beta Testname", C: "1880-1950 Gamma Testname",
          D: "1882-1952 Delta Testname", E: "1860-1930 Epsilon Testname", F: "1862-1932 Zeta Testname"}


def fk(pid):
    return f"{LINE}/{FOLDER[pid]}"


def _year(body_facts, kind):
    for f in body_facts or []:
        if f["type"].endswith(kind):
            return int(f["date"]["original"])
    return None


class FakeFS:
    """Fictional FamilySearch: persons, couples, child-and-parents relationships."""

    def __init__(self):
        self.people = {p: dict(name=f"Person {p}", living=False, status="ok", birth=None, death=None)
                       for p in LINKED}
        self.couples = {}   # rel id -> frozenset
        self.cap = {}       # rel id -> (child, tuple(parents))
        self.tree = []      # other people in the tree: dicts(id, given, surname, birth, death, living)
        self.search_error = None
        self.search_calls = []
        self.calls = []
        self.n = 0
        self.apply = True            # does a write take effect
        self.leak_to = None          # write also (wrongly) links this person
        self.write_error = None
        self.write_error_after_effect = False
        self.ret_id = None           # override the returned id
        self.delete_effect = True
        self.new_name = None
        self.derive_couple = False   # a child link (wrongly) also creates a NEW couple between the parents
        self.couple_extras = {}      # couple id -> extras dict (facts/sources/notes/changes)
        self.strict_break_after_delete = False
        self.double_write = False
        self.points_elsewhere = False
        self.no_links = set()        # persons whose section links are missing (strict reads fail)
        self.sources = {}
        self.memories = {}
        self.changes = {}            # pid -> [(who, text)]
        self.new_living = False
        self.new_years = True

    # reader
    def get_fs_person(self, pid):
        self.calls.append(("person", pid))
        p = self.people.get(pid)
        if p is None:
            return FsPerson(pid, "", None, None, None, "not-found")
        return FsPerson(pid, p["name"], p["birth"], p["death"], p["living"], p["status"])

    def get_relatives(self, pid, strict=False):
        if strict and pid in self.no_links:
            raise ReadFailed("no section links")
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

    def search_duplicates(self, given, surname, birth, death, alt_surnames=()):
        self.search_calls.append((given, surname, birth, death))
        if self.search_error:
            raise self.search_error
        names = {surname.lower(), *(a.lower() for a in alt_surnames)}
        hits = []
        for t in self.tree:
            if t["surname"].lower() not in names:
                continue
            near = any(y is not None and q is not None and abs(y - q) <= 10
                       for y, q in ((t["birth"], birth), (t["death"], death)))
            if near:
                hits.append(FsPerson(t["id"], f"{t['given']} {t['surname']}", t["birth"], t["death"],
                                     t["living"], "ok"))
        return hits

    def get_couple(self, rid):
        return self.couples.get(rid)

    def get_child_and_parents(self, rid):
        v = self.cap.get(rid)
        return None if v is None else (v[0], frozenset(v[1]))

    def get_couple_ids(self, a, b):
        return [rid for rid, s in self.couples.items() if s == frozenset((a, b))]

    def current_user_ids(self):
        return frozenset({"me"})

    def get_couple_extras(self, rid):
        return self.couple_extras.get(rid, {"facts": 0, "sources": 0, "notes": 0,
                                            "changes": [("me", "create couple")]})

    def get_source_ids(self, pid):
        return list(self.sources.get(pid, []))

    def get_memory_ids(self, pid):
        return list(self.memories.get(pid, []))

    def get_change_summary(self, pid):
        return self.changes.get(pid, [("me", "create person")])

    # writer
    def _id(self, prefix):
        self.n += 1
        return self.ret_id or f"{prefix}{self.n}"

    def _maybe_fail(self):
        if self.write_error and not self.write_error_after_effect:
            raise FSHTTPError(self.write_error, "x")

    def create_couple(self, p1, p2, *, reason):
        self.calls.append(("create_couple", p1, p2))
        self._maybe_fail()
        rid = self._id("R")
        if self.apply:
            self.couples[rid] = frozenset((p1, p2))
            if self.double_write:
                self.couples[rid + "dup"] = frozenset((p1, p2))
        if self.leak_to:
            self.couples[rid + "x"] = frozenset((p1, self.leak_to))
        if self.points_elsewhere:
            self.couples["ROTHER"] = frozenset((E, F))
            return "ROTHER"
        return rid

    def create_parent_child(self, *, child_id, parent_ids, reason):
        self.calls.append(("create_parent_child", child_id, tuple(parent_ids)))
        self._maybe_fail()
        rid = self._id("P")
        if self.apply:
            self.cap[rid] = (child_id, tuple(parent_ids))
            if self.derive_couple and len(parent_ids) == 2:
                self.couples[f"D{self.n}"] = frozenset(parent_ids)
        if self.leak_to:
            self.cap[rid + "x"] = (self.leak_to, (parent_ids[0],))
        return rid

    def create_person(self, person, *, reason):
        self.calls.append(("create_person", json.dumps(person, sort_keys=True)))
        self.n += 1
        rid = self.ret_id or f"NNNN-{self.n:03d}"
        if self.apply or self.write_error_after_effect:
            name = self.new_name or person["names"][0]["nameForms"][0]["fullText"]
            facts = person.get("facts")
            self.people[rid] = dict(name=name, living=self.new_living, status="ok",
                                    birth=_year(facts, "Birth") if self.new_years else None,
                                    death=_year(facts, "Death") if self.new_years else None)
        if self.write_error:
            raise FSHTTPError(self.write_error, "x")
        return rid

    def delete_couple(self, rid, *, reason):
        self.calls.append(("delete_couple", rid))
        if self.delete_effect:
            pair = self.couples.pop(rid, None)
            if self.strict_break_after_delete and pair:
                self.no_links.update(pair)

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
    return TreeAction("add-couple", f"spouse:{x}:{y}",
                      {"a": x, "b": y, "a_folder": fk(x), "b_folder": fk(y)}, "planned", "r")


def child(c=C, parents=(A, B)):
    ps = sorted(parents)
    return TreeAction("add-child-parents", f"child-parents:{c}",
                      {"child": c, "child_folder": fk(c), "parents": ps,
                       "parent_folders": [fk(p) for p in ps]}, "planned", "r")


def newp(name="Zed Testname", birth=1850, death=1910):
    return TreeAction("create-person", creation_key(name, birth, death),
                      {"name": name, "birth": birth, "death": death, "gender": "Unknown"}, "planned", "r")


def approved_for(actions):
    rel, new = [], []
    for a in actions:
        if a.kind == "add-couple":
            rel.append(a.key)
        elif a.kind == "add-child-parents":
            rel += [f"parent-child:{p}:{a.params['child']}" for p in a.params["parents"]]
        else:
            new.append(a.key)
    return rel, new


def make(tmp_path, actions, env="beta", approve=True, links=LINKED):
    arch = tmp_path / "arch"
    imp = arch / "_import"
    imp.mkdir(parents=True, exist_ok=True)
    for pid in LINKED:  # archive folders: deceased by the years in the folder name
        (arch / LINE / FOLDER[pid]).mkdir(parents=True, exist_ok=True)
    plan = TreePlan(env, tuple(actions), compute_tree_plan_hash(actions, env))
    path = imp / "TreePlan.json"
    path.write_text(json.dumps(tree_plan_json(plan)))
    (imp / "links.json").write_text(json.dumps({"people": [
        {"line": LINE, "folder": FOLDER[x], "verdict": "confirmed", "chosen_id": x} for x in links]}))
    rel, new = approved_for(actions)
    appr = {"approved_relationships": rel, "approved_creations": new}
    if approve:
        appr["approved_tree_plan_hash"] = plan.plan_hash
    (imp / "approvals.json").write_text(json.dumps(appr))
    return arch, path, plan


def pf_for(tmp_path, actions, env="beta", **kw):
    arch, path, plan = make(tmp_path, actions, env, **kw)
    return arch, verify_tree_plan_for_execution(arch, path, plan.plan_hash, env), plan


def seed_parent_couples(fs, actions):
    """Two-parent child links need the parents to be a couple already (unless an add-couple precedes)."""
    for a in actions:
        if a.kind == "add-child-parents" and len(a.params["parents"]) == 2:
            x, y = a.params["parents"]
            if not any(c.kind == "add-couple" and c.key == f"spouse:{x}:{y}" for c in actions) \
                    and not fs.get_couple_ids(x, y):
                fs.couples[f"PRE{len(fs.couples)}"] = frozenset((x, y))


def run(tmp_path, fs, actions, env="beta", execute=True, seed=True, **kw):
    if seed:
        seed_parent_couples(fs, actions)
    arch, pf, _ = pf_for(tmp_path, actions, env)
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    svc = TreeServices(reader=fs, writer=fs, archive=arch, env=env)
    if execute:
        with RunLock(arch):
            return run_tree_plan(pf, svc, led, execute=True, **kw), led
    return run_tree_plan(pf, svc, led, execute=False, **kw), led


def statuses(summary):
    return [o.status for o in summary.outcomes]


def led_of(tmp_path):
    return Ledger(tmp_path / "arch" / "_import" / "tree-ledger.jsonl")


def age_intents(led):
    lines = [json.loads(x) for x in led.path.read_text().splitlines()]
    for x in lines:
        if x["type"] == "intent":
            x["ts"] = "2020-01-01T00:00:00Z"
    led.path.write_text("".join(json.dumps(x) + "\n" for x in lines))


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
    data = json.loads((arch / "_import" / "approvals.json").read_text())
    data["approved_tree_plan_hash"] = "1" * 64
    (arch / "_import" / "approvals.json").write_text(json.dumps(data))
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


def test_verify_rejects_missing_folder_keys(tmp_path):
    a = couple()
    bad = TreeAction(a.kind, a.key, {"a": A, "b": B}, "planned", "r")
    arch, path, plan = make(tmp_path, [bad])
    with pytest.raises(GateError, match="folder"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_rejects_living_creation(tmp_path):
    arch, path, plan = make(tmp_path, [newp(birth=1950, death=None)])
    with pytest.raises(GateError, match="deceased"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_rejects_old_style_creation_key(tmp_path):
    n = newp()
    old = TreeAction(n.kind, "create-person:0123456789abcdef", n.params, "planned", "r")
    arch, path, plan = make(tmp_path, [old])
    with pytest.raises(GateError, match="key does not match"):
        verify_tree_plan_for_execution(arch, path, plan.plan_hash, "beta")


def test_verify_rechecks_per_item_approvals(tmp_path):  # minor 4
    arch, path, plan = make(tmp_path, [couple()])
    data = json.loads((arch / "_import" / "approvals.json").read_text())
    data["approved_relationships"] = []
    (arch / "_import" / "approvals.json").write_text(json.dumps(data))
    with pytest.raises(GateError, match="no longer in approvals"):
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


def test_executor_runs_couples_before_children_before_people(tmp_path):  # M8 ordering
    fs = FakeFS()
    s, _ = run(tmp_path, fs, [newp(), child(E, (C, D)), couple(A, B)], execute=False)
    assert [o.kind for o in s.outcomes] == ["add-couple", "add-child-parents", "create-person"]


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
    assert len(fs.search_calls) == 1
    body = json.loads(next(c for c in fs.calls if c[0] == "create_person")[1])
    assert body["gender"]["type"].endswith("Unknown") and body["living"] is False
    assert led.records()[-1]["created"] == {"person_id": "NNNN-001"}


def test_rerun_is_noop(tmp_path):
    fs = FakeFS()
    run(tmp_path, fs, [couple()])
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
    (lambda fs: fs.cap.update(X=(A, (B,))), "contradictory"),
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
    assert statuses(s) == ["skipped"] and "link" in s.outcomes[0].reason
    assert fs.writes() == []


def test_link_checked_per_folder_not_pooled(tmp_path):  # M4 at execution time
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    imp = arch / "_import"
    data = json.loads((imp / "approvals.json").read_text())
    data["approved_links"] = {fk(A): "XXXX-009"}  # folder A re-pointed; AAAA-001 is still linked elsewhere? no
    (imp / "approvals.json").write_text(json.dumps(data))
    s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), Ledger(imp / "tree-ledger.jsonl"))
    assert statuses(s) == ["skipped"] and "link changed" in s.outcomes[0].reason
    assert fs.writes() == []


def test_archive_status_living_blocks_write(tmp_path):  # minor 3
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    folder = arch / LINE / FOLDER[A]
    (folder / "ImportRecord.json").write_text(json.dumps({"status": "living", "reviewed": True}))
    s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), Ledger(arch / "_import" / "tree-ledger.jsonl"))
    assert statuses(s) == ["skipped"]
    assert "deceased" in s.outcomes[0].reason or "ImportRecord" in s.outcomes[0].reason
    assert fs.writes() == []


def test_archive_folder_missing_blocks_write(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    import shutil
    shutil.rmtree(arch / LINE / FOLDER[B])
    s = run_tree_plan(pf, TreeServices(fs, fs, arch, "beta"), Ledger(arch / "_import" / "tree-ledger.jsonl"))
    assert statuses(s) == ["skipped"] and "not found in archive" in s.outcomes[0].reason


def test_couple_refused_without_negative_control(tmp_path):
    fs2 = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()], links=[A, B])
    s2 = run_tree_plan(pf, TreeServices(fs2, fs2, arch, "beta"), Ledger(arch / "_import" / "tree-ledger.jsonl"))
    assert statuses(s2) == ["skipped"] and "negative control" in s2.outcomes[0].reason


def test_living_control_people_are_not_used(tmp_path):
    fs = FakeFS()
    for pid in (C, D, E, F):
        fs.people[pid]["living"] = True
    s, _ = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["skipped"] and "negative control" in s.outcomes[0].reason


@pytest.mark.parametrize("mutate,reason", [
    (lambda fs: fs.cap.update(X=(C, (D,))), "already has a parent"),
    (lambda fs: fs.people[C].update(living=True), "living"),
    (lambda fs: fs.people[A].update(living=True), "living"),
    (lambda fs: fs.people[B].update(status="deleted"), "deleted"),
    (lambda fs: fs.couples.update(X=frozenset((C, A))), "contradictory"),
])
def test_child_prechecks(tmp_path, mutate, reason):
    fs = FakeFS()
    mutate(fs)
    s, _ = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["skipped"] and reason in s.outcomes[0].reason
    assert fs.writes() == []


def test_strict_read_failure_stops_without_write(tmp_path):  # M2: missing section links fail closed
    fs = FakeFS()
    fs.no_links = {A}
    s, _ = run(tmp_path, fs, [couple()])
    assert s.stopped and fs.writes() == []


def test_read_failure_stops_without_write(tmp_path):
    fs = FakeFS()

    def boom(pid, strict=False):
        raise ReadFailed("boom")
    fs.get_relatives = boom
    s, _ = run(tmp_path, fs, [couple()])
    assert s.stopped and fs.writes() == []


# ------------------------------------------- create-person duplicate search (B1)


def tree_person(pid="MMMM-111", given="Zed", surname="Testname", birth=1850, death=1910, living=False):
    return dict(id=pid, given=given, surname=surname, birth=birth, death=death, living=living)


def test_create_person_refuses_plausible_match(tmp_path):
    fs = FakeFS()
    fs.tree = [tree_person()]
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["skipped"] and "plausible match MMMM-111" in s.outcomes[0].reason
    assert fs.writes() == [] and not led.path.exists()


def test_search_results_depend_on_the_query(tmp_path):
    fs = FakeFS()
    fs.tree = [tree_person(surname="Otherfamily")]  # different surname: no hit
    s, _ = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["created"]
    assert fs.search_calls == [("Zed", "Testname", 1850, 1910)]


def test_birth_year_off_by_four_still_a_match(tmp_path):
    fs = FakeFS()
    fs.tree = [tree_person(given="Johann", birth=1846, death=None)]  # other given name, birth off by 4
    s, _ = run(tmp_path, fs, [newp(birth=1850, death=None)])
    assert statuses(s) == ["skipped"] and "plausible match" in s.outcomes[0].reason


def test_year_outside_range_is_not_a_match(tmp_path):
    fs = FakeFS()
    fs.tree = [tree_person(birth=1800, death=1860)]
    s, _ = run(tmp_path, fs, [newp(birth=1850, death=1910)])
    assert statuses(s) == ["created"]


@pytest.mark.parametrize("name", ["Mary", "Mrs. Smith", "J. Smith", "Johann (Hans)"])
def test_single_token_or_initial_names_are_not_created(tmp_path, name):
    fs = FakeFS()
    s, led = run(tmp_path, fs, [newp(name=name)])
    assert statuses(s) == ["skipped"] and "given name and a surname" in s.outcomes[0].reason
    assert fs.writes() == [] and fs.search_calls == []


def test_duplicate_search_error_skips(tmp_path):
    fs = FakeFS()
    fs.search_error = ReadFailed("boom")
    s, _ = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["skipped"] and "search failed" in s.outcomes[0].reason
    assert fs.writes() == []


def test_living_hit_ids_are_not_printed(tmp_path):
    fs = FakeFS()
    fs.tree = [tree_person("LIVE-001", living=True), tree_person("DEAD-001")]
    s, _ = run(tmp_path, fs, [newp()])
    reason = s.outcomes[0].reason
    assert "DEAD-001" in reason and "LIVE-001" not in reason and "living" in reason


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


def test_double_couple_write_is_caught(tmp_path):  # M1
    fs = FakeFS()
    fs.double_write = True
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"] and "more than one couple" in s.outcomes[0].reason


def test_leak_to_non_control_person_is_caught_by_subject_diff(tmp_path):  # M1/M2
    fs = FakeFS()
    fs.leak_to = E  # controls are C and D; the subject diff must catch it
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"]
    assert led.blocked_keys("beta")


def test_control_full_relationship_set_is_compared(tmp_path):  # M2
    fs = FakeFS()
    orig = fs.create_couple

    def sneaky(p1, p2, *, reason):
        rid = orig(p1, p2, reason=reason)
        fs.couples["SNEAK"] = frozenset((C, F))  # control C gains a link to a third person
        return rid
    fs.create_couple = sneaky
    s, _ = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"] and "control" in s.outcomes[0].reason


def test_child_extra_spouse_link_to_parent_fails_readback(tmp_path):  # M1: beta's ParentChild failure
    fs = FakeFS()
    orig = fs.create_parent_child

    def extra(**kw):
        rid = orig(**kw)
        fs.couples["BAD"] = frozenset((kw["child_id"], kw["parent_ids"][0]))
        return rid
    fs.create_parent_child = extra
    s, _ = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["failed-readback"]


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


def test_returned_id_must_name_the_planned_people(tmp_path):  # M6
    fs = FakeFS()
    fs.points_elsewhere = True
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"] and "does not name exactly" in s.outcomes[0].reason


def test_two_parent_child_requires_parents_to_be_a_couple(tmp_path):  # N-M1 simplification
    fs = FakeFS()
    s, led = run(tmp_path, fs, [child(C, (A, B))], seed=False)
    assert statuses(s) == ["skipped"] and "not yet a couple; approve add-couple first" in s.outcomes[0].reason
    assert fs.writes() == [] and not led.path.exists()


def test_child_link_runs_after_an_add_couple_in_the_same_run(tmp_path):
    fs = FakeFS()
    s, _ = run(tmp_path, fs, [child(C, (A, B)), couple(A, B)], seed=False)
    assert statuses(s) == ["created", "created"]
    assert [c[0] for c in fs.writes()] == ["create_couple", "create_parent_child"]


def test_dry_run_assumes_the_planned_couple_exists(tmp_path):
    fs = FakeFS()
    s, _ = run(tmp_path, fs, [child(C, (A, B)), couple(A, B)], seed=False, execute=False)
    assert statuses(s) == ["would-create", "would-create"]


def test_new_couple_between_parents_after_child_write_fails_readback(tmp_path):  # kept assertion
    fs = FakeFS()
    fs.derive_couple = True
    s, led = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["failed-readback"] and "new couple" in s.outcomes[0].reason
    assert led.records()[-1]["created"] == {"child_parents_id": "P1"}
    assert led.blocked_keys("beta")


def test_single_parent_child_needs_no_couple(tmp_path):
    fs = FakeFS()
    s, _ = run(tmp_path, fs, [child(C, (A,))], seed=False)
    assert statuses(s) == ["created"]


def test_person_name_mismatch_fails_readback(tmp_path):
    fs = FakeFS()
    fs.new_name = "Someone Else"
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["failed-readback"]
    assert led.records()[-1]["created"] == {"person_id": "NNNN-001"}


def test_new_person_with_unknown_living_status_fails_readback(tmp_path):  # minor 1
    fs = FakeFS()
    fs.new_living = None
    s, _ = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["failed-readback"] and "deceased" in s.outcomes[0].reason


def test_new_person_years_must_be_stored(tmp_path):  # minor 1
    fs = FakeFS()
    fs.new_years = False
    s, _ = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["failed-readback"] and "year" in s.outcomes[0].reason


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
    orig = fs.get_relatives

    def rels(pid, strict=False):
        if fs.couples:  # pre-check reads succeed; interrupt during read-back
            raise KeyboardInterrupt
        return orig(pid, strict)
    fs.get_relatives = rels
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, fs, [couple()])
    last = led_of(tmp_path).records()[-1]
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


def test_create_person_timeout_is_ambiguous_and_cannot_be_auto_resolved(tmp_path):  # B2
    fs = FakeFS()
    fs.write_error, fs.write_error_after_effect = 504, True  # the person WAS created, then the POST timed out
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["ambiguous"] and s.stopped
    age_intents(led)
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    key = newp().key
    with RunLock(arch):
        r = resolve_tree(led, svc, [key], execute=True)
    assert statuses(r) == ["still-blocked"] and "--confirm-absent" in r.outcomes[0].reason
    assert led.blocked_keys("beta") == {key: "ambiguous"}
    # and a rerun is still blocked: no second person is created
    fs.write_error = None
    s2, _ = run(tmp_path, fs, [newp()])
    assert s2.stopped and len([c for c in fs.calls if c[0] == "create_person"]) == 1


def test_confirm_absent_lifts_a_create_person_block(tmp_path):
    fs = FakeFS()
    fs.write_error = 504
    s, led = run(tmp_path, fs, [newp()])
    assert statuses(s) == ["ambiguous"]
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    key = newp().key
    with RunLock(arch):
        with pytest.raises(GateError, match="also be given as --key"):
            resolve_tree(led, svc, [key], execute=True, confirm_absent=["create-person:other"])
        r = resolve_tree(led, svc, [key], execute=True, confirm_absent=[key])
    assert statuses(r) == ["resolved"] and led.blocked_keys("beta") == {}


def test_confirm_absent_only_for_create_person(tmp_path):
    fs = FakeFS()
    fs.write_error = 500
    run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    with pytest.raises(GateError, match="create-person keys only"):
        resolve_tree(led_of(tmp_path), TreeServices(fs, fs, arch, "beta"), [couple().key],
                     confirm_absent=[couple().key])


def test_resolve_absent_couple_lifts_block(tmp_path):
    fs = FakeFS()
    fs.write_error = 500
    _, led = run(tmp_path, fs, [couple()])
    age_intents(led)
    arch = tmp_path / "arch"
    with RunLock(arch):
        s = resolve_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key], execute=True)
    assert statuses(s) == ["resolved"] and led.blocked_keys("beta") == {}


def test_resolve_present_couple_is_not_recorded_as_ours(tmp_path):  # N minor 4
    fs = FakeFS()
    fs.write_error = 500
    _, led = run(tmp_path, fs, [couple()])
    fs.couples["Q"] = frozenset((A, B))  # exists, but nothing proves we made it
    arch = tmp_path / "arch"
    with RunLock(arch):
        s = resolve_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key], execute=True)
    assert statuses(s) == ["still-blocked"] and "not recorded as ours" in s.outcomes[0].reason
    assert couple().key not in led.executed_keys("beta")
    assert couple().key in led.blocked_keys("beta")


def test_resolve_child_requires_exact_parent_set(tmp_path):  # minor 6
    fs = FakeFS()
    fs.write_error = 500
    _, led = run(tmp_path, fs, [child(C, (A, B))])
    fs.cap["Q"] = (C, (A,))  # someone else linked only one parent
    age_intents(led)
    arch = tmp_path / "arch"
    s = resolve_tree(led, TreeServices(fs, fs, arch, "beta"), [child(C, (A, B)).key])
    assert statuses(s) == ["would-resolve"]  # not "would-resolve-created"


def test_resolve_refuses_key_with_id(tmp_path):
    fs = FakeFS()
    fs.apply = False
    run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    s = resolve_tree(led_of(tmp_path), TreeServices(fs, fs, arch, "beta"), [couple().key])
    assert statuses(s) == ["still-blocked"] and "rollback" in s.outcomes[0].reason


def test_resolve_unblocked_key_refused(tmp_path):
    fs = FakeFS()
    run(tmp_path, fs, [couple()])
    arch = tmp_path / "arch"
    with pytest.raises(GateError, match="not blocked"):
        resolve_tree(led_of(tmp_path), TreeServices(fs, fs, arch, "beta"), [couple().key])


# ---------------------------------------------------------------------- rollback


def test_rollback_deletes_exactly_ledger_ids(tmp_path):
    fs = FakeFS()
    fs.couples["FOREIGN"] = frozenset((C, D))  # not ours (the parents' existing couple), must survive
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


def test_rollback_checks_the_recorded_id_before_deleting(tmp_path):  # M6
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    # the ledger's id now names a different couple (C, D); A and B are still spouses via R1
    led._append({"type": "result", "intent_id": "y", "key": "spouse:" + A + ":" + B + "x", "kind": "add-couple",
                 "env": "beta", "status": "created", "reason": "", "created": {"couple_id": "RX"},
                 "params": {"a": A, "b": B}})
    fs.couples["RX"] = frozenset((C, D))
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    bad = [o for o in rb.outcomes if o.status == "rollback-failed"]
    assert bad and "does not name this couple" in bad[0].reason
    assert ("delete_couple", "RX") not in fs.calls


def test_rollback_person_alone_ok_and_foreign_relationship_refused(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [newp()])
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    fs.couples["F1"] = frozenset(("NNNN-001", D))
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert rb.outcomes[-1].status == "rollback-failed" and "not created by this ledger" in rb.outcomes[-1].reason
    assert fs.writes()[-1][0] == "create_person"
    del fs.couples["F1"]
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back"]
    assert "NNNN-001" not in fs.people


@pytest.mark.parametrize("setup,reason", [
    (lambda fs: fs.sources.update({"NNNN-001": ["S1"]}), "sources or memories"),
    (lambda fs: fs.memories.update({"NNNN-001": ["M1"]}), "sources or memories"),
    (lambda fs: fs.changes.update({"NNNN-001": [("me", "create"), ("someone-else", "add fact")]}), "contributors"),
    (lambda fs: fs.changes.update({"NNNN-001": [("me", "create"), ("me", "merge persons")]}), "merge"),
    (lambda fs: fs.changes.update({"NNNN-001": []}), "contributors"),
    (lambda fs: fs.people["NNNN-001"].update(name="Renamed Person"), "name changed"),
])
def test_rollback_person_refused_when_others_added_data(tmp_path, setup, reason):  # M7
    fs = FakeFS()
    _, led = run(tmp_path, fs, [newp()])
    setup(fs)
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert rb.outcomes[-1].status == "rollback-failed" and reason in rb.outcomes[-1].reason
    assert not [c for c in fs.calls if c[0] == "delete_person"]


def test_rollback_person_with_our_relationship_requires_relationship_first(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [newp()])
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    led._append({"type": "result", "intent_id": "c", "key": "spouse:" + D + ":NNNN-001", "kind": "add-couple",
                 "env": "beta", "status": "created", "reason": "", "created": {"couple_id": "RX"},
                 "params": {"a": D, "b": "NNNN-001"}})
    fs.couples["RX"] = frozenset((D, "NNNN-001"))
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back", "rolled-back"]
    assert [c[0] for c in fs.writes() if c[0].startswith("delete")] == ["delete_couple", "delete_person"]


def test_rollback_of_progress_only_intent_then_rerun_is_not_stuck(tmp_path):  # minor 2
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    fs.couples["R9"] = frozenset((A, B))
    led._append({"type": "intent", "intent_id": "i1", "ts": "2020-01-01T00:00:00Z", "key": couple().key,
                 "kind": "add-couple", "env": "beta", "params": couple().params})
    led._append({"type": "progress", "intent_id": "i1", "key": couple().key, "env": "beta",
                 "created": {"couple_id": "R9"}})
    svc = TreeServices(fs, fs, arch, "beta")
    with RunLock(arch):
        rb = rollback_tree(led, svc, [couple().key], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back"]
    with RunLock(arch):
        s = run_tree_plan(pf, svc, led, execute=True)
    assert not s.stopped and statuses(s) == ["created"]  # not re-blocked by reconcile
    assert led.blocked_keys("beta") == {}


def test_rollback_production_gates(tmp_path):
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()], env="production")
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    svc = TreeServices(fs, fs, arch, "production")
    with pytest.raises(GateError, match="allow-production"):
        rollback_tree(led, svc, [], False)
    with pytest.raises(GateError, match="--only"):
        rollback_tree(led, svc, [], False, allow_production=True)


# ------------------------------------------------------- round 2 regressions


def test_couple_rollback_refused_when_a_child_link_names_both(tmp_path):  # N-M2
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    fs.cap["THEIRS"] = (E, (A, B))  # someone else's child link depends on the couple
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert rb.outcomes[-1].status == "rollback-failed" and "names both people as parents" in rb.outcomes[-1].reason
    assert not [c for c in fs.calls if c[0] == "delete_couple"]


@pytest.mark.parametrize("extras,reason", [
    ({"facts": 1, "sources": 0, "notes": 0, "changes": [("me", "create")]}, "facts, sources or notes"),
    ({"facts": 0, "sources": 1, "notes": 0, "changes": [("me", "create")]}, "facts, sources or notes"),
    ({"facts": 0, "sources": 0, "notes": 0, "changes": [("me", "create"), ("them", "edit")]}, "other contributors"),
    ({"facts": 0, "sources": 0, "notes": 0, "changes": []}, "other contributors"),
    ({"facts": 0, "sources": 0, "notes": 0, "changes": [("me", "merge couples")]}, "merge"),
])
def test_couple_rollback_refused_when_others_added_data(tmp_path, extras, reason):  # N-M2
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    fs.couple_extras["R1"] = extras
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert rb.outcomes[-1].status == "rollback-failed" and reason in rb.outcomes[-1].reason
    assert not [c for c in fs.calls if c[0] == "delete_couple"]


def test_couple_and_our_child_link_roll_back_together_in_dry_run_and_for_real(tmp_path):
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple(A, B), child(C, (A, B))], seed=False)
    arch = tmp_path / "arch"
    svc = TreeServices(fs, fs, arch, "beta")
    dry = rollback_tree(led, svc, [], False)
    assert [o.status for o in dry.outcomes] == ["would-rollback", "would-rollback"]
    with RunLock(arch):
        rb = rollback_tree(led, svc, [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back", "rolled-back"]
    assert [c[0] for c in fs.writes() if c[0].startswith("delete")] == ["delete_cap", "delete_couple"]


def test_person_rollback_compares_contributor_to_current_user(tmp_path):  # minor 3
    fs = FakeFS()
    _, led = run(tmp_path, fs, [newp()])
    fs.changes["NNNN-001"] = [("someone-else", "create person")]  # one contributor, but not us
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert rb.outcomes[-1].status == "rollback-failed" and "contributors" in rb.outcomes[-1].reason
    assert "someone-else" not in rb.outcomes[-1].reason and "me" not in rb.outcomes[-1].reason.split()


def test_incomplete_rollback_keeps_the_key_blocked(tmp_path):  # N-M3
    fs = FakeFS()
    fs.apply = False
    _, led = run(tmp_path, fs, [child(C, (A,))])
    fs.cap["P1"] = (C, (A,))
    # the failed key also carries a second recorded id that cannot be rolled back
    led._append({"type": "result", "intent_id": "z", "key": child(C, (A,)).key, "kind": "add-child-parents",
                 "env": "beta", "status": "partial", "reason": "x", "created": {"couple_id": "ZZ9"},
                 "params": child(C, (A,)).params})
    arch = tmp_path / "arch"
    with RunLock(arch):
        rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [child(C, (A,)).key], True)
    from familysearch_import.treeexec import _tree_blocked
    assert _tree_blocked(led, "beta") == {child(C, (A,)).key: "rollback-incomplete"}
    n = len(fs.writes())
    s2, _ = run(tmp_path, fs, [child(C, (A,))])
    assert s2.stopped and len(fs.writes()) == n


def test_rollback_fully_done_lifts_the_block(tmp_path):
    fs = FakeFS()
    fs.apply = False
    _, led = run(tmp_path, fs, [couple()])
    fs.couples["R1"] = frozenset((A, B))
    arch = tmp_path / "arch"
    from familysearch_import.treeexec import _tree_blocked
    with RunLock(arch):
        rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key], True)
    assert _tree_blocked(led, "beta") == {}


def test_strict_read_failing_after_a_successful_delete_still_records_rolled_back(tmp_path):  # minor 9
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    fs.strict_break_after_delete = True
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back"] and "unavailable" in rb.outcomes[0].reason
    assert not rb.stopped and led.blocked_keys("beta") == {}


def test_production_execute_requires_the_guarded_writer(tmp_path):  # minor 5
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()], env="production")
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    svc = TreeServices(fs, fs, arch, "production")
    with RunLock(arch):
        with pytest.raises(GateError, match="GuardedTreeWriter"):
            run_tree_plan(pf, svc, led, only=[couple().key], execute=True, allow_production=True)
        with pytest.raises(GateError, match="GuardedTreeWriter"):
            rollback_tree(led, svc, [], True, only=[couple().key], allow_production=True)
    assert fs.calls == []
    guarded = TreeServices(fs, GuardedTreeWriter(fs, Environment.PRODUCTION, True), arch, "production")
    with RunLock(arch):
        s = run_tree_plan(pf, guarded, led, only=[couple().key], execute=True, allow_production=True)
    assert statuses(s) == ["created"]


def test_confirm_absent_prints_current_search_hits_deceased_ids_only(tmp_path):  # minor 7
    fs = FakeFS()
    fs.write_error = 504
    _, led = run(tmp_path, fs, [newp()])
    fs.tree = [tree_person("DEAD-001"), tree_person("LIVE-001", living=True)]
    arch = tmp_path / "arch"
    key = newp().key
    with RunLock(arch):
        r = resolve_tree(led, TreeServices(fs, fs, arch, "beta"), [key], execute=True, confirm_absent=[key])
    text = r.outcomes[0].reason
    assert statuses(r) == ["resolved"] and "DEAD-001" in text and "LIVE-001" not in text


def test_parenthesised_surname_is_searched_too(tmp_path):  # minor 1
    fs = FakeFS()
    fs.tree = [tree_person("MMMM-222", given="Elizabeth", surname="Vaganis", birth=1851)]
    s, _ = run(tmp_path, fs, [newp(name="Elizabeth (Vaganis) McDonough", birth=1850, death=None)])
    assert statuses(s) == ["skipped"] and "MMMM-222" in s.outcomes[0].reason


# ------------------------------------------------------- round 3 regressions


def _resolve(tmp_path, fs, led, keys, gone=(), execute=True):
    arch = tmp_path / "arch"
    with RunLock(arch):
        return resolve_tree(led, TreeServices(fs, fs, arch, "beta"), list(keys), execute=execute,
                            confirm_gone=list(gone))


def test_confirm_gone_lifts_a_key_whose_id_is_unreadable(tmp_path):  # MAJOR 1, scenario A
    fs = FakeFS()
    fs.apply = False  # the id came back but the object never became readable
    s, led = run(tmp_path, fs, [couple()])
    assert statuses(s) == ["failed-readback"]
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key], True)
    assert rb.outcomes[-1].status == "rollback-failed"
    plain = _resolve(tmp_path, fs, led, [couple().key], execute=False)
    assert statuses(plain) == ["still-blocked"]  # no exit without the new flag
    r = _resolve(tmp_path, fs, led, [couple().key], gone=[couple().key])
    assert statuses(r) == ["resolved"] and "couple_id R1: absent" in r.outcomes[0].reason
    assert led.blocked_keys("beta") == {}
    from familysearch_import.treeexec import _tree_blocked
    assert _tree_blocked(led, "beta") == {}
    assert any(x.get("reason") == "confirmed gone by operator" for x in led.records() if x["type"] == "rollback")


def test_confirm_gone_after_a_deleted_then_unreadable_rollback(tmp_path):  # scenario B/C
    fs = FakeFS()
    fs.apply = False
    _, led = run(tmp_path, fs, [couple()])
    fs.couples.pop("R1", None)  # nothing there (or removed by hand on the website)
    r = _resolve(tmp_path, fs, led, [couple().key], gone=[couple().key], execute=False)
    assert statuses(r) == ["would-resolve"]


def test_confirm_gone_refuses_when_the_id_is_still_there(tmp_path):
    fs = FakeFS()
    fs.apply = False
    _, led = run(tmp_path, fs, [couple()])
    fs.couples["R1"] = frozenset((A, B))
    r = _resolve(tmp_path, fs, led, [couple().key], gone=[couple().key])
    assert statuses(r) == ["still-blocked"] and "present" in r.outcomes[0].reason
    assert couple().key in led.blocked_keys("beta")


def test_confirm_gone_does_not_count_a_merge_redirect_as_gone(tmp_path):
    fs = FakeFS()
    fs.new_name = None
    fs.write_error = None
    fs.new_living = None  # read-back fails: the id stays recorded
    _, led = run(tmp_path, fs, [newp()])
    fs.people["NNNN-001"]["status"] = "redirected"
    r = _resolve(tmp_path, fs, led, [newp().key], gone=[newp().key])
    assert statuses(r) == ["still-blocked"] and "redirected" in r.outcomes[0].reason
    fs.people["NNNN-001"]["status"] = "deleted"
    r = _resolve(tmp_path, fs, led, [newp().key], gone=[newp().key])
    assert statuses(r) == ["resolved"]


def test_confirm_gone_key_must_also_be_a_key(tmp_path):
    fs = FakeFS()
    fs.apply = False
    _, led = run(tmp_path, fs, [couple()])
    with pytest.raises(GateError, match="also be given as --key"):
        _resolve(tmp_path, fs, led, [couple().key], gone=["spouse:ZZZZ-001:ZZZZ-002"])


def test_child_link_skipped_when_its_parents_couple_write_is_unresolved(tmp_path):  # MAJOR 3
    fs = FakeFS()
    fs.double_write = True
    s, led = run(tmp_path, fs, [couple(A, B), child(C, (A, B))], seed=False)
    assert statuses(s) == ["failed-readback"]
    n = len(fs.writes())
    s2, _ = run(tmp_path, fs, [couple(A, B), child(C, (A, B))], seed=False, only=[child(C, (A, B)).key])
    assert statuses(s2) == ["skipped"] and "blocked key" in s2.outcomes[0].reason
    assert len(fs.writes()) == n
    s3, _ = run(tmp_path, fs, [couple(A, B), child(C, (A, B))], seed=False, only=[child(C, (A, B)).key],
                execute=False)
    assert statuses(s3) == ["skipped"]  # dry run refuses too


def test_child_link_skipped_when_a_blocked_key_involves_a_parent(tmp_path):
    fs = FakeFS()
    fs.write_error = 500
    run(tmp_path, fs, [child(E, (A,))], seed=False)  # blocked: E's link to A failed
    fs.write_error = None
    s, _ = run(tmp_path, fs, [child(C, (A,)), child(E, (A,))], seed=False, only=[child(C, (A,)).key])
    assert statuses(s) == ["skipped"] and "blocked key" in s.outcomes[0].reason
    assert fs.writes() == [("create_parent_child", E, (A,))]


def test_couple_rollback_child_guard_reads_strictly(tmp_path):  # minor 1
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    fs.cap["X"] = (E, (A,))
    fs.no_links.add(E)  # a child whose parents section would read as empty
    arch = tmp_path / "arch"
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert rb.outcomes[-1].status == "rollback-failed"
    assert not [c for c in fs.calls if c[0] == "delete_couple"]


def test_unreadable_couple_id_list_refuses_a_child_link(tmp_path):  # minor 2
    fs = FakeFS()
    fs.couples["PRE"] = frozenset((A, B))
    fs.get_couple_ids = lambda a, b: []
    s, _ = run(tmp_path, fs, [child(C, (A, B))], seed=False)
    assert statuses(s) == ["skipped"] and "could not be read" in s.outcomes[0].reason


def test_new_couple_ids_are_named_in_the_readback_failure(tmp_path):  # minor 2
    fs = FakeFS()
    fs.derive_couple = True
    s, _ = run(tmp_path, fs, [child(C, (A, B))])
    assert statuses(s) == ["failed-readback"] and "new couple id(s): D" in s.outcomes[0].reason


def test_legacy_derived_couple_id_can_still_be_rolled_back(tmp_path):  # minor 3
    fs = FakeFS()
    fs.cap["P1"] = (C, (A, B))
    fs.couples["D1"] = frozenset((A, B))
    arch, pf, _ = pf_for(tmp_path, [child(C, (A, B))])
    led = Ledger(arch / "_import" / "tree-ledger.jsonl")
    led._append({"type": "result", "intent_id": "i", "key": child(C, (A, B)).key, "kind": "add-child-parents",
                 "env": "beta", "status": "created", "reason": "", "created": {"child_parents_id": "P1",
                 "derived_couple_id": "D1"}, "params": child(C, (A, B)).params})
    with RunLock(arch):
        rb = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [], True)
    assert [o.status for o in rb.outcomes] == ["rolled-back", "rolled-back"]
    assert [c[0] for c in fs.writes()] == ["delete_cap", "delete_couple"]


def test_dry_run_only_ignores_children_linked_to_exactly_this_pair(tmp_path):  # minor 4
    fs = FakeFS()
    _, led = run(tmp_path, fs, [couple()])
    led._append({"type": "result", "intent_id": "x", "key": "child-parents:" + E, "kind": "add-child-parents",
                 "env": "beta", "status": "created", "reason": "", "created": {"child_parents_id": "PX"},
                 "params": {"child": E, "parents": [A, D]}})
    fs.cap["PX"] = (E, (A, D))
    fs.cap["THEIRS"] = (E, (A, B))  # E also has A and B as parents, by someone else
    arch = tmp_path / "arch"
    dry = rollback_tree(led, TreeServices(fs, fs, arch, "beta"), [couple().key], False)
    assert dry.outcomes[-1].status == "rollback-failed" and "names both people" in dry.outcomes[-1].reason


def test_guarded_writer_environment_must_match_the_services(tmp_path):  # minor 5
    fs = FakeFS()
    arch, pf, _ = pf_for(tmp_path, [couple()])
    svc = TreeServices(fs, GuardedTreeWriter(fs, Environment.PRODUCTION, True), arch, "beta")
    with RunLock(arch):
        with pytest.raises(GateError, match="different environment"):
            run_tree_plan(pf, svc, Ledger(arch / "_import" / "tree-ledger.jsonl"), execute=True)
    assert fs.calls == []


def test_cli_confirm_gone_flag(tmp_path, monkeypatch):
    from familysearch_import import __main__ as cli
    from familysearch_import import treeexec as te
    fs = FakeFS()
    fs.apply = False
    _, _, plan = make(tmp_path, [couple()])
    monkeypatch.setattr(te, "build_tree_services",
                        lambda a, e, ap: TreeServices(reader=fs, writer=fs, archive=a, env=e))
    arch = str(tmp_path / "arch")
    assert cli.main(["tree-execute", "--archive", arch, "--env", "beta", "--plan", "TreePlan.json",
                     "--confirm-hash", plan.plan_hash, "--execute"]) == 3
    base = ["--archive", arch, "--env", "beta", "--key", couple().key]
    assert cli.main(["tree-resolve", *base, "--execute"]) == 3
    assert cli.main(["tree-resolve", *base, "--confirm-gone", couple().key, "--execute"]) == 0


# ---------------------------------------------------------------- guarded writer (M9)


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append(name)
            return "ID-1"
        return call


CALLS = [("create_couple", (A, B)), ("create_parent_child", ()), ("create_person", ({},)),
         ("delete_couple", ("R1",)), ("delete_child_and_parents", ("P1",)), ("delete_person", (A,))]


@pytest.mark.parametrize("name,args", CALLS)
def test_guarded_writer_blocks_production_without_allow(name, args):
    inner = Recorder()
    w = GuardedTreeWriter(inner, Environment.PRODUCTION, False)
    with pytest.raises(OperationLockedError):
        getattr(w, name)(*args, reason="r")
    assert inner.calls == []


@pytest.mark.parametrize("name,args", CALLS)
def test_guarded_writer_passes_with_allow_and_on_beta(name, args):
    for env, allow in ((Environment.PRODUCTION, True), (Environment.BETA, False)):
        inner = Recorder()
        w = GuardedTreeWriter(inner, env, allow)
        getattr(w, name)(*args, reason="r")
        assert inner.calls == [name]


def test_guarded_writer_requires_literal_true():
    inner = Recorder()
    w = GuardedTreeWriter(inner, Environment.PRODUCTION, "yes")  # type: ignore[arg-type]
    with pytest.raises(OperationLockedError):
        w.delete_person(A, reason="r")


def test_person_body_uses_name_parts_and_facts():
    from familysearch_import.treeexec import _person_body
    body = _person_body({"name": "Alpha (Maiden) Testname", "birth": 1850, "death": 1920})
    form = body["names"][0]["nameForms"][0]
    assert form["fullText"] == "Alpha Testname" and form["parts"]
    assert body["living"] is False and len(body["facts"]) == 2
