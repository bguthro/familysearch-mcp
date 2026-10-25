"""Offline tests for the read recipes against a scripted fake tree (no network, no cookies).

``FakeTree`` keeps the fixture family (dad, mom, child, their couple and child-and-parents
relationships) in memory and answers the person-centred reads in the GEDCOM X shapes the
recon recorded (docs/superpowers/recon/2026-10-25-api-surface/shapes.json) and the live
runs: relative lists answer 204 when empty, ``readPersons`` needs ``pids`` and answers
an unknown one 204, search (over real beta data, with the canary) answers 400 to no
parameter at all, change histories are atom feeds whose links carry the person id. For
the Task 4 reads it also keeps the relationships' records, feeds and notes, the person note, the source
(description, the child's reference to it, the user's source box) and the memory; only the
child has a note, a source and a memory, so the dad's lists answer 204 as in the recon. The
registered recipe objects run through the real ``run_recipe`` with the fixture world
replaced (``tools.verify.world.World``) by one that serves the fake's ids and names; each
``mode`` makes the fake lie in one way the recipes must catch.
"""
import json
import re
import shutil
import zlib
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
from tools.verify.recipes import reads_tree_persons as rtp
from tools.verify.recipes import reads_tree_relations as rtr

BETA = Environment.BETA.base_url
TREE = BETA + "/platform/tree"
GX = "http://gedcomx.org/"
FS_JSON = "application/x-fs-v1+json"
ATOM_JSON = "application/x-gedcomx-atom+json"
SURNAME = "ZZSPIKETESTSURNAME"
IDS = {"dad": "KWZZ-DAD", "mom": "KWZZ-MOM", "child": "KWZZ-CHD",
       "couple": "MMZZ-CPL", "child_and_parents": "MMZZ-CAP"}
GIVEN = {"dad": "ZZSPIKED100000000001", "mom": "ZZSPIKEM100000000002",
         "child": "ZZSPIKEC100000000003"}
GENDER = {"dad": "Male", "mom": "Female", "child": "Male"}
# The notes, source and memory hung on the family (Task 4), and the envelopes they live in.
NOTE_IDS = {"person_note": "0f0f0001-aaaa-4bbb-8ccc-000000000001",
            "couple_note": "0f0f0002-aaaa-4bbb-8ccc-000000000002",
            "child_and_parents_note": "0f0f0003-aaaa-4bbb-8ccc-000000000003"}
NOTE_TEXT = {"person_note": "ZZSPIKEP100000000004", "couple_note": "ZZSPIKER100000000005",
             "child_and_parents_note": "ZZSPIKEA100000000006"}
SOURCE = ("SDZZ-SRC", "ZZSPIKES100000000007")
MEMORY = ("4017777", "ZZSPIKEM100000000008")
# Records of the same kinds that are not the fixtures' (the ``other_record`` lie).
OTHER_SOURCE = ("SDZZ-OTH", "ZZSPIKES199999999999")
OTHER_MEMORY = ("4019999", "ZZSPIKEM199999999999")
PID_FORMAT = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{3,4}$")
OWN_RECIPES = {r.name for r in rtp.READS_TREE_PERSONS}
SCOPE_PREFIXES = ("/tree/persons", "/tree/search", "/tree/matches", "/tree/current-person")
# Sub-resources of a person that Task 4 verifies (with the other relationship reads).
TASK4_SEGMENTS = {"notes", "sources", "source-references", "memories", "discussions",
                  "comments", "collections"}


@pytest.fixture
def reg_path(tmp_path):
    p = tmp_path / "registry.json"
    shutil.copyfile(REGISTRY_PATH, p)
    return p


class FakeWorld:
    """The fixture world as the fake tree knows it (what tools/verify/world.py would hold)."""

    def __init__(self, client, fixtures, reason, unique):
        self.ids = {**IDS, **NOTE_IDS, "source_description": SOURCE[0], "memory": MEMORY[0]}
        self.names = {f"{role}_name": f"{given} {SURNAME}" for role, given in GIVEN.items()}
        self.names.update({f"{key}_text": text for key, text in NOTE_TEXT.items()})
        self.names.update(source_title=SOURCE[1], memory_title=MEMORY[1])
        self.needed: list[str] = []

    def need(self, *groups):
        self.needed.extend(groups)


