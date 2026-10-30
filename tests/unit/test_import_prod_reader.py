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


def test_search_query_uses_normalized_name(tmp_path):
    p = a_person(tmp_path, "1844-1915 Rev Stella H Eschliman Jr")
    fs, client, _ = make(search_routes([]))
    fs.search_candidates(p)
    params = client.calls[-1][2]
    assert params["q.givenName"] == "stella" and params["q.surname"] == "eschliman"


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


# ---- existing memories and sources ----

def links_body(pid):
    return Resp(200, {"persons": [{"id": pid, "links": {
        "artifacts": {"href": f"/m/{pid}"},
        "source-descriptions": {"href": f"/s/{pid}"},
    }}]})


def test_get_memory_titles_populated_and_empty():
    body = {"sourceDescriptions": [
        {"titles": [{"value": "Census 1900"}]}, {"titles": []}, {"titles": [{"value": "Will"}]}]}
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")], "/m/ABCD-123": [Resp(200, body)]})
    assert fs.get_memory_titles("ABCD-123") == ["Census 1900", "", "Will"]
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")], "/m/ABCD-123": [Resp(200, {})]})
    assert fs.get_memory_titles("ABCD-123") == []


def page(items, nxt=None, key="sourceDescriptions"):
    body = {key: items}
    if nxt:
        body["links"] = {"next": {"href": nxt}}
    return Resp(200, body)


def test_memories_follow_paging_links_and_count_untitled():
    routes = {
        P + "ABCD-123": [links_body("ABCD-123")],
        "/m/ABCD-123": [page([{"id": "M1", "titles": [{"value": "A"}]}], "/m/ABCD-123?start=1")],
        "/m/ABCD-123?start=1": [page([{"id": "M2"}], "/m/ABCD-123?start=2")],
        "/m/ABCD-123?start=2": [page([{"id": "M3", "titles": [{"value": "C"}]}])],
    }
    fs, _, _ = make(routes)
    assert fs.get_memory_titles("ABCD-123") == ["A", "", "C"]
    fs, _, _ = make({**routes, P + "ABCD-123": [links_body("ABCD-123")]})
    assert fs.get_memory_ids("ABCD-123") == ["M1", "M2", "M3"]


def test_sources_follow_paging_links_list_form():
    first = Resp(200, {"sourceDescriptions": [{"id": "S1", "about": "https://e.org/1"}],
                       "links": [{"rel": "next", "href": "/s/ABCD-123?start=1"}]})
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")], "/s/ABCD-123": [first],
                     "/s/ABCD-123?start=1": [page([{"id": "S2", "titles": [{"value": "T"}]}])]})
    refs = fs.get_source_refs("ABCD-123")
    assert [r.url for r in refs] == ["https://e.org/1", None] and refs[1].title == "T"


def test_paging_past_the_cap_is_read_failed_never_truncated():
    routes = {P + "ABCD-123": [links_body("ABCD-123")], "/m/ABCD-123": [page([{"id": "M0"}], "/m/p1")]}
    for n in range(1, 60):
        routes[f"/m/p{n}"] = [page([{"id": f"M{n}"}], f"/m/p{n + 1}")]
    fs, _, _ = make(routes)
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_memory_titles("ABCD-123")


def test_paging_loop_is_read_failed():
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")],
                     "/m/ABCD-123": [page([{"id": "M0"}], "/m/ABCD-123")]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_memory_ids("ABCD-123")


def test_item_without_id_is_read_failed_for_id_reads():
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")], "/m/ABCD-123": [page([{"titles": []}])]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_memory_ids("ABCD-123")


def test_get_source_refs_populated():
    body = {"sourceDescriptions": [
        {"about": "https://example.org/a", "titles": [{"value": "Record A"}]},
        {"titles": [{"value": "No url"}]}]}
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")], "/s/ABCD-123": [Resp(200, body)]})
    refs = fs.get_source_refs("ABCD-123")
    assert refs == [prod_reader.SourceRef("Record A", "https://example.org/a"),
                    prod_reader.SourceRef("No url", None)]


