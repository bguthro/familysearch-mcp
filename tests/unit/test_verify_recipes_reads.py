"""Offline tests for the read recipes against a scripted fake tree (no network, no cookies).

``FakeTree`` keeps the fixture family (dad, mom, child, their couple and child-and-parents
relationships) in memory and answers the person-centred reads in the GEDCOM X shapes the
recon recorded (docs/superpowers/recon/2026-10-25-api-surface/shapes.json): relative
lists answer 204 when empty, ``readPersons`` needs ``pids``, search answers 400 without
``flag=fst``, change histories are atom feeds whose links carry the person id. The
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
STRANGER = {"id": "KWZZ-STR", "name": "Somebody Else"}
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
        self.ids = dict(IDS)
        self.names = {f"{role}_name": f"{given} {SURNAME}" for role, given in GIVEN.items()}
        self.needed: list[str] = []

    def need(self, *groups):
        self.needed.extend(groups)


class FakeTree:
    """The fixture family on a fake beta.

    ``mode`` is None (honest) or one lie: ``other_person`` answers every person read with
    the next person's data (dad -> mom -> child -> dad) and search with strangers;
    ``ignore_pids`` answers readPersons with everyone; ``catch_all`` answers 200 for
    unknown ids (and search without ``flag=fst``); ``empty`` answers 204 for every
    populated list; ``stale_head`` answers HEAD with another revision than the GET.
    """

    def __init__(self, mode=None, *, search_lag=0):
        self.mode = mode
        self.role = {pid: role for role, pid in IDS.items() if role in GIVEN}
        self.search_params: list[dict] = []
        self.search_lag = search_lag  # positive searches answered 204 before the index has them

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
        return {"id": IDS["couple"], "type": GX + "Couple",
                "person1": self._ref(IDS["dad"]), "person2": self._ref(IDS["mom"])}

    def cap(self):
        return {"id": IDS["child_and_parents"], "parent1": self._ref(IDS["dad"]),
                "parent2": self._ref(IDS["mom"]), "child": self._ref(IDS["child"])}

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
        if self.mode != "catch_all" and not all(PID_FORMAT.match(p) for p in pids):
            return self._answer(request, 400, {"errors": [{"message": "invalid person id"}]})
        if any(p not in self.role for p in pids):
            # Beta (live run): an unknown well-formed id is reported as nothing, 204.
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
            return self._list(request, self.family(role, *[r for r in GIVEN if r != role],
                                                   couple=True, cap=True))
        if what == "relationships" and len(rest) == 2:
            other = rest[1]
            if other not in self.role:
                if self.mode == "catch_all":
                    return self._answer(request, 200, {"persons": []})
                return self._answer(request, 400, {"errors": [{"code": 400}]})
            # Live: the child-and-parents id is not in this answer; the persons are.
            return self._answer(request, 200, self.family(role, self.role[self._who(other)]))
        return self._answer(request, 404, {"errors": [{"code": 404}]})

    def search(self, request):
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        self.search_params.append(query)
        if query.get("flag") != "fst":
            if self.mode == "catch_all":
                return self._answer(request, 204)
            return self._answer(request, 400, {"errors": [{"message": "upstream 400"}]})
        if self.search_lag:
            self.search_lag -= 1
            return self._answer(request, 204)
        if self.mode == "other_person":
            hits = [{"id": STRANGER["id"], "content": {"gedcomx": {"persons": [{
                "id": STRANGER["id"], "display": {"name": STRANGER["name"]}}]}}}]
        else:
            hits = [{"id": IDS[r], "score": 1.0,
                     "content": {"gedcomx": {"persons": [self.person(IDS[r])]}}}
                    for r in GIVEN if query.get("q.surname") == SURNAME]
        return self._list(request, {"entries": hits, "results": len(hits)} if hits else None,
                          ATOM_JSON)

    def serve(self):
        persons_list = re.compile(re.escape(TREE + "/persons") + r"(\?.*)?$")
        person = re.compile(re.escape(TREE + "/persons/") + r".+")
        search = re.compile(re.escape(TREE + "/search") + r"(\?.*)?$")
        for method in (responses.GET, responses.HEAD):
            responses.add_callback(method, persons_list, callback=self.persons)
            responses.add_callback(method, person, callback=self.person_route)
            responses.add_callback(method, search, callback=self.search)


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
    assert result == {"recipe": name, "passed": True, "problems": []}
    registry = json.loads(reg_path.read_text())["operations"]
    for op in RECIPES[name].ops:
        entry = registry[op]["generated"]
        assert entry["status"] == "verified" and entry["evidence"] == "read-content"
        assert entry["recipe"] == name


@responses.activate
def test_search_sends_the_proven_shape(reg_path, fake_world):
    _result, tree = run("reads.tree.search", reg_path)
    positive, control = tree.search_params
    assert positive["flag"] == "fst" and positive["q.surname"] == SURNAME
    # The control sends the parameter with a dead value and no flag: a server answer.
    assert "flag" not in control and control["q.surname"] != SURNAME


@responses.activate
def test_search_waits_for_the_index_but_not_forever(reg_path, fake_world):
    attempts, pause = rtp.SEARCH_EVENTUAL
    result, tree = run("reads.tree.search", reg_path, search_lag=attempts - 1)
    assert result["passed"] is True, result["problems"]
    assert fake_world == [pause] * (attempts - 1)
    assert len(tree.search_params) == attempts + 1  # the control is asked once
    fake_world.clear()
    result, tree = run("reads.tree.search", reg_path, search_lag=attempts)
    assert result["passed"] is False and "answered 204" in "\n".join(result["problems"])


@responses.activate
def test_read_persons_control_is_a_malformed_id(reg_path, fake_world):
    # Live, an unknown well-formed pid answers 204, which readPersons does not document
    # (so control_may_be_empty may not vouch for it); a malformed id must be refused.
    _result, _tree = run("reads.tree.persons", reg_path)
    controls = [c.request.url for c in responses.calls
                if c.request.url.startswith(TREE + "/persons?")][1:]
    assert len(controls) == 1 and rtp.MALFORMED_PERSON_ID in controls[0].replace("%2C", ",")
    assert not PID_FORMAT.match(rtp.MALFORMED_PERSON_ID)


@responses.activate
def test_failures_carry_the_response_skeleton(reg_path, fake_world):
    result, _tree = run("reads.tree.relatives", reg_path, "other_person")
    assert "response: 200" in "\n".join(result["problems"])
    # A search hit's person (and its name) is deep enough to be shown.
    result, _tree = run("reads.tree.search", reg_path, "other_person")
    assert STRANGER["name"] in "\n".join(result["problems"])


# ------------------------------------------------------------------ the fake lies

LIES = {
    "other_person": OWN_RECIPES,
    "catch_all": OWN_RECIPES,
    "ignore_pids": {"reads.tree.persons"},
    "empty": {"reads.tree.relatives", "reads.tree.search", "reads.tree.history"},
    "stale_head": {"reads.tree.persons", "reads.tree.history"},
}
# What each lie is caught by (so a recipe cannot fail for an unrelated reason).
CAUGHT_BY = {
    "other_person": "anchor",
    "catch_all": "not-found control answered",
    "ignore_pids": "not the requested",
    "empty": "answered 204",
    "stale_head": "validator",
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
