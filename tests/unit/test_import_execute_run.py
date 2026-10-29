import hashlib
import json

import pytest

from familysearch.errors import FSHTTPError
from familysearch_import.execute import (
    ExecServices, GateError, Ledger, PlanFile, RunLock, run_plan, verify_plan_for_execution,
)
from familysearch_import.linking import FsPerson
from familysearch_import.planner import Action, Plan, PersonPlan, action_key, compute_plan_hash
from familysearch_import.planreport import plan_json
from familysearch_import.prod_reader import ReadFailed, SourceRef

LINE = "Example Line"
FOLDER = "1844-1915 Pat Example"
FS_ID = "ZZ11-AAA"


class FakeFS:
    """Fake FamilySearch: per-person memories/sources plus knobs to misbehave."""

    def __init__(self):
        self.people = {FS_ID: dict(status="ok", living=False, id=FS_ID)}
        self.mem = {FS_ID: []}      # [(id, title)]
        self.src = {FS_ID: []}      # [(id, title, url)]
        self.sd = {}
        self.calls = []
        self.upload_appears = True
        self.upload_error = None
        self.read_fail = None
        self.next = 0

    # reader
    def get_fs_person(self, pid):
        self.calls.append(("person", pid))
        p = self.people[pid]
        return FsPerson(p["id"], "n", None, None, p["living"], p["status"])

    def get_memory_titles(self, pid):
        self.calls.append(("memories", pid))
        if self.read_fail == "memories":
            raise ReadFailed("boom")
        return [t for _, t in self.mem[pid]]

    def get_source_refs(self, pid):
        self.calls.append(("sources", pid))
        if self.read_fail == "sources":
            raise ReadFailed("boom")
        return [SourceRef(t, u) for _, t, u in self.src[pid]]

    # memories
    def upload_to_person(self, pid, data, *, filename, content_type, title, reason):
        self.calls.append(("upload", pid, filename, content_type, title, reason, data))
        if self.upload_error:
            raise FSHTTPError(self.upload_error, "dup")
        self.next += 1
        mid = f"M{self.next}"
        if self.upload_appears:
            self.mem[pid].append((mid, title))
        return mid

    def delete(self, mid, *, reason):
        self.calls.append(("delete-memory", mid, reason))
        for pid in self.mem:
            self.mem[pid] = [m for m in self.mem[pid] if m[0] != mid]

    # sources
    def create_description(self, url, title, citation, *, reason):
        self.calls.append(("create-sd", url, title, reason))
        self.next += 1
        sid = f"S{self.next}"
        self.sd[sid] = (title, url)
        return sid

    def attach_to_person(self, pid, sid, *, reason):
        self.calls.append(("attach", pid, sid, reason))
        title, url = self.sd[sid]
        self.src[pid].append((sid, title, url))

    def writes(self):
        return [c for c in self.calls if c[0] in ("upload", "create-sd", "attach", "delete-memory")]


def services(fs, arch, env="beta"):
    return ExecServices(reader=fs, memories=fs, sources=fs, archive=arch, env=env)


def mkfile(tmp_path, name="Census 1900.pdf", data=b"%PDF-census", folder=FOLDER):
    d = tmp_path / "arch" / LINE / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return d / name


def up(name="Census 1900.pdf", data=b"%PDF-census", folder=FOLDER, fs_id=FS_ID):
    sha = hashlib.sha256(data).hexdigest()
    return Action(action_key(fs_id, "upload-document", sha), "upload-document", LINE, folder, fs_id,
                  name, name.rsplit(".", 1)[0], None, "planned", "r", sha)


def src(url="https://example.org/r/1", folder=FOLDER):
    return Action(action_key(FS_ID, "add-source", url), "add-source", LINE, folder, FS_ID, "c.url",
                  "Cite one", url, "planned", "r", None)


