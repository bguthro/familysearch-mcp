"""Offline tests for the read-verification fixture world (no network, no cookies)."""
import itertools
import json
import re
import shutil
from urllib.parse import unquote, urlsplit

import pytest
import responses

from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError, FSHTTPError
from tools.verify import runner
from tools.verify.fixtures import FixtureSet
from tools.verify.reads import ReadCase, json_path, reads_recipe, seal
from tools.verify.world import CANARY_GROUP, CANARY_NAMES, FIXTURE_GROUPS, GROUPS, World

BETA = Environment.BETA.base_url
P = BETA + "/platform"
PERSONS = P + "/tree/persons"
REASON = "ZZSPIKE world test"
UNIQUE = "ZZSPIKE0a1b2c3d"


def beta_client():
    return FSClient("tkn", Environment.BETA, min_write_interval=0)


class FakeBeta:
    """A scripted beta over `responses`: every object the world can create, by URL.

    Ids look like ``ID0001X`` so that none is a substring of another.
    """

    def __init__(self, rsps):
        self.rsps = rsps
        self.live: dict[str, dict] = {}  # path -> stored body
        self.counter = itertools.count(1)
        self.fail: set[str] = set()  # kinds whose create answers fail_status
        self.fail_status = 500
        self.attached: dict[str, list[str]] = {}  # person id -> source description urls
        self.filed: dict[str, list[str]] = {}  # collection path -> description ids
        self.noid: set[str] = set()  # kinds whose create answers 201 with no id header
        self.list_refs = True  # source-references lists attached sources
        self.refs_status = None  # set: source-references answers this error status
        self.list_personas = True  # a memory's personas list the person it was uploaded to
        self.personas_status = None  # set: the personas read answers this error status
        self.personas: dict[str, str] = {}  # memory path -> its persona id
        self.persona_form = "{mid}-{n}"  # how beta forms a persona id
        self.rel_attached: dict[str, list] = {}  # relationship path -> (envelope, url)
        self.fail_person_at = 0  # the n-th person POST answers 500
        self.person_posts = 0
        rsps.add(responses.GET, P + "/collections/tree", json={"collections": [{
            "links": {
                "persons": {"href": PERSONS + "?flag=fsh"},
                "relationships": {"href": P + "/tree/relationships"},
                "artifacts": {"href": P + "/memories/memories"},
                "source-descriptions": {"href": P + "/sources/descriptions"},
            }}]})
        pat = re.compile(r"https://beta\.familysearch\.org/platform/(tree|memories|sources)/.+")
        rsps.add_callback(responses.POST, pat, callback=self._post)
        rsps.add_callback(responses.GET, pat, callback=self._get)
        rsps.add_callback(responses.DELETE, pat, callback=self._delete)

    @staticmethod
    def path(url):
        return unquote(urlsplit(url).path).rstrip("/")

    def new_id(self):
        return f"ID{next(self.counter):04d}X"

    def posts(self, prefix=""):
        return [self.path(c.request.url) for c in self.rsps.calls
                if c.request.method == "POST" and self.path(c.request.url).startswith(prefix)]

    def _created(self, kind, path, body, *, header="Location"):
        if kind in self.fail:
            return self.fail_status, {}, json.dumps({"errors": [{"message": "scripted"}]})
        self.live[path] = body
        if kind in self.noid:
            return 201, {}, ""
        oid = path.rsplit("/", 1)[-1]
        if header == "Location":
            return 201, {"Location": BETA + path + "?flag=x"}, ""
        return 201, {"X-ENTITY-ID": oid}, ""

    def _post(self, request):
        path = self.path(request.url)
        raw = request.body or b""
        raw = raw if isinstance(raw, bytes) else raw.encode()
        body = json.loads(raw) if raw[:1] == b"{" else None
        tree = "/platform/tree"
        if path == tree + "/persons":
            self.person_posts += 1
            if self.person_posts == self.fail_person_at:
                return 500, {}, ""
            return self._created("person", f"{tree}/persons/{self.new_id()}", body,
                                 header="X-ENTITY-ID")
        if path == tree + "/relationships":
            if "childAndParentsRelationships" in body:
                return self._created("child_and_parents",
                                     f"{tree}/child-and-parents-relationships/{self.new_id()}",
                                     body)
            return self._created("couple", f"{tree}/couple-relationships/{self.new_id()}", body)
        m = re.fullmatch(rf"{tree}/(persons|couple-relationships|"
                         r"child-and-parents-relationships)/([^/]+)/notes", path)
        if m:
            if path.rsplit("/notes", 1)[0] not in self.live:
                return 404, {}, ""
            kind = {"persons": "person_note", "couple-relationships": "couple_note",
                    "child-and-parents-relationships": "child_and_parents_note"}[m.group(1)]
            return self._created(kind, f"{path}/{self.new_id()}", body)
        m = re.fullmatch(rf"{tree}/persons/([^/]+)/memories", path)
        if m:
            if path.rsplit("/memories", 1)[0] not in self.live:
                return 404, {}, ""
            return self._created("memory", f"/platform/memories/memories/{self.new_id()}",
                                 {"person": m.group(1), "query": request.url})
        if path == "/platform/sources/descriptions":
            return self._created("source", f"/platform/sources/descriptions/{self.new_id()}",
                                 body, header="X-ENTITY-ID")
        if path == "/platform/sources/collections":
            return self._created("collection", f"/platform/sources/collections/{self.new_id()}",
                                 body, header="X-ENTITY-ID")
        m = re.fullmatch(r"(/platform/sources/collections/[^/]+)/descriptions", path)
        if m:
            if m.group(1) not in self.live or "file" in self.fail:
                return 404, {}, ""
            self.filed.setdefault(m.group(1), []).extend(
                d["id"] for d in body["sourceDescriptions"])
            return 204, {}, ""
        m = re.fullmatch(rf"({tree}/(?:couple|child-and-parents)-relationships/[^/]+)"
                         r"/source-references", path)
        if m:  # attach a source to a relationship
            if m.group(1) not in self.live or "rel_attach" in self.fail:
                return 404 if m.group(1) not in self.live else 500, {}, ""
            envelope = next(iter(body))
            for s in body[envelope][0]["sources"]:
                self.rel_attached.setdefault(m.group(1), []).append((envelope, s["description"]))
            return 201, {}, ""
        m = re.fullmatch(rf"{tree}/persons/([^/]+)", path)
        if m and path in self.live:  # attach a source to a person
            if "attach" in self.fail:
                return 500, {}, ""
            for s in body["persons"][0]["sources"]:
                self.attached.setdefault(m.group(1), []).append(
                    (self.new_id(), s["description"]))
            return 204, {}, ""
        return 404, {}, ""

    def _get(self, request):
        path = self.path(request.url)
        if path == "/platform/sources/collections":  # the user's folder list
            return 200, {}, json.dumps({"collections": [
                {"id": p.rsplit("/", 1)[-1], "title": b["collections"][0]["title"]}
                for p, b in self.live.items() if p.startswith(path + "/")]})
        m = re.fullmatch(r"(/platform/memories/memories/[^/]+)/personas", path)
        if m:
            if self.personas_status:
                return self.personas_status, {}, json.dumps({"errors": [{"code": 404}]})
            memory = self.live.get(m.group(1))
            if memory is None or not self.list_personas:
                return 204, {}, ""
            # Live (Task 5 run 1): "<memory id>-<sequence>", e.g. 238410642-74542947.
            mid = m.group(1).rsplit("/", 1)[-1]
            pid = self.personas.setdefault(
                m.group(1), self.persona_form.format(mid=mid, n=74542900 + next(self.counter)))
            return 200, {}, json.dumps({"persons": [{"id": pid, "identifiers": {
                "http://familysearch.org/v1/FamilyTreePerson": [memory["person"]]}}]})
        m = re.fullmatch(r"/platform/tree/persons/([^/]+)/source-references", path)
        if m:
            if self.refs_status:
                return self.refs_status, {}, json.dumps({"errors": [{"code": self.refs_status}]})
            refs = self.attached.get(m.group(1)) if self.list_refs else None
            if not refs:
                return 204, {}, ""
            return 200, {}, json.dumps({"persons": [{"id": m.group(1), "sources": [
                {"id": rid, "description": d} for rid, d in refs]}]})
        if path not in self.live:
            return 404, {}, ""
        m = re.fullmatch(r"/platform/tree/persons/([^/]+)", path)
        if m:
            person = dict(self.live[path]["persons"][0], id=m.group(1))
            person["links"] = {"artifacts": {"href": BETA + path + "/memories"}}
            name = person["names"][0]["nameForms"][0]["fullText"]
            person["display"] = {"name": name}
            return 200, {"Content-Type": "application/x-fs-v1+json"}, json.dumps(
                {"persons": [person]})
        return 200, {}, "{}"

    def _delete(self, request):
        path = self.path(request.url)
        if path not in self.live:
            return 404, {}, ""
        del self.live[path]
        # Embedded notes go with their owner, as on FamilySearch.
        for p in [p for p in self.live if p.startswith(path + "/notes/")]:
            del self.live[p]
        return 204, {}, ""


