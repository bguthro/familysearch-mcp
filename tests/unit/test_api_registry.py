import dataclasses
import itertools

import pytest

from familysearch.api.registry import (
    OperationLockedError, check_call, effective_status, load_registry,
)
from familysearch.api.spec import OpSpec
from familysearch.auth import Environment
from familysearch.errors import FSError

BETA, PROD = Environment.BETA, Environment.PRODUCTION
FP = "fp-current"
# (allow_unverified, allow_production, confirm_destructive)
ALL_FLAGS = list(itertools.product([0, 1], repeat=3))


def _spec(kind="write", destructive=False, fingerprint=FP):
    return OpSpec(
        operation_id="Op", method_name="op", area="Family Tree Endpoints",
        http_method="POST" if kind == "write" else "GET", path="/x/{id}",
        summary="s", kind=kind, destructive=destructive, path_params=("id",),
        required_query=(), accepts=(), body="json", fingerprint=fingerprint,
    )


def _registry(status, fingerprint=FP, reason="because"):
    if status is None:
        return {"schema": 1, "operations": {}}
    return {"schema": 1, "operations": {"Op": {"generated": {
        "status": status, "fingerprint": fingerprint, "reason": reason}}}}


# Written-out expectations: (status, environment, destructive) -> the exact set of
# (allow_unverified, allow_production, confirm_destructive) combinations that may run
# (writes only). Every other combination of the 8 must be denied.
ALLOWED = {
    # verified, beta: always runs (destructive needs confirm)
    ("verified", BETA, False): {(a, p, c) for a in (0, 1) for p in (0, 1) for c in (0, 1)},
    ("verified", BETA, True): {(a, p, 1) for a in (0, 1) for p in (0, 1)},
    # verified, production: needs allow_production
    ("verified", PROD, False): {(a, 1, c) for a in (0, 1) for c in (0, 1)},
    ("verified", PROD, True): {(a, 1, 1) for a in (0, 1)},
    # unverified/stale, beta: needs allow_unverified
    ("unverified", BETA, False): {(1, p, c) for p in (0, 1) for c in (0, 1)},
    ("unverified", BETA, True): {(1, p, 1) for p in (0, 1)},
    ("stale", BETA, False): {(1, p, c) for p in (0, 1) for c in (0, 1)},
    ("stale", BETA, True): {(1, p, 1) for p in (0, 1)},
    # unverified/stale, production: never
    ("unverified", PROD, False): set(),
    ("unverified", PROD, True): set(),
    ("stale", PROD, False): set(),
    ("stale", PROD, True): set(),
    # unverifiable/excluded: never, anywhere
    ("unverifiable", BETA, False): set(),
    ("unverifiable", BETA, True): set(),
    ("unverifiable", PROD, False): set(),
    ("unverifiable", PROD, True): set(),
    ("excluded", BETA, False): set(),
    ("excluded", BETA, True): set(),
    ("excluded", PROD, False): set(),
    ("excluded", PROD, True): set(),
}


def _registry_for(status):
    if status == "stale":
        return _registry("verified", fingerprint="old-fingerprint")
    if status == "unverified":
        return _registry(None)
    return _registry(status)


@pytest.mark.parametrize("kind", ["read", "write"])
@pytest.mark.parametrize("status", ["verified", "unverified", "stale", "unverifiable", "excluded"])
@pytest.mark.parametrize("env", [BETA, PROD])
@pytest.mark.parametrize("destructive", [False, True])
@pytest.mark.parametrize("flags", ALL_FLAGS)
def test_truth_table(kind, status, env, destructive, flags):
    spec = _spec(kind=kind, destructive=destructive)
    au, ap, cd = flags
    kwargs = dict(allow_unverified=bool(au), allow_production=bool(ap),
                  confirm_destructive=bool(cd))
    registry = _registry_for(status)
    if kind == "read" or flags in ALLOWED[(status, env, destructive)]:
        check_call(spec, env, registry, **kwargs)
    else:
        with pytest.raises(OperationLockedError):
            check_call(spec, env, registry, **kwargs)


def test_read_never_locked_even_with_unverifiable_status_flags_irrelevant():
    spec = _spec(kind="read", destructive=True)
    for env in (BETA, PROD):
        check_call(spec, env, _registry("unverifiable"))
        check_call(spec, env, _registry("excluded"))


def test_stale_when_fingerprint_changes():
    registry = _registry("verified", fingerprint="aaa")
    spec = _spec(fingerprint="bbb")
    assert effective_status("Op", "bbb", registry) == "stale"
    assert effective_status("Op", "aaa", registry) == "verified"
    with pytest.raises(OperationLockedError):
        check_call(spec, BETA, registry)
    check_call(spec, BETA, registry, allow_unverified=True)
    with pytest.raises(OperationLockedError):
        check_call(spec, PROD, registry, allow_unverified=True, allow_production=True)


def test_missing_entry_is_unverified():
    assert effective_status("Nope", FP, _registry(None)) == "unverified"
    with pytest.raises(OperationLockedError):
        check_call(_spec(), BETA, _registry(None))