class FakeTree:
    """The fixture family on a fake beta.

    ``mode`` is None (honest) or one lie: ``other_person`` answers every person read with
    the next person's data (dad -> mom -> child -> dad); ``ignore_pids`` answers
    readPersons with everyone; ``catch_all`` answers 200 for unknown ids; ``empty``
    answers 204 for every populated list; ``stale_head`` answers HEAD with another
    revision than the GET. For the Task 4 reads: ``other_record`` answers each note,
    source and memory read with another record of its kind; ``swap_relationships``
    answers the couple's resources with the child-and-parents' and back;
    ``pedigree_swap`` answers ancestry with the descendancy and back; ``ignore_nid``
    answers a single-note read with the owner's note whatever the id; ``ignore_sdids``
    answers readSourceDescriptions with the whole source box.
    """

    def __init__(self, mode=None, *, relationship_lag=0, search_lie=None, reference_lag=0):
        self.relationship_lag = relationship_lag  # 204s before the relationship is computed
        # 204s each person -> source read answers before the attach shows (Task 4 run 1)
        self.reference_lag = {"sources": reference_lag, "references": reference_lag}
        self.search_lie = search_lie  # ignore_count / other_first / no_confidence
        self.search_params: list[dict] = []
        self.mode = mode
        self.role = {pid: role for role, pid in IDS.items() if role in GIVEN}

    # ------------------------------------------------------------ shapes

    def _who(self, pid):
        if self.mode == "other_person" and pid in self.role:
            order = ["dad", "mom", "child"]
            return IDS[order[(order.index(self.role[pid]) + 1) % 3]]
        return pid

    def person(self, pid):
        role = self.role[pid]
        name = f"{GIVEN[role]} {SURNAME}"
        return {"id": pid, "living": False,
                "gender": {"type": GX + GENDER[role]},
                "names": [{"type": GX + "BirthName",
                           "nameForms": [{"fullText": name}]}],
                "display": {"name": name, "gender": GENDER[role], "lifespan": "Deceased"},
                "links": {"person": {"href": f"{TREE}/persons/{pid}"}}}

    @staticmethod
    def _ref(pid):
        return {"resource": f"{TREE}/persons/{pid}", "resourceId": pid}

    def couple(self):
        ref = lambda role: self._ref(self._who(IDS[role]))  # noqa: E731
        return {"id": IDS["couple"], "type": GX + "Couple",
                "person1": ref("dad"), "person2": ref("mom")}

    def cap(self):
        ref = lambda role: self._ref(self._who(IDS[role]))  # noqa: E731
        return {"id": IDS["child_and_parents"], "parent1": ref("dad"),
                "parent2": ref("mom"), "child": ref("child")}

    def note(self, key):
        if self.mode == "other_record":  # the next note: person -> couple -> c&p -> person
            order = list(NOTE_IDS)
            key = order[(order.index(key) + 1) % len(order)]
        text = NOTE_TEXT[key]
        return {"id": NOTE_IDS[key], "subject": f"{text} subject", "text": text}

    def description(self, record=SOURCE):
        if self.mode == "other_record":
            record = OTHER_SOURCE if record == SOURCE else OTHER_MEMORY
        sid, title = record
        return {"id": sid, "about": f"https://example.org/{title.lower()}",
                "titles": [{"value": title}], "citations": [{"value": f"{title} citation"}]}

    def cited_by(self, role):
        """``role`` as /tree/source-references and /persons/{pid}/sources answer it."""
        sid = OTHER_SOURCE[0] if self.mode == "other_record" else SOURCE[0]
        return {"id": IDS[role], "sources": [{
            "id": "SRZZ-REF", "description": f"{BETA}/platform/sources/descriptions/{sid}"}]}

    def family(self, *roles, couple=False, cap=False):
        body = {"description": "#sd1",
                "persons": [self.person(IDS[r]) for r in roles],
                "sourceDescriptions": [{"id": "sd1", "about": "#p1"}]}
        if couple:
            body["relationships"] = [self.couple()]
        if cap:
            body["childAndParentsRelationships"] = [self.cap()]
        return body

    def changes(self, pid):
        role = self.role[pid]
        url = f"{TREE}/persons/{pid}?flag=fsh"  # as beta links it (live run)
        entry = {"id": "1729-1", "title": "Name Added", "updated": 1729000000000,
                 "links": {"person": {"href": url}},
                 "content": {"gedcomx": {"persons": [{
                     "names": [{"nameForms": [{"fullText": f"{GIVEN[role]} {SURNAME}"}]}]}]}}}
        created = {"id": "1729-0", "title": "Person Created", "updated": 1728000000000,
                   "links": {"person": {"href": url}}}
        return {"entries": [entry, created], "updated": 1729000000000,
                "links": {"person": {"href": url},
                          "self": {"href": f"{TREE}/persons/{pid}/changes"}}}

    # ------------------------------------------------------------ answers

    def _etag(self, path):
        return '"%d"' % zlib.crc32(path.encode())

    def _answer(self, request, status, body=None, content_type=FS_JSON):
        path = urlsplit(request.url).path
        headers = {"ETag": self._etag(path)}
        if request.method == "HEAD":
            if self.mode == "stale_head":
                headers["ETag"] = '"stale-0"'
            return status, headers, ""
        if body is None:
            return status, headers, ""
        headers["Content-Type"] = content_type
        return status, headers, json.dumps(body)

    def _unknown(self, request):
        if self.mode == "catch_all":
            return self._answer(request, 200, {"persons": []})
        return self._answer(request, 404, {"errors": [{"code": 404}]})

    def _list(self, request, body, content_type=FS_JSON):
        if self.mode == "empty" or body is None:
            return self._answer(request, 204)
        return self._answer(request, 200, body, content_type)

    def persons(self, request):
        query = parse_qs(urlsplit(request.url).query)
        if "pids" not in query:
            return self._answer(request, 400, {"errors": [{"message": "pids is required"}]})
        pids = [p for value in query["pids"] for p in value.split(",") if p]
        if self.mode == "ignore_pids":
            pids = [IDS[r] for r in GIVEN]
        if self.mode == "empty":
            return self._answer(request, 204)
        if any(p not in self.role for p in pids):
            # Beta (live runs): an unknown id, well-formed or not, is reported as nothing.
            return self._unknown(request) if self.mode == "catch_all" else self._answer(
                request, 204)
        return self._answer(request, 200,
                            {"persons": [self.person(self._who(p)) for p in pids]})

    def person_route(self, request):
        parts = urlsplit(request.url).path.split("/platform/tree/persons/", 1)[1].split("/")
        pid, rest = parts[0], parts[1:]
        if pid not in self.role:
            return self._unknown(request)
        role = self.role[self._who(pid)]
        if not rest:
            return self._answer(request, 200, self.family(role, couple=True, cap=True))
        what = rest[0]
        if what == "changes":
            if request.headers.get("Accept") != ATOM_JSON:
                return 406, {}, ""  # beta (live run): /changes serves only the atom types
            return self._list(request, self.changes(self._who(pid)), ATOM_JSON)
        if what == "parents":
            body = self.family("dad", "mom", couple=True, cap=True) if role == "child" else None
            return self._list(request, body)
        if what == "children":
            body = self.family("child", cap=True) if role in ("dad", "mom") else None
            return self._list(request, body)
        if what == "spouses":
            other = {"dad": "mom", "mom": "dad"}.get(role)
            return self._list(request, self.family(other, couple=True) if other else None)
        if what == "families":
            if self.mode == "families_as_parents":  # the child's /parents payload
                return self._list(request, self.family("dad", "mom", couple=True, cap=True))
            return self._list(request, self.family(role, *[r for r in GIVEN if r != role],
                                                   couple=True, cap=True))
        if what == "relationships" and len(rest) == 2:
            other = rest[1]
            if self.mode == "ignore_opid":  # answers the child-dad relationship for any opid
                other = IDS["dad"]
            if other not in self.role:
                if self.mode == "catch_all":
                    return self._answer(request, 200, {"persons": []})
                return self._answer(request, 204)  # live run 3: unknown opid is "none"
            if self.relationship_lag:  # live run 2: 204 for just-created fixtures
                self.relationship_lag -= 1
                return self._answer(request, 204)
            # Live: the child-and-parents id is not in this answer; the persons are.
            return self._answer(request, 200, self.family(role, self.role[self._who(other)]))
        # Recon: only the child has a note, a source and a memory; the dad's lists are 204.
        if what == "notes":
            if role != "child":
                return self._list(request, None) if len(rest) == 1 else self._unknown(request)
            return self._notes(request, "persons", IDS["child"], "person_note", rest[1:])
        if what == "sources" and len(rest) == 1:
            if role == "child" and self._lagging("sources"):
                return self._answer(request, 204)
            body = {"persons": [self.cited_by("child")],
                    "sourceDescriptions": [self.description()]} if role == "child" else None
            return self._list(request, body)
        if what == "memories" and len(rest) == 1:
            memory = {**self.description(MEMORY), "mediaType": "image/png"}
            body = {"persons": [], "sourceDescriptions": [memory],
                    "links": {"self": {"href": request.url}}} if role == "child" else None
            return self._list(request, body)
        return self._answer(request, 404, {"errors": [{"code": 404}]})

    def _lagging(self, what):
        if self.reference_lag[what]:
            self.reference_lag[what] -= 1
            return True
        return False

    def _notes(self, request, envelope, owner, key, rest):
        """An owner's notes (``rest`` empty) or one of them (``rest`` = [nid])."""
        if rest and rest[0] != NOTE_IDS[key] and self.mode != "ignore_nid":
            return self._unknown(request)  # recon: a note id that is not the owner's: 404
        return self._list(request, {envelope: [{"id": owner, "notes": [self.note(key)]}]})

    # ------------------------------------------------------------ relationships

    KINDS = {"couple-relationships": ("couple", "relationships", "couple_note"),
             "child-and-parents-relationships": ("child_and_parents",
                                                 "childAndParentsRelationships",
                                                 "child_and_parents_note")}

    def relationship_route(self, request):
        kind, rest = urlsplit(request.url).path.split("/platform/tree/", 1)[1].split("/", 1)
        rid, *rest = rest.split("/")
        role = self.KINDS[kind][0]
        if rid != IDS[role]:
            return self._unknown(request)
        if self.mode == "swap_relationships":
            kind = next(k for k in self.KINDS if k != kind)
        role, envelope, note = self.KINDS[kind]
        rid = IDS[role]
        record = self.couple() if role == "couple" else self.cap()
        if not rest:
            return self._list(request, {envelope: [record]})
        if rest == ["changes"]:
            if request.headers.get("Accept") != ATOM_JSON:
                return 406, {}, ""  # as the person feeds: atom types only
            url = f"{TREE}/{kind}/{rid}?flag=fsh"
            entry = {"id": "1729-0", "title": "Relationship Created", "updated": 1729000000000,
                     "links": {"relationship": {"href": url}},
                     "content": {"gedcomx": {envelope: [record]}}}
            return self._list(request, {"entries": [entry], "updated": 1729000000000,
                                        "links": {"relationship": {"href": url},
                                                  "self": {"href": f"{url}/changes"}}},
                              ATOM_JSON)
        if rest[0] == "notes":
            return self._notes(request, envelope, rid, note, rest[1:])
        return self._answer(request, 404, {"errors": [{"code": 404}]})

    # ------------------------------------------------------------ pedigrees, sources

    def pedigree(self, request):
        path = urlsplit(request.url).path
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        ancestry = path.endswith("/ancestry")
        if "person" not in query or (not ancestry and "generations" not in query):
            return self._answer(request, 400, {"errors": [{"message": "missing parameter"}]})
        pid = query["person"]
        if pid not in self.role:
            return self._unknown(request)
        if self.mode == "pedigree_swap":
            ancestry = not ancestry
        role = self.role[self._who(pid)]
        if ancestry:
            roles = [role, "dad", "mom"] if role == "child" else [role]
        else:
            roles = ["dad", "mom", "child"] if role in ("dad", "mom") else [role]
        return self._list(request, {"persons": [self.person(IDS[r]) for r in roles],
                                    "links": {"collection": {"href": TREE}}})

    def source_references(self, request):
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        if "description" not in query:  # recon: 404 without a parameter
            return self._answer(request, 404, {"errors": [{"code": 404}]})
        if query["description"] != SOURCE[0]:
            return self._unknown(request)  # documented: unknown description, 404
        if self._lagging("references"):
            return self._answer(request, 204)  # documented: "no results" (yet)
        role = "dad" if self.mode == "other_record" else "child"
        return self._list(request, {"persons": [self.cited_by(role)]})

    def source_descriptions(self, request):
        """/sources/descriptions?sdids= and /sources/descriptions/{sdid}."""
        path = urlsplit(request.url).path
        sdid = path.split("/platform/sources/descriptions", 1)[1].strip("/")
        if sdid:
            if sdid != SOURCE[0]:
                return self._unknown(request)
            return self._list(request, {"sourceDescriptions": [self.description()]})
        query = parse_qs(urlsplit(request.url).query)
        sdids = [s for value in query.get("sdids", []) for s in value.split(",") if s]
        if self.mode == "ignore_sdids":
            return self._list(request, {"sourceDescriptions": self.source_box()})
        if any(s != SOURCE[0] for s in sdids):
            # The spec documents 204 for "no source descriptions matched".
            if self.mode == "catch_all":
                return self._unknown(request)
            return self._answer(request, 204)
        return self._list(request, {"sourceDescriptions": [self.description()]})

    def source_box(self):
        stranger = {"id": "SDZZ-BOX", "titles": [{"value": "an older source"}]}
        return [stranger, self.description()]

    def user_source_descriptions(self, request):
        uid = urlsplit(request.url).path.split("/platform/sources/", 1)[1].split("/")[0]
        if uid != rtr.CURRENT_USER:
            return self._unknown(request)
        return self._list(request, {"sourceDescriptions": self.source_box(),
                                    "links": {"self": {"href": request.url}}})

    def search(self, request):
        """Beta's tree search over real data, as probed: the canary is the best hit for
        its own name and birth year; no parameter at all answers 400."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        self.search_params.append(query)
        if not query:
            if self.mode == "catch_all":
                return self._answer(request, 200, {"entries": []}, ATOM_JSON)
            return self._answer(request, 400, {"errors": [{"message": "upstream 400"}]})
        if self.mode == "empty":
            return self._answer(request, 204)
        count = 20 if self.search_lie == "ignore_count" else int(query.get("count", 20))
        canary = (query.get("q.givenName"), query.get("q.surname")) == (
            rtp.CANARY_NAME.split()[0], rtp.CANARY_NAME.split()[-1])
        hits = [{"id": f"GQZZ-{n:03d}", "confidence": 2, "content": {"gedcomx": {"persons": [{
            "id": f"GQZZ-{n:03d}", "display": {"name": f"Rosine Zopf {n}",
                                                "birthDate": "1871"}}]}}}
                for n in range(count)]
        if canary and self.mode != "other_person" and self.search_lie != "other_first":
            hits[0] = {"id": rtp.CANARY_ID, "confidence": 4, "content": {"gedcomx": {
                "persons": [{"id": rtp.CANARY_ID, "display": {
                    "name": rtp.CANARY_NAME, "birthDate": rtp.CANARY_BIRTH_DATE,
                    "birthPlace": "Switzerland"}}]}}}
        if self.search_lie == "no_confidence":
            for hit in hits:
                hit.pop("confidence")
        return self._answer(request, 200, {"entries": hits, "results": 200}, ATOM_JSON)

    def serve(self):
        persons_list = re.compile(re.escape(TREE + "/persons") + r"(\?.*)?$")
        person = re.compile(re.escape(TREE + "/persons/") + r".+")
        search = re.compile(re.escape(TREE + "/search") + r"(\?.*)?$")
        relationship = re.compile(
            re.escape(TREE) + r"/(couple|child-and-parents)-relationships/.+")
        pedigree = re.compile(re.escape(TREE) + r"/(ancestry|descendancy)(\?.*)?$")
        references = re.compile(re.escape(TREE + "/source-references") + r"(\?.*)?$")
        descriptions = re.compile(re.escape(BETA + "/platform/sources/descriptions")
                                  + r"(/[^?]*)?(\?.*)?$")
        box = re.compile(re.escape(BETA + "/platform/sources/") + r"[^/]+/collections/"
                         r"descriptions(\?.*)?$")
        for method in (responses.GET, responses.HEAD):
            responses.add_callback(method, persons_list, callback=self.persons)
            responses.add_callback(method, person, callback=self.person_route)
            responses.add_callback(method, search, callback=self.search)
            responses.add_callback(method, relationship, callback=self.relationship_route)
            responses.add_callback(method, pedigree, callback=self.pedigree)
            responses.add_callback(method, references, callback=self.source_references)
            responses.add_callback(method, descriptions, callback=self.source_descriptions)
            responses.add_callback(method, box, callback=self.user_source_descriptions)


@pytest.fixture
def fake_world(monkeypatch):
    # The registered recipes use default_world, which builds tools.verify.world.World:
    # replace that, so the very objects in RECIPES run (no test world factory).
    monkeypatch.setattr(world, "World", FakeWorld)
    slept: list[float] = []
    monkeypatch.setattr(reads, "sleep", slept.append)  # eventual polling, without waiting
    return slept


def run(name, reg_path, mode=None, **kw):
    tree = FakeTree(mode, **kw)
    tree.serve()
    recipe = RECIPES[name]
    client = FSClient("tkn", Environment.BETA, min_write_interval=0)
    ctx = runner.make_context(client, recipe, registry_path=reg_path)
    return runner.run_recipe(recipe, ctx, registry_path=reg_path, commit="abc123",
                             today="2026-10-25"), tree


# ------------------------------------------------------------------ the fake passes


@responses.activate
@pytest.mark.parametrize("name", sorted(OWN_RECIPES))
def test_recipe_passes_against_the_honest_fake(name, reg_path, fake_world):
    result, _tree = run(name, reg_path)
    notes = result.pop("notes", [])
    assert result == {"recipe": name, "passed": True, "problems": []}
    # Only a polled case reports its attempts (the honest fake answers at once).
    polled = [c.op for r in rtp.READS_TREE_PERSONS if r.name == name
              for c in (rtp.RELATIVES_CASES if name == "reads.tree.relatives" else ())
              if c.eventual]
    assert notes == [f"{op}: content after 1 of {rtp.RELATIONSHIP_EVENTUAL[0]} attempts"
                     for op in polled]
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        entry = registry[op]["generated"]
        assert entry["status"] == "verified" and entry["evidence"] == "read-content"
        assert entry["recipe"] == name


@responses.activate
def test_search_reads_the_canary_and_its_control_is_an_empty_query(reg_path, fake_world):
    # Fresh fixtures are never indexed (live runs 1-3): the case reads a stable real
    # beta record instead, anchor-free, and asks the server one question it must refuse.
    assert "searchTreePersons" not in rtp.DEFERRED
    case = next(c for c in rtp.SEARCH_CASES if c.op == "searchTreePersons")
    assert case.anchor_free_reason and not case.needs and case.eventual is None
    assert set(case.literal_anchors) == {
        rtp.CANARY_ID, rtp.CANARY_NAME, rtp.CANARY_BIRTH_DATE}
    result, tree = run("reads.tree.search", reg_path)
    assert result["passed"] is True, result["problems"]
    positive, control = tree.search_params
    assert positive == {k: str(v) for k, v in rtp.CANARY_QUERY.items()}
    assert positive["flag"] == "fst" and positive["count"] == str(rtp.CANARY_COUNT)
    assert control == {}  # no parameter at all: beta answers 400


def test_current_tree_person_stays_deferred_for_want_of_a_control():
    # A discovered anchor (readCurrentUser's personId) would be sound, but the operation
    # takes no parameter, so no request to it can be a not-found control.
    reason = rtp.DEFERRED["readCurrentTreePerson"]
    assert "control" in reason and "no path or query parameter" in reason


@responses.activate
@pytest.mark.parametrize("lie", ["ignore_count", "other_first", "no_confidence"])
def test_search_fails_when_the_canary_answer_is_wrong(lie, reg_path, fake_world):
    result, _tree = run("reads.tree.search", reg_path, search_lie=lie)
    assert result["passed"] is False, lie


@responses.activate
def test_find_relationship_control_is_an_empty_answer_for_an_unknown_opid(
        reg_path, fake_world):
    case = next(c for c in rtp.RELATIVES_CASES if c.op == "findRelationship")
    assert case.control_may_be_empty and "204" in case.control_may_be_empty
    assert case.eventual == rtp.RELATIONSHIP_EVENTUAL
    result, _tree = run("reads.tree.relatives", reg_path)
    assert result["passed"] is True, result["problems"]
    controls = [c for c in responses.calls if c.request.url.endswith(
        f"/relationships/{rtp.NO_SUCH_PERSON}")]
    assert len(controls) == 1 and controls[0].response.status_code == 204


@responses.activate
def test_read_persons_control_is_an_empty_answer_for_an_unknown_pid(reg_path, fake_world):
    # Live (runs 1 and 2), beta answers any unknown pid with an empty 204, which the
    # spec omits; the case says so, and its positive call is anchored content first.
    case = next(c for c in rtp.PERSONS_CASES if c.op == "readPersons")
    assert case.control_may_be_empty and "204" in case.control_may_be_empty
    result, _tree = run("reads.tree.persons", reg_path)
    assert result["passed"] is True, result["problems"]
    controls = [c.request.url for c in responses.calls
                if c.request.url.startswith(TREE + "/persons?")][1:]
    assert len(controls) == 1 and rtp.NO_SUCH_PERSON in controls[0]
    assert PID_FORMAT.match(rtp.NO_SUCH_PERSON)  # well-formed, just absent


@responses.activate
def test_find_relationship_waits_for_the_calculation_but_not_forever(reg_path, fake_world):
    attempts, pause = rtp.RELATIONSHIP_EVENTUAL
    result, _tree = run("reads.tree.relatives", reg_path, relationship_lag=attempts - 1)
    assert result["passed"] is True, result["problems"]
    assert fake_world == [pause] * (attempts - 1)
    result, _tree = run("reads.tree.relatives", reg_path, relationship_lag=attempts)
    problems = "\n".join(result["problems"])
    assert result["passed"] is False and "findRelationship: positive call answered 204" in problems


@responses.activate
def test_failures_carry_the_response_skeleton(reg_path, fake_world):
    result, _tree = run("reads.tree.relatives", reg_path, "other_person")
    problems = "\n".join(result["problems"])
    assert "response: 200" in problems and GIVEN["dad"] in problems  # the names shown


# ------------------------------------------------------------------ the fake lies

LIES = {
    "other_person": OWN_RECIPES,
    "catch_all": OWN_RECIPES,
    "ignore_pids": {"reads.tree.persons"},
    "empty": {"reads.tree.persons", "reads.tree.relatives", "reads.tree.search",
              "reads.tree.history"},
    "stale_head": {"reads.tree.persons", "reads.tree.history"},
    "families_as_parents": {"reads.tree.relatives"},
    "ignore_opid": {"reads.tree.relatives"},
}
# What each lie is caught by (so a recipe cannot fail for an unrelated reason).
CAUGHT_BY = {
    "other_person": "anchor",
    "catch_all": "not-found control answered",
    "ignore_pids": "not the requested",
    "empty": "answered 204",
    "stale_head": "validator",
    "families_as_parents": "readPersonFamilies: anchor",
    "ignore_opid": "findRelationship: not-found control answered 200",
}


@responses.activate
@pytest.mark.parametrize("mode, name", sorted(
    (mode, name) for mode, names in LIES.items() for name in names))
def test_recipe_fails_when_the_fake_lies(mode, name, reg_path, fake_world):
    raw = reg_path.read_bytes()
    result, _tree = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    assert CAUGHT_BY[mode] in "\n".join(result["problems"]), result["problems"]
    assert reg_path.read_bytes() == raw  # nothing recorded


@responses.activate
def test_204_for_the_childless_variant_only_counts_after_the_populated_one(
        reg_path, fake_world):
    # The empty variants (child's children and spouses, dad's parents) pass with the
    # honest fake; with every list empty, the populated cases fail and so do they.
    result, _tree = run("reads.tree.relatives", reg_path, "empty")
    problems = "\n".join(result["problems"])
    assert "populated" in problems and "readPersonChildren" in problems


# ------------------------------------------------------------------ coverage


def _scope() -> set[str]:
    """This task's reads, from the snapshot: person-centred Family Tree reads."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    ops, _excluded = resolve(snapshot, load_overrides())
    scope = set()
    for op in ops:
        if op.module != "family_tree" or op.kind != "read":
            continue
        if not op.path.startswith(SCOPE_PREFIXES):
            continue
        segments = set(op.path.split("/"))
        if segments & TASK4_SEGMENTS:
            continue
        scope.add(op.operation_id)
    return scope - {"readPerson"}  # verified by the pilot


