"""A stateful fake of beta's Genealogies (private tree) API, for the P7 recipe tests.

Shapes are the recipes' guesses (nothing is proven live yet): a tree is a GEDCOM X
``collections`` entry (``id``, ``title``); persons, sources and relationships follow the
Family Tree shapes (``persons`` with ``living``, names, facts, gender; ``sourceDescriptions``).
What is modelled from the recon: every id must carry the prefix (here ``GZ``): any other
id answers 400 ``Invalid prefix for genealogies: (id)``, and a tree id shorter than four
characters answers 500 ``Range [0, 4) out of bounds for length 1``; a well-formed unknown
id answers 404; lists answer 204 when empty; a deleted person answers 410.

``mode`` is None (honest) or one lie (``LIES``), each caught by the recipe named beside
it. ``sd_trees`` is not a lie: the fake accepts a tree create only in the
``sourceDescriptions`` shape, to exercise the recipes' fallback. ``calls`` lists
``(method, path)``; ``live()`` the objects created and not deleted.
"""
import json
import re
from urllib.parse import urlsplit

import responses

from familysearch.auth import Environment

BETA = Environment.BETA.base_url
G = BETA + "/platform/genealogies"
FS_JSON = "application/x-fs-v1+json"
GX = "http://gedcomx.org/"
PREFIX = "GZ"

LIES = (
    # reads
    "read_other",             # every single read answers another record of its kind
    "catch_all",              # every single read answers 200 for any id (even unknown)
    # trees
    "ignore_tree_create",     # tree create answers 201 but stores nothing
    "wrong_tree_title",       # the stored title is not the posted one
    "tree_update_noop",       # tree update answers 200 but adds nothing
    "tree_update_other",      # tree update adds the person to the other tree
    "tree_delete_noop",       # tree delete answers 204, deletes nothing
    "tree_delete_collateral", # tree delete also deletes every other tree
    # persons
    "drop_living",            # the person is stored not-living
    "ignore_person_create",   # person create answers 201, stores nothing
    "ignore_person_update",
    "person_update_creates",  # an update by id adds a new name instead
    "person_update_collateral",  # an update drops the first fact
    "person_update_unlives",  # an update flips living to false
    "person_delete_noop",
    "person_restore_noop",
    "ignore_conclusion_delete",
    "conclusion_collateral",  # a conclusion delete removes the other fact too
    # sources
    "ignore_source_create", "ignore_source_update", "source_update_creates",
    "source_update_collateral", "source_delete_noop",
    # relationships
    "ignore_relationship_update", "relationship_update_collateral",  # an update adds a stray extra fact
    "relationship_delete_noop", "relationship_delete_persons",
)


