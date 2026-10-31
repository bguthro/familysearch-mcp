"""The generic operation tools: list/describe/call over the generated, locked API."""

import inspect
import json

import pytest
import responses

import familysearch.api._runtime as runtime_mod
from familysearch.api import OPERATIONS, FamilySearchApi, OperationLockedError
from familysearch.api._runtime import ApiResult
from familysearch.api.registry import load_registry
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch_mcp import tools_ops
from familysearch_mcp.context import Context, ContextError
from familysearch_mcp.tools_ops import (
    MAX_RESULT_CHARS,
    describe,
    operation_summary,
    register_op_tools,
    shape_result,
)

BETA = Environment.BETA.base_url
PROD = Environment.PRODUCTION.base_url
REASON = "Testing the generic operation tools on beta"
SUMMARY_KEYS = {"operation_id", "area", "method", "path", "kind", "destructive", "status"}

VERIFIED_WRITE = "createPerson"        # verified, not destructive, json body
DESTRUCTIVE = "deletePerson"           # verified, destructive, no body
UNVERIFIED_WRITE = "createDiscussion"  # unverified, not destructive, json body
UNVERIFIABLE = "createPartnerAccount"  # unverifiable
UPLOAD = "createMemories"              # body == "upload"


def _entry(op_id, status, **extra):
    gen = {"status": status, **extra}
    if status == "verified":
        gen["fingerprint"] = OPERATIONS[op_id].fingerprint
    return {"generated": gen}


# Hand-built registry in the nested shape; everything else is unverified.
REG = {"schema": 1, "operations": {
    "readPerson": _entry("readPerson", "verified"),
    VERIFIED_WRITE: _entry(VERIFIED_WRITE, "verified"),
    DESTRUCTIVE: _entry(DESTRUCTIVE, "verified"),
    UNVERIFIABLE: _entry(UNVERIFIABLE, "unverifiable", reason="partner-only"),
}}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class Obj:
    pass


class FakeCtx:
    """Mirrors Context: environment, the write gate, call(fn) and services()."""

    def __init__(self, env=Environment.BETA, writes=None, registry=REG):
        self.environment = env
        self.writes = (env is Environment.BETA) if writes is None else writes
        s = Obj()
        s.client = FSClient("tkn", env, min_write_interval=0)
        s.api = FamilySearchApi(s.client, registry=registry)
        self.svc = s
        self.call_count = 0

    def services(self):
        return self.svc

    def call(self, fn):
        self.call_count += 1
        return fn(self.svc)

    def writes_allowed(self):
        return self.writes

    def require_writes(self):
        if not self.writes:
            raise ContextError("Writes are disabled on production.")


def tools(ctx=None, registry=REG):
    mcp = FakeMCP()
    register_op_tools(mcp, ctx or FakeCtx(registry=registry), registry=registry)
    return mcp.tools


# ---- list_operations ----

def test_list_operations_filters_and_limit():
    t = tools()
    rows = t["list_operations"](limit=500)
    assert len(rows) == 200 < len(OPERATIONS)
    assert [r["operation_id"] for r in rows] == sorted(r["operation_id"] for r in rows)
    assert all(set(r) == SUMMARY_KEYS for r in rows)
    assert len(t["list_operations"](limit=0)) == 1

    area = t["list_operations"](area="memories endpoints", limit=200)
    assert area and all(r["area"] == "Memories Endpoints" for r in area)

    reads = t["list_operations"](kind="read", limit=200)
    assert reads and all(r["kind"] == "read" for r in reads)
    writes = t["list_operations"](kind="WRITE", limit=200)
    assert writes and all(r["kind"] == "write" for r in writes)

    persons = t["list_operations"](query="PERSONS", limit=200)
    assert persons
    for r in persons:
        spec = OPERATIONS[r["operation_id"]]
        hay = (spec.operation_id + spec.summary + spec.path).lower()
        assert "persons" in hay

    real = tools(registry=load_registry())
    verified = real["list_operations"](status="verified", limit=200)
    # The pilot's four and whatever read verification has recorded since: exactly the
    # registry's verified rows (the set grows as recipes pass live).
    registry = load_registry()["operations"]
    assert {r["operation_id"] for r in verified} == {
        op for op, row in registry.items() if row["generated"]["status"] == "verified"}
    assert {"createPerson", "deletePerson", "readPerson", "restorePerson"} <= {
        r["operation_id"] for r in verified}
    assert real["list_operations"](status="unverifiable", limit=200)


