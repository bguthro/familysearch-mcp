"""Live beta acceptance for the MCP server, driven through the REAL FastMCP call path.

Every write creates ZZSPIKE throwaway data on BETA ONLY and removes it at teardown,
even on failure. Run with FS_USE_BRAVE=1 (Keychain prompt) or FS_BETA_TOKEN=<token>;
add `-s` to see the "[OBSERVED]" live-shape lines.
"""
import asyncio
import base64
import json
import os
import re
import sys
import uuid

import pytest

if not (os.environ.get("FS_BETA_TOKEN") or os.environ.get("FS_USE_BRAVE") == "1"):
    pytest.skip("no beta token (FS_BETA_TOKEN or FS_USE_BRAVE=1)", allow_module_level=True)

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

import familysearch_mcp.context as context_mod  # noqa: E402
from familysearch import Environment  # noqa: E402
from familysearch_mcp.context import Context  # noqa: E402
from familysearch_mcp.server import build_server  # noqa: E402

from _tracker import Tracker  # noqa: E402

REASON = "ZZSPIKE automated MCP integration test"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
ID_RE = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{3,5}")
FORBIDDEN_USER_KEYS = ("birthDate", "mobilePhoneNumber", "helperAccessPin", "email")


# ---- fixtures ------------------------------------------------------------

@pytest.fixture(scope="module")
def upload_root(tmp_path_factory):
    return tmp_path_factory.mktemp("zzspike_upload_root")


@pytest.fixture(scope="module")
def ctx(upload_root):
    mp = pytest.MonkeyPatch()
    mp.setenv("FS_ENVIRONMENT", "beta")
    mp.setenv("FS_UPLOAD_ROOT", str(upload_root))
    mp.delenv("FS_ALLOW_PROD_WRITES", raising=False)
    token = os.environ.get("FS_BETA_TOKEN")
    if token:
        mp.setattr(context_mod, "extract_brave_token", lambda: token)
    c = Context()
    assert c.environment is Environment.BETA, "integration suite must run on beta"
    yield c
    mp.undo()


@pytest.fixture(scope="module")
def server(ctx):
    return build_server(ctx)


def _payload(result):
    """Normalize call_tool output to the tool's JSON value."""
    content, structured = result if isinstance(result, tuple) else (result, None)
    if structured is not None:
        return structured["result"] if set(structured) == {"result"} else structured
    return json.loads("".join(getattr(c, "text", "") for c in content))


@pytest.fixture(scope="module")
def beta_verified(server, ctx):
    """HARD GATE: nothing else runs unless auth_status says beta."""
    assert ctx.environment is Environment.BETA
    out = _payload(asyncio.run(server.call_tool("auth_status", {})))
    if out.get("environment") != "beta" or out.get("writes_allowed") is not True:
        pytest.exit(f"NOT BETA: auth_status said {out.get('environment')!r}; aborting "
                    "before any write", returncode=2)
    return out


@pytest.fixture
def call(server, beta_verified):
    def _call(name, **args):
        return _payload(asyncio.run(server.call_tool(name, args)))
    return _call


@pytest.fixture
def cleanup(ctx, beta_verified):
    t = Tracker(ctx)
    yield t
    t.run()
    assert not t.errors, f"LEFTOVER ZZSPIKE data on beta: {t.errors}"


@pytest.fixture
def observe(record_property):
    def _observe(key, value):
        text = json.dumps(value, default=str, sort_keys=True)
        record_property(key, text)
        print(f"[OBSERVED] {key}: {text}", file=sys.stderr)
    return _observe


# ---- helpers -------------------------------------------------------------

def uniq():
    return f"ZZSPIKE{uuid.uuid4().hex[:8]}"


def make_person(call, cleanup, gender="Male", birth="1850"):
    out = call("create_person", given="Test", surname=uniq(), gender=gender,
               birth_date=birth, reason=REASON)
    if isinstance(out, dict) and out.get("id"):
        cleanup.persons.append(out["id"])  # tracked before any assertion
    assert out["status"] == "created", out
    return out["id"]


