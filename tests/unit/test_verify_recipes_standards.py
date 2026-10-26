"""Offline tests for the Standards, Memories, Users and Utilities read recipes (no network).

``FakeStandards`` answers the standards reads for the canaries in the shapes the recon
recorded (docs/superpowers/recon/2026-10-25-api-surface/shapes.json): place lists under
``places``, searches as atom ``entries``, place types and vocabularies as JSON-LD, the
standardized date as text/plain; unknown ids answer 404, a search with no match the
documented 204. It also keeps the fixture memory (a description titled with its own
value, and the child's persona). The registered recipe objects run through the real
``run_recipe`` with ``tools.verify.world.World`` replaced by a world that serves the
fake's values; each ``mode`` makes the fake lie in one way the recipes must catch.
"""
import json
import re
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from familysearch.api import OPERATIONS
from familysearch.api.registry import REGISTRY_PATH
from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.spec.resolve import SNAPSHOT_PATH, load_overrides, resolve
from tools.verify import reads, runner, world
from tools.verify.recipes import RECIPES, validate_recipes
from tools.verify.recipes import reads_misc as rm
from tools.verify.recipes import reads_standards as rs
from tools.verify.recipes import reads_tree_persons as rtp
from tools.verify.recipes import reads_tree_relations as rtr

BETA = Environment.BETA.base_url
P = BETA + "/platform"
FS_JSON = "application/x-fs-v1+json"
ATOM_JSON = "application/x-gedcomx-atom+json"
LD_JSON = "application/ld+json"
GX = "http://gedcomx.org/"
CHILD = "KWZZ-CHD"
CHILD_NAME = "ZZSPIKEC100000000003 ZZSPIKETESTSURNAME"
MEMORY = ("4017777", "ZZSPIKEM100000000008")
OTHER_MEMORY_TITLE = "ZZSPIKEM199999999999"
COMMENT = "ZZSPIKEC155555555555"
OTHER_COMMENT = "ZZSPIKEC199999999999"
PERSONA_SEQ = "74542947"
PERSONA = f"{MEMORY[0]}-{PERSONA_SEQ}"  # live run 1: "<memory id>-<sequence>"
STANDARDS = {r.name for r in rs.READS_STANDARDS}
MISC = {r.name for r in rm.READS_MISC}
OWN = STANDARDS | MISC
SCOPE_MODULES = {"standards", "memories", "users", "utilities"}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


class FakeWorld:
    """The world as the fake knows it: the memory fixture, or the canary block."""

    def __init__(self, client, fixtures, reason, unique):
        self.ids, self.names = {}, {}

    def need(self, *groups):
        if world.CANARY_GROUP in groups:
            assert set(groups) == {world.CANARY_GROUP}, groups  # never mixed with fixtures
            self.names.update(world.CANARY_NAMES)
            return
        self.ids.update(dad="KWZZ-DAD", mom="KWZZ-MOM", child=CHILD, memory=MEMORY[0],
                        memory_persona_seq=PERSONA_SEQ)
        self.names.update(child_name=CHILD_NAME,
                          memory_title=MEMORY[1], memory_comment_text=COMMENT)