def test_unknown_status_value_fails_closed_for_writes_and_names_it():
    assert effective_status("Op", FP, _registry("bogus")) == "unknown"
    with pytest.raises(OperationLockedError, match="bogus"):
        check_call(_spec(), BETA, _registry("bogus"), allow_unverified=True,
                   allow_production=True, confirm_destructive=True)
    check_call(_spec(kind="read"), BETA, _registry("bogus"))


def test_task8_shape_unverifiable_and_excluded_never_run():
    for status in ("unverifiable", "excluded"):
        reg = {"schema": 1, "operations": {"Op": {"generated": {"status": status, "reason": "r"}}}}
        with pytest.raises(OperationLockedError):
            check_call(_spec(), BETA, reg, allow_unverified=True,
                       allow_production=True, confirm_destructive=True)


def test_task8_shape_verified_matching_fingerprint_honored():
    reg = {"schema": 1, "operations": {"Op": {"generated": {
        "status": "verified", "fingerprint": FP, "evidence": "e", "recipe": "r"}}}}
    check_call(_spec(), BETA, reg)
    assert effective_status("Op", FP, reg) == "verified"


def test_handwritten_entry_does_not_affect_generated_status():
    reg = {"schema": 1, "operations": {"Op": {"handwritten": {
        "status": "verified", "fingerprint": FP}}}}
    assert effective_status("Op", FP, reg) == "unverified"
    assert effective_status("Op", FP, reg, impl="handwritten") == "verified"
    with pytest.raises(OperationLockedError):
        check_call(_spec(), BETA, reg)


def test_environment_fails_closed():
    for env in ("production", None, "beta", object()):
        with pytest.raises(OperationLockedError):
            check_call(_spec(), env, _registry("verified"))
        with pytest.raises(OperationLockedError):
            check_call(_spec(), env, _registry(None), allow_unverified=True,
                       allow_production=False)
    check_call(_spec(), "production", _registry("verified"), allow_production=True)


def test_verified_requires_nonempty_matching_fingerprints():
    for stored, current in ((None, FP), ("", FP), (FP, ""), ("", ""), (None, None)):
        reg = _registry("verified", fingerprint=stored)
        assert effective_status("Op", current, reg) == "stale"
    reg = {"operations": {"Op": {"generated": {"status": "verified"}}}}
    assert effective_status("Op", FP, reg) == "stale"


def test_flags_must_be_literally_true():
    for bad in ("no", "false", 1, "yes", [1]):
        with pytest.raises(OperationLockedError):
            check_call(_spec(), BETA, _registry(None), allow_unverified=bad)
        with pytest.raises(OperationLockedError):
            check_call(_spec(), PROD, _registry("verified"), allow_production=bad)
        with pytest.raises(OperationLockedError):
            check_call(_spec(destructive=True), BETA, _registry("verified"),
                       confirm_destructive=bad)


def test_destructive_fails_closed():
    for d in (None, "no", 0, "false"):
        with pytest.raises(OperationLockedError, match="confirm_destructive"):
            check_call(_spec(destructive=d), BETA, _registry("verified"))
    check_call(_spec(destructive=False), BETA, _registry("verified"))


@pytest.mark.parametrize("registry", [
    None, [], {"operations": None}, {"operations": []}, {"operations": "x"},
    {"operations": {"Op": "str"}}, {"operations": {"Op": None}} if False else {"operations": {"Op": 5}},
    {"operations": {"Op": {"generated": "verified"}}},
    {"operations": {"Op": {"generated": ["verified"]}}},
])
def test_malformed_registry_raises_locked_error(registry):
    with pytest.raises(OperationLockedError):
        check_call(_spec(), BETA, registry, allow_unverified=True)
    with pytest.raises(OperationLockedError):
        effective_status("Op", FP, registry)
    check_call(_spec(kind="read"), BETA, registry)


def test_unverifiable_and_excluded_statuses_reported():
    assert effective_status("Op", FP, _registry("unverifiable")) == "unverifiable"
    assert effective_status("Op", FP, _registry("excluded")) == "excluded"


def test_error_messages_name_the_flag():
    with pytest.raises(OperationLockedError, match="allow_production"):
        check_call(_spec(), PROD, _registry("verified"))
    with pytest.raises(OperationLockedError, match="allow_unverified"):
        check_call(_spec(), BETA, _registry(None))
    with pytest.raises(OperationLockedError, match="confirm_destructive"):
        check_call(_spec(destructive=True), BETA, _registry("verified"))
    with pytest.raises(OperationLockedError, match="no live verification possible"):
        check_call(_spec(), BETA,
                   _registry("unverifiable", reason="no live verification possible"),
                   allow_unverified=True, allow_production=True, confirm_destructive=True)


def test_override_flags_on_production_for_unverified_still_raise():
    for registry in (_registry(None), _registry("verified", fingerprint="old")):
        with pytest.raises(OperationLockedError):
            check_call(_spec(), PROD, registry, allow_unverified=True,
                       allow_production=True, confirm_destructive=True)


def test_error_is_fserror():
    assert issubclass(OperationLockedError, FSError)


def test_load_registry_default_ships_empty_schema_1():
    reg = load_registry()
    assert reg["schema"] == 1 and reg["operations"] == {}


def test_opspec_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _spec().path = "/y"