def test_operation_summary_shape():
    row = operation_summary(OPERATIONS[DESTRUCTIVE], REG)
    assert row == {"operation_id": DESTRUCTIVE, "area": "Family Tree Endpoints",
                   "method": "DELETE", "path": "/tree/persons/{pid}", "kind": "write",
                   "destructive": True, "status": "verified"}
    assert operation_summary(OPERATIONS[UNVERIFIED_WRITE], REG)["status"] == "unverified"


# ---- describe_operation ----

def test_describe_operation_fields_and_can_run_here():
    t = tools()
    d = t["describe_operation"]("readPerson")
    assert SUMMARY_KEYS <= set(d)
    assert d["description"] == "Read Person"
    assert d["path_params"] == ["pid"] and d["body"] == "none"
    assert "application/x-fs-v1+json" in d["accepts"]
    assert d["needs_reason"] is False
    assert d["can_run_here"] is True and d["blocked_reason"] is None

    d = t["describe_operation"](UNVERIFIED_WRITE)
    assert d["needs_reason"] is True and d["body"] == "json"
    assert d["can_run_here"] is False and "allow_unverified" in d["blocked_reason"]

    d = t["describe_operation"](DESTRUCTIVE)
    assert d["can_run_here"] is False and "confirm_destructive" in d["blocked_reason"]

    d = t["describe_operation"](VERIFIED_WRITE)
    assert d["can_run_here"] is True and d["blocked_reason"] is None

    d = t["describe_operation"](UNVERIFIABLE)
    assert d["can_run_here"] is False and "can never be run" in d["blocked_reason"]

    d = t["describe_operation"](UPLOAD)
    assert d["can_run_here"] is False and "upload_memory" in d["blocked_reason"]

    prod = tools(FakeCtx(Environment.PRODUCTION, writes=True))
    for op in (VERIFIED_WRITE, DESTRUCTIVE):
        d = prod["describe_operation"](op)
        assert d["can_run_here"] is False
        assert "create_person" in d["blocked_reason"] and "beta" in d["blocked_reason"]
    assert prod["describe_operation"]("readPerson")["can_run_here"] is True


def test_describe_read_post_has_note():
    d = describe(OPERATIONS["performPersonMatchesByExample"], REG, Environment.PRODUCTION,
                 False)
    assert d["kind"] == "read" and d["needs_reason"] is False and d["can_run_here"]
    assert d["note"] == tools_ops.READ_POST_NOTE


def test_describe_writes_disabled_on_beta_is_blocked():
    d = describe(OPERATIONS[VERIFIED_WRITE], REG, Environment.BETA, False)
    assert d["can_run_here"] is False and "disabled" in d["blocked_reason"]


def test_describe_unknown_id_raises():
    with pytest.raises(ValueError, match="readPerson"):
        tools()["describe_operation"]("readperson")


# ---- call_operation: reads ----

@responses.activate
def test_call_read_returns_shaped_result():
    responses.get(BETA + "/platform/tree/persons/A1",
                  json={"persons": [{"id": "A1"}]},
                  headers={"Set-Cookie": "secret=1"})
    out = tools()["call_operation"]("readPerson", path_args={"pid": "A1"})
    assert out == {"status": 200, "content_type": "application/json", "entity_id": None,
                   "body": {"persons": [{"id": "A1"}]}, "truncated": False}
    req = responses.calls[0].request
    assert "X-Reason" not in req.headers
    assert "tkn" not in json.dumps(out) and "secret" not in json.dumps(out)


@responses.activate
def test_call_read_on_production_needs_no_flags():
    responses.get(PROD + "/platform/tree/persons/A1", json={"ok": 1})
    out = tools(FakeCtx(Environment.PRODUCTION, writes=False))["call_operation"](
        "readPerson", path_args={"pid": "A1"}, query={"relatives": True})
    assert out["body"] == {"ok": 1}
    assert "relatives=true" in responses.calls[0].request.url


@responses.activate
def test_call_read_204_body_none():
    responses.get(BETA + "/platform/tree/persons/A1", status=204)
    out = tools()["call_operation"]("readPerson", path_args={"pid": "A1"})
    assert out["status"] == 204 and out["body"] is None and out["truncated"] is False


