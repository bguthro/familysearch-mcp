"""A stateful fake of beta's Genealogies (private tree) API, for the P7 recipe tests.

Shapes are the recipes' guesses except what live runs showed: a tree is created with a
``sourceDescriptions`` title (the ``collections`` body is refused 400), its id comes back as
``2:2:2:<id>`` (that prefix is required: any other id answers 400 ``Invalid prefix for
genealogies``; a tree id shorter than four characters answers 500 ``Range [0, 4) out of
bounds``), and ``GET /trees`` answers a list of PUBLIC collections (the Pedigree Resource
File), never the user's own trees. Persons and sources follow the Family Tree
shapes (guesses). A well-formed unknown id answers 404, other lists answer 204 when empty,
a deleted person answers 410. Paths in ``calls`` and ``bodies`` are unquoted.

``mode`` is None (honest) or one lie (``LIES``), each caught by the recipe named beside it.
``calls`` lists ``(method, path)``; ``live()`` the objects created and not deleted.
"""
import json
import re
from urllib.parse import unquote, urlsplit

import responses

from familysearch.auth import Environment

BETA = Environment.BETA.base_url
G = BETA + "/platform/genealogies"
FS_JSON = "application/x-fs-v1+json"
GX = "http://gedcomx.org/"
PREFIX = "2:2:2:"  # live: a tree id came back as 2:2:2:MMMF-S73


def P(url):
    """The unquoted path of a request URL (ids carry colons, sent percent-encoded)."""
    return unquote(urlsplit(url).path)


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
    "persons_list_extra",     # the tree's person list names a person that is not there
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
    "source_delete_needs_local",  # only a delete by the local id s_<n> takes effect
    "ignore_source_create", "ignore_source_update", "source_update_creates",
    "source_update_collateral", "source_delete_noop",
)