class FakeGenealogies:
    def __init__(self, mode=None, *, sd_trees=False, entity_id=False):
        if mode is not None and mode not in LIES:
            raise ValueError(f"unknown lie {mode!r}")
        self.mode = mode
        self.sd_trees = sd_trees
        self.entity_id = entity_id  # creates answer X-ENTITY-ID as well as Location
        self.trees: dict[str, dict] = {}
        self.persons: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.relationships: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, object]] = []  # (method, path, posted json)
        self._seq = {"tree": 0, "person": 0, "source": 0, "rel": 0, "concl": 0}

    # ------------------------------------------------------------ ids and helpers

    def _id(self, kind):
        self._seq[kind] += 1
        n = self._seq[kind]
        tag = {"tree": "TR", "person": "PE", "source": "SD", "rel": "RL", "concl": "CC"}[kind]
        return f"{PREFIX}{tag}-{n:03d}"

    def _record(self, request):
        path = urlsplit(request.url).path
        self.calls.append((request.method, path))
        body = None
        if request.body:
            try:
                body = json.loads(request.body)
            except ValueError:
                body = None
        self.bodies.append((request.method, path, body))
        return body

    @staticmethod
    def _json(status, body, headers=None):
        return status, {"Content-Type": FS_JSON, **(headers or {})}, json.dumps(body)

    def _created(self, kind_path, oid):
        headers = {"Location": f"{G}/{kind_path}/{oid}"}
        if self.entity_id:
            headers["X-ENTITY-ID"] = oid
        return 201, headers, ""

    def _bad_id(self, oid, *, tree=False):
        """The recon's errors for an id that is not a genealogies id, else None."""
        if tree and len(oid) < 4:
            return 500, {}, "Range [0, 4) out of bounds for length 1"
        if not oid.startswith(PREFIX):
            return 400, {}, f"Invalid prefix for genealogies: ({oid})"
        return None

    def live(self):
        return ([t for t, r in self.trees.items() if not r["deleted"]]
                + [p for p, r in self.persons.items() if not r["deleted"]]
                + [s for s, r in self.sources.items() if not r["deleted"]])

    # ------------------------------------------------------------ trees

    def tree_json(self, tid):
        return {"id": tid, "title": self.trees[tid]["title"]}

    def _live_trees(self):
        return {t: r for t, r in self.trees.items() if not r["deleted"]}

    def trees_root(self, request):
        body = self._record(request)
        if request.method == "GET":
            live = self._live_trees()
            if not live:
                return 204, {}, ""
            return self._json(200, {"collections": [self.tree_json(t) for t in live]})
        if self.sd_trees:
            if not (isinstance(body, dict) and body.get("sourceDescriptions")):
                return 400, {}, "A source description is required"
            title = body["sourceDescriptions"][0]["titles"][0]["value"]
        else:
            if not (isinstance(body, dict) and body.get("collections")):
                return 400, {}, "A collection is required"
            title = body["collections"][0]["title"]
        assert title.startswith("ZZSPIKE")
        tid = self._id("tree")
        if self.mode != "ignore_tree_create":
            self.trees[tid] = {"title": "ZZSPIKE other" if self.mode == "wrong_tree_title"
                               else title, "deleted": False}
        return self._created("trees", tid)

    def tree_route(self, request):
        body = self._record(request)
        path = urlsplit(request.url).path
        tid = path.rsplit("/", 1)[-1]
        if self.mode == "catch_all" and request.method == "GET":
            first = next(iter(self.trees))
            return self._json(200, {"collections": [self.tree_json(first)]})
        bad = self._bad_id(tid, tree=True)
        if bad:
            return bad
        record = self.trees.get(tid)
        if record is None:
            return 404, {}, ""
        if record["deleted"]:
            return 410, {}, ""
        if request.method == "GET":
            if self.mode == "read_other":
                return self._json(200, {"collections": [{"id": tid,
                                                          "title": "ZZSPIKE other"}]})
            return self._json(200, {"collections": [self.tree_json(tid)]})
        if request.method == "DELETE":
            if self.mode == "tree_delete_noop":
                return 204, {}, ""
            record["deleted"] = True
            if self.mode == "tree_delete_collateral":
                for r in self.trees.values():
                    r["deleted"] = True
            return 204, {}, ""
        return self.tree_update(tid, body)

    def tree_update(self, tid, body):
        """POST: add persons (local ids replaced by server ids) and relationships."""
        if self.mode == "tree_update_noop":
            return self._json(200, {"persons": []})
        target = tid
        if self.mode == "tree_update_other":
            target = next((t for t in self.trees if t != tid), tid)
        local = {}
        out = []
        for person in body.get("persons") or []:
            pid = self._id("person")
            local[person.get("id")] = pid
            self.persons[pid] = self._new_person(target, person)
            out.append({"id": pid})
        for rel in body.get("relationships") or []:
            p1 = local.get(str(rel["person1"]["resource"]).lstrip("#"))
            p2 = local.get(str(rel["person2"]["resource"]).lstrip("#"))
            assert p1 and p2
            male = self.persons[p1]["gender"]["type"] == GX + "Male"
            female = self.persons[p2]["gender"]["type"] == GX + "Female"
            if not (male and female):
                return 400, {}, "person1 must be male and person2 female"
            self.relationships[self._id("rel")] = {
                "persons": [p1, p2], "facts": [], "deleted": False, "type": rel["type"]}
        return self._json(200, {"persons": out})

    # ------------------------------------------------------------ persons

    def _new_person(self, tid, posted):
        living = posted.get("living")
        if self.mode == "drop_living":
            living = False
        return {
            "tid": tid, "living": living, "deleted": False,
            "names": [self._named(n) for n in posted.get("names") or []],
            "facts": [self._named(f) for f in posted.get("facts") or []],
            "gender": self._named(posted["gender"]) if posted.get("gender") else None,
        }

    def _named(self, conclusion):
        return {**{k: v for k, v in conclusion.items() if k != "id"},
                "id": conclusion.get("id") or self._id("concl")}

    def person_json(self, pid):
        p = self.persons[pid]
        body = {"id": pid, "living": p["living"], "names": p["names"], "facts": p["facts"],
                "display": {"name": (p["names"][0]["nameForms"][0]["fullText"]
                                     if p["names"] else "")}}
        if p["gender"]:
            body["gender"] = p["gender"]
        rels = [self.relationship_json(c) for c, r in self.relationships.items()
                if pid in r["persons"] and not r["deleted"]]
        if rels:
            body["relationships"] = rels
        return body

    def relationship_json(self, cid):
        r = self.relationships[cid]
        ref = lambda pid: {"resource": f"{G}/persons/{pid}", "resourceId": pid}  # noqa: E731
        return {"id": cid, "type": r["type"], "person1": ref(r["persons"][0]),
                "person2": ref(r["persons"][1]), "facts": r["facts"]}

    def persons_in_tree(self, request, tid):
        body = self._record(request)
        bad = self._bad_id(tid, tree=True)
        if bad:
            return bad
        record = self.trees.get(tid)
        if record is None or record["deleted"]:
            return (404 if record is None else 410), {}, ""
        if request.method == "GET":
            live = [p for p, r in self.persons.items() if r["tid"] == tid and not r["deleted"]]
            if not live:
                return 204, {}, ""
            return self._json(200, {"persons": [self.person_json(p) for p in live]})
        posted = body["persons"][0]
        assert posted.get("living") is not None and posted["names"][0]["nameForms"][0][
            "fullText"].startswith("ZZSPIKE")
        pid = self._id("person")
        if self.mode != "ignore_person_create":
            self.persons[pid] = self._new_person(tid, posted)
        return self._created("persons", pid)

    def person_route(self, request):
        body = self._record(request)
        path = urlsplit(request.url).path
        rest = path.split("/genealogies/persons/", 1)[1].split("/")
        pid = rest[0]
        bad = self._bad_id(pid)
        if bad:
            return bad
        person = self.persons.get(pid)
        if person is None:
            return 404, {}, ""
        if len(rest) == 2 and rest[1] == "restore":
            if not person["deleted"]:
                return 404, {}, ""
            if self.mode != "person_restore_noop":
                person["deleted"] = False
            return 204, {}, ""
        if person["deleted"]:
            return 410, {}, ""
        if len(rest) == 1:
            return self._person(request.method, pid, person, body)
        if rest[1] == "conclusions":
            return self._delete_conclusion(pid, person, rest[2])
        if rest[1] == "relationships":
            return self._relationship(request.method, pid, rest[2], body)
        return 404, {}, ""

    def _person(self, method, pid, person, body):
        if method == "GET":
            return self._json(200, {"persons": [self.person_json(pid)]})
        if method == "DELETE":
            if self.mode != "person_delete_noop":
                person["deleted"] = True
            return 204, {}, ""
        posted = body["persons"][0]
        if self.mode == "ignore_person_update":
            return 204, {}, ""
        if any(not (n.get("nameForms") or [{}])[0].get("parts") for n in posted.get("names", [])):
            return 400, {}, "name parts are required"
        for part in ("names", "facts"):
            for c in posted.get(part) or []:
                at = next((i for i, x in enumerate(person[part]) if c.get("id") == x["id"]),
                          None)
                if at is None or self.mode == "person_update_creates":
                    person[part].append(self._named({k: v for k, v in c.items() if k != "id"}))
                else:
                    person[part][at] = {**c, "id": person[part][at]["id"]}
        if self.mode == "person_update_collateral" and person["facts"]:
            person["facts"] = person["facts"][1:]
        if self.mode == "person_update_unlives":
            person["living"] = False
        return 204, {}, ""

    def _delete_conclusion(self, pid, person, cid):
        found = [(part, c) for part in ("names", "facts") for c in person[part]
                 if c["id"] == cid]
        if not found:
            return 404, {}, ""
        if self.mode == "ignore_conclusion_delete":
            return 204, {}, ""
        person["facts"] = [f for f in person["facts"] if f["id"] != cid]
        if self.mode == "conclusion_collateral":
            person["facts"] = person["facts"][1:]
        return 204, {}, ""

    def _relationship(self, method, pid, cid, body):
        rel = self.relationships.get(cid)
        if rel is None or pid not in rel["persons"]:
            return 404, {}, ""
        if rel["deleted"]:
            return 410, {}, ""
        if method == "DELETE":
            if self.mode != "relationship_delete_noop":
                rel["deleted"] = True
            if self.mode == "relationship_delete_persons":
                for p in rel["persons"]:
                    self.persons[p]["deleted"] = True
            return 204, {}, ""
        if self.mode == "ignore_relationship_update":
            return 204, {}, ""
        facts = (body["relationships"][0]).get("facts") or []
        if self.mode == "relationship_update_collateral":
            rel["facts"].append(self._named({"type": GX + "Divorce"}))
        for fact in facts:
            rel["facts"].append(self._named(fact))
        return 204, {}, ""

    # ------------------------------------------------------------ sources

    def source_json(self, sdid):
        s = self.sources[sdid]
        return {"sourceDescriptions": [{"id": sdid, "about": s["about"],
                                        "titles": [{"value": s["title"]}],
                                        "citations": [{"value": s["citation"]}]}]}

    def sources_in_tree(self, request, tid):
        body = self._record(request)
        bad = self._bad_id(tid, tree=True)
        if bad:
            return bad
        record = self.trees.get(tid)
        if record is None or record["deleted"]:
            return (404 if record is None else 410), {}, ""
        desc = body["sourceDescriptions"][0]
        sdid = self._id("source")
        if self.mode != "ignore_source_create":
            self.sources[sdid] = {"tid": tid, "about": desc["about"],
                                  "title": desc["titles"][0]["value"],
                                  "citation": desc["citations"][0]["value"], "deleted": False}
        return self._created("sources", sdid)

    def source_route(self, request):
        body = self._record(request)
        sdid = urlsplit(request.url).path.rsplit("/", 1)[-1]
        bad = self._bad_id(sdid)
        if bad:
            return bad
        source = self.sources.get(sdid)
        if source is None:
            return 404, {}, ""
        if source["deleted"]:
            return 410, {}, ""
        if request.method == "GET":
            return self._json(200, self.source_json(sdid))
        if request.method == "DELETE":
            if self.mode != "source_delete_noop":
                source["deleted"] = True
            return 204, {}, ""
        if self.mode == "ignore_source_update":
            return 204, {}, ""
        desc = body["sourceDescriptions"][0]
        if self.mode == "source_update_creates":
            new = self._id("source")
            self.sources[new] = {**source, "title": desc["titles"][0]["value"],
                                 "citation": desc["citations"][0]["value"]}
            return 204, {"Location": f"{G}/sources/{new}"}, ""
        source.update(about=desc["about"], title=desc["titles"][0]["value"],
                      citation=desc["citations"][0]["value"])
        if self.mode == "source_update_collateral":
            for other in self.sources.values():
                if other is not source:
                    other["title"] = "ZZSPIKE collateral"
        return 204, {}, ""

    # ------------------------------------------------------------ wiring

    def serve(self):
        def rx(tail):
            return re.compile(re.escape(G) + tail + r"(\?.*)?$")

        for method in (responses.GET, responses.POST):
            responses.add_callback(method, rx("/trees"), callback=self.trees_root)
        for method in (responses.GET, responses.POST, responses.DELETE):
            responses.add_callback(method, rx(r"/trees/[^/?]+"), callback=self.tree_route)
            responses.add_callback(method, rx(r"/persons/[^?]+"), callback=self.person_route)
            responses.add_callback(method, rx(r"/sources/[^/?]+"), callback=self.source_route)
        responses.add_callback(
            responses.GET, rx(r"/trees/[^/?]+/persons"),
            callback=lambda r: self.persons_in_tree(r, urlsplit(r.url).path.split("/")[-2]))
        responses.add_callback(
            responses.POST, rx(r"/trees/[^/?]+/persons"),
            callback=lambda r: self.persons_in_tree(r, urlsplit(r.url).path.split("/")[-2]))
        responses.add_callback(
            responses.POST, rx(r"/trees/[^/?]+/sources"),
            callback=lambda r: self.sources_in_tree(r, urlsplit(r.url).path.split("/")[-2]))
        return self