def test_recipes_cover_exactly_the_person_reads_minus_deferred():
    scope = _scope()
    covered = {op for r in rtp.READS_TREE_PERSONS for op in r.ops}
    assert "readPerson" not in covered and "readPerson" not in rtp.DEFERRED
    assert not covered & set(rtp.DEFERRED), covered & set(rtp.DEFERRED)
    assert covered | set(rtp.DEFERRED) == scope, (
        f"uncovered: {sorted(scope - covered - set(rtp.DEFERRED))}; "
        f"outside scope: {sorted((covered | set(rtp.DEFERRED)) - scope)}")
    for op, reason in rtp.DEFERRED.items():
        assert isinstance(reason, str) and len(reason.split()) >= 8, op
    # findRelationship lives under /tree/persons and is person-centred: this task's.
    assert "findRelationship" in covered


def test_head_cases_compare_with_their_get():
    heads = [op for op in _scope() if OPERATIONS[op].http_method == "HEAD"]
    covered = {op for r in rtp.READS_TREE_PERSONS for op in r.ops}
    assert set(heads) <= covered | set(rtp.DEFERRED)


def test_recipes_registered_and_reference_real_read_ops():
    assert validate_recipes(RECIPES) == []
    for recipe in rtp.READS_TREE_PERSONS:
        assert RECIPES[recipe.name] is recipe
        assert len(recipe.ops) <= 12
    seen: dict[str, str] = {}
    for recipe in RECIPES.values():
        if recipe.evidence != "read-content":
            continue
        for op in recipe.ops:
            assert op in OPERATIONS and OPERATIONS[op].kind == "read", op
            assert op not in seen, f"{op} in both {seen[op]} and {recipe.name}"
            seen[op] = recipe.name
    assert len({r.name for r in RECIPES.values()}) == len(RECIPES)