@pytest.fixture
def beta():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeBeta(rsps)


def make_world(client=None):
    client = client or beta_client()
    fixtures = FixtureSet(client, reason=REASON)
    return World(client, fixtures, REASON, UNIQUE), fixtures


def all_values(world):
    return [*world.ids.values(), *world.names.values()]


# ------------------------------------------------------------------ construction


@responses.activate
def test_refuses_non_beta_client_without_io():
    prod = FSClient("tkn", Environment.PRODUCTION, min_write_interval=0)
    beta_fx = FixtureSet(beta_client(), reason=REASON)
    with pytest.raises(FSError, match="beta"):
        World(prod, beta_fx, REASON, UNIQUE)
    assert len(responses.calls) == 0


def test_constructing_creates_nothing(beta):
    world, fixtures = make_world()
    assert dict(world.ids) == {} and dict(world.names) == {}
    assert beta.rsps.calls == [] or all(c.request.method == "GET" for c in beta.rsps.calls)
    assert beta.posts() == []


def test_unknown_group_is_refused(beta):
    world, _ = make_world()
    with pytest.raises(ValueError, match="unknown"):
        world.need("persons", "discussions")
    assert beta.posts() == []  # refused before building anything


def test_groups_are_the_documented_ones():
    assert set(FIXTURE_GROUPS) == {"persons", "couple", "person_note", "couple_note",
                                   "child_and_parents_note", "notes", "source", "collection",
                                   "memory", "relationship_source"}
    assert set(GROUPS) == set(FIXTURE_GROUPS) | {CANARY_GROUP} and CANARY_GROUP == "canaries"


