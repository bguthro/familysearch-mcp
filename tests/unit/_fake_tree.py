"""Fake FamilySearch beta trees shared by the verification recipe tests (offline).

``FakeTree`` is the read recipes' fixed fixture family (moved here unchanged from
test_verify_recipes_reads.py); ``WriteTree`` (below) is a stateful tree whose writes
change what later reads answer, for the write recipes.
"""
import json
import re
import zlib
from urllib.parse import parse_qs, urlsplit

import responses

from familysearch.auth import Environment
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
# The notes, source and memory hung on the family (Task 4), and the envelopes they live in.
NOTE_IDS = {"person_note": "0f0f0001-aaaa-4bbb-8ccc-000000000001",
            "couple_note": "0f0f0002-aaaa-4bbb-8ccc-000000000002",
            "child_and_parents_note": "0f0f0003-aaaa-4bbb-8ccc-000000000003"}
NOTE_TEXT = {"person_note": "ZZSPIKEP100000000004", "couple_note": "ZZSPIKER100000000005",
             "child_and_parents_note": "ZZSPIKEA100000000006"}
SOURCE = ("SDZZ-SRC", "ZZSPIKES100000000007")
MEMORY = ("4017777", "ZZSPIKEM100000000008")
COLLECTION = ("UDZZ-COL", "ZZSPIKEF100000000009")  # a folder holding the source
# Records of the same kinds that are not the fixtures' (the ``other_record`` lie).
OTHER_SOURCE = ("SDZZ-OTH", "ZZSPIKES199999999999")
OTHER_MEMORY = ("4019999", "ZZSPIKEM199999999999")
OTHER_COLLECTION = ("UDZZ-OTH", "ZZSPIKEF199999999999")


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
    answers readSourceDescriptions with the whole source box; ``wrong_location`` answers
    a source description's HEAD (which, as on beta, carries no validator) with another
    description's Content-Location. The fixture collection holds the source; an unknown
    collection id answers 400, as in the recon.
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
        if path.startswith("/platform/sources/descriptions/"):
            # Live run 2: a source description's HEAD and GET carry no validator, only
            # where the description lives.
            headers = {"Content-Location": f"{path}?flag=fsh"}
            if request.method == "HEAD" and self.mode == "wrong_location":
                headers = {"Content-Location": "/platform/sources/descriptions/SDZZ-OTH"}
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
        if rest in (["sources"], ["source-references"]):
            # The source attached to the relationship (the world attaches the fixture
            # source to the couple and the child-and-parents): its reference and, for
            # /sources, the description. Both are polled reads, as the person's are.
            if self._lagging("sources" if rest == ["sources"] else "references"):
                return self._answer(request, 204)
            body = {envelope: [self.cited_by(role)]}
            if rest == ["sources"]:
                body["sourceDescriptions"] = [self.description()]
            return self._list(request, body)
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
            if self.mode == "catch_all":
                return self._unknown(request)
            return self._answer(request, 204)  # live run 2: unknown description, empty
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

    def collection_route(self, request):
        """/sources/collections/{udcid} and its /descriptions."""
        udcid, *rest = urlsplit(request.url).path.split("/platform/sources/collections/",
                                                        1)[1].split("/")
        if udcid != COLLECTION[0]:
            if self.mode == "catch_all":
                return self._answer(request, 200, {"collections": []})
            if rest == ["descriptions"]:  # live: an empty 204 for an unknown collection
                return self._answer(request, 204)
            return self._answer(request, 400, {"errors": [
                {"message": "Failure in upstream call: Unable to read collection."}]})
        if rest == ["descriptions"]:
            return self._list(request, {"sourceDescriptions": [self.description()]})
        if rest:
            return self._answer(request, 404, {"errors": [{"code": 404}]})
        cid, title = OTHER_COLLECTION if self.mode == "other_record" else COLLECTION
        # As beta may: the folder links its owner, the signed-in user (never reported).
        return self._list(request, {"collections": [{
            "id": cid, "title": title, "size": 1,
            "links": {"owner": {"href": f"{BETA}/platform/sources/{UID}/collections"}}}]})

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
        collection = re.compile(re.escape(BETA + "/platform/sources/collections/")
                                + r"[^?]+(\?.*)?$")
        for method in (responses.GET, responses.HEAD):
            responses.add_callback(method, persons_list, callback=self.persons)
            responses.add_callback(method, person, callback=self.person_route)
            responses.add_callback(method, search, callback=self.search)
            responses.add_callback(method, relationship, callback=self.relationship_route)
            responses.add_callback(method, pedigree, callback=self.pedigree)
            responses.add_callback(method, references, callback=self.source_references)
            responses.add_callback(method, descriptions, callback=self.source_descriptions)
            responses.add_callback(method, collection, callback=self.collection_route)


# ====================================================================== WriteTree

PERSONS = TREE + "/persons"
RELATIONSHIPS = TREE + "/relationships"
COUPLES = TREE + "/couple-relationships"
CAPS = TREE + "/child-and-parents-relationships"
DESCRIPTIONS = BETA + "/platform/sources/descriptions"
COLLECTIONS = BETA + "/platform/sources/collections"
MEMORIES = BETA + "/platform/memories/memories"
# The signed-in user (never to reach a report) and their untitled default folder.
UID = "cis.user.MZZZ-USR1"
AGENT = "AZZZ-AGT1"
DEFAULT_FOLDER = "UDZZ-DEF"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The envelope each note owner's notes are posted and answered in (the read world's).
NOTE_ENVELOPES = {"person": "persons", "couple": "relationships",
                  "cap": "childAndParentsRelationships"}