class FakeStandards:
    """Beta's standards and memories reads; ``mode`` is None (honest) or one lie:
    ``other_record`` answers each read with another record of its kind; ``ignore_id``
    answers every id or query with the canary's record; ``catch_all`` answers unknown
    ids 200; ``empty`` answers every populated read 204; ``wrong_endpoint`` answers
    with a neighbouring endpoint's payload (a place's description for its children and
    back, a vocab list for its term and back, the memory list for a persona); ``echo``
    answers the date and name services with what was sent."""

    def __init__(self, mode=None):
        self.mode = mode

    # ------------------------------------------------------------ values

    def name(self, key):
        if self.mode == "other_record":
            return f"Elsewhere record for {key.replace('_', ' ')}"
        return world.CANARY_NAMES[key]

    def _known(self, value, canary):
        return self.mode == "ignore_id" or value == canary

    def _answer(self, status, body=None, content_type=FS_JSON):
        if body is None:
            return status, {}, ""
        if isinstance(body, str):
            return status, {"Content-Type": "text/plain;charset=UTF-8"}, body
        return status, {"Content-Type": content_type}, json.dumps(body)

    def _ok(self, body, content_type=FS_JSON):
        if self.mode == "empty":
            return self._answer(204)
        return self._answer(200, body, content_type)

    def _unknown(self, content_type=FS_JSON):
        if self.mode == "catch_all":
            return self._answer(200, {"places": [], "entries": []}, content_type)
        return self._answer(404, {"errors": [{"code": 404, "message": "not found"}]})

    def _nothing(self, content_type=FS_JSON):
        """A search with no match: the documented empty 204."""
        if self.mode == "catch_all":
            return self._answer(200, {"entries": []}, content_type)
        return self._answer(204)

    # ------------------------------------------------------------ places

    def description(self):
        name = self.name("place_name")
        return {"id": world.CANARY_PLACE_DESCRIPTION, "names": [{"value": name}],
                "display": {"name": name, "fullName": f"{name}, Somewhere"}}

    def child_place(self):
        return {"id": "7700001", "display": {"name": self.name("place_child_name")},
                "jurisdiction": {"resource": f"{P}/places/description/"
                                             f"{world.CANARY_PLACE_DESCRIPTION}"}}

    def jurisdiction(self):
        return {"id": "7700002", "display": {"name": "A Jurisdiction Above"}}

    def places(self, request):
        path = urlsplit(request.url).path.split("/platform/places", 1)[1]
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        pdid = world.CANARY_PLACE_DESCRIPTION
        if path == "/description":
            if not self._known(query.get("pdids"), pdid):
                return self._unknown()
            return self._ok({"places": [self.description()]})
        m = re.fullmatch(r"/description/([^/]+)(/\w+)?", path)
        if m:
            if not self._known(m.group(1), pdid):
                return self._unknown()
            what = m.group(2) or ""
            if self.mode == "wrong_endpoint":
                what = {"": "/children", "/children": "", "/related": ""}.get(what, what)
            if what == "":
                return self._ok({"places": [self.description(), self.jurisdiction()]})
            if what == "/children":
                return self._ok({"places": [self.child_place()]})
            if what == "/related":  # live run 2: the canary first, linked as such; no child
                place = dict(self.description(), links={"description-with-related": {
                    "href": f"{P}/places/description/{pdid}/related"}})
                return self._ok({"places": [place, self.jurisdiction()]})
            if what == "/attributes":
                # Live run 1: the attribute values are empty; each attribute has an id.
                return self._ok({"@id": f"{P}/places/description/{pdid}/attributes",
                                 "474": [{"@id": self.name("place_attribute_id"),
                                          "@type": "fs-place-attribute-type:474",
                                          "value": ""}],
                                 "@context": {"474": {
                                     "http://www.w3.org/2000/01/rdf-schema#label":
                                         "Wikipedia link"}}}, LD_JSON)
        m = re.fullmatch(r"/groups/([^/]+)", path)
        if m:
            if not self._known(m.group(1), world.CANARY_PLACE_GROUP):
                return self._unknown()
            return self._ok({"description": "#g", "places": [self.child_place()],
                             "sourceDescriptions": [{"id": world.CANARY_PLACE_GROUP, "titles": [
                                 {"lang": "de", "value": self.name("place_group_title")},
                                 {"lang": "en", "value": "Asia and Middle East"}]}]})
        m = re.fullmatch(r"/(types|type-groups)/([^/]+)", path)
        if m:
            kind = m.group(1)
            canary = world.CANARY_PLACE_TYPE if kind == "types" else world.CANARY_PLACE_TYPE_GROUP
            if not self._known(m.group(2), canary):
                return self._unknown(LD_JSON)
            if kind == "types":  # live run 1: a short label and a long description
                return self._ok({"id": canary, "@id": f"{P}/places/types/{canary}",
                                 "labels": [{"@language": "en", "@value": "City"}],
                                 "descriptions": [{"@language": "en", "@value": self.name(
                                     "place_type_description") + " with complex systems"}]},
                                LD_JSON)
            return self._ok({"id": canary, "@id": f"{P}/places/type-groups/{canary}",
                             "title": "Administrative",
                             "description": self.name("place_type_group_description"),
                             "elements": [{"id": "321", "labels": [
                                 {"@value": "Principality or Emirate"}]}]}, LD_JSON)
        if path in ("/search", "/parents"):
            asked = query.get("q", "") if path == "/search" else query.get("value", "")
            if world.CANARY_PLACE_QUERY not in asked and self.mode != "ignore_id":
                return self._nothing(ATOM_JSON)
            full = ("Provo, Elsewhere" if self.mode == "other_record"
                    else world.CANARY_NAMES["provo_full_name"])
            return self._ok({"entries": [{"id": "7700003", "content": {"gedcomx": {"places": [
                {"id": "7700003", "display": {"name": "Provo", "fullName": full}}]}}}]},
                ATOM_JSON)
        m = re.fullmatch(r"/([^/]+)", path)
        if m:  # /places/{pid}
            if not self._known(m.group(1), world.CANARY_PLACE):
                return self._unknown()
            # Live run 1: the place's descriptions, each with its many names.
            return self._ok({"places": [{"id": "1", "names": [
                {"lang": "en", "value": "u s"},
                {"lang": "en", "value": self.name("place_by_id_name")}]}]})
        return self._unknown()

    # ------------------------------------------------------------ vocab

    def vocab(self, request):
        path = urlsplit(request.url).path.split("/platform/vocab", 1)[1]
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        if path == "/concepts/search":
            if "List" not in query.get("q", "") and self.mode != "ignore_id":
                return self._nothing(ATOM_JSON)
            hit = "(3) - " + self.name("vocab_search_hit") + " for works"
            return self._ok({"entries": [{"content": {"gedcomx": {"vocabConcepts": [
                {"id": "135106", "description": hit}]}}}], "index": 0}, ATOM_JSON)
        m = re.fullmatch(r"/(lists|terms|concepts)/([^/]+)(?:/(translations|definition)/"
                         r"([^/]+))?", path)
        if not m:
            return self._unknown()
        kind, rid, sub, locale = m.groups()
        canary = {"lists": world.CANARY_VOCAB_LIST, "terms": world.CANARY_VOCAB_TERM,
                  "concepts": world.CANARY_VOCAB_CONCEPT}[kind]
        if not self._known(rid, canary) or (sub and locale != world.CANARY_LOCALE
                                            and self.mode != "ignore_id"):
            return self._unknown()
        if self.mode == "wrong_endpoint" and not sub:
            kind = {"lists": "terms", "terms": "lists"}.get(kind, kind)
        label = {"@value": self.name("vocab_term_label"), "@language": "en"}
        if sub == "translations":  # live run 1: {"lang", "text"}
            return self._ok({"lang": "en", "text": self.name("vocab_term_label")})
        if sub == "definition":
            return self._ok({"lang": "en", "text": self.name("vocab_definition") + " region"})
        if kind == "lists":
            return self._ok({"id": canary, "@id": f"{P}/vocab/lists/{canary}",
                             "title": self.name("vocab_list_title"),
                             "elements": [{"@id": f"{P}/vocab/terms/1"}]}, LD_JSON)
        if kind == "terms":
            return self._ok({"id": canary, "@id": f"{P}/vocab/terms/{canary}",
                             "labels": [label]}, LD_JSON)
        # Live run 1: concept 123363 is Census (not the term of the same id).
        return self._ok({"vocabConcepts": [{"id": canary,
                                            "gedcomxUri": self.name("vocab_concept_uri"),
                                            "vocabTerms": [{"id": "101613", "values": [
                                                {"value": "Federal Census Record"}]}]}]},
                        "application/ld-v2+json")

    # ------------------------------------------------------------ dates and names

    def dates(self, request):
        date = parse_qs(urlsplit(request.url).query).get("date", [""])[0]
        if self.mode == "echo":
            return self._answer(200, date)
        if date != rs.DATE_SENT and self.mode != "ignore_id":
            return self._answer(400, {"errors": [{"message": "cannot parse the date"}]})
        standard = "27 November 1934" if self.mode == "other_record" else rs.DATE_STANDARD
        return self._answer(204) if self.mode == "empty" else self._answer(200, standard)

    def names(self, request):
        if request.method == "POST":
            body = json.loads(request.body)
            parts = body["persons"][0]["names"][0]["nameForms"][0]["parts"]
            if not parts and self.mode != "ignore_id":
                return self._answer(400, {"errors": [{"message": "no name parts"}]})
            values = [p["value"] for p in parts] or ["Somebody"]
            if self.mode == "other_record":
                values = values[::-1]
            form = {"parts": parts} if self.mode == "echo" else {"fullText": " ".join(values)}
            return self._ok({"persons": [{"names": [{"nameForms": [form]}]}]})
        full = parse_qs(urlsplit(request.url).query, keep_blank_values=True).get(
            "fullName", [""])[0]
        if not full.strip() and self.mode != "ignore_id":
            return self._answer(400, {"errors": [{"message": "A name must be provided."}]})
        words = (full or "Somebody Else").split()
        if self.mode == "other_record":
            words = words[::-1]
        form = {"fullText": full}
        if self.mode != "echo":
            form["parts"] = [{"type": GX + "Given", "value": " ".join(words[:-1])},
                             {"type": GX + "Surname", "value": words[-1]}]
        return self._ok({"persons": [{"names": [{"nameForms": [form]}]}]})

    # ------------------------------------------------------------ memories

    def memory(self):
        title = OTHER_MEMORY_TITLE if self.mode == "other_record" else MEMORY[1]
        return {"id": MEMORY[0], "titles": [{"value": title}], "mediaType": "image/png"}

    def persona(self, tree_id):
        # Live run 2: the persona is named as the child it was made for.
        name = ("ZZSPIKEC199999999999 ZZSPIKETESTSURNAME" if self.mode == "other_record"
                else CHILD_NAME)
        person = {"id": PERSONA, "display": {"name": name},
                  "names": [{"nameForms": [{"fullText": name}]}],
                  "media": [{"description": f"{P}/memories/memories/{MEMORY[0]}"}]}
        if tree_id:
            ark = f"{BETA}/ark:/61903/4:1:{'KWZZ-OTH' if self.mode == 'other_record' else CHILD}"
            person["identifiers"] = {"http://familysearch.org/v1/FamilyTreePerson": [ark]}
        return person

    def memories(self, request):
        path = urlsplit(request.url).path.split("/platform/memories", 1)[1]
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        if path == "/memories":
            if not self._known(query.get("mids"), MEMORY[0]):
                return self._nothing()  # documented: no results
            extra = [{"id": "4019999", "titles": [{"value": "an older memory"}]}]
            return self._ok({"sourceDescriptions": [self.memory()] + (
                extra if self.mode == "ignore_id" else [])})
        c = re.fullmatch(r"/memories/([^/]+)/comments", path)
        if c:
            if not self._known(c.group(1), MEMORY[0]):
                return self._unknown()
            text = OTHER_COMMENT if self.mode == "other_record" else COMMENT
            # Live shape unknown: the recon shows only discussions and sourceDescriptions.
            return self._ok({"discussions": [{"id": "D1", "comments": [
                {"id": "C1", "text": text}]}], "sourceDescriptions": [self.memory()]})
        m = re.fullmatch(r"/memories/([^/]+)(?:/personas(?:/([^/]+))?)?", path)
        if not m or not self._known(m.group(1), MEMORY[0]):
            return self._unknown()
        if self.mode == "wrong_endpoint" and "/personas" in path:
            return self._ok({"sourceDescriptions": [self.memory()]})
        if path.endswith("/personas"):
            tree = query.get("includeTreeIdentifier") == "true"
            return self._ok({"persons": [self.persona(tree)],
                             "sourceDescriptions": [self.memory()]})
        if m.group(2) is not None:
            if not self._known(m.group(2), PERSONA):
                return self._unknown()
            # Live run 2: the memory here is a bare reference, without its title.
            return self._ok({"persons": [self.persona(True)],
                             "sourceDescriptions": [{"id": MEMORY[0]}]})
        return self._ok({"sourceDescriptions": [self.memory()]})

    def serve(self):
        for method in (responses.GET, responses.POST):
            responses.add_callback(method, re.compile(re.escape(P + "/places/") + ".*"),
                                   callback=self.places)
            responses.add_callback(method, re.compile(re.escape(P + "/vocab/") + ".*"),
                                   callback=self.vocab)
            responses.add_callback(method, re.compile(re.escape(P + "/dates") + r"(\?.*)?$"),
                                   callback=self.dates)
            responses.add_callback(method, re.compile(re.escape(P + "/names/") + ".*"),
                                   callback=self.names)
            responses.add_callback(method, re.compile(re.escape(P + "/memories/") + ".*"),
                                   callback=self.memories)