@responses.activate
def test_call_read_non_json_returns_text():
    responses.get(BETA + "/platform/tree/persons/A1", body="<person/>",
                  content_type="application/xml")
    out = tools()["call_operation"]("readPerson", path_args={"pid": "A1"},
                                    accept="application/xml")
    assert out["body"] == "<person/>"
    assert responses.calls[0].request.headers["Accept"] == "application/xml"


@responses.activate
def test_call_read_post_json_body_allowed():
    responses.post(BETA + "/platform/tree/matches", json={"entries": []})
    body = {"persons": [{"names": []}]}
    out = tools(FakeCtx(writes=False))["call_operation"](
        "performPersonMatchesByExample", body=body)
    assert out["body"] == {"entries": []}
    req = responses.calls[0].request
    assert json.loads(req.body) == body
    assert "X-Reason" not in req.headers


@responses.activate
def test_result_truncated_over_cap():
    big = {"x": "y" * (MAX_RESULT_CHARS + 10)}
    responses.get(BETA + "/platform/tree/persons/A1", json=big)
    out = tools()["call_operation"]("readPerson", path_args={"pid": "A1"})
    assert out["truncated"] is True
    preview = out["body"]["truncated_preview"]
    assert len(preview) == MAX_RESULT_CHARS and preview == json.dumps(big)[:MAX_RESULT_CHARS]


def test_shape_result_text_truncated_and_entity_id():
    text = "z" * (MAX_RESULT_CHARS + 1)
    out = shape_result(ApiResult(200, {"Location": "https://x/tree/persons/NEW1"},
                                 "text/plain", None, text))
    assert out["truncated"] is True and out["entity_id"] == "NEW1"
    assert out["body"] == {"truncated_preview": text[:MAX_RESULT_CHARS]}
    out = shape_result(ApiResult(200, {}, "application/json", None, "not json {"))
    assert out["body"] == "not json {"
    out = shape_result(ApiResult(200, {}, "", None, ""))
    assert out["body"] is None and out["truncated"] is False


# ---- call_operation: production writes (Review Focus) ----

@responses.activate
@pytest.mark.parametrize("op, flags", [
    (VERIFIED_WRITE, {}),
    (DESTRUCTIVE, {"confirm_destructive": True}),
    (UNVERIFIED_WRITE, {"allow_unverified": True}),
    (UNVERIFIED_WRITE, {"allow_unverified": True, "confirm_destructive": True}),
])
def test_production_writes_refused_before_network(monkeypatch, op, flags):
    monkeypatch.setenv("FS_ENVIRONMENT", "production")
    monkeypatch.setenv("FS_ALLOW_PROD_WRITES", "1")
    ctx = Context()
    assert ctx.writes_allowed() is True

    def no_build():
        raise AssertionError("services built (token lifted) for a production write")

    monkeypatch.setattr(ctx, "_build", no_build)
    path_args = {"pid": "A1"} if OPERATIONS[op].path_params else None
    body = {"persons": []} if OPERATIONS[op].body == "json" else None
    with pytest.raises(ContextError, match="beta") as ei:
        tools(ctx, registry=load_registry())["call_operation"](
            op, path_args=path_args, body=body, reason=REASON, **flags)
    assert "create_person" in str(ei.value)
    assert len(responses.calls) == 0


@responses.activate
def test_allow_production_unreachable(monkeypatch):
    params = inspect.signature(tools()["call_operation"]).parameters
    assert "allow_production" not in params
    seen = []
    real = runtime_mod.check_call

    def spy(*a, **k):
        seen.append(k.get("allow_production"))
        return real(*a, **k)

    monkeypatch.setattr(runtime_mod, "check_call", spy)
    responses.post(BETA + "/platform/tree/persons", status=201,
                   headers={"X-Entity-Id": "NEW1"})
    out = tools()["call_operation"](VERIFIED_WRITE, body={"persons": []}, reason=REASON)
    assert out["entity_id"] == "NEW1"
    with pytest.raises(TypeError):
        tools()["call_operation"](VERIFIED_WRITE, body={}, reason=REASON,
                                  allow_production=True)
    assert seen == [False]


# ---- call_operation: argument checks (Review Focus) ----

