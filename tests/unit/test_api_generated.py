import inspect
import json
import re
from urllib.parse import urlsplit

import pytest
import responses

from familysearch import ApiResult as TopApiResult
from familysearch import FamilySearchApi as TopFamilySearchApi
from familysearch.api import ApiResult, FamilySearchApi, OperationLockedError, OPERATIONS
from familysearch.auth import Environment
from familysearch.client import DEFAULT_ACCEPT, FS_JSON, FSClient
from tools.spec import generate
from tools.spec.resolve import (
    OVERRIDES_PATH, SNAPSHOT_PATH, load_overrides, resolve, to_snake,
)

BETA = Environment.BETA.base_url
OPS = sorted(OPERATIONS.values(), key=lambda o: o.operation_id)
WRITES = [o for o in OPS if o.kind == "write"]
READS = [o for o in OPS if o.kind == "read"]
IDS = [o.operation_id for o in OPS]
WRITE_IDS = [o.operation_id for o in WRITES]
READ_IDS = [o.operation_id for o in READS]
FLAGS = ["reason", "allow_unverified", "allow_production", "confirm_destructive"]
EMPTY = {"schema": 1, "operations": {}}


def _verified_registry():
    return {"schema": 1, "operations": {
        o.operation_id: {"generated": {"status": "verified", "fingerprint": o.fingerprint}}
        for o in OPS
    }}


def _api(registry):
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    return FamilySearchApi(client, registry=registry)


def _kwargs(op):
    kw = {"query": {k: "x" for k in op.required_query}} if op.required_query else {}
    if op.body == "json":
        kw["body"] = {"a": 1}
    elif op.body == "upload":
        kw["data"] = b"x"
        kw["data_content_type"] = "image/png"
    if op.kind == "write":
        kw["reason"] = "test"
        if op.destructive:
            kw["confirm_destructive"] = True
    return kw


def _call(api, op, **kw):
    args = ["A1"] * len(op.path_params)
    return getattr(api, op.method_name)(*args, **kw)


def _expected_path(op):
    path = op.path
    for name in op.path_params:
        path = path.replace("{" + name + "}", "A1")
    return "/platform" + path


def _mock(op):
    url = re.compile(re.escape(BETA + "/platform") + ".*")
    if op.http_method == "HEAD":  # a HEAD response carries no body
        responses.add(op.http_method, url, status=200)
    else:
        responses.add(op.http_method, url, json={"ok": True}, status=200)


def test_top_level_exports():
    assert TopFamilySearchApi is FamilySearchApi and TopApiResult is ApiResult


def test_drift_committed_equals_regenerated():
    assert generate.main(["--check"]) == 0


def test_op_count():
    ops, excluded = resolve(
        json.loads(SNAPSHOT_PATH.read_text()), load_overrides(OVERRIDES_PATH)
    )
    assert len(OPERATIONS) == len(ops) == 203
    assert sum(o.kind == "write" for o in ops) == len(WRITES)
    assert {o.operation_id for o in ops} == set(OPERATIONS)
    assert not set(excluded) & set(OPERATIONS)


def test_fingerprints_match_resolver():
    ops, _ = resolve(json.loads(SNAPSHOT_PATH.read_text()), load_overrides(OVERRIDES_PATH))
    for o in ops:
        assert OPERATIONS[o.operation_id].fingerprint == o.fingerprint, o.operation_id


@pytest.mark.parametrize("op", OPS, ids=IDS)
def test_every_operation_has_a_method(op):
    assert op.method_name == to_snake(op.operation_id)
    method = getattr(FamilySearchApi, op.method_name)
    params = inspect.signature(method).parameters
    positional = [
        n for n, p in params.items()
        if n != "self" and p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert tuple(positional) == op.path_params
    kwonly = {n for n, p in params.items() if p.kind is inspect.Parameter.KEYWORD_ONLY}
    if op.kind == "write":
        assert set(FLAGS) <= kwonly
    else:
        assert not set(FLAGS) & set(params)


@pytest.mark.parametrize("op", OPS, ids=IDS)
@responses.activate
def test_request_shape_per_operation(op):
    _mock(op)
    result = _call(_api(_verified_registry()), op, **_kwargs(op))
    assert isinstance(result, ApiResult) and result.status == 200
    req = responses.calls[0].request
    assert req.method == op.http_method
    assert urlsplit(req.url).path == _expected_path(op)
    assert req.headers["Accept"] == (op.accepts[0] if op.accepts else DEFAULT_ACCEPT)
    assert ("X-Reason" in req.headers) == (op.kind == "write")
    if op.body == "json":
        assert req.headers["Content-Type"] == FS_JSON
        assert json.loads(req.body) == {"a": 1}
    elif op.body == "upload":
        assert req.headers["Content-Type"] == "image/png"
    for key in op.required_query:
        assert key in req.url


@pytest.mark.parametrize("op", WRITES, ids=WRITE_IDS)
@responses.activate
def test_unverified_write_is_refused_before_network(op):
    with pytest.raises(OperationLockedError):
        _call(_api(EMPTY), op, **_kwargs(op))
    assert len(responses.calls) == 0


@pytest.mark.parametrize("op", READS, ids=READ_IDS)
@responses.activate
def test_reads_work_with_empty_registry(op):
    _mock(op)
    result = _call(_api(EMPTY), op, **_kwargs(op))
    assert result.status == 200 and len(responses.calls) == 1


@pytest.mark.parametrize(
    "name",
    ["getSourceDescriptionChanges", "performPersonMatchesByExample", "createNameSegments"],
)
@responses.activate
def test_read_posts_with_json_body_send_body_without_reason(name):
    op = OPERATIONS[name]
    assert op.kind == "read" and op.body == "json"
    _mock(op)
    _call(_api(EMPTY), op, **_kwargs(op))
    req = responses.calls[0].request
    assert json.loads(req.body) == {"a": 1}
    assert "X-Reason" not in req.headers