@pytest.fixture
def fake_world(monkeypatch):
    monkeypatch.setattr(world, "World", FakeWorld)
    slept: list[float] = []
    monkeypatch.setattr(reads, "sleep", slept.append)
    return slept


def run(name, reg_path, mode=None):
    FakeStandards(mode).serve()
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    return runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                             today="2026-10-25")


# ------------------------------------------------------------------ the fake passes


@responses.activate
@pytest.mark.parametrize("name", sorted(OWN))
def test_recipe_passes_against_the_honest_fake(name, reg_path, fake_world):
    result = run(name, reg_path)
    assert result.pop("notes", []) == []  # nothing polls
    assert result == {"recipe": name, "passed": True, "problems": []}
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        entry = registry[op]["generated"]
        assert entry["status"] == "verified" and entry["evidence"] == "read-content"
        assert entry["recipe"] == name


@responses.activate
def test_place_searches_send_provo_and_anchor_on_the_full_name(reg_path, fake_world):
    result = run("reads.standards.place_search", reg_path)
    assert result["passed"] is True, result["problems"]
    urls = [c.request.url for c in responses.calls]
    assert any("/places/parents?value=Provo" in u for u in urls)
    controls = [c for c in responses.calls if rs.NO_MATCH in c.request.url]
    assert len(controls) == 2 and all(c.response.status_code == 204 for c in controls)
    # The anchor is never what was sent.
    assert world.CANARY_PLACE_QUERY != world.CANARY_NAMES["provo_full_name"]