# ------------------------------------------------------------------ Task 4: relations

RELATION_RECIPES = {r.name for r in rtr.READS_TREE_RELATIONS}
RELATION_CASES = {
    "reads.tree.relationships": rtr.RELATIONSHIPS_CASES,
    "reads.tree.relationship_history": rtr.RELATIONSHIP_HISTORY_CASES,
    "reads.tree.pedigree": rtr.PEDIGREE_CASES,
    "reads.tree.notes": rtr.NOTES_CASES,
    "reads.tree.couple_notes": rtr.COUPLE_NOTES_CASES,
    "reads.tree.child_and_parents_notes": rtr.CHILD_AND_PARENTS_NOTES_CASES,
    "reads.tree.sources": rtr.SOURCES_CASES,
    "reads.tree.source_queries": rtr.SOURCE_QUERIES_CASES,
    "reads.tree.memories": rtr.MEMORIES_CASES,
}
# Only the groups each recipe reads: the couple and child-and-parents note envelopes are
# unproven on beta, so each has a recipe of its own, apart from the person note and source.
RELATION_NEEDS = {
    "reads.tree.relationships": {"persons", "couple"},
    "reads.tree.relationship_history": {"persons", "couple"},
    "reads.tree.pedigree": {"persons", "couple"},
    "reads.tree.notes": {"person_note"},
    "reads.tree.couple_notes": {"couple_note"},
    "reads.tree.child_and_parents_notes": {"child_and_parents_note"},
    "reads.tree.sources": {"source"},
    "reads.tree.source_queries": {"source"},
    "reads.tree.memories": {"memory"},
}