def make_plan(tmp_path, actions):
    arch = tmp_path / "arch"
    imp = arch / "_import"
    imp.mkdir(parents=True, exist_ok=True)
    people = []
    for folder in dict.fromkeys(a.person_folder for a in actions):
        people.append(PersonPlan(LINE, folder, FS_ID, "planned",
                                 tuple(a for a in actions if a.person_folder == folder), ""))
    plan = Plan(tuple(people), compute_plan_hash(people))
    (imp / "Plan.json").write_text(json.dumps(plan_json(plan)))
    (imp / "approvals.json").write_text(json.dumps({"approved_links": {}, "approved_plan_hash": plan.plan_hash}))
    return verify_plan_for_execution(arch, imp / "Plan.json", plan.plan_hash)


def run(tmp_path, fs, pf, **kw):
    arch = tmp_path / "arch"
    led = Ledger(arch / "_import" / "ledger.jsonl")
    kw.setdefault("execute", True)
    env = kw.pop("env", "beta")
    with RunLock(arch):
        return run_plan(pf, services(fs, arch, env), led, **kw), led


def test_dry_run_default_writes_nothing(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    s, led = run(tmp_path, fs, pf, execute=False)
    assert [o.status for o in s.outcomes] == ["would-create"]
    assert fs.writes() == [] and not led.path.exists()
    assert ("memories", FS_ID) in fs.calls  # read checks did run


def test_execute_uploads_once_with_ledger_and_rerun_writes_nothing(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    s, led = run(tmp_path, fs, pf)
    assert [o.status for o in s.outcomes] == ["created"] and not s.stopped
    (w,) = fs.writes()
    assert w[:6] == ("upload", FS_ID, "Census 1900.pdf", "application/pdf", "Census 1900",
                     "Added from family research archive") and w[6] == b"%PDF-census"
    recs = led.records()
    assert [r["type"] for r in recs] == ["intent", "result"]
    assert recs[1]["status"] == "created" and recs[1]["memory_id"] == "M1"
    s2, _ = run(tmp_path, fs, pf)
    assert len(fs.writes()) == 1
    assert [o.status for o in s2.outcomes] == ["skipped"]


def test_rerun_with_empty_ledger_still_stops_on_fresh_read(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    run(tmp_path, fs, pf)
    (tmp_path / "arch" / "_import" / "ledger.jsonl").unlink()
    s, _ = run(tmp_path, fs, pf)
    assert len(fs.writes()) == 1 and "already present" in s.outcomes[0].reason


def test_interrupted_run_title_present_is_reconciled_not_reuploaded(tmp_path):
    mkfile(tmp_path)
    a = up()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    fs.mem[FS_ID].append(("M9", "Census 1900"))
    led = Ledger(tmp_path / "arch" / "_import" / "ledger.jsonl")
    led.intent(action=a, env="beta", plan_hash=pf.plan_hash)  # crash before result
    s, led = run(tmp_path, fs, pf)
    assert fs.writes() == []
    assert s.outcomes[0].status == "reconciled-created"
    assert a.key in led.executed_keys("beta") and led.unresolved_intents() == []
    assert led.records()[-1]["reason"] == "created (reconciled)"


def test_interrupted_run_title_absent_is_eligible_again(tmp_path):
    mkfile(tmp_path)
    a = up()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    led = Ledger(tmp_path / "arch" / "_import" / "ledger.jsonl")
    led.intent(action=a, env="beta", plan_hash=pf.plan_hash)
    s, led = run(tmp_path, fs, pf)
    assert [o.status for o in s.outcomes] == ["reconciled-absent", "created"]
    assert len(fs.writes()) == 1 and led.unresolved_intents() == []


def test_interrupted_dry_run_does_not_touch_ledger(tmp_path):
    mkfile(tmp_path)
    a = up()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    led = Ledger(tmp_path / "arch" / "_import" / "ledger.jsonl")
    led.intent(action=a, env="beta", plan_hash=pf.plan_hash)
    before = led.path.read_text()
    run(tmp_path, fs, pf, execute=False)
    assert led.path.read_text() == before


def test_reconcile_read_failure_stops_before_any_write(tmp_path):
    mkfile(tmp_path)
    a = up()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    fs.read_fail = "memories"
    Ledger(tmp_path / "arch" / "_import" / "ledger.jsonl").intent(action=a, env="beta", plan_hash="h")
    s, _ = run(tmp_path, fs, pf)
    assert s.stopped and fs.writes() == []


def test_interrupted_source_intent_reconciles_by_url(tmp_path):
    a = src()
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    fs.src[FS_ID].append(("S9", "whatever", a.url))
    Ledger(tmp_path / "arch" / "_import" / "ledger.jsonl").intent(action=a, env="beta", plan_hash="h")
    s, _ = run(tmp_path, fs, pf)
    assert fs.writes() == [] and s.outcomes[0].status == "reconciled-created"


def test_file_changed_is_skipped_others_continue(tmp_path):
    mkfile(tmp_path, "A.pdf", b"%PDF-a")
    mkfile(tmp_path, "B.pdf", b"%PDF-b")
    pf = make_plan(tmp_path, [up("A.pdf", b"%PDF-a"), up("B.pdf", b"%PDF-b")])
    (tmp_path / "arch" / LINE / FOLDER / "A.pdf").write_bytes(b"%PDF-tampered")
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf)
    assert [(o.status, o.reason) for o in s.outcomes] == [
        ("skipped", "file changed since planning"), ("created", "created")]
    assert [w[4] for w in fs.writes()] == ["B"]


def test_file_missing_is_skipped(tmp_path):
    mkfile(tmp_path, "A.pdf", b"%PDF-a")
    pf = make_plan(tmp_path, [up("A.pdf", b"%PDF-a")])
    (tmp_path / "arch" / LINE / FOLDER / "A.pdf").unlink()
    mkfile(tmp_path, "other.pdf")
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf)
    assert s.outcomes[0].status == "skipped" and fs.writes() == []


@pytest.mark.parametrize("person,why", [
    (dict(status="ok", living=True, id=FS_ID), "living"),
    (dict(status="ok", living=None, id=FS_ID), "living"),
    (dict(status="redirected", living=False, id=FS_ID), "redirected"),
    (dict(status="deleted", living=False, id=FS_ID), "deleted"),
    (dict(status="not-found", living=False, id=FS_ID), "not-found"),
    (dict(status="ok", living=False, id="ZZ22-BBB"), "no longer matches"),
])
def test_person_state_changes_skip(tmp_path, person, why):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    fs.people[FS_ID] = person
    s, led = run(tmp_path, fs, pf)
    assert s.outcomes[0].status == "skipped" and why in s.outcomes[0].reason
    assert fs.writes() == [] and not led.path.exists()


def test_person_read_error_stops(tmp_path):
    mkfile(tmp_path, "A.pdf", b"%PDF-a")
    mkfile(tmp_path, "B.pdf", b"%PDF-b")
    pf = make_plan(tmp_path, [up("A.pdf", b"%PDF-a"), up("B.pdf", b"%PDF-b")])
    fs = FakeFS()
    fs.people[FS_ID]["status"] = "error"
    s, _ = run(tmp_path, fs, pf)
    assert s.stopped and fs.writes() == []


def test_memory_read_failure_stops(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    fs.read_fail = "memories"
    s, led = run(tmp_path, fs, pf)
    assert s.stopped and fs.writes() == [] and not led.path.exists()


def test_title_already_exists_skipped(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    fs.mem[FS_ID].append(("M0", "  census 1900 "))
    s, led = run(tmp_path, fs, pf)
    assert s.outcomes[0].status == "skipped" and fs.writes() == [] and not led.path.exists()


def test_archive_no_longer_deceased_skipped(tmp_path):
    folder = "1950 Sam Example"  # born 1900+, no death: living by the archive rule
    mkfile(tmp_path, folder=folder)
    pf = make_plan(tmp_path, [up(folder=folder)])
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf)
    assert s.outcomes[0].status == "skipped" and "deceased" in s.outcomes[0].reason
    assert fs.writes() == [] and fs.calls == []  # archive check precedes any FamilySearch read


def test_archive_record_marking_living_is_honoured(tmp_path):
    f = mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    rec = {"version": 1, "status": "living", "familysearch_id": None, "names": [], "gender": None,
           "facts": [], "relationships": [], "sources": [], "reviewed": True}
    (f.parent / "ImportRecord.json").write_text(json.dumps(rec))
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf)
    assert fs.writes() == [] and "deceased" in s.outcomes[0].reason


def test_invalid_archive_record_fails_closed(tmp_path):
    f = mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    (f.parent / "ImportRecord.json").write_text(json.dumps({"status": "living", "reviewed": True}))
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf)
    assert fs.writes() == [] and s.outcomes[0].status == "skipped" and fs.calls == []


