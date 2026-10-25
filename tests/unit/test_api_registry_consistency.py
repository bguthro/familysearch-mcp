import ast
import copy
import datetime
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

from familysearch.api import OPERATIONS
from familysearch.api.registry import (
    OperationLockedError, check_call, effective_status, load_registry,
)
from familysearch.api.spec import OpSpec
from familysearch.auth import Environment
from tools.spec.bootstrap_registry import (
    HANDWRITTEN, REGISTRY_PATH, UNVERIFIABLE, build_registry, merge_registry, render,
)
from tools.spec.resolve import SNAPSHOT_PATH, load_overrides, resolve

ROOT = Path(__file__).resolve().parents[2]
GENERATED_STATUSES = {"unverified", "verified", "unverifiable", "excluded"}


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(SNAPSHOT_PATH.read_text())


@pytest.fixture(scope="module")
def overrides():
    return load_overrides()


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def resolved(snapshot, overrides):
    return resolve(snapshot, overrides)


def _recipes():
    try:
        found = importlib.util.find_spec("tools.verify.recipes")
    except ModuleNotFoundError:  # tools.verify does not exist yet
        found = None
    if found is None:
        return None
    from tools.verify.recipes import RECIPES
    return RECIPES


def test_every_snapshot_operation_is_accounted_for(registry, resolved, snapshot):
    ops, excluded = resolved
    expected = [o.operation_id for o in ops] + list(excluded)
    assert len(expected) == len(snapshot["operations"]) == 205
    assert len(set(expected)) == 205
    assert sorted(registry["operations"]) == sorted(expected)


def test_entries_have_only_generated_and_handwritten_keys(registry):
    for oid, entry in registry["operations"].items():
        assert set(entry) <= {"generated", "handwritten"}, oid
        assert "generated" in entry, oid


def test_status_values_valid_and_reasons_present(registry):
    for oid, entry in registry["operations"].items():
        assert entry["generated"]["status"] in GENERATED_STATUSES, oid
        for impl, e in entry.items():
            if e["status"] in ("unverifiable", "excluded"):
                assert isinstance(e.get("reason"), str) and e["reason"], (oid, impl)


def test_generated_statuses(registry):
    recipes = _recipes()
    for oid, entry in registry["operations"].items():
        gen = entry["generated"]
        status = gen["status"]
        assert status in GENERATED_STATUSES, oid
        if oid in ("getAuthorizationPage", "getAccessToken"):
            assert status == "excluded", oid
        elif oid in UNVERIFIABLE:
            assert status == "unverifiable", oid
        elif status == "unverifiable":
            assert isinstance(gen.get("reason"), str) and gen["reason"].strip(), oid
        elif status == "verified":
            assert oid in _recipe_ops(recipes, gen.get("recipe")), f"{oid} not in recipe ops"
            if recipes is not None:
                assert gen.get("evidence") == recipes[gen["recipe"]].evidence, oid
            assert re.fullmatch(r"[0-9a-f]{40}", str(gen.get("commit"))), oid
            datetime.date.fromisoformat(gen.get("verified_on"))
            _assert_commit_in_history(gen["commit"])
            assert gen.get("evidence") in ("state-change", "read-content"), oid
            assert gen.get("commit") and gen.get("verified_on"), oid
            assert gen.get("recipe"), oid
            if recipes is not None:
                assert gen["recipe"] in recipes, oid
            assert gen.get("fingerprint") == OPERATIONS[oid].fingerprint, f"{oid} is stale"
        else:
            assert status == "unverified", oid


def _recipe_ops(recipes, name):
    if recipes is None or name not in recipes:
        return ()
    return recipes[name].ops


def _assert_commit_in_history(commit):
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    done = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=ROOT,
                          capture_output=True)
    assert done.returncode == 0, f"{commit} is not an ancestor of HEAD"


def _function_exists(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text())
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        for n in ast.walk(tree)
    )


def test_verified_entries_reference_existing_recipes(registry):
    recipes = _recipes()
    for oid, entry in registry["operations"].items():
        hw = entry.get("handwritten")
        if hw is not None:
            assert hw["status"] == "verified", oid
            path, _, func = hw["recipe"].partition("::")
            assert func, oid
            assert (ROOT / path).is_file(), (oid, path)
            assert _function_exists(ROOT / path, func), (oid, hw["recipe"])
        gen = entry["generated"]
        if gen["status"] == "verified" and recipes is not None:
            assert gen.get("recipe") in recipes, oid