def test_relation_recipes_declare_only_the_groups_they_read():
    assert set(RELATION_CASES) == RELATION_RECIPES
    for name, cases in RELATION_CASES.items():
        assert {g for c in cases for g in c.needs} == RELATION_NEEDS[name], name
        assert set(RECIPES[name].ops) == {c.op for c in cases}
        assert all(g in world.GROUPS for g in RELATION_NEEDS[name])


@responses.activate
@pytest.mark.parametrize("name", sorted(RELATION_RECIPES))
def test_relation_recipe_passes_against_the_honest_fake(name, reg_path, fake_world):
    result, _tree = run(name, reg_path)
    # Only the polled cases (the person -> source reads) report attempts.
    polled = [c.op for c in RELATION_CASES[name] if c.eventual]
    assert result.pop("notes", []) == [
        f"{op}: content after 1 of {rtr.REFERENCE_EVENTUAL[0]} attempts" for op in polled]
    assert result == {"recipe": name, "passed": True, "problems": []}
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        entry = registry[op]["generated"]
        assert entry["status"] == "verified" and entry["evidence"] == "read-content"
        assert entry["recipe"] == name


@responses.activate
def test_relationship_feed_heads_are_sent_with_the_atom_accept(reg_path, fake_world):
    # HEAD documents no Accept; beta answers 406 to the fs-v1 json the feeds do not serve.
    result, _tree = run("reads.tree.relationship_history", reg_path)
    assert result["passed"] is True, result["problems"]
    heads = [c.request for c in responses.calls if c.request.method == "HEAD"]
    assert len(heads) == 4  # each HEAD case: positive and control
    assert all(h.headers["Accept"] == ATOM_JSON for h in heads)


