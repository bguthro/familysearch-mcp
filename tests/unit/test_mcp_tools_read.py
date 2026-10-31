import json
from pathlib import Path

import pytest

from familysearch import FSError
from familysearch_mcp.tools_read import register_read_tools

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class Rec:
    """Records every call made to it."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc:
            raise self.exc
        return self.result


class Obj:
    pass


PERSON = load("person_named.json")["persons"][0]
HIT = load("search_entry.json")
REC = load("record_entry.json")
CURRENT_USER = {"users": [{
    "id": "cis.user.X1", "displayName": "Ben Guthro", "personId": "BMXM-2HZ",
    "birthDate": "1 Jan 1975", "mobilePhoneNumber": "+15551234567",
    "helperAccessPin": "987654", "email": "x@example.com",
}]}


def make_services(user_body=CURRENT_USER):
    s = Obj()
    s.client = Obj()
    s.client.get = Rec(Resp(user_body))
    s.discovery = Obj()
    s.discovery.href = Rec("/platform/users/current")
    s.tree = Obj()
    s.tree.get_person = Rec(PERSON)
    s.tree.get_relatives = Rec({"parents": [PERSON], "children": [], "spouses": [PERSON]})
    s.tree.get_sources = Rec([{"id": 7, "titles": [{"value": "Census"}],
                               "citations": [{"value": "cite"}], "about": "https://x"}])
    s.tree.get_memories = Rec([{"id": 9, "titles": [{"value": "Photo"}]}])
    s.tree.get_matches = Rec([HIT])
    s.search = Obj()
    s.search.tree_search = Rec([HIT])
    s.search.record_search = Rec({"total": 42, "next": "https://n", "entries": [REC]})
    return s


class FakeCtx:
    def __init__(self, services, env="beta", writes=True):
        self.services = services
        self.environment = env
        self._writes = writes
        self.call_count = 0

    def call(self, fn):
        self.call_count += 1
        return fn(self.services)

    def writes_allowed(self):
        return self._writes


@pytest.fixture
def setup():
    svc = make_services()
    ctx = FakeCtx(svc)
    mcp = FakeMCP()
    register_read_tools(mcp, ctx)
    return mcp.tools, svc, ctx


def test_registers_eight_tools_with_docstrings(setup):
    tools, _, _ = setup
    assert set(tools) == {
        "auth_status", "search_tree", "search_records", "get_person",
        "get_relatives", "get_person_sources", "get_person_memories", "get_matches",
        "get_record_image", "get_record_image_info",
    }
    for name, fn in tools.items():
        assert fn.__doc__ and fn.__doc__.strip(), name


def test_auth_status_whitelist(setup):
    tools, svc, _ = setup
    out = tools["auth_status"]()
    assert out == {
        "environment": "beta",
        "writes_allowed": True,
        "user": {"id": "cis.user.X1", "displayName": "Ben Guthro", "personId": "BMXM-2HZ"},
    }
    assert svc.discovery.href.calls == [(("current-user",), {})]
    assert svc.client.get.calls == [(("/platform/users/current",), {})]
    blob = json.dumps(out)
    for secret in ("birthDate", "mobilePhoneNumber", "helperAccessPin",
                   "1 Jan 1975", "+15551234567", "987654", "x@example.com"):
        assert secret not in blob


def test_auth_status_environment_is_string_name():
    class Env:
        name = "PRODUCTION"

    ctx = FakeCtx(make_services(), env=Env(), writes=False)
    mcp = FakeMCP()
    register_read_tools(mcp, ctx)
    out = mcp.tools["auth_status"]()
    assert out["environment"] == "production"
    assert out["writes_allowed"] is False


@pytest.mark.parametrize("body", [None, {}, {"users": []}, {"users": ["x"]}, {"users": "x"}, []])
def test_auth_status_odd_body_gives_none_user(body):
    ctx = FakeCtx(make_services(body))
    mcp = FakeMCP()
    register_read_tools(mcp, ctx)
    assert mcp.tools["auth_status"]()["user"] == {
        "id": None, "displayName": None, "personId": None}


def test_search_tree(setup):
    tools, svc, _ = setup
    out = tools["search_tree"](given="John", surname="Smith", birth_date="1846",
                               birth_place="Ontario", count=5)
    assert svc.search.tree_search.calls == [((), {
        "given": "John", "surname": "Smith", "birth_date": "1846",
        "birth_place": "Ontario", "count": 5})]
    assert len(out) == 1
    assert out[0]["name"] == "John Anson Smith"
    assert out[0]["confidence"] == 4
    assert out[0]["birth"] == {"date": "25 December 1846", "place": "Ontario, Canada"}


@pytest.mark.parametrize("given,expected", [(9999, 50), (0, 1), (-3, 1), (20, 20)])
def test_search_tree_clamps_count(setup, given, expected):
    tools, svc, _ = setup
    tools["search_tree"](surname="Smith", count=given)
    assert svc.search.tree_search.calls[0][1]["count"] == expected


def test_search_records(setup):
    tools, svc, _ = setup
    out = tools["search_records"](given="August", surname="Anderson", count=10, offset=20)
    assert svc.search.record_search.calls == [((), {
        "given": "August", "surname": "Anderson", "count": 10, "offset": 20})]
    assert out["total"] == 42
    assert out["next"] == "https://n"
    assert out["entries"][0]["name"] == "August Anderson"
    assert out["entries"][0]["facts"][0]["date"] == "1 May 1880"


def test_search_records_clamps(setup):
    tools, svc, _ = setup
    tools["search_records"](count=9999, offset=-5)
    assert svc.search.record_search.calls[0][1]["count"] == 50
    assert svc.search.record_search.calls[0][1]["offset"] == 0
    tools["search_records"](count=0)
    assert svc.search.record_search.calls[1][1]["count"] == 1


def test_get_person(setup):
    tools, svc, _ = setup
    out = tools["get_person"]("BMXM-2HZ")
    assert svc.tree.get_person.calls == [(("BMXM-2HZ",), {})]
    assert out["id"] == "BMXM-2HZ"
    assert out["name"] == "Ben Guthro"
    assert out["lifespan"] == "1975-"


def test_get_relatives(setup):
    tools, _, _ = setup
    out = tools["get_relatives"]("BMXM-2HZ")
    assert set(out) == {"parents", "children", "spouses"}
    assert out["parents"][0]["name"] == "Ben Guthro"
    assert out["children"] == []
    assert out["spouses"][0]["id"] == "BMXM-2HZ"


def test_get_person_sources(setup):
    tools, svc, _ = setup
    out = tools["get_person_sources"]("BMXM-2HZ")
    assert svc.tree.get_sources.calls == [(("BMXM-2HZ",), {})]
    assert out == [{"id": "7", "title": "Census", "citation": "cite", "about": "https://x"}]


def test_get_person_memories(setup):
    tools, svc, _ = setup
    out = tools["get_person_memories"]("BMXM-2HZ")
    assert svc.tree.get_memories.calls == [(("BMXM-2HZ",), {})]
    assert out[0]["id"] == "9"
    assert out[0]["title"] == "Photo"


def test_get_matches(setup):
    tools, svc, _ = setup
    out = tools["get_matches"]("BMXM-2HZ")
    assert svc.tree.get_matches.calls == [(("BMXM-2HZ",), {})]
    assert out[0]["name"] == "John Anson Smith"


ID_TOOLS = ["get_person", "get_relatives", "get_person_sources",
            "get_person_memories", "get_matches"]


@pytest.mark.parametrize("tool", ID_TOOLS)
@pytest.mark.parametrize("bad", ["", "nope", "../users/current", "GKLV-DY8/../x", None, 123])
def test_invalid_person_id_no_service_calls(setup, tool, bad):
    tools, svc, ctx = setup
    with pytest.raises(ValueError):
        tools[tool](bad)
    assert ctx.call_count == 0
    for group in (svc.tree, svc.search, svc.client):
        for attr in vars(group).values():
            assert not getattr(attr, "calls", [])


@pytest.mark.parametrize("tool,method", [
    ("get_person", "get_person"), ("get_relatives", "get_relatives"),
    ("get_person_sources", "get_sources"), ("get_person_memories", "get_memories"),
    ("get_matches", "get_matches"),
])
def test_id_normalized_before_library(setup, tool, method):
    tools, svc, _ = setup
    tools[tool](" gklv-dy8 ")
    assert getattr(svc.tree, method).calls == [(("GKLV-DY8",), {})]


def test_errors_propagate(setup):
    tools, svc, _ = setup
    svc.tree.get_person = Rec(exc=FSError("boom"))
    svc.search.tree_search = Rec(exc=FSError("boom"))
    svc.client.get = Rec(exc=FSError("boom"))
    with pytest.raises(FSError):
        tools["get_person"]("BMXM-2HZ")
    with pytest.raises(FSError):
        tools["search_tree"](surname="x")
    with pytest.raises(FSError):
        tools["auth_status"]()


def test_get_record_image_tool(setup):
    from mcp.server.fastmcp import Image
    tools, svc, _ = setup
    svc.client = Obj()
    svc.client.base_url = "https://www.familysearch.org"
    svc.client.get = lambda url, accept=None: Resp({"link": [{
        "rel": "stream-image-dist",
        "href": "https://sg30p0.familysearch.org/service/records/storage/"
                "dascloud/das/v2/AP-1/dist.jpg"}]})
    svc.client.get_image = lambda url: (b"IMG", "image/jpeg")
    out = tools["get_record_image"]("ark:/61903/3:1:ABCD-123")
    assert isinstance(out, Image) and out.data == b"IMG"
    with pytest.raises(FSError):
        tools["get_record_image"]("not an ark")
    with pytest.raises(ValueError):
        tools["get_record_image"]("ark:/61903/3:1:ABCD-123", size="deepzoom")
