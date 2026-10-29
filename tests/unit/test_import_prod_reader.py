import pytest
import requests

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError, FSHTTPError, TokenExpiredError
from familysearch_import import prod_reader
from familysearch_import.prod_reader import ReadOnlyError, ReadOnlyFS
from familysearch_import.scan import scan_archive


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeClient(FSClient):
    """Real FSClient surface (get/post/delete -> request); request never hits a network."""

    def __init__(self, routes):
        super().__init__("tok", Environment.PRODUCTION)
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, path_or_url, **kw):
        self.calls.append((method, path_or_url, kw.get("params")))
        queue = self.routes.get(path_or_url)
        if not queue:
            raise AssertionError(f"unexpected request {method} {path_or_url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if item.status_code == 401:
            raise TokenExpiredError("401")
        if item.status_code >= 400:
            raise FSHTTPError(item.status_code, "")
        return item


def person_body(pid, name="Pat Example", birth="1844", death="1915", living=False):
    return Resp(200, {"persons": [{
        "id": pid, "living": living,
        "display": {"name": name, "birthDate": birth, "deathDate": death},
    }]})


def make(routes):
    sleeps = []
    client = FakeClient(routes)
    fs = ReadOnlyFS(client, sleep=sleeps.append, clock=lambda: 0.0)
    return fs, client, sleeps


P = "/platform/tree/persons/"


def test_guard_blocks_writes():
    fs, client, _ = make({})
    for call in (
        lambda: client.post("/platform/tree/persons", reason="x", json={}),
        lambda: client.delete(P + "ABCD-123", reason="x"),
        lambda: client.request("PUT", "/x", write=False),
        lambda: client.request("GET", "/x", write=True, reason="r"),
    ):
        with pytest.raises(ReadOnlyError):
            call()
    assert client.calls == []


def test_get_allowed_and_head_allowed():
    fs, client, _ = make({"/x": [Resp(200, {})]})
    client.request("HEAD", "/x", write=False)
    client.get("/x")
    assert [c[0] for c in client.calls] == ["HEAD", "GET"]


def test_get_fs_person_ok():
    fs, client, sleeps = make({P + "ABCD-123": [person_body("ABCD-123")]})
    got = fs.get_fs_person("ABCD-123")
    assert (got.id, got.name, got.birth_year, got.death_year, got.living, got.status) == (
        "ABCD-123", "Pat Example", 1844, 1915, False, "ok")
    assert all(m == "GET" for m, _, _ in client.calls)


def test_fact_year_fallback():
    body = Resp(200, {"persons": [{"id": "ABCD-123", "names": [{"nameForms": [{"fullText": "Pat Example"}]}],
                                   "facts": [{"type": "http://gedcomx.org/Birth", "date": {"original": "3 May 1844"}}]}]})
    fs, _, _ = make({P + "ABCD-123": [body]})
    got = fs.get_fs_person("ABCD-123")
    assert (got.name, got.birth_year, got.death_year) == ("Pat Example", 1844, None)


def test_followed_redirect_is_redirected_with_survivor():
    fs, _, _ = make({P + "OLDA-123": [person_body("WXYZ-9AB")]})
    got = fs.get_fs_person("OLDA-123")
    assert (got.id, got.status, got.survivor_id, got.name) == ("OLDA-123", "redirected", "WXYZ-9AB", "Pat Example")


def test_unfollowed_301_fetches_survivor():
    loc = {"Location": "https://www.familysearch.org/platform/tree/persons/WXYZ-9AB"}
    fs, _, _ = make({
        P + "OLDA-123": [Resp(301, None, loc)],
        P + "WXYZ-9AB": [person_body("WXYZ-9AB")],
    })
    got = fs.get_fs_person("OLDA-123")
    assert (got.status, got.survivor_id, got.birth_year) == ("redirected", "WXYZ-9AB", 1844)


def test_unfollowed_301_without_usable_location_is_error():
    fs, _, _ = make({P + "OLDA-123": [Resp(301, None, {})]})
    assert fs.get_fs_person("OLDA-123").status == "error"


def test_404_410_mapping():
    fs, _, _ = make({P + "AAAA-111": [Resp(404)], P + "BBBB-222": [Resp(410)]})
    assert fs.get_fs_person("AAAA-111").status == "not-found"
    assert fs.get_fs_person("BBBB-222").status == "deleted"


def test_retry_backoff_then_success():
    fs, client, sleeps = make({P + "ABCD-123": [Resp(429), Resp(503), person_body("ABCD-123")]})
    assert fs.get_fs_person("ABCD-123").status == "ok"
    assert len(client.calls) == 3
    assert [s for s in sleeps if s >= 1.0] == [1.0, 2.0]


def test_retries_exhausted_is_error_not_not_found():
    fs, client, sleeps = make({P + "ABCD-123": [Resp(500)]})
    got = fs.get_fs_person("ABCD-123")
    assert got.status == "error"
    assert len(client.calls) == 4
    assert [s for s in sleeps if s >= 1.0] == [1.0, 2.0, 4.0]


def test_network_error_retries_then_error():
    fs, client, _ = make({P + "ABCD-123": [requests.ConnectionError("boom")]})
    assert fs.get_fs_person("ABCD-123").status == "error"
    assert len(client.calls) == 4


def test_401_is_fatal_not_per_person():
    fs, _, _ = make({P + "ABCD-123": [Resp(401)]})
    with pytest.raises(TokenExpiredError):
        fs.get_fs_person("ABCD-123")


def test_pacing_sleeps_between_calls():
    now = [0.0]
    sleeps = []
    client = FakeClient({P + "AAAA-111": [person_body("AAAA-111")], P + "BBBB-222": [person_body("BBBB-222")]})
    fs = ReadOnlyFS(client, sleep=sleeps.append, clock=lambda: now[0])
    fs.get_fs_person("AAAA-111")
    fs.get_fs_person("BBBB-222")
    assert sleeps and sleeps[0] >= 0.3 - 1e-9


# --- search ---

COLLECTION = "/platform/collections/tree"
SEARCH_URL = "/platform/tree/search"


def search_routes(entries):
    return {
        COLLECTION: [Resp(200, {"collections": [{"links": {"person-search": {"template": SEARCH_URL + "{?q}"}}}]})],
        SEARCH_URL: [Resp(200, {"entries": entries})],
    }


def entry(pid, name, birth, death):
    return {"id": pid, "content": {"gedcomx": {"persons": [{
        "id": pid, "display": {"name": name, "birthDate": birth, "deathDate": death}}]}}}


def a_person(tmp_path, folder="1844-1915 Pat Example"):
    (tmp_path / "a" / "L" / folder).mkdir(parents=True)
    (p,) = scan_archive(tmp_path / "a")
    return p


def test_search_candidates_filters_ranks_and_limits(tmp_path):
    p = a_person(tmp_path)
    entries = [
        entry("CCCC-333", "Pat Example", "1846", "1915"),  # probable
        entry("DDDD-444", "Pat Example", "1700", "1760"),  # mismatch
        entry("AAAA-111", "Pat Example", "1844", "1915"),  # confirmed
        entry("EEEE-555", "Pat Other", "1844", "1915"),  # name mismatch
        entry("BBBB-222", "Pat Example", "1844", None),  # confirmed
    ]
    fs, client, _ = make(search_routes(entries))
    got = fs.search_candidates(p, limit=2)
    assert [c.id for c in got] == ["AAAA-111", "BBBB-222"]
    method, url, params = client.calls[-1]
    assert method == "GET" and url == SEARCH_URL
    assert params["q.givenName"] == "pat" and params["q.surname"] == "example"
    assert params["q.birthLikeDate"] == "1844"


def test_search_refuses_living(tmp_path):
    living = a_person(tmp_path, "1980 Kim Example")
    fs, client, _ = make({})
    with pytest.raises(ValueError):
        fs.search_candidates(living)
    assert client.calls == []


def test_search_refuses_unknown_status(tmp_path):
    (tmp_path / "a" / "L" / "?-? Pat Example").mkdir(parents=True)
    (p,) = scan_archive(tmp_path / "a")
    fs, client, _ = make({})
    with pytest.raises(ValueError):
        fs.search_candidates(p)
    assert client.calls == []


# --- production() ---

def test_production_requires_production_session(monkeypatch):
    with pytest.raises(FSError):
        ReadOnlyFS.production(token_fn=lambda: "tok", http_get=lambda base, tok: 401)
    seen = []

    def ok(base, tok):
        seen.append((base, tok))
        return 200

    fs = ReadOnlyFS.production(token_fn=lambda: "tok", http_get=ok)
    assert seen == [("https://www.familysearch.org", "tok")]
    assert fs._client.environment is Environment.PRODUCTION
    with pytest.raises(ReadOnlyError):
        fs._client.post("/x", reason="r", json={})


def test_module_has_no_write_calls():
    import inspect

    src = inspect.getsource(prod_reader)
    for forbidden in ("TreeWriter", ".post(", ".delete(", ".put("):
        assert forbidden not in src


def test_status_get_sends_browser_user_agent(monkeypatch):
    from familysearch.client import DEFAULT_USER_AGENT

    seen = {}

    def fake_get(url, **kw):
        seen.update(url=url, **kw)
        return Resp(200)

    monkeypatch.setattr(prod_reader.requests, "get", fake_get)
    assert prod_reader._status_get("https://www.familysearch.org", "tok") == 200
    assert seen["headers"]["User-Agent"] == DEFAULT_USER_AGENT
    assert seen["url"].endswith("/platform/users/current")