@responses.activate
def test_standards_cases_send_the_canary_ids_never_as_world_values(reg_path, fake_world):
    run("reads.standards.place_descriptions", reg_path)
    paths = {urlsplit(c.request.url).path for c in responses.calls}
    assert f"/platform/places/description/{world.CANARY_PLACE_DESCRIPTION}/children" in paths
    for value in (world.CANARY_PLACE_DESCRIPTION, world.CANARY_VOCAB_TERM,
                  world.CANARY_PLACE_GROUP):
        assert value not in world.CANARY_NAMES.values()


@responses.activate
def test_compose_name_posts_parts_and_is_a_read(reg_path, fake_world):
    from familysearch.api.spec import is_write
    assert not is_write(OPERATIONS["createNameSegments"])
    result = run("reads.standards.compose_name", reg_path)
    assert result["passed"] is True, result["problems"]
    posts = [c.request for c in responses.calls if c.request.method == "POST"]
    assert len(posts) == 2 and all("X-Reason" not in r.headers for r in posts)
    sent = json.dumps(json.loads(posts[0].body))
    assert rs.NAME_FULL not in sent  # the literal is computed, not echoed


def test_user_memories_deferred_for_want_of_the_account_id():
    # Live run 2: uid=current answers 400; the world holds no account id (ruling I-4).
    reason = rm.DEFERRED["readUserMemories"]
    assert "400" in reason and "I-4" in reason
    assert "reads.memories.user" not in RECIPES