def test_readback_failure_stops_everything_and_records_ids(tmp_path):
    mkfile(tmp_path, "A.pdf", b"%PDF-a")
    mkfile(tmp_path, "B.pdf", b"%PDF-b")
    pf = make_plan(tmp_path, [up("A.pdf", b"%PDF-a"), up("B.pdf", b"%PDF-b")])
    fs = FakeFS()
    fs.upload_appears = False
    s, led = run(tmp_path, fs, pf)
    assert s.stopped and [o.status for o in s.outcomes] == ["failed-readback"]
    assert len(fs.writes()) == 1  # B never attempted
    last = led.records()[-1]
    assert last["status"] == "failed-readback" and last["memory_id"] == "M1"
    assert led.executed_keys() == frozenset()


def test_readback_read_failure_stops(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    orig = fs.get_memory_titles
    n = {"c": 0}

    def flaky(pid):
        n["c"] += 1
        if n["c"] >= 2:  # pre-check ok, read-back fails
            raise ReadFailed("x")
        return orig(pid)
    fs.get_memory_titles = flaky
    s, led = run(tmp_path, fs, pf)
    assert s.stopped and led.records()[-1]["status"] == "failed-readback"


def test_write_error_409_records_failed_and_stops(tmp_path):
    mkfile(tmp_path, "A.pdf", b"%PDF-a")
    mkfile(tmp_path, "B.pdf", b"%PDF-b")
    pf = make_plan(tmp_path, [up("A.pdf", b"%PDF-a"), up("B.pdf", b"%PDF-b")])
    fs = FakeFS()
    fs.upload_error = 409
    s, led = run(tmp_path, fs, pf)
    assert s.stopped and len(fs.writes()) == 1
    last = led.records()[-1]
    assert last["status"] == "failed" and "409" in last["reason"] and "dup" not in last["reason"]
    assert led.executed_keys() == frozenset() and led.unresolved_intents() == []


def test_same_key_under_two_folders_uploads_once(tmp_path):
    other = "1850-1920 Alex Example"
    mkfile(tmp_path)
    mkfile(tmp_path, folder=other)
    pf = make_plan(tmp_path, [up(), up(folder=other)])
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf)
    assert len(fs.writes()) == 1
    assert [o.status for o in s.outcomes] == ["created", "skipped"]