@responses.activate
def test_relationship_heads_are_sent_with_their_gets_accept(reg_path, fake_world):
    result, _tree = run("reads.tree.relationships", reg_path)
    assert result["passed"] is True, result["problems"]
    heads = [c.request for c in responses.calls if c.request.method == "HEAD"]
    assert heads and all(h.headers["Accept"] == FS_JSON for h in heads)


@responses.activate
def test_single_note_controls_send_an_unknown_note_of_the_real_owner(reg_path, fake_world):
    result, _tree = run("reads.tree.notes", reg_path)
    assert result["passed"] is True, result["problems"]
    control = [c.request.url for c in responses.calls if rtr.NO_SUCH_NOTE in c.request.url]
    assert control == [f"{TREE}/persons/{IDS['child']}/notes/{rtr.NO_SUCH_NOTE}"]


@responses.activate
def test_source_queries_send_the_fixture_ids_and_anchor_on_what_comes_back(
        reg_path, fake_world):
    result, _tree = run("reads.tree.source_queries", reg_path)
    assert result["passed"] is True, result["problems"]
    urls = [c.request.url for c in responses.calls]
    assert f"{TREE}/source-references?description={SOURCE[0]}" in urls
    assert f"{BETA}/platform/sources/descriptions?sdids={SOURCE[0]}" in urls
    # The signed-in user's box by the spec's literal, not a world value.
    assert rtr.CURRENT_USER not in FakeWorld(None, None, "", "").ids.values()
    assert f"{BETA}/platform/sources/current/collections/descriptions" in urls