PARENT_FACTS = ("parent1Facts", "parent2Facts")
# A write lie: the server says yes and does something else (or nothing).
WRITE_LIES = frozenset({
    "ignore_update",      # a person POST answers 204 and changes nothing
    "ignore_names", "ignore_facts", "ignore_gender",  # ... ignores that part only
    "replace_facts",      # posted facts replace all the person's facts
    "update_creates",     # a posted conclusion with an id is added, not updated
    "collateral",         # every person POST also drops the person's oldest fact
    "ignore_conclusion_delete",  # deleting a conclusion answers 204, changes nothing
    "wrong_conclusion",   # deleting a conclusion deletes another fact
    "ref_not_removed",    # deleting a source reference answers 204, changes nothing
    "ref_removes_all",    # ... and removes every reference of the person
    "memory_not_attached",  # a person memory upload makes a floating memory
    "no_entity_id",       # a memory upload answers without X-ENTITY-ID (Location only)
    # notes (every owner)
    "note_ignore_create",    # a note POST answers 201 with an id and stores nothing
    "note_ignore_update",    # a note update answers 204 and changes nothing
    "note_update_creates",   # a note update adds a new note instead
    "note_update_replaces",  # a note update replaces all the owner's notes with it
    "note_sibling_changed",  # a note update also rewrites the owner's other notes
    "note_wrong_delete",     # deleting a note deletes another one
    "note_delete_noop",      # deleting a note answers 204, changes nothing
    # relationships
    "cap_creates_couples",   # the original bug: a child-and-parents POST makes couples
    "duplicate_couple",      # a two-parent child-and-parents POST derives a new couple
                             # even when the parents already have one
    "ignore_rel_update",     # a relationship update answers 204, changes nothing
    "rel_update_replaces",   # posted relationship facts replace all its facts
    "rel_delete_noop",       # the first relationship delete answers 204, changes nothing
    "rel_delete_wrong",      # the first relationship delete deletes another of its kind
    "restore_noop",          # restoring a relationship answers 204, changes nothing
    "restore_new_id",        # a restore makes a copy under a new id (201, X-ENTITY-ID,
                             # Location); the old id stays deleted
    "restore_new_id_silent",  # ... and answers 204 with no id: only the lists show it
    "parent_delete_noop",    # deleting a parent answers 204, changes nothing
    "parent_delete_wrong",   # deleting a parent removes the other parent
    # relationship details (source references, conclusions, order)
    "rel_ref_ignored",       # a relationship source reference POST answers 201, stores nothing
    "rel_ref_to_person",     # ... attaches the source to the relationship's first person
    "rel_ref_delete_noop",   # deleting a relationship reference answers 204, changes nothing
    "rel_ref_delete_wrong",  # ... deletes the relationship's other reference
    "rel_ref_delete_all",    # ... deletes every reference of the relationship
    "rel_conclusion_noop",   # deleting a relationship conclusion answers 204, changes nothing
    "rel_conclusion_wrong",  # ... deletes another fact of the relationship
    "rel_conclusion_all",    # ... deletes every fact of the relationship
    "order_noop",            # setting the order answers 204, changes nothing
    "order_drops",           # ... puts the posted first person in both roles
    # source descriptions
    "desc_update_ignored",   # a description update answers 204, changes nothing
    "desc_update_creates",   # ... makes a new description (201, X-ENTITY-ID) instead
    "desc_update_sibling",   # ... also retitles the user's other descriptions
    "desc_delete_noop",      # the first description delete answers 204, changes nothing
    # source box (user-defined collections)
    "folder_create_ignored",  # a collection create answers 201 with an id, stores nothing
    "folder_create_no_id",    # a collection create answers 201 with no id header at all
    "folder_create_entity_id",  # ... with the id in Entity-Id only (the spec's name)
    "folder_rename_ignored",  # a collection update answers 204, changes nothing
    "folder_update_creates",  # ... makes a new collection with the new title instead
    "folder_delete_noop",     # the first collection delete answers 204, changes nothing
    "members_add_ignored",    # adding descriptions to a collection answers 204, no change
    "members_add_one",        # ... adds only the first one posted
    "members_remove_noop",    # removing descriptions from collections answers 204, no change
    "members_remove_all",     # ... empties every collection they were in
    "members_remove_wrong",   # ... removes another member instead
    "members_remove_deletes",  # ... deletes the description itself
})


