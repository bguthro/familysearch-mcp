"""Server assembly under the REAL mcp SDK: schemas, stdio protocol, call path."""

import asyncio
import importlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import familysearch_mcp.server as server_mod
from familysearch_mcp import tools_write
from familysearch_mcp.context import ContextError
from familysearch_mcp.server import SERVER_INSTRUCTIONS, build_server, main

FIX = Path(__file__).parent / "fixtures"
PERSON = json.loads((FIX / "person_named.json").read_text())["persons"][0]
HIT = json.loads((FIX / "search_entry.json").read_text())

READ = {"auth_status", "search_tree", "search_records", "get_person", "get_relatives",
        "get_person_sources", "get_person_memories", "get_matches"}
WRITE = {"create_person", "add_fact", "create_couple", "create_parent_child",
         "upload_memory", "link_memory", "create_source", "attach_source"}
ALL = READ | WRITE

REQUIRED = {
    "auth_status": [],
    "search_tree": [],
    "search_records": [],
    "get_person": ["person_id"],
    "get_relatives": ["person_id"],
    "get_person_sources": ["person_id"],
    "get_person_memories": ["person_id"],
    "get_matches": ["person_id"],
    "create_person": ["given", "surname", "gender", "reason"],
    "add_fact": ["person_id", "fact_type", "reason"],
    "create_couple": ["person1_id", "person2_id", "reason"],
    "create_parent_child": ["child_id", "parent_ids", "reason"],
    "upload_memory": ["file_path", "reason"],
    "link_memory": ["person_id", "memory_id", "reason"],
    "create_source": ["about_url", "title", "citation", "reason"],
    "attach_source": ["person_id", "source_id", "reason"],
}


class Obj:
    pass


class Rec:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self.result


class FakeCtx:
    environment = "beta"

    def __init__(self, svc, writes=True):
        self.svc = svc
        self.writes = writes
        self.call_count = 0

    def call(self, fn):
        self.call_count += 1
        return fn(self.svc)

    def writes_allowed(self):
        return self.writes

    def require_writes(self):
        if not self.writes:
            raise ContextError("Writes are disabled on production.")


def fake(writes=True, hits=()):
    s = Obj()
    s.tree = Obj()
    s.tree.get_person = Rec(PERSON)
    s.search = Obj()
    s.search.tree_search = Rec(list(hits))
    s.writer = Obj()
    s.writer.create_person = Rec("NEW1-ABC")
    ctx = FakeCtx(s, writes)
    return build_server(ctx), s, ctx