def test_handwritten_entries_match_mapping(registry):
    assert {o for o, e in registry["operations"].items() if "handwritten" in e} == set(HANDWRITTEN)


def test_verified_generated_entry_has_fingerprint_and_evidence(registry, resolved):
    ops, _ = resolved
    fingerprints = {o.operation_id: o.fingerprint for o in ops}
    for oid, entry in registry["operations"].items():
        gen = entry["generated"]
        if gen["status"] != "verified":
            continue
        assert gen.get("fingerprint") == fingerprints[oid], oid
        assert gen.get("evidence") in ("state-change", "read-content"), oid
        assert gen.get("commit") and gen.get("verified_on") and gen.get("recipe"), oid


def test_registry_matches_bootstrap_output(registry, snapshot, overrides):
    built = build_registry(snapshot, overrides)
    assert built == build_registry(snapshot, overrides)
    assert merge_registry(built, registry) == registry
    assert REGISTRY_PATH.read_text() == render(registry)


def test_merge_preserves_work_adds_new_ops_and_is_idempotent(snapshot, overrides):
    built = build_registry(snapshot, overrides)
    committed = copy.deepcopy(built)
    ops = committed["operations"]
    verified = {"status": "verified", "fingerprint": "f", "recipe": "r"}
    ops["readPerson"]["generated"] = verified
    ops["allowPersonMerge"]["generated"] = {"status": "unverifiable", "reason": "classified"}
    del ops["checkPlaceIsChild"]
    merged = merge_registry(built, committed)
    assert merged["operations"]["readPerson"]["generated"] == verified
    assert merged["operations"]["readPerson"]["handwritten"] == (
        built["operations"]["readPerson"]["handwritten"])
    assert merged["operations"]["allowPersonMerge"]["generated"]["status"] == "unverifiable"
    assert merged["operations"]["checkPlaceIsChild"]["generated"] == {"status": "unverified"}
    assert merge_registry(built, merged) == merged


def test_merge_refuses_entries_for_vanished_operations(snapshot, overrides):
    built = build_registry(snapshot, overrides)
    committed = copy.deepcopy(built)
    committed["operations"]["noLongerExists"] = {"generated": {"status": "unverified"}}
    with pytest.raises(ValueError, match="noLongerExists"):
        merge_registry(built, committed)


def _spec_for(op) -> OpSpec:
    return OpSpec(
        operation_id=op.operation_id, method_name=op.method_name, area=op.area,
        http_method=op.http_method, path=op.path, summary=op.summary, kind=op.kind,
        destructive=op.destructive, path_params=op.path_params,
        required_query=op.required_query, accepts=op.accepts, body=op.body,
        fingerprint=op.fingerprint,
    )


def _first_write(resolved):
    ops, _ = resolved
    return next(o for o in ops if o.kind == "write" and o.operation_id not in UNVERIFIABLE)


def test_stale_detected_when_spec_changes(registry, resolved):
    op = _first_write(resolved)
    reg = copy.deepcopy(registry)
    reg["operations"][op.operation_id]["generated"] = {
        "status": "verified", "fingerprint": "not-the-real-fingerprint",
    }
    assert effective_status(op.operation_id, op.fingerprint, reg) == "stale"
    with pytest.raises(OperationLockedError):
        check_call(_spec_for(op), Environment.BETA, reg, confirm_destructive=True)
    # allow_unverified is the only way through on beta
    check_call(_spec_for(op), Environment.BETA, reg, allow_unverified=True,
               confirm_destructive=True)


def test_unverifiable_ops_have_no_generated_method_unlock(registry, resolved):
    ops, _ = resolved
    by_id = {o.operation_id: o for o in ops}
    assert set(UNVERIFIABLE) <= set(by_id)
    for oid in UNVERIFIABLE:
        spec = _spec_for(by_id[oid])
        for env in (Environment.BETA, Environment.PRODUCTION):
            for au in (False, True):
                for ap in (False, True):
                    for cd in (False, True):
                        with pytest.raises(OperationLockedError):
                            check_call(
                                spec, env, registry, allow_unverified=au,
                                allow_production=ap, confirm_destructive=cd,
                            )