def write_png(root, name):
    p = root / name
    p.write_bytes(PNG)
    return str(p)


# ---- tests ---------------------------------------------------------------

def test_auth_status(call, beta_verified):
    out = call("auth_status")
    assert out["environment"] == "beta"
    assert out["writes_allowed"] is True
    assert set(out["user"]) == {"id", "displayName", "personId"}
    blob = json.dumps(out)
    for k in FORBIDDEN_USER_KEYS:
        assert k not in blob


def test_search_tree_sees_real_results(call):
    hits = call("search_tree", given="John", surname="Smith")
    assert isinstance(hits, list) and hits
    for h in hits:
        assert isinstance(h["name"], str) and h["name"]
        assert isinstance(h["id"], str) and h["id"]


def test_match_gate_live_blocks_john_smith(call, cleanup):
    out = call("create_person", given="John", surname="Smith", gender="Male",
               birth_date="1846", reason=REASON)
    if out.get("id"):  # should never happen; make sure it is cleaned up anyway
        cleanup.persons.append(out["id"])
    assert out["status"] == "matches_found", out
    assert out["candidates"]
    assert "id" not in out


def test_create_deceased_person_and_get(call, cleanup):
    surname = uniq()
    out = call("create_person", given="Test", surname=surname, gender="Male",
               birth_date="1850", reason=REASON)
    if out.get("id"):
        cleanup.persons.append(out["id"])
    assert out["status"] == "created", out
    assert out["living"] is False and out["life_status"] == "deceased_by_age"
    assert ID_RE.fullmatch(out["id"])
    person = call("get_person", person_id=out["id"])
    assert person["id"] == out["id"]
    assert surname in (person["name"] or "")


def test_living_routing(call, cleanup, observe):
    surname = uniq()
    args = dict(given="Test", surname=surname, gender="Female", birth_date="1990",
                reason=REASON)
    held = call("create_person", **args)
    if held.get("id"):
        cleanup.persons.append(held["id"])
    assert held["status"] == "needs_life_confirmation", held
    assert "id" not in held
    out = call("create_person", living=True, **args)
    if out.get("id"):
        cleanup.persons.append(out["id"])
    assert out["status"] == "created", out
    assert out["living"] is True
    person = call("get_person", person_id=out["id"])
    observe("living_person_get_person_living", person.get("living"))
    observe("living_person_get_person", person)
    assert person["id"] == out["id"]
    assert person["living"] is not False  # True, or None if the API omits it


def test_add_fact_residence(call, cleanup, observe):
    pid = make_person(call, cleanup)
    out = call("add_fact", person_id=pid, fact_type="Residence",
               place="Lynn, Massachusetts", reason=REASON)
    assert out["status"] == "added", out
    person = call("get_person", person_id=pid)
    residences = [f for f in person["facts"] if f["type"] == "Residence"]
    observe("residence_fact", residences)
    observe("all_facts", person["facts"])
    assert residences, person["facts"]


def test_relationships(call, cleanup):
    dad = make_person(call, cleanup, "Male", "1850")
    mom = make_person(call, cleanup, "Female", "1852")
    couple = call("create_couple", person1_id=dad, person2_id=mom, reason=REASON)
    if couple.get("relationship_id"):
        cleanup.relationships.append(couple["relationship_id"])
    assert couple["status"] == "created" and couple["relationship_id"], couple
    child = make_person(call, cleanup, "Male", "1875")
    pc = call("create_parent_child", child_id=child, parent_ids=[dad, mom], reason=REASON)
    cleanup.relationships.extend(i for i in pc.get("relationship_ids", []) if i)
    assert pc["status"] == "created", pc
    assert len(pc["relationship_ids"]) == 2 and all(pc["relationship_ids"])
    rel = call("get_relatives", person_id=child)
    assert {p["id"] for p in rel["parents"]} == {dad, mom}, rel