def test_requires_reason_and_unique():
    client = beta_client()
    fx = FixtureSet(client, reason=REASON)
    with pytest.raises(ValueError):
        World(client, fx, " ", UNIQUE)
    with pytest.raises(ValueError):
        World(client, fx, REASON, "")


# ------------------------------------------------------------------ persons


def test_persons_creates_three_tracked_deceased_persons(beta):
    world, fixtures = make_world()
    world.need("persons")
    assert set(world.ids) == {"dad", "mom", "child"}
    assert fixtures.tracked("person") == [world.ids["dad"], world.ids["mom"], world.ids["child"]]
    assert beta.posts() == ["/platform/tree/persons"] * 3
    bodies = [beta.live[f"/platform/tree/persons/{world.ids[r]}"]["persons"][0]
              for r in ("dad", "mom", "child")]
    assert [b["gender"]["type"].rsplit("/", 1)[-1] for b in bodies] == ["Male", "Female", "Male"]
    assert all(b["living"] is False for b in bodies)
    assert all(any("Birth" in f["type"] for f in b["facts"]) for b in bodies)
    full = [b["names"][0]["nameForms"][0]["fullText"] for b in bodies]
    assert full == [world.names["dad_name"], world.names["mom_name"], world.names["child_name"]]
    # Same run surname (the runner's unique tag), distinct given names.
    assert {f.split()[-1] for f in full} == {UNIQUE}
    assert len({f.split()[0] for f in full}) == 3
    assert all(f.startswith("ZZSPIKE") for f in full)
    assert fixtures.teardown() == [] and beta.live == {}