def test_memory_read_error_is_read_failed_not_empty():
    fs, _, _ = make({P + "ABCD-123": [links_body("ABCD-123")], "/m/ABCD-123": [Resp(404)]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_memory_titles("ABCD-123")


def test_source_read_retries_then_fails():
    fs, _, sleeps = make({P + "ABCD-123": [links_body("ABCD-123")], "/s/ABCD-123": [Resp(503)]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_source_refs("ABCD-123")
    assert len(sleeps) >= 3


def test_redirect_on_read_is_read_failed():
    # the client surfaces an unfollowed redirect as a plain FSError
    fs, _, _ = make({P + "ABCD-123": [FSError("redirect")]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_memory_titles("ABCD-123")
    fs, _, _ = make({P + "ABCD-123": [Resp(200, None)]})  # unparseable body
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_source_refs("ABCD-123")


def test_read_401_stays_fatal():
    fs, _, _ = make({P + "ABCD-123": [Resp(401)]})
    with pytest.raises(TokenExpiredError):
        fs.get_source_refs("ABCD-123")


# ---- relatives ----

def rel_links_body(pid):
    return Resp(200, {"persons": [{"id": pid, "links": {
        "parents": {"href": f"/r/{pid}/parents"},
        "children": {"href": f"/r/{pid}/children"},
        "spouses": {"href": f"/r/{pid}/spouses"},
    }}]})


def rel_person(pid, name, living=False, birth="1850", death="1920"):
    return {"id": pid, "living": living, "display": {"name": name, "birthDate": birth, "deathDate": death}}


def test_get_relatives_maps_sections_living_and_skips_self():
    routes = {
        P + "AAAA-111": [rel_links_body("AAAA-111")],
        "/r/AAAA-111/parents": [Resp(200, {"persons": [rel_person("PPPP-111", "Parent One")]})],
        "/r/AAAA-111/children": [Resp(200, {"persons": [rel_person("CCCC-111", "Kid One", living=True, death=None)]})],
        "/r/AAAA-111/spouses": [Resp(200, {"persons": [rel_person("AAAA-111", "Pat Example"), rel_person("SSSS-111", "Spouse One")]})],
    }
    fs, _, _ = make(routes)
    rels = fs.get_relatives("AAAA-111")
    got = {(r.relationship, r.fs.id): r.fs for r in rels}
    assert set(got) == {("parent", "PPPP-111"), ("child", "CCCC-111"), ("spouse", "SSSS-111")}
    assert got[("child", "CCCC-111")].living is True
    assert got[("parent", "PPPP-111")].birth_year == 1850 and got[("parent", "PPPP-111")].status == "ok"


def test_get_relatives_follows_paging():
    first = Resp(200, {"persons": [rel_person("CCCC-111", "Kid One")], "links": {"next": {"href": "/r/AAAA-111/children?p=2"}}})
    routes = {
        P + "AAAA-111": [rel_links_body("AAAA-111")],
        "/r/AAAA-111/parents": [Resp(200, {})],
        "/r/AAAA-111/children": [first],
        "/r/AAAA-111/children?p=2": [Resp(200, {"persons": [rel_person("CCCC-222", "Kid Two")]})],
        "/r/AAAA-111/spouses": [Resp(200, {})],
    }
    fs, _, _ = make(routes)
    assert [r.fs.id for r in fs.get_relatives("AAAA-111")] == ["CCCC-111", "CCCC-222"]


def test_get_relatives_error_is_read_failed_and_retries_transient():
    routes = {P + "AAAA-111": [rel_links_body("AAAA-111")], "/r/AAAA-111/parents": [Resp(404)]}
    fs, _, _ = make(routes)
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_relatives("AAAA-111")
    routes = {P + "AAAA-111": [rel_links_body("AAAA-111")], "/r/AAAA-111/parents": [Resp(503), Resp(200, {})],
              "/r/AAAA-111/children": [Resp(200, {})], "/r/AAAA-111/spouses": [Resp(200, {})]}
    fs, _, sleeps = make(routes)
    assert fs.get_relatives("AAAA-111") == [] and sleeps


# ------------------------------------------------ tree-write support (strict reads, relationships by id)


def test_get_relatives_strict_fails_on_missing_section_link():
    body = Resp(200, {"persons": [{"id": "AAAA-111", "links": {"parents": {"href": "/r/AAAA-111/parents"}}}]})
    routes = {P + "AAAA-111": [body], "/r/AAAA-111/parents": [Resp(200, {})]}
    fs, _, _ = make(routes)
    assert fs.get_relatives("AAAA-111") == []  # lenient mode: a missing link reads as empty
    fs, _, _ = make(routes)
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_relatives("AAAA-111", strict=True)


COUPLE = "/platform/tree/couple-relationships/R-1"
CAP = "/platform/tree/child-and-parents-relationships/P-1"


def _url(path):
    return Environment.PRODUCTION.base_url + path


def test_get_couple_reads_by_id_and_maps_404_to_none():
    body = Resp(200, {"relationships": [{"type": "http://gedcomx.org/Couple",
                                         "person1": {"resourceId": "AAAA-111"}, "person2": {"resourceId": "BBBB-111"}}]})
    fs, _, _ = make({_url(COUPLE): [body]})
    assert fs.get_couple("R-1") == frozenset(("AAAA-111", "BBBB-111"))
    fs, _, _ = make({_url(COUPLE): [Resp(404)]})
    assert fs.get_couple("R-1") is None
    fs, _, _ = make({_url(COUPLE): [Resp(200, {"relationships": []})]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_couple("R-1")
    fs, _, _ = make({_url(COUPLE): [Resp(403)]})
    with pytest.raises(prod_reader.ReadFailed):
        fs.get_couple("R-1")


def test_get_child_and_parents_reads_by_id():
    body = Resp(200, {"childAndParentsRelationships": [{
        "child": {"resourceId": "CCCC-111"}, "parent1": {"resourceId": "AAAA-111"},
        "parent2": {"resourceId": "BBBB-111"}}]})
    fs, _, _ = make({_url(CAP): [body]})
    assert fs.get_child_and_parents("P-1") == ("CCCC-111", frozenset(("AAAA-111", "BBBB-111")))
    fs, _, _ = make({_url(CAP): [Resp(410)]})
    assert fs.get_child_and_parents("P-1") is None


def test_get_couple_ids_between_two_people():
    rels = {"relationships": [
        {"id": "R-1", "type": "http://gedcomx.org/Couple", "person1": {"resourceId": "AAAA-111"},
         "person2": {"resourceId": "BBBB-111"}},
        {"id": "R-2", "type": "http://gedcomx.org/Couple", "person1": {"resourceId": "AAAA-111"},
         "person2": {"resourceId": "CCCC-111"}}]}
    routes = {P + "AAAA-111": [rel_links_body("AAAA-111")], "/r/AAAA-111/spouses": [Resp(200, rels)]}
    fs, _, _ = make(routes)
    assert fs.get_couple_ids("AAAA-111", "BBBB-111") == ["R-1"]


def test_get_change_summary_contributors_and_operations():
    items = {"entries": [{"title": "Create person", "contributors": [{"resourceId": "USER-1"}],
                          "changeInfo": [{"operation": "http://familysearch.org/v1/Create"}]},
                         {"title": "Merge", "contributors": [{"name": "Someone"}], "changeInfo": []}]}
    routes = {P + "AAAA-111": [Resp(200, {"persons": [{"id": "AAAA-111", "links": {
        "change-history": {"href": "/r/AAAA-111/changes"}}}]})], "/r/AAAA-111/changes": [Resp(200, items)]}
    fs, _, _ = make(routes)
    got = fs.get_change_summary("AAAA-111")
    assert [w for w, _ in got] == ["USER-1", "Someone"] and "merge" in got[1][1]


def test_search_duplicates_is_unfiltered_and_queries_years_and_surname_only():
    fs, _, _ = make({})
    calls = []

    def fake_search(**q):
        calls.append(q)
        if q.get("birth_date") == "1850" and q.get("given") is None:
            return [{"id": "XXXX-111", "content": {"gedcomx": {"persons": [
                {"display": {"name": "Totally Different", "birthDate": "1861"}, "living": False}]}}}]
        return []
    fs._search.tree_search = fake_search
    hits = fs.search_duplicates("Zed", "Testname", 1850, 1910)
    assert [h.id for h in hits] == ["XXXX-111"]  # no compare() filtering: a different name still counts
    assert {q.get("birth_date") for q in calls} >= {"1840", "1845", "1850", "1855", "1860"}
    assert {q.get("death_date") for q in calls} >= {"1900", "1910", "1920"}
    assert any(q.get("given") is None and q["surname"] == "Testname" for q in calls)
    assert any(q.get("given") == "Zed" for q in calls)


def test_search_duplicates_counts_unparseable_entries_and_errors_propagate():
    fs, _, _ = make({})
    fs._search.tree_search = lambda **q: [{"id": "ODD-1", "content": {}}]
    assert [h.id for h in fs.search_duplicates("Zed", "Testname", 1850, None)] == ["ODD-1"]

    def boom(**q):
        raise requests.ConnectionError("x")
    fs._search.tree_search = boom
    with pytest.raises(requests.RequestException):
        fs.search_duplicates("Zed", "Testname", 1850, None)
    with pytest.raises(ValueError):
        fs.search_duplicates("Zed", "Testname", None, None)