@responses.activate
def test_related_places_need_the_related_link_not_a_child(reg_path, fake_world):
    # Live run 2: the 94 related places hold no child of the canary.
    case = next(c for c in rs.PLACE_DESCRIPTIONS_CASES
                if c.op == "readPlaceDescriptionWithRelated")
    assert [a(FakeWorldFor("canaries")) for a in case.anchors] == ["Weymouth"]
    # The plain description (its jurisdiction chain, no related link) must not pass.
    result = run("reads.standards.place_descriptions", reg_path, "wrong_endpoint")
    assert ("readPlaceDescriptionWithRelated: place 4229303 is not linked as described "
            "with related places") in "\n".join(result["problems"])


def test_memory_persona_anchors_on_the_child_name():
    case = next(c for c in rm.MEMORIES_CASES if c.op == "readMemoryPersona")
    assert [a(FakeWorldFor("memory")) for a in case.anchors] == [CHILD_NAME]


def test_memory_persona_case_needs_the_discovered_persona():
    case = next(c for c in rm.MEMORIES_CASES if c.op == "readMemoryPersona")
    assert case.needs == ("memory",)
    assert case.path_args(FakeWorldFor("memory")) == {"mid": MEMORY[0], "pid": PERSONA}


def test_memory_comments_case_anchors_on_the_comment_text():
    # createMemoryComments is verified by its own write recipe; the world posts the comment.
    case = rm.MEMORY_COMMENTS_CASES[0]
    assert case.op == "readMemoryComments" and "readMemoryComments" not in rm.DEFERRED
    assert case.needs == ("memory_comment",) and case.missing == rm.MISSING_MID
    assert [a(FakeWorldFor("memory_comment")) for a in case.anchors] == [COMMENT]


def FakeWorldFor(*groups):  # noqa: N802 - a tiny factory for one assertion
    w = FakeWorld(None, None, None, None)
    w.need(*groups)
    return w


