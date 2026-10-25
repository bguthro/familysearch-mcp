import copy
import json

import pytest

from tools.spec.resolve import (
    OVERRIDES_PATH,
    SNAPSHOT_PATH,
    load_overrides,
    resolve,
    to_snake,
)


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(SNAPSHOT_PATH.read_text())


@pytest.fixture(scope="module")
def overrides():
    return load_overrides(OVERRIDES_PATH)


@pytest.fixture(scope="module")
def resolved(snapshot, overrides):
    return resolve(snapshot, overrides)


def by_source(ops, sid):
    return [o for o in ops if o.source_operation_id == sid]


def test_to_snake():
    assert to_snake("readPerson") == "read_person"
    assert to_snake("getAccessToken") == "get_access_token"


def test_counts(resolved):
    ops, excluded = resolved
    assert len(ops) == 203
    assert len(excluded) == 2
    assert sum(o.destructive for o in ops) == 43
    assert len({o.method_name for o in ops}) == len(ops)
    assert ops == sorted(ops, key=lambda o: (o.module, o.operation_id))


def test_rename_delete_operation(resolved):
    ops, _ = resolved
    (op,) = by_source(ops, "delete")
    assert op.operation_id == "deleteUserHistoryEntry"
    assert op.method_name == "delete_user_history_entry"


def test_path_fix_applied_to_both_doubled_ops(resolved):
    ops, _ = resolved
    good = "/tree/users/{uid}/preferred-parent-relationships/{pid}"
    hits = [o for o in ops if "preferred-parent-relationships" in o.path and o.path.startswith("/tree/users")]
    assert len(hits) >= 2
    assert all(o.path.count("preferred-parent-relationships") == 1 for o in hits)
    assert len([o for o in hits if o.path == good]) >= 2


def test_kind_overrides(resolved):
    ops, _ = resolved
    expect = {
        "performPersonMatchesByExample": "read",
        "createNameSegments": "read",
        "getSourceDescriptionChanges": "read",
        "createPartnerAccount": "write",
    }
    for sid, kind in expect.items():
        (op,) = by_source(ops, sid)
        assert op.kind == kind


def test_required_query_pids(resolved):
    ops, _ = resolved
    (op,) = by_source(ops, "readPersons")
    assert "pids" in op.required_query


def test_upload_ops_have_body_upload(resolved):
    ops, _ = resolved
    for sid in ("createPersonMemory", "createMemories", "updateMemoryArtifact"):
        (op,) = by_source(ops, sid)
        assert op.body == "upload"
    assert all(o.body != "upload" for o in ops if o.source_operation_id not in
               ("createPersonMemory", "createMemories", "updateMemoryArtifact"))


def test_path_params_in_template_order(resolved):
    ops, _ = resolved
    for op in ops:
        import re
        assert op.path_params == tuple(re.findall(r"\{(\w+)\}", op.path))
    multi = [o for o in ops if len(o.path_params) >= 2]
    assert multi


def test_fingerprint_stable_and_sensitive(snapshot, overrides, resolved):
    ops, _ = resolved
    again, _ = resolve(copy.deepcopy(snapshot), copy.deepcopy(overrides))
    assert [o.fingerprint for o in ops] == [o.fingerprint for o in again]

    base = {o.source_operation_id: o.fingerprint for o in ops}
    target = "readPerson"

    snap = copy.deepcopy(snapshot)
    for o in snap["operations"]:
        if o["operation_id"] == target:
            o["path"] += "/x"
    changed, _ = resolve(snap, overrides)
    diff = {o.source_operation_id for o in changed if o.fingerprint != base[o.source_operation_id]}
    assert diff == {target}

    ov = copy.deepcopy(overrides)
    ov["kind"][target] = "write"
    changed, _ = resolve(snapshot, ov)
    diff = {o.source_operation_id for o in changed if o.fingerprint != base[o.source_operation_id]}
    assert diff == {target}

    snap = copy.deepcopy(snapshot)
    for o in snap["operations"]:
        if o["operation_id"] == target:
            o["params"].append({"name": "Accept", "in": "header", "required": False,
                                "type": "string", "enum": ["application/x-test"], "example": None})
    changed, _ = resolve(snap, overrides)
    diff = {o.source_operation_id for o in changed if o.fingerprint != base[o.source_operation_id]}
    assert diff == {target}


def test_unknown_override_target_raises(snapshot, overrides):
    for key, val in (
        ("exclude", {"nope": "x"}),
        ("rename", {"nope": "other"}),
        ("kind", {"nope": "read"}),
        ("required_query", {"nope": ["a"]}),
        ("body_kind", {"nope": "upload"}),
        ("path_fix", {"/no/such/path": "/x"}),
    ):
        ov = copy.deepcopy(overrides)
        ov[key] = {**ov[key], **val}
        with pytest.raises(ValueError):
            resolve(snapshot, ov)
    ov = copy.deepcopy(overrides)
    ov["destructive"]["add"] = ["nope"]
    with pytest.raises(ValueError):
        resolve(snapshot, ov)


def test_invalid_kind_override_raises(snapshot, overrides):
    sid = next(o["operation_id"] for o in snapshot["operations"]
               if o["operation_id"] not in overrides["exclude"])
    ov = copy.deepcopy(overrides)
    ov["kind"] = {**ov["kind"], sid: "Write"}
    with pytest.raises(ValueError, match=sid):
        resolve(snapshot, ov)


@pytest.mark.parametrize("method", ["DELETE", "PUT"])
def test_read_override_on_delete_or_put_raises(snapshot, overrides, method):
    sid = next(o["operation_id"] for o in snapshot["operations"]
               if o["method"] == method and o["operation_id"] not in overrides["exclude"])
    ov = copy.deepcopy(overrides)
    ov["kind"] = {**ov["kind"], sid: "read"}
    with pytest.raises(ValueError, match=sid):
        resolve(snapshot, ov)


def test_unknown_top_level_key_raises(tmp_path, overrides):
    p = tmp_path / "o.json"
    p.write_text(json.dumps({**overrides, "bogus": {}}))
    with pytest.raises(ValueError):
        load_overrides(p)
    p.write_text(json.dumps({**overrides, "schema": 2}))
    with pytest.raises(ValueError):
        load_overrides(p)


def _op(oid, path="/a"):
    return {"operation_id": oid, "area": "Family Tree Endpoints", "method": "GET",
            "path": path, "summary": "s", "params": [], "request_media": []}


def test_collision_raises():
    snap = {"schema": 1, "operations": [_op("readThing", "/a"), _op("read_thing", "/b")]}
    ov = {"schema": 1}
    with pytest.raises(ValueError, match="collision"):
        resolve(snap, ov)