def test_only_the_person_to_source_reads_poll():
    polled = {c.op for cases in RELATION_CASES.values() for c in cases if c.eventual}
    assert polled == {"readPersonSources", "readSourceReferences"}
    empty_controls = {c.op for cases in RELATION_CASES.values() for c in cases
                      if c.control_may_be_empty}
    assert empty_controls == {"readSourceDescriptions"}
    case = next(c for c in rtr.SOURCE_QUERIES_CASES if c.op == "readSourceDescriptions")
    assert "204" in case.control_may_be_empty and case.anchors


@responses.activate
@pytest.mark.parametrize("name", ["reads.tree.sources", "reads.tree.source_queries"])
def test_source_reference_reads_wait_for_the_attach_but_not_forever(
        name, reg_path, fake_world):
    attempts, pause = rtr.REFERENCE_EVENTUAL
    result, _tree = run(name, reg_path, reference_lag=attempts - 1)
    assert result["passed"] is True, result["problems"]
    assert fake_world == [pause] * (attempts - 1)
    result, _tree = run(name, reg_path, reference_lag=attempts)
    problems = "\n".join(result["problems"])
    assert result["passed"] is False and "positive call answered 204" in problems


@responses.activate
def test_unmatched_sdids_control_is_the_documented_empty_answer(reg_path, fake_world):
    result, _tree = run("reads.tree.source_queries", reg_path)
    assert result["passed"] is True, result["problems"]
    [control] = [c for c in responses.calls
                 if c.request.url.endswith(f"/sources/descriptions?sdids={rtr.NO_SUCH_SOURCE}")]
    assert control.response.status_code == 204 and not control.response.text


