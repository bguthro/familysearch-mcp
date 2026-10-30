import pytest

from familysearch_import import __main__ as cli
from familysearch_import import treeexec as te
from familysearch_import.treeexec import TreeServices
from test_import_treeexec import FakeFS, couple, make


@pytest.fixture
def harness(tmp_path, monkeypatch):
    fs = FakeFS()
    built = []

    def fake_build(archive, env, allow_production):
        built.append((env, allow_production))
        return TreeServices(reader=fs, writer=fs, archive=archive, env=env)

    monkeypatch.setattr(te, "build_tree_services", fake_build)
    return fs, built


def targs(tmp_path, plan, *extra, env="beta", confirm=None, cmd="tree-execute"):
    return [cmd, "--archive", str(tmp_path / "arch"), "--env", env, "--plan", "TreePlan.json",
            "--confirm-hash", confirm or plan.plan_hash, *extra]


def test_dry_run_default_then_execute(tmp_path, harness, capsys):
    fs, built = harness
    _, _, plan = make(tmp_path, [couple()])
    assert cli.main(targs(tmp_path, plan)) == 0
    assert "DRY RUN" in capsys.readouterr().out and fs.writes() == []
    assert cli.main(targs(tmp_path, plan, "--execute")) == 0
    assert fs.writes() == [("create_couple", "AAAA-001", "BBBB-002")]
    assert not (tmp_path / "arch" / "_import" / "execute.lock").exists()


def test_bad_hash_exit_2_and_no_services(tmp_path, harness):
    fs, built = harness
    _, _, plan = make(tmp_path, [couple()])
    assert cli.main(targs(tmp_path, plan, confirm="0" * 64)) == 2
    assert built == []


def test_production_gate_exit_2(tmp_path, harness):
    fs, built = harness
    _, _, plan = make(tmp_path, [couple()], env="production")
    assert cli.main(targs(tmp_path, plan, env="production", cmd="tree-execute")) == 2
    assert built == []


def test_stopped_run_exit_3_then_resolve_and_rollback_cli(tmp_path, harness, capsys):
    fs, built = harness
    fs.apply = False
    _, _, plan = make(tmp_path, [couple()])
    assert cli.main(targs(tmp_path, plan, "--execute")) == 3
    base = ["--archive", str(tmp_path / "arch"), "--env", "beta", "--key", couple().key]
    assert cli.main(["tree-resolve", *base]) == 3  # carries an id: still blocked
    fs.couples["R1"] = frozenset(("AAAA-001", "BBBB-002"))
    assert cli.main(["tree-rollback", *base, "--execute"]) == 0
    assert "R1" not in fs.couples


def test_outcomes_print_the_full_action_key(tmp_path, harness, capsys):  # minor 8
    _, _, plan = make(tmp_path, [couple()])
    assert cli.main(targs(tmp_path, plan)) == 0
    assert couple().key in capsys.readouterr().out


def test_tree_resolve_confirm_absent_flag(tmp_path, harness, capsys):  # B2
    from test_import_treeexec import newp
    fs, _ = harness
    fs.write_error = 504
    _, _, plan = make(tmp_path, [newp()])
    assert cli.main(targs(tmp_path, plan, "--execute")) == 3
    key = newp().key
    base = ["--archive", str(tmp_path / "arch"), "--env", "beta", "--key", key]
    assert cli.main(["tree-resolve", *base, "--execute"]) == 3  # search never lifts it
    assert "--confirm-absent" in capsys.readouterr().out
    assert cli.main(["tree-resolve", *base, "--confirm-absent", key, "--execute"]) == 0