# ------------------------------------------------------------------ the fake lies

ALL = OWN
LIES = {
    "other_record": ALL,
    "ignore_id": ALL,
    "catch_all": ALL - {"reads.standards.dates_names", "reads.standards.compose_name"},
    "empty": ALL,
    "wrong_endpoint": {"reads.standards.place_descriptions", "reads.standards.vocab",
                       "reads.memories"},
    "echo": {"reads.standards.dates_names", "reads.standards.compose_name"},
}
CAUGHT_BY = {
    "other_record": ("not in the response", "no name form segments"),
    "ignore_id": ("not-found control answered 200", "not only the memory"),
    "catch_all": ("not-found control answered 200",),
    "empty": ("positive call answered 204",),
    "wrong_endpoint": ("not in the response", "no persons in the response",
                       "not linked as described with related"),
    "echo": ("not in the response",),
}


@responses.activate
@pytest.mark.parametrize("mode, name", sorted(
    (mode, name) for mode, names in LIES.items() for name in names))
def test_recipe_fails_when_the_fake_lies(mode, name, reg_path, fake_world):
    raw = reg_path.read_bytes()
    result = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    problems = "\n".join(result["problems"])
    assert any(s in problems for s in CAUGHT_BY[mode]), result["problems"]
    assert reg_path.read_bytes() == raw  # nothing recorded


@responses.activate
def test_every_wrong_endpoint_case_is_caught(reg_path, fake_world):
    swapped = {"readPlaceDescription", "readPlaceChildren", "readPlaceDescriptionWithRelated",
               "readVocabList",
               "readVocabTerm", "readMemoryPersonas", "readMemoryPersona"}
    failed = set()
    for name in LIES["wrong_endpoint"]:
        problems = "\n".join(run(name, reg_path, "wrong_endpoint")["problems"])
        failed |= {op for op in swapped if f"{op}:" in problems}
    assert failed == swapped


# ------------------------------------------------------------------ coverage


def _scope() -> set[str]:
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    ops, _excluded = resolve(snapshot, load_overrides())
    return {o.operation_id for o in ops if o.module in SCOPE_MODULES and o.kind == "read"}


def test_recipes_cover_exactly_the_scope_minus_deferred():
    scope = _scope()
    assert len(scope) == 35
    covered = {op for r in (*rs.READS_STANDARDS, *rm.READS_MISC) for op in r.ops}
    deferred = set(rs.DEFERRED) | set(rm.DEFERRED)
    assert not set(rs.DEFERRED) & set(rm.DEFERRED)
    assert not covered & deferred, covered & deferred
    assert covered | deferred == scope, (
        f"uncovered: {sorted(scope - covered - deferred)}; "
        f"outside scope: {sorted((covered | deferred) - scope)}")
    tree = ({op for r in (*rtp.READS_TREE_PERSONS, *rtr.READS_TREE_RELATIONS) for op in r.ops}
            | set(rtp.DEFERRED) | set(rtr.DEFERRED))
    assert not (covered | deferred) & tree
    for op, reason in {**rs.DEFERRED, **rm.DEFERRED}.items():
        assert isinstance(reason, str) and len(reason.split()) >= 12, op
        assert OPERATIONS[op].kind == "read", op
    assert "readPartnerEligibility" in rm.DEFERRED and "403" in rm.DEFERRED[
        "readPartnerEligibility"]


def test_recipes_registered_small_and_canaries_stay_apart():
    assert validate_recipes(RECIPES) == []
    for recipe in (*rs.READS_STANDARDS, *rm.READS_MISC):
        assert RECIPES[recipe.name] is recipe and len(recipe.ops) <= 12
    cases = [c for r in (rs.PLACE_DESCRIPTIONS_CASES, rs.PLACE_GROUPS_CASES,
                         rs.PLACES_BY_ID_CASES, rs.PLACE_SEARCH_CASES, rs.VOCAB_CASES,
                         rs.VOCAB_MORE_CASES, rs.DATES_NAMES_CASES, rs.COMPOSE_NAME_CASES)
             for c in r]
    for case in cases:  # canaries only, or anchor-free with no needs at all
        assert case.needs in ((world.CANARY_GROUP,), ()), case.op
        assert bool(case.needs) != bool(case.anchor_free_reason), case.op
    for case in rm.MEMORIES_CASES:
        assert case.needs == ("memory",)