RELATION_LIES = {
    "other_person": {"reads.tree.relationships", "reads.tree.relationship_history",
                     "reads.tree.pedigree", "reads.tree.notes", "reads.tree.sources",
                     "reads.tree.memories"},
    "other_record": {"reads.tree.notes", "reads.tree.couple_notes",
                     "reads.tree.child_and_parents_notes", "reads.tree.sources",
                     "reads.tree.source_queries", "reads.tree.memories"},
    "swap_relationships": {"reads.tree.relationships", "reads.tree.relationship_history",
                           "reads.tree.couple_notes", "reads.tree.child_and_parents_notes"},
    "catch_all": RELATION_RECIPES,
    "empty": RELATION_RECIPES,
    "stale_head": {"reads.tree.relationships", "reads.tree.relationship_history",
                   "reads.tree.sources"},
    "pedigree_swap": {"reads.tree.pedigree"},
    "ignore_nid": {"reads.tree.notes", "reads.tree.couple_notes",
                   "reads.tree.child_and_parents_notes"},
    "ignore_sdids": {"reads.tree.source_queries"},
}
# What each lie is caught by: any of these (a person swapped out of a list read leaves
# the dad's empty 204, of a relationship read a missing anchor).
RELATION_CAUGHT_BY = {
    "other_person": ("anchor", "positive call answered 204"),
    "other_record": ("anchor",),
    "swap_relationships": ("anchor", "no relationships in the response", "is not the"),
    "catch_all": ("not-found control answered 200",),
    "empty": ("positive call answered 204",),
    "stale_head": ("validator",),
    "pedigree_swap": ("anchor",),
    "ignore_nid": ("not-found control answered 200",),
    "ignore_sdids": ("not the requested",),
}


@responses.activate
@pytest.mark.parametrize("mode, name", sorted(
    (mode, name) for mode, names in RELATION_LIES.items() for name in names))
def test_relation_recipe_fails_when_the_fake_lies(mode, name, reg_path, fake_world):
    raw = reg_path.read_bytes()
    result, _tree = run(name, reg_path, mode)
    assert result["passed"] is False, f"{name} passed against a fake in mode {mode}"
    problems = "\n".join(result["problems"])
    assert any(s in problems for s in RELATION_CAUGHT_BY[mode]), result["problems"]
    assert reg_path.read_bytes() == raw  # nothing recorded


@responses.activate
def test_every_head_case_fails_on_a_stale_validator(reg_path, fake_world):
    # Each HEAD (not only one per recipe) is judged against its own GET's validator.
    heads = {c.op for cases in RELATION_CASES.values() for c in cases
             if OPERATIONS[c.op].http_method == "HEAD"}
    failed = set()
    for name in ("reads.tree.relationships", "reads.tree.relationship_history",
                 "reads.tree.sources"):
        result, _tree = run(name, reg_path, "stale_head")
        problems = "\n".join(result["problems"])
        failed |= {op for op in heads if f"{op}: HEAD and" in problems}
    assert failed == heads


@responses.activate
def test_empty_variants_only_count_after_their_populated_case(reg_path, fake_world):
    # The dad's notes, sources and memories are 204 (recon); with every list empty the
    # populated cases fail, and so do the empty variants that lean on them.
    for name, op in (("reads.tree.notes", "readPersonNotes"),
                     ("reads.tree.sources", "readPersonSources"),
                     ("reads.tree.memories", "getPersonMemories")):
        result, _tree = run(name, reg_path, "empty")
        problems = "\n".join(result["problems"])
        assert f"{op}: 204 (empty) accepted only after a populated" in problems, problems


def _family_tree_reads() -> set[str]:
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    ops, _excluded = resolve(snapshot, load_overrides())
    return {o.operation_id for o in ops if o.module == "family_tree" and o.kind == "read"}


def test_relation_recipes_cover_exactly_the_rest_of_the_family_tree_reads():
    every = _family_tree_reads()
    scope = every - _scope() - {"readPerson"}
    covered = {op for r in rtr.READS_TREE_RELATIONS for op in r.ops}
    deferred = set(rtr.DEFERRED)
    assert not covered & deferred, covered & deferred
    assert covered | deferred == scope, (
        f"uncovered: {sorted(scope - covered - deferred)}; "
        f"outside scope: {sorted((covered | deferred) - scope)}")
    # No overlap with Task 3, and together (with the pilot's readPerson) every read.
    task3 = {op for r in rtp.READS_TREE_PERSONS for op in r.ops} | set(rtp.DEFERRED)
    assert not (covered | deferred) & task3
    assert "readPerson" not in covered | deferred | task3
    assert covered | deferred | task3 | {"readPerson"} == every
    for op, reason in rtr.DEFERRED.items():
        assert isinstance(reason, str) and len(reason.split()) >= 12, op
        assert OPERATIONS[op].kind == "read", op


def test_relation_heads_compare_with_their_get():
    for cases in RELATION_CASES.values():
        for case in cases:
            if OPERATIONS[case.op].http_method == "HEAD":
                assert OPERATIONS[case.compare_with].path == OPERATIONS[case.op].path


def test_relation_recipes_registered():
    assert validate_recipes(RECIPES) == []
    for recipe in rtr.READS_TREE_RELATIONS:
        assert RECIPES[recipe.name] is recipe
        assert len(recipe.ops) <= 12
