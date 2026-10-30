import json

import pytest

from familysearch.api import FamilySearchApi, OperationLockedError
from familysearch.auth import Environment
from familysearch.errors import FSError, FSHTTPError
from familysearch_import import __main__ as cli
from familysearch_import import execute as ex
from familysearch_import.execute import ExecServices, Ledger, _SourcesAdapter
from test_import_execute_run import FOLDER, FS_ID, LINE, FakeFS, make_plan, mkfile, src, up


class SrcFake:
    def __init__(self, fs):
        self.fs = fs

    def create_description(self, *a, **k):
        return self.fs.create_description(*a, **k)

    def attach_to_person(self, *a, **k):
        return self.fs.attach_to_person(*a, **k)

    def delete(self, sid, *, reason):
        self.fs.calls.append(("delete-sd", sid, reason))
        for pid in self.fs.src:
            self.fs.src[pid] = [s for s in self.fs.src[pid] if s[0] != sid]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    fs = FakeFS()
    built = []

    def fake_build(archive, env, allow_production):
        built.append((env, allow_production))
        if fs.env_error:
            raise FSError("Brave's FamilySearch session is not signed in to " + env)
        return ExecServices(reader=fs, memories=fs, sources=SrcFake(fs), archive=archive, env=env)

    fs.env_error = False
    monkeypatch.setattr(ex, "build_services", fake_build)
    return fs, built


def args(tmp_path, pf, *extra, env="beta", confirm=None):
    return ["execute", "--archive", str(tmp_path / "arch"), "--env", env, "--plan", "Plan.json",
            "--confirm-hash", confirm or pf.plan_hash, *extra]


def ledger_path(tmp_path):
    return tmp_path / "arch" / "_import" / "ledger.jsonl"


def test_dry_run_is_default_and_writes_nothing(tmp_path, harness, capsys):
    fs, built = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    assert cli.main(args(tmp_path, pf)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would-create" in out
    assert fs.writes() == [] and not ledger_path(tmp_path).exists()
    assert not (tmp_path / "arch" / "_import" / "execute.lock").exists()


def test_execute_then_rerun_writes_nothing(tmp_path, harness, capsys):
    fs, _ = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    assert cli.main(args(tmp_path, pf, "--execute")) == 0
    assert len(fs.writes()) == 1
    assert cli.main(args(tmp_path, pf, "--execute")) == 0
    assert len(fs.writes()) == 1
    assert not (tmp_path / "arch" / "_import" / "execute.lock").exists()
    out = capsys.readouterr().out
    assert "Bearer" not in out and "token" not in out.lower()


def test_wrong_hash_exits_2_before_any_service(tmp_path, harness):
    fs, built = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    assert cli.main(args(tmp_path, pf, "--execute", confirm="0" * 64)) == 2
    assert built == [] and fs.calls == [] and not ledger_path(tmp_path).exists()


def test_edited_plan_and_edited_approvals(tmp_path, harness):
    fs, built = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    plan_file = tmp_path / "arch" / "_import" / "Plan.json"
    orig = plan_file.read_text()
    d = json.loads(orig)
    d["people"][0]["actions"][0]["fs_id"] = "ZZ22-BBB"
    plan_file.write_text(json.dumps(d))
    assert cli.main(args(tmp_path, pf, "--execute")) == 2
    plan_file.write_text(orig)
    appr = tmp_path / "arch" / "_import" / "approvals.json"
    appr.write_text(json.dumps({"approved_links": {}, "approved_plan_hash": "1" * 64}))
    assert cli.main(args(tmp_path, pf, "--execute")) == 2
    assert built == [] and fs.writes() == [] and not ledger_path(tmp_path).exists()


@pytest.mark.parametrize("extra", [
    [],                                                       # no --allow-production, no --only
    ["--only", f"{LINE}/{FOLDER}"],                           # no --allow-production
    ["--allow-production"],                                   # no --only
    ["--allow-production", "--only", f"{LINE}/{FOLDER}", "--max-actions", "26"],
])
def test_production_gates_exit_2_no_write(tmp_path, harness, extra):
    fs, built = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    assert cli.main(args(tmp_path, pf, "--execute", *extra, env="production")) == 2
    assert built == [] and fs.calls == [] and not ledger_path(tmp_path).exists()


def test_production_with_all_gates_runs(tmp_path, harness):
    fs, built = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()], env="production")
    rc = cli.main(args(tmp_path, pf, "--execute", "--allow-production", "--only", f"{LINE}/{FOLDER}",
                       env="production"))
    assert rc == 0 and built == [("production", True)] and len(fs.writes()) == 1