def tools_of(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


# ---- schemas (G2) ----

def test_exact_tool_names_and_no_delete_merge():
    names = set(tools_of(build_server(FakeCtx(Obj()))))
    assert names == ALL
    assert not [n for n in names if "delete" in n or "merge" in n]


def test_required_table_covers_all_tools():
    assert set(REQUIRED) == ALL


@pytest.mark.parametrize("name", sorted(ALL))
def test_schema_required_and_description(name):
    t = tools_of(build_server(FakeCtx(Obj())))[name]
    assert t.description and t.description.strip()
    schema = t.inputSchema
    assert "properties" in schema
    assert sorted(schema.get("required", [])) == sorted(REQUIRED[name])


def _types(prop):
    if "type" in prop:
        return {prop["type"]}
    return {a.get("type") for a in prop.get("anyOf", [])}


def test_property_types():
    tools = tools_of(build_server(FakeCtx(Obj())))
    cp = tools["create_person"].inputSchema["properties"]
    for k in ("given", "surname", "gender", "reason"):
        assert _types(cp[k]) == {"string"}
    for k in ("birth_date", "birth_place", "death_date"):
        assert _types(cp[k]) == {"string", "null"}
    assert _types(cp["living"]) == {"boolean", "null"}
    for k in ("create_despite_matches", "confirm_deceased"):
        assert _types(cp[k]) == {"boolean"}
    pc = tools["create_parent_child"].inputSchema["properties"]["parent_ids"]
    assert pc["type"] == "array" and pc["items"]["type"] == "string"
    sr = tools["search_records"].inputSchema["properties"]
    assert _types(sr["count"]) == {"integer"} and _types(sr["offset"]) == {"integer"}
    assert _types(tools["search_tree"].inputSchema["properties"]["count"]) == {"integer"}
    um = tools["upload_memory"].inputSchema["properties"]
    assert _types(um["override_rights"]) == {"boolean"}
    assert _types(um["title"]) == {"string", "null"}


S, N = "string", "null"
EXPECTED_TYPES = {
    "auth_status": {},
    "search_tree": {"given": {S, N}, "surname": {S, N}, "birth_date": {S, N},
                    "birth_place": {S, N}, "count": {"integer"}},
    "search_records": {"given": {S, N}, "surname": {S, N}, "count": {"integer"},
                       "offset": {"integer"}},
    "get_person": {"person_id": {S}},
    "get_relatives": {"person_id": {S}},
    "get_person_sources": {"person_id": {S}},
    "get_person_memories": {"person_id": {S}},
    "get_matches": {"person_id": {S}},
    "create_person": {"given": {S}, "surname": {S}, "gender": {S}, "reason": {S},
                      "birth_date": {S, N}, "birth_place": {S, N}, "death_date": {S, N},
                      "living": {"boolean", N}, "create_despite_matches": {"boolean"},
                      "confirm_deceased": {"boolean"}},
    "add_fact": {"person_id": {S}, "fact_type": {S}, "reason": {S}, "date": {S, N},
                 "place": {S, N}},
    "create_couple": {"person1_id": {S}, "person2_id": {S}, "reason": {S}},
    "create_parent_child": {"child_id": {S}, "parent_ids": {"array"}, "reason": {S}},
    "upload_memory": {"file_path": {S}, "reason": {S}, "title": {S, N},
                      "provenance": {S, N}, "override_rights": {"boolean"}},
    "link_memory": {"person_id": {S}, "memory_id": {S}, "reason": {S}},
    "create_source": {"about_url": {S}, "title": {S}, "citation": {S}, "reason": {S}},
    "attach_source": {"person_id": {S}, "source_id": {S}, "reason": {S}},
}


def test_expected_types_table_covers_all_tools():
    assert set(EXPECTED_TYPES) == ALL


@pytest.mark.parametrize("name", sorted(ALL))
def test_property_types_all_tools(name):
    props = tools_of(build_server(FakeCtx(Obj())))[name].inputSchema["properties"]
    assert set(props) == set(EXPECTED_TYPES[name])
    for prop, want in EXPECTED_TYPES[name].items():
        assert _types(props[prop]) == want, (name, prop, props[prop])
    if name == "create_parent_child":
        assert props["parent_ids"]["items"]["type"] == "string"


# ---- stdout hygiene / lazy build / main (G3, G6) ----

def test_import_and_build_print_nothing(monkeypatch, capsys):
    monkeypatch.setenv("FS_ENVIRONMENT", "beta")
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod = importlib.reload(server_mod)
        mod.build_server()
    assert buf.getvalue() == ""
    assert capsys.readouterr().out == ""


def test_import_prints_nothing_in_subprocess():
    proc = subprocess.run([sys.executable, "-c", "import familysearch_mcp.server"],
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b""


def test_build_is_lazy(monkeypatch):
    import familysearch_mcp.context as c

    def boom():
        raise AssertionError("token lifted at build time")

    monkeypatch.setattr(c, "extract_brave_token", boom)
    monkeypatch.setenv("FS_ENVIRONMENT", "beta")
    assert build_server() is not None


def test_main_bad_environment_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("FS_ENVIRONMENT", "bogus")
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("familysearch-mcp: ") and "FS_ENVIRONMENT" in err
    assert "Traceback" not in err


def test_main_runs_server(monkeypatch):
    ran = []

    class S:
        def run(self):
            ran.append(1)

    monkeypatch.setattr(server_mod, "build_server", lambda: S())
    main()
    assert ran == [1]


# ---- instructions (G4) ----

def test_instructions_content():
    assert len(SERVER_INSTRUCTIONS) <= 1500
    for word in ("Ben", "create_despite_matches", "confirm_deceased", "override_rights",
                 "auth_status", "FS_UPLOAD_ROOT", "provenance"):
        assert word in SERVER_INSTRUCTIONS
    assert build_server(FakeCtx(Obj())).instructions == SERVER_INSTRUCTIONS


# ---- real stdio protocol (G5) ----

async def _smoke():
    env = {k: v for k, v in os.environ.items() if k != "FS_ALLOW_PROD_WRITES"}
    env["FS_ENVIRONMENT"] = "beta"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "familysearch_mcp.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            return init, listed.tools


def test_stdio_smoke():
    init, tools = asyncio.run(asyncio.wait_for(_smoke(), 30))
    assert {t.name for t in tools} == ALL
    assert not [t.name for t in tools if "delete" in t.name or "merge" in t.name]
    for t in tools:
        assert t.description and t.description.strip()
        assert "properties" in t.inputSchema
    assert init.instructions == SERVER_INSTRUCTIONS


# ---- real call path, in-process (G8) ----

def call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def payload(result):
    """Normalize call_tool output to the tool's JSON value."""
    content, structured = result if isinstance(result, tuple) else (result, None)
    if structured is not None:
        return structured["result"] if set(structured) == {"result"} else structured
    return json.loads("".join(getattr(c, "text", "") for c in content))


def test_call_get_person_success():
    server, svc, _ = fake()
    out = payload(call(server, "get_person", {"person_id": "GKLV-DY8"}))
    assert out["id"] == PERSON["id"]
    assert svc.tree.get_person.calls[0][0] == ("GKLV-DY8",)


def test_call_invalid_person_id_is_error_and_no_service_call():
    server, svc, ctx = fake()
    with pytest.raises(ToolError) as ei:
        call(server, "get_person", {"person_id": "not an id!"})
    assert "person" in str(ei.value).lower()
    assert ctx.call_count == 0 and svc.tree.get_person.calls == []


def test_call_write_disabled_payload_not_error():
    server, svc, ctx = fake(writes=False)
    out = payload(call(server, "create_person", dict(
        given="John", surname="Smith", gender="Male", reason="test")))
    assert out["status"] == "writes_disabled"
    assert svc.writer.create_person.calls == []


def test_call_create_person_matches_found(monkeypatch):
    monkeypatch.setattr(tools_write, "_current_year", lambda: 2026)
    server, svc, _ = fake(hits=[HIT])
    out = payload(call(server, "create_person", dict(
        given="John", surname="Smith", gender="Male", reason="test",
        birth_date="1850")))
    assert out["status"] == "matches_found"
    assert out["candidates"]
    assert svc.writer.create_person.calls == []


def test_call_missing_required_rejected_by_sdk():
    server, svc, ctx = fake()
    with pytest.raises(ToolError):
        call(server, "create_person", dict(given="John", surname="Smith", gender="Male"))
    assert ctx.call_count == 0 and svc.writer.create_person.calls == []