def test_persons_is_lazy_no_relationship_note_source_or_memory(beta):
    world, _ = make_world()
    world.need("persons")
    posts = beta.posts()
    assert all(p == "/platform/tree/persons" for p in posts)
    assert not any("memories" in p or "sources" in p or "relationships" in p for p in posts)
    assert set(world.ids) == {"dad", "mom", "child"}


def test_need_is_idempotent(beta):
    world, fixtures = make_world()
    world.need("persons")
    first = dict(world.ids)
    world.need("persons")
    world.need()
    assert dict(world.ids) == first and len(beta.posts()) == 3
    world.need("notes", "couple", "notes")
    n = len(beta.posts())
    world.need("notes", "couple", "persons")
    assert len(beta.posts()) == n
    assert fixtures.tracked("person") == [first["dad"], first["mom"], first["child"]]


# ------------------------------------------------------------------ couple


def test_couple_implies_persons_and_tracks_relationships(beta):
    world, fixtures = make_world()
    world.need("couple")
    assert {"dad", "mom", "child", "couple", "child_and_parents"} <= set(world.ids)
    assert fixtures.tracked("couple") == [world.ids["couple"]]
    assert fixtures.tracked("child_and_parents") == [world.ids["child_and_parents"]]
    couple = beta.live["/platform/tree/couple-relationships/" + world.ids["couple"]]
    rel = couple["relationships"][0]
    assert rel["type"].endswith("Couple")
    assert {rel["person1"]["resourceId"], rel["person2"]["resourceId"]} == {
        world.ids["dad"], world.ids["mom"]}
    cap = beta.live["/platform/tree/child-and-parents-relationships/"
                    + world.ids["child_and_parents"]]["childAndParentsRelationships"][0]
    assert cap["child"]["resourceId"] == world.ids["child"]
    assert {cap["parent1"]["resourceId"], cap["parent2"]["resourceId"]} == {
        world.ids["dad"], world.ids["mom"]}
    assert not any("memories" in p or "sources" in p for p in beta.posts())
    # Strict teardown: relationships first (child-and-parents before couple), then persons.
    assert fixtures.teardown() == [] and beta.live == {}
    deletes = [beta.path(c.request.url) for c in beta.rsps.calls if c.request.method == "DELETE"]
    assert deletes[0].startswith("/platform/tree/child-and-parents-relationships/")
    assert deletes[1].startswith("/platform/tree/couple-relationships/")
    assert all(d.startswith("/platform/tree/persons/") for d in deletes[2:])


# ------------------------------------------------------------------ notes


def test_notes_imply_couple_and_post_to_each_owner(beta):
    world, fixtures = make_world()
    world.need("notes")
    ids = world.ids
    owners = {
        "person_note": f"/platform/tree/persons/{ids['child']}",
        "couple_note": f"/platform/tree/couple-relationships/{ids['couple']}",
        "child_and_parents_note":
            f"/platform/tree/child-and-parents-relationships/{ids['child_and_parents']}",
    }
    envelope = {"person_note": "persons", "couple_note": "relationships",
                "child_and_parents_note": "childAndParentsRelationships"}
    texts = set()
    for key, owner in owners.items():
        stored = beta.live[f"{owner}/notes/{ids[key]}"]
        note = stored[envelope[key]][0]["notes"][0]
        assert note["text"] == world.names[f"{key}_text"]
        assert note["subject"]
        texts.add(note["text"])
    assert len(texts) == 3
    # Notes are embedded in their (already tracked) owners: torn down with them.
    assert fixtures.teardown() == [] and beta.live == {}


def test_note_without_any_new_id_is_an_error(beta):
    world, fixtures = make_world()
    beta.noid.add("person_note")
    with pytest.raises(FSError, match="no new id"):
        world.need("notes")
    assert "person_note" not in world.ids  # never None, never a guess
    assert fixtures.teardown() == [] and beta.live == {}