def test_same_key_two_folders_dry_run_counts_once(tmp_path):
    other = "1850-1920 Alex Example"
    mkfile(tmp_path)
    mkfile(tmp_path, folder=other)
    pf = make_plan(tmp_path, [up(), up(folder=other)])
    s, _ = run(tmp_path, FakeFS(), pf, execute=False)
    assert [o.status for o in s.outcomes] == ["would-create", "skipped"]


def test_max_actions_and_only(tmp_path):
    other = "1850-1920 Alex Example"
    mkfile(tmp_path, "A.pdf", b"%PDF-a")
    mkfile(tmp_path, "B.pdf", b"%PDF-b")
    mkfile(tmp_path, "C.pdf", b"%PDF-c", folder=other)
    acts = [up("A.pdf", b"%PDF-a"), up("B.pdf", b"%PDF-b"), up("C.pdf", b"%PDF-c", folder=other)]
    pf = make_plan(tmp_path, acts)
    fs = FakeFS()
    s, _ = run(tmp_path, fs, pf, max_actions=1)
    assert [w[4] for w in fs.writes()] == ["A"] and s.truncated and not s.stopped
    fs2 = FakeFS()
    run(tmp_path, fs2, pf, only=[f"{LINE}/{other}"])
    assert [w[4] for w in fs2.writes()] == ["C"]