def test_memory_upload_link_list(call, cleanup, upload_root, observe):
    pid = make_person(call, cleanup)
    path = write_png(upload_root, "zzspike-clean.png")
    up = call("upload_memory", file_path=path,
              provenance="ZZSPIKE generated locally for testing", reason=REASON)
    if up.get("memory_id"):
        cleanup.memory_ids.append(up["memory_id"])
    assert up["status"] == "uploaded", up
    mid = up["memory_id"]
    assert mid.isdigit()
    link = call("link_memory", person_id=pid, memory_id=mid, reason=REASON)
    assert link["status"] == "linked", link
    mems = call("get_person_memories", person_id=pid)
    observe("memory_list_items", mems)
    assert isinstance(mems, list)
    match = [m for m in mems if m["id"] == mid]
    assert match, f"memory {mid} not in {mems}"
    assert match[0]["id"] is not None


def test_provenance_gates_live(call, cleanup, upload_root, monkeypatch):
    blocked = write_png(upload_root, "ZZSPIKE Fold3 test.png")
    out = call("upload_memory", file_path=blocked, provenance="x", reason=REASON)
    if out.get("memory_id"):
        cleanup.memory_ids.append(out["memory_id"])
    assert out["status"] == "provenance_blocked", out
    assert out["aggregator"] == "fold3"
    assert "memory_id" not in out

    clean = write_png(upload_root, "zzspike-noprov.png")
    out = call("upload_memory", file_path=clean, reason=REASON)
    if out.get("memory_id"):
        cleanup.memory_ids.append(out["memory_id"])
    assert out["status"] == "provenance_unknown", out
    assert "memory_id" not in out

    monkeypatch.delenv("FS_UPLOAD_ROOT")
    out = call("upload_memory", file_path=clean, provenance="ZZSPIKE local", reason=REASON)
    if out.get("memory_id"):
        cleanup.memory_ids.append(out["memory_id"])
    assert out["status"] == "upload_root_not_configured", out


def test_source_create_attach_list(call, cleanup, observe):
    pid = make_person(call, cleanup)
    src = call("create_source", about_url="https://example.org/zzspike",
               title="ZZSPIKE source", citation="ZZSPIKE citation", reason=REASON)
    if src.get("source_id"):
        cleanup.source_ids.append(src["source_id"])
    assert src["status"] == "created" and src["source_id"], src
    sid = src["source_id"]
    att = call("attach_source", person_id=pid, source_id=sid, reason=REASON)
    assert att["status"] == "attached", att
    sources = call("get_person_sources", person_id=pid)
    observe("source_list_items", sources)
    assert isinstance(sources, list)
    assert sid in [s["id"] for s in sources], sources


def test_matches_and_record_search(call, cleanup, ctx, observe):
    pid = make_person(call, cleanup)
    own = call("get_matches", person_id=pid)
    assert isinstance(own, list)

    rec = call("search_records", surname="Anderson", given="August", count=3)
    assert isinstance(rec["total"], int) and rec["total"] > 0
    assert rec["entries"]
    for e in rec["entries"]:
        assert isinstance(e["name"], str) and e["name"]

    hits = call("search_tree", given="John", surname="Smith", count=5)
    known = next((h["id"] for h in hits if h.get("id")), None)
    if known is None:
        pytest.skip("no pre-existing person found via search_tree")
    projected = call("get_matches", person_id=known)
    raw = ctx.call(lambda s: s.tree.get_matches(known))
    observe("matches_raw_first_entry_keys", sorted(raw[0]) if raw else None)
    observe("matches_raw_count", len(raw))
    observe("matches_projected", projected[:3])
    assert isinstance(projected, list) and len(projected) == len(raw)
    for h in projected:
        assert set(h) == {"id", "confidence", "name", "birth", "death", "gender"}


def test_error_paths(call, cleanup):
    with pytest.raises(ToolError) as ei:
        call("get_person", person_id="not-an-id")
    assert "not-an-id" in str(ei.value)

    try:
        out = call("create_person", given="Test", surname=uniq(), gender="Male",
                   birth_date="1850", reason="   ")
    except ToolError:
        return
    if isinstance(out, dict) and out.get("id"):
        cleanup.persons.append(out["id"])
    pytest.fail(f"blank reason was accepted: {out}")