def test_note_id_from_location_then_entity_header():
    world, _ = make_world()
    r = type("R", (), {"headers": {"Location": BETA + "/platform/tree/persons/A/notes/N9?x=1"}})()
    assert world._note_id(r) == "N9"  # noqa: SLF001
    r = type("R", (), {"headers": {"X-ENTITY-ID": "N8"}})()
    assert world._note_id(r) == "N8"  # noqa: SLF001


def test_person_note_alone_implies_only_persons(beta):
    world, fixtures = make_world()
    world.need("person_note")
    assert set(world.ids) == {"dad", "mom", "child", "person_note"}
    assert not any("relationships" in p for p in beta.posts())
    assert set(world.names) == {"dad_name", "mom_name", "child_name", "person_note_text"}
    assert fixtures.teardown() == [] and beta.live == {}


@pytest.mark.parametrize("group", ["couple_note", "child_and_parents_note"])
def test_relationship_note_implies_couple(beta, group):
    world, _ = make_world()
    world.need(group)
    assert {"couple", "child_and_parents", group} <= set(world.ids)
    assert not any("person_note" in k for k in world.ids)


def test_one_bad_note_envelope_does_not_fail_the_others(beta):
    world, fixtures = make_world()
    beta.fail.add("couple_note")
    beta.fail_status = 400
    with pytest.raises(FSHTTPError) as err:
        world.need("notes")
    assert err.value.status == 400
    assert "person_note" in world.ids and "child_and_parents_note" in world.ids
    assert "couple_note" not in world.ids and "couple_note_text" not in world.names
    world.need("person_note")  # built: no new POST, no error
    beta.fail.clear()
    world.need("notes")  # the retry posts only the missing couple note
    assert "couple_note" in world.ids
    assert len(beta.posts("/platform/tree/persons/" + world.ids["child"] + "/notes")) == 1
    assert fixtures.teardown() == [] and beta.live == {}


# ------------------------------------------------------------------ source


def test_source_implies_persons_tracks_then_attaches_and_finds_reference(beta):
    world, fixtures = make_world()
    world.need("source")
    sd = world.ids["source_description"]
    assert fixtures.tracked("source") == [sd]
    assert fixtures.tracked("couple") == [] and "couple" not in world.ids
    stored = beta.live["/platform/sources/descriptions/" + sd]["sourceDescriptions"][0]
    assert stored["titles"][0]["value"] == world.names["source_title"]
    [(rid, desc)] = beta.attached[world.ids["child"]]
    assert desc == P + "/sources/descriptions/" + sd
    assert world.ids["source_reference"] == rid
    assert fixtures.teardown() == [] and beta.live == {}


def test_source_reference_absent_when_not_discoverable(beta):
    world, fixtures = make_world()
    beta.list_refs = False  # attach succeeds, the listing answers 204
    world.need("source")
    assert "source_description" in world.ids and "source_reference" not in world.ids
    assert fixtures.teardown() == [] and beta.live == {}


@pytest.mark.parametrize("status", [404, 400, 410])
def test_source_reference_lookup_error_still_builds_and_tears_down(beta, status):
    # Live (Task 4 run 1): right after the attach, beta answered 404 to the child's
    # source-references. The reference is optional: the group builds without it.
    world, fixtures = make_world()
    beta.refs_status = status
    world.need("source")
    assert "source_description" in world.ids and "source_reference" not in world.ids
    assert fixtures.tracked("source") == [world.ids["source_description"]]
    assert world.ids["child"] in beta.attached  # the attach itself still ran
    world.need("source")  # built: no second lookup or create
    assert len(beta.posts("/platform/sources/descriptions")) == 1
    assert fixtures.teardown() == [] and beta.live == {}


def test_source_reference_lookup_server_error_is_not_swallowed(beta):
    world, fixtures = make_world()
    beta.refs_status = 500
    with pytest.raises(FSHTTPError):
        world.need("source")
    assert fixtures.tracked("source") == [world.ids["source_description"]]
    assert fixtures.teardown() == [] and beta.live == {}