def test_environment_mismatch_exits_2_and_writes_nothing(tmp_path, harness):
    fs, _ = harness
    fs.env_error = True
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    assert cli.main(args(tmp_path, pf, "--execute")) == 2
    assert fs.calls == [] and not ledger_path(tmp_path).exists()
    assert not (tmp_path / "arch" / "_import" / "execute.lock").exists()


def test_lock_present_refuses_and_break_lock(tmp_path, harness, capsys):
    fs, _ = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    lock = tmp_path / "arch" / "_import" / "execute.lock"
    lock.write_text("stale holder")
    assert cli.main(args(tmp_path, pf, "--execute")) == 2
    assert fs.writes() == [] and lock.read_text() == "stale holder"
    assert cli.main(args(tmp_path, pf, "--execute", "--break-lock")) == 0
    assert "stale holder" in capsys.readouterr().out and len(fs.writes()) == 1


def test_run_stopped_exits_3(tmp_path, harness, capsys):
    fs, _ = harness
    fs.upload_appears = False
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    assert cli.main(args(tmp_path, pf, "--execute")) == 3
    assert "STOPPED" in capsys.readouterr().out


def test_plan_outside_import_dir_refused(tmp_path, harness):
    fs, built = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    stray = tmp_path / "Plan.json"
    stray.write_text((tmp_path / "arch" / "_import" / "Plan.json").read_text())
    rc = cli.main(["execute", "--archive", str(tmp_path / "arch"), "--env", "beta", "--plan", str(stray),
                   "--confirm-hash", pf.plan_hash, "--execute"])
    assert rc == 2 and built == []


# ---- rollback

def rargs(tmp_path, *extra, env="beta"):
    return ["rollback", "--archive", str(tmp_path / "arch"), "--env", env, *extra]


def populated(tmp_path, harness, actions):
    fs, _ = harness
    for a in actions:
        if a.kind == "upload-document":
            mkfile(tmp_path, a.file, b"%PDF-" + a.file.encode(), folder=a.person_folder)
    fixed = [up(a.file, b"%PDF-" + a.file.encode()) if a.kind == "upload-document" else a
             for a in actions]
    pf = make_plan(tmp_path, fixed)
    assert cli.main(args(tmp_path, pf, "--execute")) == 0
    return fs, pf


def test_rollback_dry_run_default_then_execute_only_ledger_ids(tmp_path, harness):
    fs, built = harness
    fs.mem[FS_ID].append(("PRE", "Census 1900"))  # someone else's memory with a different title below
    fs.mem[FS_ID].clear()
    fs.mem[FS_ID].append(("PRE", "Mine, not ours"))
    fs, pf = populated(tmp_path, harness, [up("A.pdf"), src()])
    assert [m[0] for m in fs.mem[FS_ID]] == ["PRE", "M1"] and len(fs.src[FS_ID]) == 1
    assert cli.main(rargs(tmp_path)) == 0
    assert not [c for c in fs.calls if c[0].startswith("delete")]
    assert cli.main(rargs(tmp_path, "--execute")) == 0
    deletes = [c for c in fs.calls if c[0].startswith("delete")]
    assert {c[1] for c in deletes} == {"M1", "S2"}
    assert [m[0] for m in fs.mem[FS_ID]] == ["PRE"] and fs.src[FS_ID] == []
    # idempotent: second run deletes nothing more
    n = len(deletes)
    assert cli.main(rargs(tmp_path, "--execute")) == 0
    assert len([c for c in fs.calls if c[0].startswith("delete")]) == n
    types = [r["type"] for r in Ledger(ledger_path(tmp_path)).records()]
    assert types.count("rollback") == 2