class WriteTree:
    """A stateful fake beta: writes change what later reads answer, in live shapes.

    Persons hold their conclusions (names, facts, gender), each with a server id, as
    readPerson answers them. A POST to the person resource follows the spec: a
    conclusion that carries its id updates that conclusion, one without an id is
    created (and ``sources`` add references). Names without ``parts`` are refused 400,
    as beta refuses them. Conclusions and source references are deleted by id; source
    descriptions and memories are resources of their own (GET/DELETE, 410 once
    deleted). A memory upload to a person answers 201 with the memory id in
    ``X-ENTITY-ID`` and a ``Location`` naming the persona reference
    (``.../memory-references/{mid}-1``), as beta does.

    Notes are embedded in their owner (a person, a couple or a child-and-parents
    relationship) and posted and answered in its envelope; a note list answers each
    note's id and subject only, a single-note read the whole note. Relationships are
    created by one POST to ``/tree/relationships`` (a ``relationships`` Couple, or a
    ``childAndParentsRelationships``) and live at ``couple-relationships/{id}`` /
    ``child-and-parents-relationships/{id}`` (GET, POST update, DELETE, POST restore,
    DELETE ``/{parent1|parent2}``; 410 once deleted). As on beta, a two-parent
    child-and-parents relationship derives a couple for its parents when they have none.
    The persons' spouses / parents / children lists follow the live relationships.

    A relationship's source references are created by a POST to its
    ``/source-references`` (``{envelope: [{"sources": [{"description": url}]}]}``; 201
    with the reference's ``Location`` and ``X-ENTITY-ID``) and deleted at
    ``/source-references/{srid}``; its resource answers them in ``sources``, and
    ``/tree/source-references?description={sdid}`` answers every live person and
    relationship citing the description (204 when none). A conclusion (fact) is deleted
    at ``couple-relationships/{id}/conclusions/{cid}`` or
    ``child-and-parents-relationships/{id}/{parent1|parent2}/conclusions/{cid}`` (404
    unless it is in that list). ``spouses/order`` / ``parents/order`` take the
    relationship's own persons in the roles wanted and swap them; as the spec's 400
    for "traditional order", a couple (or two parents) of a male and a female is refused.

    ``mode`` is None or one of ``WRITE_LIES``. ``source_lag`` / ``memory_lag``: how
    many reads of a person's sources / memories still answer the state before the
    last change of them (beta's person -> source lag); ``reference_lag`` the same for
    ``/tree/source-references`` after a relationship reference changed;
    ``resource_sources=False`` leaves ``sources`` out of a relationship's resource;
    ``owner_entity_id`` answers a reference create with the relationship's id in
    ``X-ENTITY-ID`` (the Location still names the reference).

    Source descriptions are updated by a POST to their resource (the posted ``about``,
    title and citation replace the old ones). The signed-in user's source box holds
    every live description in exactly one folder, an untitled default folder at
    first: ``/sources/collections`` (POST create, GET 307 to the user's
    ``/sources/{uid}/collections`` list, whose links carry the uid),
    ``/collections/{udcid}`` (GET, POST rename, DELETE: 400 while it holds
    descriptions), its ``/descriptions`` (GET, 204 when empty; POST moves the posted
    ids into it) and ``/sources/{uid}/collections/descriptions`` (GET: the whole box,
    whatever the uid, as on beta; DELETE ``?id=``: back to the default folder). An
    unknown or deleted collection id answers ``gone_folder`` (beta: 400 "Unable to read
    collection"). ``member_lag``: how many membership reads of a collection still
    answer the state before its last change.
    ``calls`` lists
    ``(method, path)`` for every request.
    """

    def __init__(self, mode=None, *, source_lag=0, memory_lag=0, restore_lag=0,
                 reference_lag=0, resource_sources=True, owner_entity_id=False,
                 member_lag=0, gone_folder=400):
        if mode is not None and mode not in WRITE_LIES:
            raise ValueError(f"unknown lie {mode!r}")
        self.mode = mode
        self.persons: dict[str, dict] = {}
        self.descriptions: dict[str, dict] = {}
        self.memories: dict[str, dict] = {}
        self.relationships: dict[str, dict] = {}
        self._fooled = False  # a one-shot lie was told
        # A restored relationship still reads 410 (and is unlisted) for this many
        # resource reads after its restore.
        self.restore_lag = restore_lag
        self._restoring: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.resource_sources = resource_sources  # a relationship's resource lists them
        self.owner_entity_id = owner_entity_id
        self.lag = {"sources": source_lag, "memories": memory_lag,
                    "references": reference_lag, "members": member_lag}
        self.gone_folder = gone_folder
        self.collections: dict[str, dict] = {
            DEFAULT_FOLDER: {"title": None, "deleted": False, "default": True}}
        self._pending: dict[tuple[str, str], int] = {}  # (what, pid) -> stale reads left
        self._shown: dict[tuple[str, str], object] = {}  # (what, pid) -> the stale state
        self._seq = {"person": 0, "conclusion": 0, "source": 0, "reference": 0,
                     "memory": 0, "couple": 0, "cap": 0, "note": 0, "collection": 0}

    # ------------------------------------------------------------ ids

    def _id(self, kind):
        self._seq[kind] += 1
        n = self._seq[kind]
        return {"person": f"WZ{n:02d}-ZZZ", "conclusion": f"C.{n:04d}-ZZ",
                "source": f"SDWZ-{n:03d}", "reference": f"SRWZ-{n:03d}",
                "memory": str(4100000 + n), "couple": f"CR{n:02d}-ZZZ",
                "cap": f"CP{n:02d}-ZZZ", "collection": f"UDWZ-{n:03d}",
                "note": f"0e0e{n:04d}-aaaa-4bbb-8ccc-{n:012d}"}[kind]

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _json(status, body):
        return status, {"Content-Type": FS_JSON}, json.dumps(body)

    @staticmethod
    def _gone(record):
        return (404, {}, "") if record is None else (410, {}, "")

    def _record(self, request):
        self.calls.append((request.method, urlsplit(request.url).path))

    def _changed(self, what, pid, before):
        """``what`` of ``pid`` changed from ``before``: reads show it after the lag."""
        if self.lag[what]:
            key = (what, pid)
            if not self._pending.get(key):
                self._shown[key] = before
            self._pending[key] = self.lag[what]

    def _visible(self, what, pid, current):
        key = (what, pid)
        if self._pending.get(key):
            self._pending[key] -= 1
            return self._shown[key]
        return current

    def _named(self, conclusion):
        return {**conclusion, "id": conclusion.get("id") or self._id("conclusion")}

    @staticmethod
    def _bare(conclusion):
        return {k: v for k, v in conclusion.items() if k != "id"}

    # ------------------------------------------------------------ persons

    def person_json(self, pid):
        p = self.persons[pid]
        name = (p["names"][0]["nameForms"][0].get("fullText") if p["names"] else "") or ""
        body = {"id": pid, "living": p["living"], "names": p["names"], "facts": p["facts"],
                "display": {"name": name, "lifespan": "Deceased"},
                "links": {"person": {"href": f"{PERSONS}/{pid}"},
                          "artifacts": {"href": f"{PERSONS}/{pid}/memories?flag=fsh"},
                          "source-descriptions": {"href": f"{PERSONS}/{pid}/sources?flag=fsh"}}}
        if p["gender"]:
            body["gender"] = p["gender"]
        return body

    @staticmethod
    def _bad_names(names):
        return any(not (n.get("nameForms") or [{}])[0].get("parts") for n in names)

    def create_person(self, request):
        self._record(request)
        person = json.loads(request.body)["persons"][0]
        assert person["living"] is False
        names = person.get("names") or []
        assert names and names[0]["nameForms"][0]["fullText"].startswith("ZZSPIKE")
        if self._bad_names(names):
            return 400, {}, "name parts are required"
        pid = self._id("person")
        self.persons[pid] = {
            "living": False, "deleted": False, "names": [self._named(n) for n in names],
            "facts": [self._named(f) for f in person.get("facts") or []],
            "gender": self._named(person["gender"]) if person.get("gender") else None,
            "sources": [], "memories": [], "notes": []}
        return 201, {"X-ENTITY-ID": pid, "Location": f"{PERSONS}/{pid}"}, ""

    def _merge(self, current, posted, part):
        """Posted conclusions into ``current``: by id an update, without one a create."""
        if self.mode in ("ignore_update", f"ignore_{part}"):
            return current
        if self.mode == f"replace_{part}":
            return [self._named(self._bare(c)) for c in posted]
        out = [dict(c) for c in current]
        for conclusion in posted:
            cid = conclusion.get("id")
            at = next((i for i, c in enumerate(out) if cid and c["id"] == cid), None)
            if at is None or self.mode == "update_creates":
                out.append(self._named(self._bare(conclusion)))
            else:
                out[at] = {**conclusion, "id": cid}
        return out

    def update_person(self, request, pid):
        person = self.persons[pid]
        posted = json.loads(request.body)["persons"]
        assert len(posted) == 1 and posted[0].get("id") in (None, pid)
        posted = posted[0]
        if self._bad_names(posted.get("names") or []):
            return 400, {}, "name parts are required"
        if "names" in posted:
            person["names"] = self._merge(person["names"], posted["names"], "names")
        if "facts" in posted:
            person["facts"] = self._merge(person["facts"], posted["facts"], "facts")
        if "gender" in posted and self.mode not in ("ignore_update", "ignore_gender"):
            gender, old = posted["gender"], person["gender"]
            keep = old and gender.get("id") == old["id"]
            person["gender"] = {**gender, "id": old["id"]} if keep \
                else self._named(self._bare(gender))
        if posted.get("sources") and self.mode != "ignore_update":
            before = [dict(r) for r in person["sources"]]
            for ref in posted["sources"]:
                person["sources"].append({"id": self._id("reference"),
                                          "description": ref["description"]})
            self._changed("sources", pid, before)
        if self.mode == "collateral" and person["facts"]:
            person["facts"] = person["facts"][1:]
        return 204, {}, ""

    def delete_conclusion(self, pid, cid):
        person = self.persons[pid]
        conclusions = [*person["names"], *person["facts"], person["gender"]]
        if not any(c and c["id"] == cid for c in conclusions):
            return 404, {}, ""
        if self.mode == "ignore_conclusion_delete":
            return 204, {}, ""
        if self.mode == "wrong_conclusion":
            others = [f for f in person["facts"] if f["id"] != cid]
            cid = others[0]["id"] if others else cid
        person["names"] = [n for n in person["names"] if n["id"] != cid]
        person["facts"] = [f for f in person["facts"] if f["id"] != cid]
        if person["gender"] and person["gender"]["id"] == cid:
            person["gender"] = None
        return 204, {}, ""

    def person_sources(self, pid):
        refs = self._visible("sources", pid, self.persons[pid]["sources"])
        if not refs:
            return 204, {}, ""
        sids = [r["description"].split("?")[0].rsplit("/", 1)[-1] for r in refs]
        return self._json(200, {
            "persons": [{"id": pid, "sources": [dict(r) for r in refs]}],
            "sourceDescriptions": [self.description_json(s) for s in sids
                                   if s in self.descriptions]})

    def delete_reference(self, pid, srid):
        person = self.persons[pid]
        if not any(r["id"] == srid for r in person["sources"]):
            return 404, {}, ""
        if self.mode == "ref_not_removed":
            return 204, {}, ""
        before = [dict(r) for r in person["sources"]]
        person["sources"] = [] if self.mode == "ref_removes_all" else [
            r for r in person["sources"] if r["id"] != srid]
        self._changed("sources", pid, before)
        return 204, {}, ""

    def person_memories(self, request, pid):
        listed = self._visible("memories", pid, list(self.persons[pid]["memories"]))
        listed = [m for m in listed if not self.memories[m]["deleted"]]
        if not listed:
            return 204, {}, ""
        return self._json(200, {"persons": [], "sourceDescriptions": [
            self.memory_json(m) for m in listed], "links": {"self": {"href": request.url}}})

    def upload_memory(self, request, pid):
        assert b'name="artifact"' in request.body and PNG_SIGNATURE in request.body
        assert b"Content-Type: image/png" in request.body
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        mid = self._id("memory")
        self.memories[mid] = {"title": query.get("title"), "filename": query.get("filename"),
                              "deleted": False}
        if self.mode != "memory_not_attached":
            before = list(self.persons[pid]["memories"])
            self.persons[pid]["memories"].append(mid)
            self._changed("memories", pid, before)
        headers = {"Location": f"{PERSONS}/{pid}/memory-references/{mid}-1"}
        if self.mode != "no_entity_id":
            headers["X-ENTITY-ID"] = mid
        return 201, headers, ""

    def person_route(self, request):
        self._record(request)
        path = urlsplit(request.url).path
        pid, *rest = path.split("/platform/tree/persons/", 1)[1].split("/")
        person = self.persons.get(pid)
        if person is None or person["deleted"]:
            return self._gone(person)
        method = request.method
        if not rest:
            if method == "GET":
                return self._json(200, {"persons": [self.person_json(pid)]})
            if method == "POST":
                return self.update_person(request, pid)
            person["deleted"] = True  # DELETE
            return 204, {}, ""
        what = rest[0]
        if what == "conclusions" and len(rest) == 2 and method == "DELETE":
            return self.delete_conclusion(pid, rest[1])
        if what == "sources" and len(rest) == 1 and method == "GET":
            return self.person_sources(pid)
        if what == "source-references" and len(rest) == 2 and method == "DELETE":
            return self.delete_reference(pid, rest[1])
        if what == "memories" and len(rest) == 1:
            if method == "GET":
                return self.person_memories(request, pid)
            if method == "POST":
                return self.upload_memory(request, pid)
        if what == "notes":
            return self.notes_route(request, person, "person", pid, rest[1:])
        if what in ("spouses", "parents", "children") and len(rest) == 1 and method == "GET":
            return getattr(self, f"person_{what}")(pid)
        # Live: GET /persons/{pid}/source-references answered 404; /sources is GET-only.
        return 404, {}, ""

    # ------------------------------------------------------------ sources, memories

    def description_json(self, sid):
        d = self.descriptions[sid]
        return {"id": sid, "about": d["about"], "titles": [{"value": d["title"]}],
                "citations": [{"value": d["citation"]}]}

    def create_description(self, request):
        self._record(request)
        desc = json.loads(request.body)["sourceDescriptions"][0]
        return self._new_description(desc)

    def _new_description(self, desc):
        assert desc["titles"][0]["value"].startswith("ZZSPIKE")
        sid = self._id("source")
        self.descriptions[sid] = {"about": desc["about"], "title": desc["titles"][0]["value"],
                                  "citation": desc["citations"][0]["value"], "deleted": False,
                                  "folder": DEFAULT_FOLDER}
        return 201, {"X-ENTITY-ID": sid, "Location": f"{DESCRIPTIONS}/{sid}"}, ""

    def description_route(self, request):
        self._record(request)
        sid = urlsplit(request.url).path.rsplit("/", 1)[-1]
        desc = self.descriptions.get(sid)
        if desc is None or desc["deleted"]:
            return self._gone(desc)
        if request.method == "DELETE":
            if self.mode == "desc_delete_noop" and not self._fooled:
                self._fooled = True
                return 204, {}, ""
            self._set_folder(sid, None)
            desc["deleted"] = True
            return 204, {}, ""
        if request.method == "POST":
            return self.update_description(request, sid)
        return self._json(200, {"sourceDescriptions": [self.description_json(sid)]})

    def update_description(self, request, sid):
        posted = json.loads(request.body).get("sourceDescriptions")
        if not (isinstance(posted, list) and len(posted) == 1):
            return 400, {}, "one source description is expected"
        posted = posted[0]
        if posted.get("id") not in (None, sid):
            return 400, {}, "another description's id"
        if self.mode == "desc_update_ignored":
            return 204, {}, ""
        if self.mode == "desc_update_creates":
            return self._new_description(posted)
        fields = {"about": posted["about"], "title": posted["titles"][0]["value"],
                  "citation": posted["citations"][0]["value"]}
        self.descriptions[sid].update(fields)
        if self.mode == "desc_update_sibling":
            for other in self.descriptions.values():
                other["title"] = fields["title"]
        return 204, {}, ""

    # ------------------------------------------------------------ source box

    def _box(self):
        return [s for s, d in self.descriptions.items() if not d["deleted"]]

    def _members_now(self, udcid):
        return [s for s in self._box() if self.descriptions[s]["folder"] == udcid]

    def _set_folder(self, sid, udcid):
        """Move ``sid`` to ``udcid`` (None: out of the box), lagging the reads."""
        old = self.descriptions[sid]["folder"]
        if old == udcid:
            return
        for folder in {old, udcid} - {None}:
            self._changed("members", folder, self._members_now(folder))
        self.descriptions[sid]["folder"] = udcid

    def _folder(self, udcid):
        folder = self.collections.get(udcid)
        return folder if folder and not folder["deleted"] else None

    def collection_json(self, udcid):
        c = self.collections[udcid]
        body = {"id": udcid, "size": len(self._members_now(udcid)),
                "attribution": {"contributor": {"resourceId": AGENT}},
                "links": {"self": {"href": f"{COLLECTIONS}/{udcid}"},
                          "owner": {"href": f"{BETA}/platform/sources/{UID}/collections"}}}
        if c["title"] is not None:
            body["title"] = c["title"]
        return body

    @staticmethod
    def _posted_collection(request):
        posted = json.loads(request.body).get("collections")
        if not (isinstance(posted, list) and len(posted) == 1):
            return None
        title = posted[0].get("title")
        return posted[0] if isinstance(title, str) and title.startswith("ZZSPIKE") else None

    def _titled(self, title):
        return any(c["title"] == title and not c["deleted"] for c in self.collections.values())

    def _new_collection(self, title):
        udcid = self._id("collection")
        self.collections[udcid] = {"title": title, "deleted": False, "default": False}
        return udcid

    def create_collection(self, request):
        self._record(request)
        posted = self._posted_collection(request)
        if posted is None:
            return 400, {}, "invalid collection name"
        if self._titled(posted["title"]):
            return 400, {}, "the collection already exists"
        if self.mode == "folder_create_ignored":
            udcid = self._id("collection")
        else:
            udcid = self._new_collection(posted["title"])
        if self.mode == "folder_create_no_id":
            return 201, {}, ""
        if self.mode == "folder_create_entity_id":
            return 201, {"Entity-Id": udcid}, ""
        return 201, {"X-ENTITY-ID": udcid, "Location": f"{COLLECTIONS}/{udcid}"}, ""

    def user_collections(self, request):
        self._record(request)
        live = [u for u, c in self.collections.items() if not c["deleted"]]
        return self._json(200, {
            "collections": [self.collection_json(u) for u in live],
            "links": {"self": {"href": f"{BETA}/platform/sources/{UID}/collections"}}})

    def collections_root(self, request):
        """GET /sources/collections: 307 to the signed-in user's folders (spec)."""
        self._record(request)
        return 307, {"Location": f"{BETA}/platform/sources/{UID}/collections"}, ""

    def collection_route(self, request):
        self._record(request)
        udcid, *rest = urlsplit(request.url).path.split("/platform/sources/collections/",
                                                        1)[1].split("/")
        folder, method = self._folder(udcid), request.method
        if folder is None:
            return self.gone_folder, {}, json.dumps({"errors": [
                {"message": "Failure in upstream call: Unable to read collection."}]})
        if rest == ["descriptions"]:
            if method == "GET":
                listed = self._visible("members", udcid, self._members_now(udcid))
                listed = [s for s in listed if not self.descriptions[s]["deleted"]]
                if not listed:
                    return 204, {}, ""
                return self._json(200, {"sourceDescriptions": [
                    self.description_json(s) for s in listed]})
            return self.add_members(request, udcid)
        if rest:
            return 404, {}, ""
        if method == "GET":
            return self._json(200, {"collections": [self.collection_json(udcid)]})
        if method == "POST":
            posted = self._posted_collection(request)
            if posted is None or posted.get("id") not in (None, udcid):
                return 400, {}, "the requested collection id is invalid"
            if self.mode == "folder_rename_ignored":
                return 204, {}, ""
            if self.mode == "folder_update_creates":
                self._new_collection(posted["title"])
                return 204, {}, ""
            folder["title"] = posted["title"]
            return 204, {}, ""
        if self._members_now(udcid):  # DELETE
            return 400, {}, "The requested collection is not empty."
        if self.mode == "folder_delete_noop" and not self._fooled:
            self._fooled = True
            return 204, {}, ""
        folder["deleted"] = True
        return 204, {}, ""

    def add_members(self, request, udcid):
        posted = json.loads(request.body).get("sourceDescriptions")
        sids = [d.get("id") for d in posted or [] if isinstance(d, dict)]
        if not sids or any(s not in self._box() for s in sids):
            return 404, {}, "One or more of the descriptions referenced was invalid."
        if self.mode == "members_add_ignored":
            return 204, {}, ""
        for sid in sids[:1] if self.mode == "members_add_one" else sids:
            self._set_folder(sid, udcid)
        return 204, {}, ""

    def box_route(self, request):
        """/sources/{uid}/collections/descriptions: the box (GET) or removal (DELETE)."""
        self._record(request)
        if request.method == "GET":
            return self._json(200, {"sourceDescriptions": [
                self.description_json(s) for s in self._box()],
                "links": {"self": {"href": request.url}}})
        query = parse_qs(urlsplit(request.url).query)
        sids = [s for value in query.get("id", []) for s in value.split(",") if s]
        if not sids or any(s not in self._box() for s in sids):
            return 400, {}, "One or more of the descriptions referenced was invalid."
        if self.mode == "members_remove_noop":
            return 204, {}, ""
        for sid in sids:
            folder = self.descriptions[sid]["folder"]
            if self.mode == "members_remove_deletes":
                self._set_folder(sid, None)
                self.descriptions[sid]["deleted"] = True
            elif self.mode == "members_remove_all":
                for other in self._members_now(folder):
                    self._set_folder(other, DEFAULT_FOLDER)
            elif self.mode == "members_remove_wrong":
                others = [o for o in self._members_now(folder) if o != sid]
                self._set_folder(others[0] if others else sid, DEFAULT_FOLDER)
            else:
                self._set_folder(sid, DEFAULT_FOLDER)
        return 204, {}, ""

    def memory_json(self, mid):
        m = self.memories[mid]
        return {"id": mid, "about": f"{MEMORIES}/{mid}", "mediaType": "image/png",
                "titles": [{"value": m["title"]}] if m["title"] else []}

    def memory_route(self, request):
        self._record(request)
        mid = urlsplit(request.url).path.rsplit("/", 1)[-1]
        memory = self.memories.get(mid)
        if memory is None or memory["deleted"]:
            return self._gone(memory)
        if request.method == "DELETE":
            memory["deleted"] = True
            return 204, {}, ""
        return self._json(200, {"sourceDescriptions": [self.memory_json(mid)]})

    # ------------------------------------------------------------ notes

    def notes_route(self, request, owner, kind, owner_id, rest):
        """An owner's notes: list / create (``rest`` empty), read / update / delete one."""
        envelope, method = NOTE_ENVELOPES[kind], request.method
        notes = owner["notes"]
        if not rest:
            if method == "GET":
                if not notes:
                    return 204, {}, ""
                return self._json(200, {envelope: [{"id": owner_id, "notes": [
                    {"id": n["id"], "subject": n["subject"]} for n in notes]}]})
            if method != "POST":
                return 405, {}, ""
            posted = self._posted_note(request, envelope)
            if posted is None:
                return 400, {}, "wrong note envelope"
            nid = self._id("note")
            if self.mode != "note_ignore_create":
                notes.append({"id": nid, **posted})
            url = urlsplit(request.url)
            return 201, {"Location": f"{url.scheme}://{url.netloc}{url.path}/{nid}",
                         "X-ENTITY-ID": nid}, ""
        if len(rest) != 1:
            return 404, {}, ""
        note = next((n for n in notes if n["id"] == rest[0]), None)
        if note is None:
            return 404, {}, ""
        if method == "GET":
            return self._json(200, {envelope: [{"id": owner_id, "notes": [dict(note)]}]})
        if method == "POST":
            posted = self._posted_note(request, envelope)
            if posted is None:
                return 400, {}, "wrong note envelope"
            if self.mode == "note_ignore_update":
                pass
            elif self.mode == "note_update_creates":
                notes.append({"id": self._id("note"), **posted})
            elif self.mode == "note_update_replaces":
                owner["notes"] = [{**note, **posted}]
            else:
                note.update(posted)
                if self.mode == "note_sibling_changed":
                    for other in notes:
                        other.update(posted)
            return 204, {}, ""
        if method == "DELETE":
            target = note["id"]
            if self.mode == "note_delete_noop":
                return 204, {}, ""
            if self.mode == "note_wrong_delete":
                others = [n["id"] for n in notes if n["id"] != target]
                target = others[0] if others else target
            owner["notes"] = [n for n in notes if n["id"] != target]
            return 204, {}, ""
        return 405, {}, ""

    @staticmethod
    def _posted_note(request, envelope):
        body = json.loads(request.body)
        owners = body.get(envelope) if isinstance(body, dict) else None
        if not (isinstance(owners, list) and len(owners) == 1):
            return None
        notes = owners[0].get("notes") or []
        assert len(notes) == 1, "one note per request"
        note = notes[0]
        assert str(note.get("text", "")).startswith("ZZSPIKE")
        return {"subject": note.get("subject"), "text": note.get("text")}

    # ------------------------------------------------------------ relationships

    @staticmethod
    def _pref(pid):
        return {"resource": f"{PERSONS}/{pid}", "resourceId": pid}

    def _posted_pid(self, ref):
        """The person a posted reference names; it must be a live fake person."""
        if not isinstance(ref, dict):
            return None
        pid = ref.get("resourceId") or str(ref.get("resource") or "").rsplit("/", 1)[-1]
        if ref.get("resource") and ref["resource"] != f"{PERSONS}/{pid}":
            return None  # a clean persons URL, as the library sends
        person = self.persons.get(pid)
        return pid if person and not person["deleted"] else None

    def _live(self, kind):
        return {rid: r for rid, r in self.relationships.items()
                if r["kind"] == kind and not r["deleted"] and not self._restoring.get(rid)}

    def _new_couple(self, p1, p2):
        rid = self._id("couple")
        self.relationships[rid] = {"kind": "couple", "deleted": False, "person1": p1,
                                   "person2": p2, "facts": [], "notes": [], "sources": []}
        return rid

    def couples_between(self, a, b):
        return [rid for rid, r in self._live("couple").items()
                if {r["person1"], r["person2"]} == {a, b}]

    def create_relationship(self, request):
        self._record(request)
        body = json.loads(request.body)
        if list(body) == ["relationships"] and len(body["relationships"]) == 1:
            rel = body["relationships"][0]
            p1, p2 = self._posted_pid(rel.get("person1")), self._posted_pid(rel.get("person2"))
            if rel.get("type") != GX + "Couple" or not (p1 and p2) or p1 == p2:
                return 400, {}, "a couple needs type Couple and two persons"
            rid = self._new_couple(p1, p2)
            return 201, {"X-ENTITY-ID": rid, "Location": f"{COUPLES}/{rid}"}, ""
        if list(body) == ["childAndParentsRelationships"] and len(
                body["childAndParentsRelationships"]) == 1:
            rel = body["childAndParentsRelationships"][0]
            child = self._posted_pid(rel.get("child"))
            parents = [self._posted_pid(rel[k]) for k in ("parent1", "parent2") if rel.get(k)]
            if not child or not parents or None in parents or child in parents:
                return 400, {}, "a child and one or two parents are required"
            if self.mode == "cap_creates_couples":
                ids = [self._new_couple(child, p) for p in parents]
                return 201, {"X-ENTITY-ID": ids[0], "Location": f"{COUPLES}/{ids[0]}"}, ""
            cid = self._id("cap")
            self.relationships[cid] = {
                "kind": "cap", "deleted": False, "child": child,
                "parent1": parents[0], "parent2": parents[1] if len(parents) == 2 else None,
                "parent1Facts": [], "parent2Facts": [], "notes": [], "sources": []}
            if len(parents) == 2 and (self.mode == "duplicate_couple"
                                      or not self.couples_between(*parents)):
                self._new_couple(*parents)  # derived by the server, as beta does
            return 201, {"X-ENTITY-ID": cid, "Location": f"{CAPS}/{cid}"}, ""
        return 400, {}, "unknown relationship body"

    def couple_json(self, rid):
        r = self.relationships[rid]
        body = {"id": rid, "type": GX + "Couple", "person1": self._pref(r["person1"]),
                "person2": self._pref(r["person2"])}
        if r["facts"]:
            body["facts"] = [dict(f) for f in r["facts"]]
        if r["sources"] and self.resource_sources:
            body["sources"] = [dict(ref) for ref in r["sources"]]
        return body

    def cap_json(self, rid):
        r = self.relationships[rid]
        body = {"id": rid, "child": self._pref(r["child"])}
        for role in ("parent1", "parent2"):
            if r[role]:
                body[role] = self._pref(r[role])
        for key in PARENT_FACTS:
            if r[key]:
                body[key] = [dict(f) for f in r[key]]
        if r["sources"] and self.resource_sources:
            body["sources"] = [dict(ref) for ref in r["sources"]]
        return body

    def _relation_json(self, rid):
        if self.relationships[rid]["kind"] == "couple":
            return {"relationships": [self.couple_json(rid)]}
        return {"childAndParentsRelationships": [self.cap_json(rid)]}

    def _merge_facts(self, current, posted):
        if self.mode == "rel_update_replaces":
            return [self._named(self._bare(f)) for f in posted]
        out = [dict(f) for f in current]
        for fact in posted:
            at = next((i for i, f in enumerate(out) if fact.get("id") == f["id"]), None)
            if at is None:
                out.append(self._named(self._bare(fact)))
            else:
                out[at] = {**fact, "id": out[at]["id"]}
        return out

    def update_relationship(self, request, rid):
        r = self.relationships[rid]
        envelope = "relationships" if r["kind"] == "couple" else "childAndParentsRelationships"
        posted = json.loads(request.body).get(envelope)
        if not (isinstance(posted, list) and len(posted) == 1):
            return 400, {}, f"expected one {envelope} entry"
        posted = posted[0]
        if posted.get("id") not in (None, rid):
            return 400, {}, "another relationship's id"
        if self.mode == "ignore_rel_update":
            return 204, {}, ""
        keys = ("facts",) if r["kind"] == "couple" else PARENT_FACTS
        for key in keys:
            if key in posted:
                r[key] = self._merge_facts(r[key], posted[key])
            elif self.mode == "rel_update_replaces":
                r[key] = []  # every fact the update did not post is gone
        return 204, {}, ""

    def relationship_route(self, request):
        self._record(request)
        path = urlsplit(request.url).path
        segment, rest = path.split("/platform/tree/", 1)[1].split("/", 1)
        kind = "couple" if segment == "couple-relationships" else "cap"
        rid, *rest = rest.split("/")
        r = self.relationships.get(rid)
        method = request.method
        if r is None or r["kind"] != kind:
            return 404, {}, ""
        if rest == ["restore"] and method == "POST":
            if self.mode == "restore_noop":
                return 204, {}, ""
            if self.mode in ("restore_new_id", "restore_new_id_silent") and r["deleted"]:
                new = self._id(r["kind"])
                self.relationships[new] = {**json.loads(json.dumps(r)), "deleted": False,
                                           "notes": []}
                if self.mode == "restore_new_id_silent":
                    return 204, {}, ""
                base = COUPLES if kind == "couple" else CAPS
                return 201, {"X-ENTITY-ID": new, "Location": f"{base}/{new}"}, ""
            r["deleted"] = False
            if self.restore_lag:
                self._restoring[rid] = self.restore_lag
            return 204, {}, ""
        if self._restoring.get(rid):
            self._restoring[rid] -= 1
            return 410, {}, ""
        if r["deleted"]:
            return 410, {}, ""
        if not rest:
            if method == "GET":
                return self._json(200, self._relation_json(rid))
            if method == "POST":
                return self.update_relationship(request, rid)
            # DELETE. The no-op lie fools only the first delete (the recipe's), so
            # teardown can still remove the relationship.
            if self.mode == "rel_delete_noop" and not self._fooled:
                self._fooled = True
            elif self.mode == "rel_delete_wrong" and not self._fooled:
                self._fooled = True
                other = next((i for i in self._live(kind) if i != rid), rid)
                self.relationships[other]["deleted"] = True
            else:
                r["deleted"] = True
            return 204, {}, ""
        if rest[0] == "notes":
            return self.notes_route(request, r, kind, rid, rest[1:])
        if rest[0] == "source-references":
            if len(rest) == 1 and method == "POST":
                return self.create_rel_reference(request, kind, rid)
            if len(rest) == 2 and method == "DELETE":
                return self.delete_rel_reference(rid, rest[1])
            return 405, {}, ""
        if method == "DELETE" and (
                (kind == "couple" and len(rest) == 2 and rest[0] == "conclusions")
                or (kind == "cap" and len(rest) == 3 and rest[1] == "conclusions"
                    and rest[0] in ("parent1", "parent2"))):
            return self.delete_rel_conclusion(rid, "facts" if kind == "couple"
                                              else f"{rest[0]}Facts", rest[-1])
        if method == "POST" and rest == (["spouses", "order"] if kind == "couple"
                                         else ["parents", "order"]):
            return self.set_order(request, rid)
        if kind == "cap" and rest in (["parent1"], ["parent2"]) and method == "DELETE":
            role = rest[0]
            if not r[role]:
                return 404, {}, ""
            if self.mode == "parent_delete_noop":
                return 204, {}, ""
            if self.mode == "parent_delete_wrong":
                role = "parent1" if role == "parent2" else "parent2"
            r[role] = None
            r[f"{role}Facts"] = []
            return 204, {}, ""
        return 404, {}, ""

    # ------------------------------------------------------------ relationship details

    @staticmethod
    def _envelope(r):
        return "relationships" if r["kind"] == "couple" else "childAndParentsRelationships"

    @staticmethod
    def _sdid(url):
        return str(url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]

    def _citing_snapshot(self, sdid):
        """What /tree/source-references?description={sdid} answers now."""
        persons = [{"id": pid, "sources": [dict(ref) for ref in p["sources"]
                                           if self._sdid(ref["description"]) == sdid]}
                   for pid, p in self.persons.items() if not p["deleted"]]
        body = {"persons": [p for p in persons if p["sources"]]}
        for kind, envelope in (("couple", "relationships"),
                               ("cap", "childAndParentsRelationships")):
            entries = [{"id": rid, "sources": [dict(ref) for ref in r["sources"]
                                               if self._sdid(ref["description"]) == sdid]}
                       for rid, r in self._live(kind).items()]
            entries = [e for e in entries if e["sources"]]
            if entries:
                body[envelope] = entries
        return body if any(body.values()) else None

    def _references_changed(self, sdids):
        for sdid in sdids:
            self._changed("references", sdid, self._citing_snapshot(sdid))

    def create_rel_reference(self, request, kind, rid):
        r = self.relationships[rid]
        posted = json.loads(request.body).get(self._envelope(r))
        if not (isinstance(posted, list) and len(posted) == 1):
            return 400, {}, f"expected one {self._envelope(r)} entry"
        refs = posted[0].get("sources") or []
        if len(refs) != 1 or not str(refs[0].get("description", "")).startswith(DESCRIPTIONS):
            return 400, {}, "one source reference to a source description is expected"
        url = refs[0]["description"]
        sdid = self._sdid(url)
        if sdid not in self.descriptions or self.descriptions[sdid]["deleted"]:
            return 400, {}, "no such source description"
        srid = self._id("reference")
        before = self._citing_snapshot(sdid)
        if self.mode == "rel_ref_to_person":
            owner = r["person1"] if r["kind"] == "couple" else r["child"]
            self.persons[owner]["sources"].append({"id": srid, "description": url})
        elif self.mode != "rel_ref_ignored":
            r["sources"].append({"id": srid, "description": url})
        self._changed("references", sdid, before)
        base = COUPLES if kind == "couple" else CAPS
        return 201, {"Location": f"{base}/{rid}/source-references/{srid}",
                     "X-ENTITY-ID": rid if self.owner_entity_id else srid}, ""

    def delete_rel_reference(self, rid, srid):
        r = self.relationships[rid]
        if not any(ref["id"] == srid for ref in r["sources"]):
            return 404, {}, ""
        if self.mode == "rel_ref_delete_noop":
            return 204, {}, ""
        sdids = {self._sdid(ref["description"]) for ref in r["sources"]}
        before = {s: self._citing_snapshot(s) for s in sdids}
        if self.mode == "rel_ref_delete_all":
            r["sources"] = []
        else:
            target = srid
            if self.mode == "rel_ref_delete_wrong":
                target = next((ref["id"] for ref in r["sources"] if ref["id"] != srid), srid)
            r["sources"] = [ref for ref in r["sources"] if ref["id"] != target]
        for sdid, shown in before.items():
            self._changed("references", sdid, shown)
        return 204, {}, ""

    def source_references(self, request):
        """GET /tree/source-references?description={sdid}: who cites the description."""
        self._record(request)
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        if "description" not in query:
            return 404, {}, ""  # recon: 404 without a parameter
        sdid = query["description"]
        body = self._visible("references", sdid, self._citing_snapshot(sdid))
        return (204, {}, "") if body is None else self._json(200, body)

    def delete_rel_conclusion(self, rid, key, cid):
        r = self.relationships[rid]
        if not any(f["id"] == cid for f in r[key]):
            return 404, {}, ""
        if self.mode == "rel_conclusion_noop":
            return 204, {}, ""
        keys = ("facts",) if r["kind"] == "couple" else PARENT_FACTS
        if self.mode == "rel_conclusion_all":
            for k in keys:
                r[k] = []
            return 204, {}, ""
        if self.mode == "rel_conclusion_wrong":
            other = next(((k, f["id"]) for k in keys for f in r[k] if f["id"] != cid), None)
            if other:
                key, cid = other
        r[key] = [f for f in r[key] if f["id"] != cid]
        return 204, {}, ""

    def _gender(self, pid):
        gender = self.persons[pid]["gender"] or {}
        return gender.get("type")

    def set_order(self, request, rid):
        r = self.relationships[rid]
        roles = ("person1", "person2") if r["kind"] == "couple" else ("parent1", "parent2")
        posted = json.loads(request.body).get(self._envelope(r))
        if not (isinstance(posted, list) and len(posted) == 1):
            return 400, {}, f"expected one {self._envelope(r)} entry"
        # Live 2026-10-26: beta refused a couple entry of person refs only (400 "Exactly
        # one Couple Relationship must be provided") and answered 500 (an NPE) to a
        # child-and-parents entry of parent refs only: the whole relationship is wanted.
        entry = posted[0]
        if entry.get("id") != rid:
            return 400, {}, "the relationship's id is required"
        if r["kind"] == "couple" and entry.get("type") != GX + "Couple":
            return 400, {}, "Exactly one Couple Relationship must be provided."
        if r["kind"] == "cap" and self._posted_pid(entry.get("child")) != r["child"]:
            return 500, {}, "NullPointerException: the child is required"
        wanted = [self._posted_pid(entry.get(role)) for role in roles]
        current = [r[role] for role in roles]
        if None in wanted or sorted(wanted) != sorted(current) or wanted[0] == wanted[1]:
            return 400, {}, "the given persons are not the relationship's"
        if {self._gender(p) for p in current} == {GX + "Male", GX + "Female"}:
            return 400, {}, "the persons are currently in traditional order"
        if self.mode == "order_noop":
            return 204, {}, ""
        if self.mode == "order_drops":
            wanted = [wanted[0], wanted[0]]
        if r["kind"] == "cap" and wanted != current:
            r["parent1Facts"], r["parent2Facts"] = r["parent2Facts"], r["parent1Facts"]
        for role, pid in zip(roles, wanted):
            r[role] = pid
        return 204, {}, ""

    # ------------------------------------------------------------ relatives

    def _family(self, persons, couples=(), caps=()):
        return self._json(200, {
            "persons": [self.person_json(p) for p in dict.fromkeys(persons)],
            "relationships": [self.couple_json(c) for c in couples],
            "childAndParentsRelationships": [self.cap_json(c) for c in caps]})

    def person_spouses(self, pid):
        couples = [rid for rid, r in self._live("couple").items()
                   if pid in (r["person1"], r["person2"])]
        if not couples:
            return 204, {}, ""
        spouses = [p for c in couples for p in (self.relationships[c]["person1"],
                                                 self.relationships[c]["person2"])]
        caps = [rid for rid, r in self._live("cap").items()
                if pid in (r["parent1"], r["parent2"])]
        return self._family([pid, *spouses], couples, caps)

    def person_parents(self, pid):
        caps = [rid for rid, r in self._live("cap").items() if r["child"] == pid]
        if not caps:
            return 204, {}, ""
        parents = [p for c in caps for p in (self.relationships[c]["parent1"],
                                              self.relationships[c]["parent2"]) if p]
        couples = [rid for rid, r in self._live("couple").items()
                   if r["person1"] in parents and r["person2"] in parents]
        return self._family(parents, couples, caps)

    def person_children(self, pid):
        caps = [rid for rid, r in self._live("cap").items()
                if pid in (r["parent1"], r["parent2"])]
        if not caps:
            return 204, {}, ""
        return self._family([pid, *[self.relationships[c]["child"] for c in caps]], (), caps)

    # ------------------------------------------------------------ wiring

    def live(self):
        """What was created and not deleted (empty after a strict teardown)."""
        return ([p for p, r in self.persons.items() if not r["deleted"]]
                + [s for s, r in self.descriptions.items() if not r["deleted"]]
                + [m for m, r in self.memories.items() if not r["deleted"]]
                + [x for x, r in self.relationships.items() if not r["deleted"]]
                + [u for u, c in self.collections.items()
                   if not c["deleted"] and not c["default"]])

    def serve(self):
        responses.add(responses.GET, BETA + "/platform/collections/tree", json={
            "collections": [{"links": {
                "persons": {"href": PERSONS},
                "relationships": {"href": TREE + "/relationships"},
                "artifacts": {"href": MEMORIES},
                "source-descriptions": {"href": DESCRIPTIONS}}}]})
        responses.add(responses.GET, BETA + "/platform/users/current",
                      json={"users": [{"id": UID}]})
        responses.add_callback(responses.POST, re.compile(re.escape(PERSONS) + r"(\?.*)?$"),
                               callback=self.create_person)
        one = re.compile(re.escape(PERSONS + "/") + r"[^?]+(\?.*)?$")
        for method in (responses.GET, responses.POST, responses.DELETE):
            responses.add_callback(method, one, callback=self.person_route)
        responses.add_callback(responses.POST,
                               re.compile(re.escape(RELATIONSHIPS) + r"(\?.*)?$"),
                               callback=self.create_relationship)
        relationship = re.compile(
            re.escape(TREE) + r"/(couple|child-and-parents)-relationships/[^?]+(\?.*)?$")
        for method in (responses.GET, responses.POST, responses.DELETE):
            responses.add_callback(method, relationship, callback=self.relationship_route)
        responses.add_callback(
            responses.GET, re.compile(re.escape(TREE + "/source-references") + r"(\?.*)?$"),
            callback=self.source_references)
        responses.add_callback(responses.POST,
                               re.compile(re.escape(DESCRIPTIONS) + r"(\?.*)?$"),
                               callback=self.create_description)
        for method in (responses.GET, responses.POST, responses.DELETE):
            responses.add_callback(
                method, re.compile(re.escape(DESCRIPTIONS + "/") + r"[^/?]+(\?.*)?$"),
                callback=self.description_route)
            responses.add_callback(
                method, re.compile(re.escape(COLLECTIONS + "/") + r"[^?]+(\?.*)?$"),
                callback=self.collection_route)
        root = re.compile(re.escape(COLLECTIONS) + r"(\?.*)?$")
        responses.add_callback(responses.POST, root, callback=self.create_collection)
        responses.add_callback(responses.GET, root, callback=self.collections_root)
        responses.add_callback(
            responses.GET, re.compile(re.escape(BETA) + r"/platform/sources/[^/]+/collections"
                                      r"(\?.*)?$"), callback=self.user_collections)
        for method in (responses.GET, responses.DELETE):
            responses.add_callback(
                method, re.compile(re.escape(BETA) + r"/platform/sources/[^/]+/collections/"
                                   r"descriptions(\?.*)?$"), callback=self.box_route)
            responses.add_callback(
                method, re.compile(re.escape(MEMORIES + "/") + r"[^/?]+(\?.*)?$"),
                callback=self.memory_route)
        return self