def test_source_attach_failure_still_tears_down_and_retry_does_not_duplicate(beta):
    world, fixtures = make_world()
    beta.fail.add("attach")
    with pytest.raises(FSHTTPError):
        world.need("source")
    sd = world.ids["source_description"]
    assert fixtures.tracked("source") == [sd]  # tracked before the attach ran
    beta.fail.discard("attach")
    world.need("source")  # retry attaches the same description, creates no other
    assert fixtures.tracked("source") == [sd]
    assert len(beta.posts("/platform/sources/descriptions")) == 1
    assert world.ids["source_reference"]
    assert fixtures.teardown() == [] and beta.live == {}


# ------------------------------------------------------------------ memory


def test_memory_uploads_to_child_and_is_tracked(beta):
    world, fixtures = make_world()
    world.need("memory")
    mid = world.ids["memory"]
    assert fixtures.tracked("memory") == [mid]
    # The persona the upload made for the child, read back as deep.py does.
    # Only its sequence is a world value: the full id contains the memory id.
    persona = beta.personas["/platform/memories/memories/" + mid]
    assert f"{mid}-{world.ids['memory_persona_seq']}" == persona
    assert mid not in world.ids["memory_persona_seq"]
    assert fixtures.tracked("memory") == [mid]  # the persona goes with the memory
    stored = beta.live["/platform/memories/memories/" + mid]
    assert stored["person"] == world.ids["child"]
    assert world.names["memory_title"] in unquote(stored["query"]).replace("+", " ")
    assert not any("relationships" in p or "sources" in p or "/notes" in p
                   for p in beta.posts())
    assert fixtures.teardown() == [] and beta.live == {}


@pytest.mark.parametrize("status", [None, 400, 404])
def test_memory_persona_is_best_effort(beta, status):
    world, fixtures = make_world()
    beta.list_personas = False  # None: the personas read answers 204
    beta.personas_status = status
    world.need("memory")
    assert "memory" in world.ids and "memory_persona_seq" not in world.ids
    assert fixtures.teardown() == [] and beta.live == {}


def test_memory_persona_of_another_form_is_not_kept(beta):
    world, fixtures = make_world()
    beta.persona_form = "P{n}"  # not "<memory id>-<sequence>": not discoverable
    world.need("memory")
    assert "memory" in world.ids and "memory_persona_seq" not in world.ids
    assert fixtures.teardown() == [] and beta.live == {}


def test_memory_persona_lookup_server_error_is_not_swallowed(beta):
    world, fixtures = make_world()
    beta.personas_status = 500
    with pytest.raises(FSHTTPError):
        world.need("memory")
    assert fixtures.tracked("memory") == [world.ids["memory"]]
    beta.personas_status = None
    world.need("memory")  # the retry looks the persona up, uploads nothing new
    assert world.ids["memory_persona_seq"] and len(beta.posts("/platform/tree/persons/")) == 1
    assert fixtures.teardown() == [] and beta.live == {}


def test_persons_only_performs_no_memory_upload(beta):
    world, fixtures = make_world()
    world.need("persons")
    assert beta.posts("/platform/tree/persons/") == []  # no /memories upload, no attach
    assert fixtures.tracked("memory") == []


# ------------------------------------------------------------------ failures


@pytest.mark.parametrize("kind", ["couple", "child_and_parents", "person_note", "couple_note",
                                  "child_and_parents_note"])
def test_mid_build_failure_still_tears_everything_down(beta, kind):
    world, fixtures = make_world()
    beta.fail.add(kind)
    with pytest.raises(FSHTTPError):
        world.need("notes")
    assert beta.live  # something was created ...
    assert fixtures.teardown() == []  # ... and every created object was tracked
    assert beta.live == {}


def test_failure_on_second_person_tracks_the_first(beta):
    world, fixtures = make_world()
    beta.fail_person_at = 2
    with pytest.raises(FSHTTPError):
        world.need("persons")
    assert fixtures.tracked("person") == [world.ids["dad"]]
    assert "mom" not in world.ids
    world.need("persons")  # retry creates only the missing persons
    assert fixtures.tracked("person") == [world.ids["dad"], world.ids["mom"], world.ids["child"]]
    assert len(beta.posts("/platform/tree/persons")) == 4  # 3 created + 1 refused
    assert fixtures.teardown() == [] and beta.live == {}