def test_rollback_by_key_and_unknown_key(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf"), up("B.pdf")])
    keys = [r["key"] for r in Ledger(ledger_path(tmp_path)).records() if r["type"] == "result"]
    assert cli.main(rargs(tmp_path, "--key", "f" * 64, "--execute")) == 2
    assert fs.mem[FS_ID] and len(fs.mem[FS_ID]) == 2
    assert cli.main(rargs(tmp_path, "--key", keys[0], "--execute")) == 0
    assert [m[1] for m in fs.mem[FS_ID]] == ["B"]


def test_rollback_production_gates(tmp_path, harness):
    fs, built = harness
    fs, pf = populated(tmp_path, harness, [up("A.pdf")])
    built.clear()
    assert cli.main(rargs(tmp_path, "--execute", env="production")) == 2
    assert cli.main(rargs(tmp_path, "--execute", "--allow-production", env="production")) == 2
    assert cli.main(rargs(tmp_path, "--execute", "--only", f"{LINE}/{FOLDER}", env="production")) == 2
    assert built == [] and len(fs.mem[FS_ID]) == 1


def test_rollback_never_touches_other_env_or_failed_without_key(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf")])
    # a beta-created id must not be deleted by a production rollback
    assert cli.main(rargs(tmp_path, "--execute", "--allow-production", "--only", f"{LINE}/{FOLDER}",
                          env="production")) == 0
    assert len(fs.mem[FS_ID]) == 1
    assert not [c for c in fs.calls if c[0].startswith("delete")]
    # failed / failed-readback entries are not auto-selected
    led = Ledger(ledger_path(tmp_path))
    led.result(intent_id="i", key="k" * 64, status="failed-readback", reason="x", kind="upload-document",
               fs_id=FS_ID, env="beta", title="Ghost", person=f"{LINE}/{FOLDER}", memory_id="MX")
    assert cli.main(rargs(tmp_path, "--execute")) == 0
    assert not [c for c in fs.calls if c[0] == "delete-memory" and c[1] == "MX"]


def test_rollback_readback_failure_stops(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf"), up("B.pdf")])
    fs.delete = lambda mid, *, reason: fs.calls.append(("delete-memory", mid, reason))  # delete is a no-op
    assert cli.main(rargs(tmp_path, "--execute")) == 3
    assert len([c for c in fs.calls if c[0] == "delete-memory"]) == 1  # stopped after the first
    assert Ledger(ledger_path(tmp_path)).records()[-1]["status"] == "rollback-failed"


def test_rollback_without_ledger(tmp_path, harness):
    (tmp_path / "arch").mkdir()
    assert cli.main(rargs(tmp_path)) == 2


def test_rollback_requires_lock_for_direct_execute(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf")])
    services = ExecServices(fs, fs, SrcFake(fs), tmp_path / "arch", "beta")
    with pytest.raises(ex.GateError, match="lock"):
        ex.rollback(Ledger(ledger_path(tmp_path)), services, (), True)


# ---- real-service adapter: registry lock honoured

class RecordingClient:
    def __init__(self, env):
        self.environment = env
        self.base_url = env.base_url
        self.requests = []

    def request(self, method, path, **kw):
        self.requests.append((method, path, kw.get("reason")))

        class R:
            status_code = 204
            headers = {}
            text = ""
        return R()


class FakeSourcesLib:
    def __init__(self):
        self.attached = []

    def attach_to_person(self, pid, url, *, reason):
        self.attached.append((pid, url, reason))


class FakeDiscovery:
    def href(self, name):
        return "https://beta.familysearch.org/platform/sources/descriptions?x=1"


def test_adapter_delete_honours_registry_lock():
    prod = RecordingClient(Environment.PRODUCTION)
    adapter = _SourcesAdapter(FakeSourcesLib(), FakeDiscovery(), FamilySearchApi(prod), allow_production=False)
    with pytest.raises(OperationLockedError):
        adapter.delete("SD1", reason="r")
    assert prod.requests == []
    beta = RecordingClient(Environment.BETA)
    _SourcesAdapter(FakeSourcesLib(), FakeDiscovery(), FamilySearchApi(beta), False).delete("SD1", reason="r")
    assert beta.requests == [("DELETE", "/platform/sources/descriptions/SD1", "r")]
    allowed = RecordingClient(Environment.PRODUCTION)
    _SourcesAdapter(FakeSourcesLib(), FakeDiscovery(), FamilySearchApi(allowed), True).delete("SD1", reason="r")
    assert allowed.requests[0][0] == "DELETE"


def test_token_expiry_during_readback_keeps_created_id_and_exits_3(tmp_path, harness, capsys):
    from familysearch.errors import TokenExpiredError
    fs, _ = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    orig = fs.get_memory_titles
    n = {"c": 0}

    def expiring(pid):
        n["c"] += 1
        if n["c"] >= 2:
            raise TokenExpiredError("expired")
        return orig(pid)
    fs.get_memory_titles = expiring
    assert cli.main(args(tmp_path, pf, "--execute")) == 3
    last = Ledger(ledger_path(tmp_path)).records()[-1]
    assert last["status"] == "failed-readback" and last["memory_id"] == "M1"
    assert "STOPPED" in capsys.readouterr().out


def test_unexpected_error_before_write_exits_3_with_no_write(tmp_path, harness):
    from familysearch.errors import TokenExpiredError
    fs, _ = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])

    def boom(pid):
        raise TokenExpiredError("expired")
    fs.get_fs_person = boom
    assert cli.main(args(tmp_path, pf, "--execute")) == 3
    assert fs.writes() == [] and not ledger_path(tmp_path).exists()


def test_rollback_id_gone_from_person_is_not_claimed_rolled_back(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf")])
    fs.mem[FS_ID] = []
    assert cli.main(rargs(tmp_path, "--execute")) == 3
    assert [c for c in fs.calls if c[0] == "delete-memory"] == []
    assert Ledger(ledger_path(tmp_path)).records()[-1]["status"] == "rollback-failed"


def test_adapter_attach_builds_clean_url():
    lib = FakeSourcesLib()
    _SourcesAdapter(lib, FakeDiscovery(), None, False).attach_to_person("ZZ11-AAA", "SD9", reason="r")
    assert lib.attached == [("ZZ11-AAA", "https://beta.familysearch.org/platform/sources/descriptions/SD9", "r")]


def test_rollback_key_from_other_env_is_an_error(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf")])
    key = [r["key"] for r in Ledger(ledger_path(tmp_path)).records() if r["type"] == "result"][0]
    rc = cli.main(rargs(tmp_path, "--key", key, "--execute", "--allow-production", "--only",
                        f"{LINE}/{FOLDER}", env="production"))
    assert rc == 2 and len(fs.mem[FS_ID]) == 1


def test_rollback_reports_rows_without_ids(tmp_path, harness, capsys):
    fs, _ = harness
    (tmp_path / "arch" / "_import").mkdir(parents=True)
    led = Ledger(ledger_path(tmp_path))
    led.result(intent_id="i", key="a" * 64, status="created", reason="created (reconciled)",
               kind="upload-document", fs_id=FS_ID, env="beta", reconciled=True, person=f"{LINE}/{FOLDER}")
    assert cli.main(rargs(tmp_path, "--execute")) == 3
    out = capsys.readouterr().out
    assert "cannot roll back: no id recorded" in out and "1 cannot-rollback" in out
    assert fs.calls == []


def test_rollback_invalid_ledger_id_never_reaches_delete(tmp_path, harness):
    fs, _ = harness
    (tmp_path / "arch" / "_import").mkdir(parents=True)
    led = Ledger(ledger_path(tmp_path))
    led.result(intent_id="i", key="a" * 64, status="created", reason="x", kind="upload-document",
               fs_id=FS_ID, env="beta", memory_id="../../x", person=f"{LINE}/{FOLDER}")
    assert cli.main(rargs(tmp_path, "--execute")) == 3
    assert [c for c in fs.calls if c[0].startswith("delete")] == []


def test_rollback_partial_only_by_key_and_deletes_description(tmp_path, harness):
    fs, _ = harness
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [src()])
    fs.attach_to_person = lambda *a, **k: (_ for _ in ()).throw(FSHTTPError(500, "x"))
    assert cli.main(args(tmp_path, pf, "--execute")) == 3
    key = src().key
    assert cli.main(args(tmp_path, pf, "--execute")) == 3  # blocked
    assert len(fs.sd) == 1
    assert cli.main(rargs(tmp_path, "--execute")) == 0  # bulk ignores partial
    assert [c for c in fs.calls if c[0] == "delete-sd"] == []
    assert cli.main(rargs(tmp_path, "--key", key, "--execute")) == 0
    assert [c[1] for c in fs.calls if c[0] == "delete-sd"] == ["S1"]
    fs.attach_to_person = FakeFS.attach_to_person.__get__(fs)
    assert cli.main(args(tmp_path, pf, "--execute")) == 0  # unblocked by the rollback


def test_rollback_wrong_id_deleted_is_a_failure(tmp_path, harness):
    fs, pf = populated(tmp_path, harness, [up("A.pdf")])
    fs.mem[FS_ID].append(("M99", "Other"))

    def wrong(mid, *, reason):
        fs.calls.append(("delete-memory", mid, reason))
        fs.mem[FS_ID] = [m for m in fs.mem[FS_ID] if m[0] != "M99"]  # removes a different id
    fs.delete = wrong
    assert cli.main(rargs(tmp_path, "--execute")) == 3
    assert Ledger(ledger_path(tmp_path)).records()[-1]["status"] == "rollback-failed"


def test_errors_print_type_only(tmp_path, harness, capsys, monkeypatch):
    import requests
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])

    def leaky(*a, **k):
        raise FSError("TOP-SECRET response body")
    monkeypatch.setattr(ex, "build_services", leaky)
    assert cli.main(args(tmp_path, pf, "--execute")) == 2
    err = capsys.readouterr().err
    assert "FSError" in err and "SECRET" not in err

    def net(*a, **k):
        raise requests.ConnectionError("host=secret.example")
    monkeypatch.setattr(ex, "build_services", net)
    assert cli.main(args(tmp_path, pf, "--execute")) == 2
    err = capsys.readouterr().err
    assert "ConnectionError" in err and "secret" not in err
    assert cli.main(rargs(tmp_path)) == 2  # no ledger yet: gate


def test_corrupt_ledger_warning_printed(tmp_path, harness, capsys):
    mkfile(tmp_path)
    pf = make_plan(tmp_path, [up()])
    ledger_path(tmp_path).write_text("not json\n")
    assert cli.main(args(tmp_path, pf, "--execute")) == 0
    assert "unreadable ledger" in capsys.readouterr().err


def test_ledger_symlink_refused(tmp_path):
    import os
    from familysearch_import.execute import GateError
    (tmp_path / "real").write_text("")
    link = tmp_path / "ledger.jsonl"
    os.symlink(tmp_path / "real", link)
    with pytest.raises(GateError, match="symlink"):
        Ledger(link)
    led = Ledger(tmp_path / "later.jsonl")
    os.symlink(tmp_path / "real", led.path)  # swapped in after construction
    with pytest.raises(GateError):
        led.intent(action=up(), env="beta", plan_hash="h")
    assert (tmp_path / "real").read_text() == ""