class FakeGenealogies:
    def __init__(self, mode=None, *, entity_id=False):
        if mode is not None and mode not in LIES:
            raise ValueError(f"unknown lie {mode!r}")
        self.mode = mode
        self.entity_id = entity_id  # creates answer X-ENTITY-ID as well as Location
        self.trees: dict[str, dict] = {}
        self.persons: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, object]] = []  # (method, path, posted json)
        self._seq = {"tree": 0, "person": 0, "source": 0, "rel": 0, "concl": 0}

    # ------------------------------------------------------------ ids and helpers

    def _id(self, kind):
        self._seq[kind] += 1
        n = self._seq[kind]
        tag = {"tree": "TR", "person": "PE", "source": "SD", "rel": "RL", "concl": "CC"}[kind]
        return f"{PREFIX}GZ{tag}-{n:03d}"

    def _record(self, request):
        path = P(request.url)
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

    PUBLIC = {"about": "https://beta.familysearch.org/service/gen/llsc/collection/0",
              "id": "sd_0", "titles": [{"value": "Partner Trees"}]}

    def tree_json(self, tid, title=None):
        """The live tree read: a ``collections`` entry named only by its identifiers."""
        count = len([1 for p in self.persons.values() if p["tid"] == tid and not p["deleted"]])
        ark = f"https://beta.familysearch.org/ark:/61903/{tid}"
        return {"collections": [{
            "title": title or self.trees[tid]["title"], "size": count, "lang": "en-US",
            "content": [{"count": count, "resourceType": GX + "Person"}],
            "identifiers": {GX + "Persistent": [ark]}}],
            "description": "#t_" + tid.rsplit(":", 1)[-1], "sourceDescriptions": [self.PUBLIC]}

    def _live_trees(self):
        return {t: r for t, r in self.trees.items() if not r["deleted"]}

    def trees_root(self, request):
        body = self._record(request)
        if request.method == "GET":
            return self._json(200, {"sourceDescriptions": [{
                "id": "1", "about": "https://beta.familysearch.org/service/gen/llsc/collection/1",
                "titles": [{"value": "Pedigree Resource File"}]}]})
        if not (isinstance(body, dict) and body.get("sourceDescriptions")):
            return 400, {}, "A source description is required"
        title = body["sourceDescriptions"][0]["titles"][0]["value"]
        assert title.startswith("ZZSPIKE")
        tid = self._id("tree")
        if self.mode != "ignore_tree_create":
            self.trees[tid] = {"title": "ZZSPIKE other" if self.mode == "wrong_tree_title"
                               else title, "deleted": False}
        return self._created("trees", tid)

    def tree_route(self, request):
        body = self._record(request)
        path = P(request.url)
        tid = path.rsplit("/", 1)[-1]
        if self.mode == "catch_all" and request.method == "GET":
            first = next(iter(self.trees))
            return self._json(200, self.tree_json(first))
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
                return self._json(200, self.tree_json(tid, "ZZSPIKE other"))
            return self._json(200, self.tree_json(tid))
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
        """POST: add persons (local ids replaced by server ids)."""
        if self.mode == "tree_update_noop":
            return self._json(200, {"sourceDescriptions": []})
        target = tid
        if self.mode == "tree_update_other":
            target = next((t for t in self.trees if t != tid), tid)
        local = {}
        out = []
        for person in body.get("persons") or []:
            pid = self._id("person")
            local[person.get("id")] = pid
            self.persons[pid] = self._new_person(target, person)
            # live: keyed by the posted local id, the real id the tail of the ark
            out.append({"id": person.get("id"), "resourceType": GX + "Person", "identifiers": {
                GX + "Persistent": [f"https://beta.familysearch.org/ark:/61903/{pid}"]}})
        return self._json(200, {"sourceDescriptions": out})

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
        return body

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
            # live: ids and links only
            return self._json(200, {"links": {"self": {"href": G}}, "persons": [
                {"id": p, "links": {"person": {"href": f"{G}/persons/{p}"}}}
                for p in [*live, *(["2:2:2:GZPE-999"] if self.mode == "persons_list_extra"
                                   else [])]]})
        posted = body["persons"][0]
        if not posted.get("id"):
            return 400, {}, "A local id is required to create a person"
        assert posted.get("living") is not None and posted["names"][0]["nameForms"][0][
            "fullText"].startswith("ZZSPIKE")
        pid = self._id("person")
        if self.mode != "ignore_person_create":
            self.persons[pid] = self._new_person(tid, posted)
        return self._created("persons", pid)

    def person_route(self, request):
        body = self._record(request)
        path = P(request.url)
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

    # ------------------------------------------------------------ sources

    def source_json(self, sdid):
        s = self.sources[sdid]
        local = "s_" + sdid.rsplit(":", 1)[-1]  # live: 2:2:286671 is answered as s_286671
        return {"description": "#" + local, "sourceDescriptions": [{"id": local, "about": s["about"],
                                        "titles": [{"value": s["title"]}],
                                        "citations": [{"value": s["citation"]}]}]}

    @staticmethod
    def _about(sdid):
        return "https://beta.familysearch.org/service/gen/llsc/source/" + sdid.rsplit(":", 1)[-1]

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
            # live: the server rewrites about to its own source URL
            self.sources[sdid] = {"tid": tid, "about": self._about(sdid),
                                  "title": desc["titles"][0]["value"],
                                  "citation": desc["citations"][0]["value"], "deleted": False}
        return self._created("sources", sdid)

    def source_route(self, request):
        body = self._record(request)
        sdid = P(request.url).rsplit("/", 1)[-1]
        local_form = sdid.startswith("s_")
        if local_form:  # the delete by the local id the reads answer
            sdid = next((i for i in self.sources if i.rsplit(":", 1)[-1] == sdid[2:]), sdid)
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
            if self.mode == "source_delete_needs_local" and not local_form:
                return 204, {}, ""
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
        source.update(title=desc["titles"][0]["value"],
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
            callback=lambda r: self.persons_in_tree(r, P(r.url).split("/")[-2]))
        responses.add_callback(
            responses.POST, rx(r"/trees/[^/?]+/persons"),
            callback=lambda r: self.persons_in_tree(r, P(r.url).split("/")[-2]))
        responses.add_callback(
            responses.POST, rx(r"/trees/[^/?]+/sources"),
            callback=lambda r: self.sources_in_tree(r, P(r.url).split("/")[-2]))
        return self