# ------------------------------------------------------------------ anchors safety


def test_every_value_is_distinct_and_no_value_contains_another(beta):
    world, _ = make_world()
    world.need(*FIXTURE_GROUPS)
    values = all_values(world)
    assert len(values) == len(set(values))
    for a, b in itertools.permutations(values, 2):
        assert a not in b, f"{a!r} is contained in {b!r}"
    assert set(world.ids) == {"dad", "mom", "child", "couple", "child_and_parents",
                              "person_note", "couple_note", "child_and_parents_note",
                              "source_description", "source_reference", "collection",
                              "memory", "memory_persona_seq"}


def test_names_are_long_per_fixture_and_free_of_shared_constants(beta):
    world, _ = make_world()
    world.need(*FIXTURE_GROUPS)
    assert set(world.names) == {"dad_name", "mom_name", "child_name", "person_note_text",
                                "couple_note_text", "child_and_parents_note_text",
                                "source_title", "collection_title", "memory_title"}
    for value in world.names.values():
        assert len(value) >= 12 and value.startswith("ZZSPIKE")
        # ZZSPIKE + a tag letter + 12 digits: nothing a server could re-case.
        for word in value.split():
            assert re.fullmatch(r"ZZSPIKE[A-Z]\d{12}", word) or word == UNIQUE, word
    # The shared surname, the prefix and the reason are not values on their own.
    for shared in (UNIQUE, "ZZSPIKE", REASON):
        assert shared not in all_values(world)


def test_names_differ_between_runs(beta):
    a, _ = make_world()
    b, _ = make_world()
    a.need("persons", "notes", "source", "memory")
    b.need("persons", "notes", "source", "memory")
    assert not set(a.names.values()) & set(b.names.values())


def test_collision_between_values_is_refused():
    world, _ = make_world()
    world._ids.update({"dad": "ABCD-123", "mom": "XABCD-1234"})  # noqa: SLF001
    with pytest.raises(FSError, match="contained"):
        world._check_distinct()  # noqa: SLF001


# ------------------------------------------------------------------ canaries


def test_canaries_are_the_canary_block_and_touch_no_network(beta):
    world, fixtures = make_world()
    world.need(CANARY_GROUP)
    assert dict(world.names) == dict(CANARY_NAMES) and dict(world.ids) == {}
    assert len(beta.rsps.calls) == 0  # no discovery, no write: constants only
    assert fixtures.teardown() == []
    world.need(CANARY_GROUP)  # idempotent


def test_canary_values_are_long_distinct_and_contain_no_other():
    values = list(CANARY_NAMES.values())
    assert values and len(values) == len(set(values))
    for a, b in itertools.permutations(values, 2):
        assert a not in b, f"{a!r} is contained in {b!r}"
    for value in values:
        # Anchors are found by containment and checked against a random hex marker:
        # a short or all-digit/hex value could match by chance.
        assert len(value) >= 6 and not re.fullmatch(r"[0-9a-f]+", value), value
        assert "ZZSPIKE" not in value


@pytest.mark.parametrize("groups", [("canaries", "persons"), ("persons", "canaries"),
                                    ("memory", "canaries")])
def test_canaries_never_share_a_world_with_fixtures(beta, groups):
    # Canaries are real beta reference data; the fixtures' random digits could contain
    # them, so one world holds either, never both (in one call or call by call).
    world, _ = make_world()
    with pytest.raises(ValueError, match="canaries"):
        world.need(*groups)
    assert beta.posts() == []
    world, _ = make_world()
    world.need(groups[0])
    with pytest.raises(ValueError, match="canaries"):
        world.need(groups[1])


# ------------------------------------------------------------------ sealing


def test_ids_and_names_are_read_only(beta):
    world, _ = make_world()
    world.need("persons")
    with pytest.raises(TypeError):
        world.ids["dad"] = "x"
    with pytest.raises(TypeError):
        world.names["child_name"] = "x"
    with pytest.raises(AttributeError):
        world.ids = {}