def test_gates_inside_run_plan_precede_any_io(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    for kw in (dict(env="production"),                                  # no --allow-production
               dict(env="production", allow_production=True),           # no --only
               dict(env="production", allow_production=True, only=[f"{LINE}/{FOLDER}"], max_actions=26),
               dict(env="mars")):
        with pytest.raises(GateError):
            run(tmp_path, fs, pf, **kw)
    assert fs.calls == [] and not (tmp_path / "arch" / "_import" / "ledger.jsonl").exists()


def test_production_allowed_with_all_gates(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    fs = FakeFS()
    s, led = run(tmp_path, fs, pf, env="production", allow_production=True, only=[f"{LINE}/{FOLDER}"])
    assert len(fs.writes()) == 1 and led.records()[0]["env"] == "production"


def test_beta_ledger_does_not_block_production_but_unresolved_prod_only(tmp_path):
    mkfile(tmp_path)
    a = up()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    run(tmp_path, fs, pf)  # beta
    fs.mem[FS_ID].clear()
    s, led = run(tmp_path, fs, pf, env="production", allow_production=True, only=[f"{LINE}/{FOLDER}"])
    assert [o.status for o in s.outcomes] == ["created"]


def test_unverified_plan_and_missing_lock_refused(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    raw = PlanFile(pf.plan_hash, pf.actions, pf.path)  # hand-built, not verified
    fs = FakeFS()
    arch = tmp_path / "arch"
    led = Ledger(arch / "_import" / "ledger.jsonl")
    with RunLock(arch):
        with pytest.raises(GateError, match="verified"):
            run_plan(raw, services(fs, arch), led, execute=True)
    with pytest.raises(GateError, match="lock"):
        run_plan(pf, services(fs, arch), led, execute=True)
    assert fs.calls == [] and not led.path.exists()


def test_non_planned_states_never_run(tmp_path):
    mkfile(tmp_path)
    a = up()
    for state in ("likely-covered", "already-present", "skipped"):
        d = a.__dict__ | {"state": state}
        pf = make_plan(tmp_path, [Action(**d)])
        fs = FakeFS()
        s, _ = run(tmp_path, fs, pf)
        assert fs.writes() == [] and s.outcomes == []


def test_add_source_success_and_rerun(tmp_path):
    mkfile(tmp_path)
    a = src()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    s, led = run(tmp_path, fs, pf)
    assert [o.status for o in s.outcomes] == ["created"]
    assert [w[0] for w in fs.writes()] == ["create-sd", "attach"]
    assert led.records()[-1]["source_description_id"] == "S1"
    run(tmp_path, fs, pf)
    assert len(fs.writes()) == 2


def test_add_source_already_cited_skipped(tmp_path):
    mkfile(tmp_path)
    a = src()
    pf = make_plan(tmp_path, [a])
    fs = FakeFS()
    fs.src[FS_ID].append(("S0", "x", a.url))
    s, _ = run(tmp_path, fs, pf)
    assert fs.writes() == [] and s.outcomes[0].status == "skipped"


def test_add_source_readback_failure(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [src()])
    fs = FakeFS()
    fs.attach_to_person = lambda *a, **k: fs.calls.append(("attach", *a))  # attach does nothing
    s, led = run(tmp_path, fs, pf)
    assert s.stopped and led.records()[-1]["status"] == "failed-readback"
    assert led.records()[-1]["source_description_id"] == "S1"


def test_ledger_never_contains_file_bytes_or_token(tmp_path):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    s, led = run(tmp_path, FakeFS(), pf)
    text = led.path.read_text()
    assert "PDF-census" not in text and "token" not in text.lower()