@pytest.mark.parametrize("op_id, suggestion", [
    ("", None),
    ("readperson", "readPerson"),
    ("readPerson ", "readPerson"),
    ("deleteUserHistoryEntry ", None),
    (None, None),
    (123, None),
    ("None", None),
])
def test_unknown_operation_id_variants(op_id, suggestion):
    ctx = FakeCtx()
    with pytest.raises(ValueError, match="unknown operation") as ei:
        tools(ctx)["call_operation"](op_id)
    if suggestion:
        assert suggestion in str(ei.value)
    assert ctx.call_count == 0


@responses.activate
@pytest.mark.parametrize("path_args, match", [
    (None, "missing"),
    ({}, "missing"),
    ({"pid": "A1", "nid": "x"}, "unexpected"),
    ({"pid": "../x"}, "pid"),
    ({"pid": "a/b"}, "pid"),
    ({"pid": "a\\b"}, "pid"),
    ({"pid": ".."}, "pid"),
    ({"pid": ""}, "pid"),
    ({"pid": "   "}, "pid"),
    ({"pid": 7}, "pid"),
])
def test_path_args_validation(path_args, match):
    ctx = FakeCtx()
    with pytest.raises(ValueError, match=match):
        tools(ctx)["call_operation"]("readPerson", path_args=path_args)
    assert ctx.call_count == 0 and len(responses.calls) == 0


@responses.activate
def test_path_arg_quoted_as_one_segment():
    responses.get(BETA + "/platform/tree/persons/A%20B%3A1", json={})
    tools()["call_operation"]("readPerson", path_args={"pid": "A B:1"})
    assert len(responses.calls) == 1


@responses.activate
def test_query_may_not_carry_path_params():
    ctx = FakeCtx()
    with pytest.raises(ValueError, match="pid"):
        tools(ctx)["call_operation"]("readPerson", path_args={"pid": "A1"},
                                     query={"pid": "B2"})
    assert ctx.call_count == 0 and len(responses.calls) == 0


@responses.activate
def test_body_for_bodyless_op_raises():
    ctx = FakeCtx()
    with pytest.raises(ValueError, match="body"):
        tools(ctx)["call_operation"]("readPerson", path_args={"pid": "A1"}, body={"x": 1})
    with pytest.raises(ValueError, match="body"):
        tools(ctx)["call_operation"](DESTRUCTIVE, path_args={"pid": "A1"}, body={"x": 1},
                                     reason=REASON, confirm_destructive=True)
    assert ctx.call_count == 0 and len(responses.calls) == 0


# ---- call_operation: writes on beta ----

@responses.activate
def test_write_on_beta_needs_reason_gate_and_lock():
    body = {"discussions": []}
    for blank in (None, "", "   "):
        with pytest.raises(ValueError, match="reason"):
            tools()["call_operation"](UNVERIFIED_WRITE, body=body, reason=blank,
                                      allow_unverified=True)

    gated = FakeCtx(writes=False)
    with pytest.raises(ContextError, match="disabled"):
        tools(gated)["call_operation"](VERIFIED_WRITE, body=body, reason=REASON)
    assert gated.call_count == 0

    with pytest.raises(OperationLockedError, match="allow_unverified"):
        tools()["call_operation"](UNVERIFIED_WRITE, body=body, reason=REASON)
    assert len(responses.calls) == 0

    responses.post(BETA + "/platform/discussions/discussions", status=201,
                   headers={"Location": BETA + "/platform/discussions/discussions/D1"})
    out = tools()["call_operation"](UNVERIFIED_WRITE, body=body, reason=REASON,
                                    allow_unverified=True)
    assert out["status"] == 201 and out["entity_id"] == "D1" and out["body"] is None
    assert responses.calls[0].request.headers["X-Reason"] == REASON

    with pytest.raises(OperationLockedError, match="confirm_destructive"):
        tools()["call_operation"](DESTRUCTIVE, path_args={"pid": "A1"}, reason=REASON)
    responses.delete(BETA + "/platform/tree/persons/A1", status=204)
    out = tools()["call_operation"](DESTRUCTIVE, path_args={"pid": "A1"}, reason=REASON,
                                    confirm_destructive=True)
    assert out["status"] == 204

    with pytest.raises(OperationLockedError, match="can never be run"):
        tools()["call_operation"](UNVERIFIABLE, reason=REASON,
                                  allow_unverified=True, confirm_destructive=True)
    assert len(responses.calls) == 2