def test_sealed_view_holds_only_strings_and_no_client(beta):
    world, _ = make_world()
    world.need("notes")
    view = seal(world)
    assert dict(view.ids) == dict(world.ids) and dict(view.names) == dict(world.names)
    assert all(isinstance(v, str) for v in all_values(view))
    assert not hasattr(view, "_client") and not hasattr(view, "_fixtures")
    with pytest.raises(AssertionError, match="sealed"):
        view.need("memory")


# ------------------------------------------------------------------ through the runner


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


def test_default_world_runs_a_read_recipe_and_tears_down(beta, reg_path):
    def expect(result, world):
        person = json_path(result.body, "persons[0]")
        assert person["id"] == world.ids["child"], "persons[0] is not the child"

    case = ReadCase(op="readPerson", path_args=lambda w: {"pid": w.ids["child"]},
                    expect=expect, missing={"pid": "ZZNOPE-0000"}, needs=("persons",),
                    anchors=(lambda w: w.names["child_name"],))
    recipe = reads_recipe("reads.test.world", [case])  # default_world: the real World
    client = beta_client()
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    result = runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                               today="2026-10-25")
    assert result["passed"], result
    assert beta.live == {}  # the runner's teardown removed every fixture
    assert len(beta.posts("/platform/tree/persons")) == 3


def test_collection_holds_the_source_and_goes_after_it(beta):
    world, fixtures = make_world()
    world.need("collection")
    udcid, sid = world.ids["collection"], world.ids["source_description"]
    assert fixtures.tracked("collection") == [udcid]
    assert beta.filed == {f"/platform/sources/collections/{udcid}": [sid]}
    assert world.names["collection_title"].startswith("ZZSPIKEF")
    world.need("collection")  # built once
    assert len(beta.posts("/platform/sources/collections")) == 2  # create + file
    assert fixtures.teardown() == [] and beta.live == {}
    deletes = [beta.path(c.request.url) for c in beta.rsps.calls
               if c.request.method == "DELETE"]
    assert deletes.index(f"/platform/sources/descriptions/{sid}") < deletes.index(
        f"/platform/sources/collections/{udcid}")


def test_a_failed_filing_is_retried_without_a_second_collection(beta):
    world, fixtures = make_world()
    beta.fail.add("file")
    with pytest.raises(FSHTTPError):
        world.need("collection")
    assert len(fixtures.tracked("collection")) == 1  # tracked before the filing failed
    beta.fail.discard("file")
    world.need("collection")
    assert len(fixtures.tracked("collection")) == 1
    assert fixtures.teardown() == [] and beta.live == {}


# ------------------------------------------------------------------ relationship_source


def test_relationship_source_implies_couple_and_source_and_attaches_to_both(beta):
    world, fixtures = make_world()
    world.need("relationship_source")
    ids = world.ids
    assert {"dad", "mom", "child", "couple", "child_and_parents",
            "source_description"} <= set(ids)
    sd_url = P + "/sources/descriptions/" + ids["source_description"]
    couple = f"/platform/tree/couple-relationships/{ids['couple']}"
    cap = f"/platform/tree/child-and-parents-relationships/{ids['child_and_parents']}"
    assert beta.rel_attached == {
        couple: [("relationships", sd_url)],
        cap: [("childAndParentsRelationships", sd_url)]}
    assert fixtures.tracked("source") == [ids["source_description"]]
    world.need("relationship_source")  # built once
    assert len(beta.posts(couple + "/source-references")) == 1
    assert fixtures.teardown() == [] and beta.live == {}


def test_a_failed_relationship_attach_is_retried_without_duplicates(beta):
    world, fixtures = make_world()
    beta.fail.add("rel_attach")
    with pytest.raises(FSHTTPError):
        world.need("relationship_source")
    assert fixtures.tracked("couple")  # tracked before the attach ran
    beta.fail.discard("rel_attach")
    world.need("relationship_source")
    assert sum(len(v) for v in beta.rel_attached.values()) == 2
    assert len(fixtures.tracked("source")) == 1
    assert fixtures.teardown() == [] and beta.live == {}