@responses.activate
@pytest.mark.parametrize("env", [Environment.BETA, Environment.PRODUCTION])
def test_upload_op_refused(env):
    ctx = FakeCtx(env, writes=True)
    with pytest.raises(ValueError, match="upload_memory"):
        tools(ctx)["call_operation"](UPLOAD, reason=REASON, allow_unverified=True)
    assert ctx.call_count == 0 and len(responses.calls) == 0


# ---- registration ----

def test_tools_registered():
    assert set(tools()) == {"list_operations", "describe_operation", "call_operation"}
    from familysearch_mcp.server import build_server
    import asyncio
    names = {t.name for t in asyncio.run(build_server(FakeCtx()).list_tools())}
    assert {"list_operations", "describe_operation", "call_operation"} <= names
    assert len(names) == 21


# ---- instructions and docs ----

def test_instructions_cover_generic_tools():
    from familysearch_mcp.server import SERVER_INSTRUCTIONS as s
    assert len(s) <= 1500
    for w in ("list_operations", "describe_operation", "call_operation",
              "allow_unverified", "confirm_destructive", "upload_memory", "beta"):
        assert w in s
    assert "There is no delete or merge tool." not in s
    assert "no delete or merge tool on production" in s
    assert "guarded tools" in s


def test_tools_write_docstring_not_absolute():
    from familysearch_mcp import tools_write
    assert "There is deliberately no delete or merge tool." not in tools_write.__doc__
    assert "call_operation" in tools_write.__doc__


def test_list_and_describe_docstrings_read_only():
    t = tools()
    for name in ("list_operations", "describe_operation"):
        d = " ".join((t[name].__doc__ or "").split()).lower()
        assert "read-only" in d and "no network" in d and "203" in d


def test_readme_documents_generic_tools():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    for w in ("list_operations", "describe_operation", "call_operation", "19 tools",
              "allow_unverified", "confirm_destructive", "createPerson", "readPerson",
              "deletePerson", "restorePerson", "upload_memory"):
        assert w in readme
    assert "beta only" in readme
    assert "No delete or merge tools exist" not in readme
    assert "Merges are never performed" in readme


# ---- merge policy: mergePerson is never executed ----

MERGE = "mergePerson"


@responses.activate
def test_merge_person_is_unverifiable_with_policy_reason():
    reg = load_registry()
    gen = reg["operations"][MERGE]["generated"]
    assert gen["status"] == "unverifiable" and "never executed" in gen["reason"]
    t = tools(registry=reg)
    row = next(r for r in t["list_operations"](status="unverifiable", limit=200)
               if r["operation_id"] == MERGE)
    assert row["status"] == "unverifiable"
    d = t["describe_operation"](MERGE)
    assert d["status"] == "unverifiable" and d["can_run_here"] is False
    assert "FamilySearch website" in d["blocked_reason"]
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.parametrize("env", [Environment.BETA, Environment.PRODUCTION])
def test_merge_person_refused_via_mcp_with_every_flag(env):
    reg = load_registry()
    ctx = FakeCtx(env=env, registry=reg)
    with pytest.raises(OperationLockedError) as e:
        tools(ctx, registry=reg)["call_operation"](
            MERGE, path_args={"pid": "A1", "dpid": "A2"}, reason=REASON,
            allow_unverified=True, confirm_destructive=True)
    msg = str(e.value)
    assert "deliberately never executed by this server" in msg
    assert "FamilySearch website" in msg
    assert ctx.call_count == 0 and len(responses.calls) == 0


@responses.activate
def test_merge_person_refused_via_library_with_every_flag():
    reg = load_registry()
    api = FamilySearchApi(FSClient("tkn", Environment.BETA, min_write_interval=0),
                          registry=reg)
    with pytest.raises(OperationLockedError, match="can never be run"):
        api.merge_person("A1", "A2", reason=REASON, allow_unverified=True,
                         confirm_destructive=True)
    assert len(responses.calls) == 0


def test_instructions_forbid_merging():
    from familysearch_mcp.server import SERVER_INSTRUCTIONS as s
    low = s.lower()
    assert "merg" in low and "never" in low and "website" in low
