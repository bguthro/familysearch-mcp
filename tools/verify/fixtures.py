"""Throwaway ZZSPIKE fixtures on beta, tracked the moment they exist and torn down strictly.

Teardown deletes relationships (child-and-parents, then couples), sources, user-defined
collections (source folders: after their member descriptions, so each is empty), memories
and persons, each in reverse creation order, with the same resource URLs and library
delete methods as the live integration suites (tests/integration/_tracker.py).

A collection has no hand-written library call: it is made with the raw client (as the
read world posts notes), with the bodies below, which the claimed source box writes
send too. Its "gone" check has two signals, since beta answers an unknown collection
id 400 ("Unable to read collection", API recon), not 404: the read errors and the
user's folder list (``/sources/collections``) no longer names it.
"""
import base64
import json
import struct
import uuid
import zlib

from familysearch._urls import join_path
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError, FSHTTPError
from familysearch.memories import Memories
from familysearch.models import FACT_BIRTH, build_fact, build_person
from familysearch.sources import Sources
from familysearch.tree import TreeWriter, child_and_parents_url, couple_url

from .core import _require_beta, redact_account
from .teardown import gone_after, verified_delete

# teardown order: a collection after its member descriptions, so it is empty; the private
# (Genealogies) persons and sources before the tree that holds them
KINDS = ("child_and_parents", "couple", "source", "collection", "memory", "person",
         "genealogies_person", "genealogies_source", "genealogies_tree")
COLLECTIONS_PATH = "/platform/sources/collections"
GENEALOGIES_PATH = "/platform/genealogies"
# A private object's delete may show late: (attempts, seconds between) of the gone-check.
GENEALOGIES_GONE_POLLS = (5, 3.0)
# fixture kind -> the collection segment of its resource under /genealogies
GENEALOGIES_KINDS = {"genealogies_person": "persons", "genealogies_source": "sources",
                     "genealogies_tree": "trees"}
# An unknown collection id: beta answered 400 (API recon), so 400 joins 404/410 here,
# but only together with the folder list not naming the collection.
COLLECTION_GONE = (400, 404, 410)
PNG = base64.b64decode(  # a 1x1 PNG, as deep.py uploads
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5E"
    "rkJggg==")
PNG_NAME = "zzspike.png"


def unique_png() -> bytes:
    """A valid 1x1 PNG whose bytes differ on every call (a fresh random ``tEXt`` chunk).

    Beta rejects an upload whose content it already holds (HTTP 409 "Unable to upload
    artifact", P5 live run 1), so no two fixture uploads may share bytes.
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00" + bytes(uuid.uuid4().bytes[:3]))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel)
            + chunk(b"tEXt", b"Comment\x00ZZSPIKE" + uuid.uuid4().hex.encode())
            + chunk(b"IEND", b""))


def collection_body(title: str, *, udcid: str | None = None) -> dict:
    """A user-defined collection (a source folder); with ``udcid`` it updates that one."""
    collection: dict = {"title": title}
    if udcid:
        collection = {"id": udcid, **collection}
    return {"collections": [collection]}


def descriptions_body(sids) -> dict:
    """Source descriptions by id, as added (or moved) to a collection."""
    return {"sourceDescriptions": [{"id": sid} for sid in sids]}


def comments_body(text: str) -> dict:
    """A comment to add to a memory: the discussion shape readMemoryComments answers
    (``discussions`` holding ``comments``). Not proven on beta before P5."""
    return {"discussions": [{"comments": [{"text": text}]}]}


RECTANGLE_REGION = "http://gedcomx.org/RectangleRegion"


def portrait_body(memory_url: str, pid: str) -> dict:
    """A portrait: a person with a media reference to the memory carrying a GEDCOM X
    RectangleRegion (beta's 400: "To create a portrait, provide a person with a media
    reference that contains a Gedcomx RectangleRegion for the tag and ..."; the full
    message and the qualifier's value format are unverified, the whole image is used)."""
    return {"persons": [{"id": pid, "media": [{"description": memory_url, "qualifiers": [
        {"name": RECTANGLE_REGION, "value": "0.0,0.0,1.0,1.0"}]}]}]}


def created_header_id(headers) -> str | None:
    """A create's id: X-ENTITY-ID, Entity-Id (the createSourceFolder spec's name), else
    the Location's last segment."""
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    for name in ("x-entity-id", "entity-id"):
        if lowered.get(name):
            return lowered[name]
    location = (lowered.get("location") or "").split("?")[0].split("#")[0].rstrip("/")
    return location.rsplit("/", 1)[-1] or None if location else None


def folder_titles(body) -> dict[str, str | None]:
    """The user's folders in a ``collections`` answer: id -> title (None: untitled)."""
    collections = body.get("collections") if isinstance(body, dict) else None
    return {str(c["id"]): c.get("title") for c in collections or []
            if isinstance(c, dict) and c.get("id")}


# ------------------------------------------------------------------ private trees
# The Genealogies API (/genealogies/**) is the one place a LIVING fictional person may be
# written: the shared Family Tree never gets one. Every helper below posts only under a
# tree this run created and tracked. A tree is created with a source description title
# (live: 201, id ``2:2:2:<id>`` in Location and X-ENTITY-ID); a ``collections`` title was
# not the working body.
TREE_SHAPES = ("sourceDescriptions",)
FACT_COUPLE = "http://gedcomx.org/Couple"


def tree_body(title: str, shape: str = TREE_SHAPES[0]) -> dict:
    """A private tree named ``title``, in one of the ``TREE_SHAPES``."""
    if shape == "collections":
        return {"collections": [{"title": title}]}
    if shape == "sourceDescriptions":
        return {"sourceDescriptions": [{"titles": [{"value": title}]}]}
    raise ValueError(f"unknown tree body shape {shape!r}")


def create_tree(call, title: str):
    """Send the tree create with each body shape in turn until one is not refused 400.

    ``call(body)`` posts one; returns ``(result, shape)``. A 400 created nothing, so the
    next shape is safe to try; any other error, and the last 400, propagate.
    """
    for i, shape in enumerate(TREE_SHAPES):
        try:
            return call(tree_body(title, shape)), shape
        except FSHTTPError as e:
            if e.status != 400 or i == len(TREE_SHAPES) - 1:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def living_person_body(given: str, surname: str, gender: str = "Male", *,
                       facts: list[dict] | None = None, local_id: str | None = None) -> dict:
    """A LIVING fictional person, for the private space only (never ``/tree/persons``)."""
    person = build_person(given=given, surname=surname, gender=gender, living=True,
                          facts=facts)
    if local_id:
        person = {"id": local_id, **person}
    return {"persons": [person]}


def genealogies_description_body(about: str, title: str, citation: str) -> dict:
    return {"sourceDescriptions": [{"about": about, "titles": [{"value": title}],
                                    "citations": [{"value": citation}]}]}


def couple_tree_body(male: dict, female: dict) -> dict:
    """A tree update adding a male and a female person (local ids ``m1`` / ``f1``) and the
    Couple joining them (the spec: ``person1`` must be male, ``person2`` female)."""
    ref = lambda local: {"resource": f"#{local}", "resourceId": local}  # noqa: E731
    return {"persons": [{"id": "m1", **male}, {"id": "f1", **female}],
            "relationships": [{"type": FACT_COUPLE, "person1": ref("m1"),
                               "person2": ref("f1")}]}


def tree_entry(body, tid: str) -> dict | None:
    """The ``collections`` entry that is tree ``tid``, or None.

    Live (readGenealogiesTree): ``{"collections": [{"title", "size", "content": [{"count",
    "resourceType": Person}], "identifiers": {Persistent: [ark url ending in the tree
    id]}, ...}], "description": "#t_NNN", "sourceDescriptions": [public collections]}``:
    the entry carries no ``id``, so it is the one whose identifiers name ``tid`` (or whose
    ``id``, if one is answered, is it). ``sourceDescriptions`` are not the tree.
    """
    found = [c for c in (body.get("collections") if isinstance(body, dict) else None) or []
             if isinstance(c, dict) and (
                 same_id(str(c["id"]), tid) if c.get("id")
                 else any(same_id(u.rstrip("/").rsplit("/", 1)[-1], tid)
                          for urls in (c.get("identifiers") or {}).values()
                          for u in (urls if isinstance(urls, list) else [])
                          if isinstance(u, str)))]
    return found[0] if len(found) == 1 else None


def tree_size(entry: dict):
    """The number of persons a tree entry reports: ``size``, else ``content[0].count``."""
    if isinstance(entry.get("size"), int):
        return entry["size"]
    content = entry.get("content")
    first = content[0] if isinstance(content, list) and content else None
    return first.get("count") if isinstance(first, dict) else None


def local_part(oid: str) -> str:
    """The id without its ``2:2:2:`` style prefix and without a local-id tag (live: the
    source ``2:2:286671`` is answered as ``s_286671``)."""
    tail = str(oid).rsplit(":", 1)[-1]
    head, sep, rest = tail.partition("_")
    return rest if sep and head.isalpha() and len(head) <= 3 else tail


def same_id(a: str, b: str) -> bool:
    """Ids equal, or equal once the prefix and any local-id tag are dropped."""
    return a == b or local_part(a) == local_part(b)


def entry_title(entry: dict) -> str | None:
    """A tree entry's title: ``title``, else ``name``, else the first of ``titles``."""
    for key in ("title", "name"):
        if isinstance(entry.get(key), str):
            return entry[key]
    titles = entry.get("titles")
    first = titles[0] if isinstance(titles, list) and titles else None
    return first.get("value") if isinstance(first, dict) else None


def created_persons(body, local_ids) -> dict[str, str]:
    """The persons a tree update created: local id -> server id.

    Live (updateGenealogiesTree, 200): the persons come back as ``sourceDescriptions``
    keyed by the LOCAL ids posted (``m1``), each with the real id as the tail of its
    ark ``identifiers`` URL (``.../ark:/61903/2:2:MSGD-Z4F``). A ``persons`` entry whose
    id is not a local one is taken as the server id too.
    """
    found: dict[str, str] = {}
    if not isinstance(body, dict):
        return found
    for desc in body.get("sourceDescriptions") or []:
        if not (isinstance(desc, dict) and desc.get("id") in local_ids):
            continue
        for urls in (desc.get("identifiers") or {}).values():
            for url in urls if isinstance(urls, list) else []:
                if isinstance(url, str) and url.rstrip("/").rsplit("/", 1)[-1]:
                    found.setdefault(desc["id"], url.rstrip("/").rsplit("/", 1)[-1])
    for person in body.get("persons") or []:
        if isinstance(person, dict) and person.get("id") and person["id"] not in local_ids:
            found.setdefault(f"#{len(found)}", str(person["id"]))
    return found


class FixtureSet:
    def __init__(self, client: FSClient, *, reason: str):
        if not (isinstance(reason, str) and reason.strip()):
            raise ValueError("a non-empty reason is required")
        # TreeWriter does not consult the lock: this guard is the only one on this path.
        _require_beta(client)
        self._client = client
        self._reason = reason
        self._discovery = Discovery(client)
        self._writer = TreeWriter(client, self._discovery)
        self._memories = Memories(client, self._discovery)
        self._sources = Sources(client, self._discovery)
        self._tracked: dict[str, list[str]] = {k: [] for k in KINDS}
        self._deleted: set[tuple[str, str]] = set()

    def _kind(self, kind: str) -> str:
        if kind not in KINDS:
            raise ValueError(f"unknown fixture kind {kind!r}; expected one of {KINDS}")
        return kind

    def track(self, kind: str, object_id: str) -> None:
        if not (isinstance(object_id, str) and object_id.strip()):
            raise ValueError("object_id must be a non-empty string")
        self._tracked[self._kind(kind)].append(object_id)

    def mark_deleted(self, kind: str, object_id: str) -> None:
        """The recipe deleted (and checked) this object itself: skip it at teardown."""
        self._deleted.add((self._kind(kind), object_id))

    def tracked(self, kind: str) -> list[str]:
        return list(self._tracked[self._kind(kind)])

    def person(
        self, given: str = "Test", gender: str = "Male", birth_year: str | None = "1850",
        *, facts: list[dict] | None = None,
    ) -> str:
        """Create a deceased ZZSPIKE person and track it before anything else runs.

        ``facts`` are created with it, after the birth.
        """
        born = [build_fact(FACT_BIRTH, date=birth_year)] if birth_year else []
        pid = self._writer.create_person(
            build_person(
                given=f"ZZSPIKE {given}", surname=f"Test{uuid.uuid4().hex[:8]}",
                gender=gender, living=False, facts=[*born, *(facts or [])] or None,
            ),
            reason=self._reason,
        )
        self.track("person", pid)
        return pid

    def source(self, title: str) -> str:
        """Create a ZZSPIKE source description (as the read world does) and track it."""
        if not (isinstance(title, str) and title.startswith("ZZSPIKE")):
            raise ValueError("a fixture source's title must start with ZZSPIKE")
        sid = self._sources.create_description(
            f"https://example.org/{title.lower()}", title, f"{title} citation",
            reason=self._reason)
        self.track("source", sid)
        return sid

    def memory(self, title: str, *, person: str | None = None) -> str:
        """Upload a tiny ZZSPIKE PNG memory (the proven ``Memories`` calls) and track it.

        Standalone it is floating, attached to nobody; with ``person`` (a tracked
        fixture) it is uploaded to that person, who gets a persona of it.
        """
        if not (isinstance(title, str) and title.startswith("ZZSPIKE")):
            raise ValueError("a fixture memory's title must start with ZZSPIKE")
        if person is None:
            mid = self._memories.upload(unique_png(), filename=PNG_NAME, content_type="image/png",
                                        title=title, reason=self._reason)
        else:
            self._own_persons(person)
            mid = self._memories.upload_to_person(
                person, unique_png(), filename=PNG_NAME, content_type="image/png", title=title,
                reason=self._reason)
        self.track("memory", mid)
        return mid

    def _own_memory(self, mid: str) -> None:
        if mid not in self._tracked["memory"]:
            raise ValueError("memory fixtures touch only this run's tracked memories")

    def memory_url(self, mid: str) -> str:
        """The memory resource URL (no query), as a persona or portrait names it."""
        self._own_memory(mid)
        return self._memories.memory_url(mid).split("?")[0]

    def link_memory(self, mid: str, pid: str) -> None:
        """Link a tracked memory to a tracked person (``Memories.link_to_person``: a
        persona with the person's ark; read-back proven on beta). The persona goes with
        the memory."""
        self._own_memory(mid)
        self._own_persons(pid)
        self._memories.link_to_person(pid, mid, reason=self._reason)

    def memory_comment(self, mid: str, text: str) -> None:
        """Add a comment to a tracked memory (raw client; it goes with the memory)."""
        if not (isinstance(text, str) and text.startswith("ZZSPIKE")):
            raise ValueError("a fixture comment must start with ZZSPIKE")
        self._client.post(join_path(self.memory_url(mid), "comments"),
                          json=comments_body(text), reason=self._reason)

    def set_portrait(self, pid: str, mid: str) -> None:
        """Select a tracked memory as a tracked person's portrait (raw client; the
        selection goes with the person)."""
        self._own_persons(pid)
        url = join_path(join_path(self._collection("persons"), pid), "portraits")
        self._client.post(url, json=portrait_body(self.memory_url(mid), pid),
                          reason=self._reason)

    def collection(self, title: str) -> str:
        """Create a ZZSPIKE user-defined collection with the raw client and track it."""
        if not (isinstance(title, str) and title.startswith("ZZSPIKE")):
            raise ValueError("a fixture collection's title must start with ZZSPIKE")
        before = self.folders()
        resp = self._client.post(self.collection_url(), json=collection_body(title),
                                 reason=self._reason)
        udcid = created_header_id(resp.headers)
        if udcid:
            self.track("collection", udcid)
            return udcid
        # No id answered: the new folders under this title are ours; track them all.
        new = [u for u, t in self.folders().items() if u not in before and t == title]
        for found in new:
            self.track("collection", found)
        if len(new) != 1:
            raise FSError(f"collection create answered no id and {len(new)} new folders "
                          f"are titled {title}")
        return new[0]

    def collection_url(self, udcid: str | None = None) -> str:
        base = self._client.base_url + COLLECTIONS_PATH
        return join_path(base, udcid) if udcid else base

    def add_to_collection(self, udcid: str, sids: list[str]) -> None:
        """Move tracked sources into a tracked collection (raw client; only this run's)."""
        if (udcid not in self._tracked["collection"] or not sids
                or any(s not in self._tracked["source"] for s in sids)):
            raise ValueError("add_to_collection moves only this run's tracked fixtures")
        self._client.post(join_path(self.collection_url(udcid), "descriptions"),
                          json=descriptions_body(sids), reason=self._reason)

    def folders(self) -> dict[str, str | None]:
        """The user's folder list (``/sources/collections``, which redirects to the
        signed-in user's): id -> title. 204 means no folders."""
        resp = self._client.get(self.collection_url())
        if resp.status_code == 204 or not resp.text.strip():
            return {}
        return folder_titles(resp.json())

    def folder_listed(self, udcid: str) -> bool:
        """Does the user's folder list name the collection?"""
        return udcid in self.folders()

    def _collection_get(self, udcid: str, url: str):
        """GET the collection; a 400 counts as gone only when the folder list agrees."""
        def get():
            try:
                return self._client.get(url)
            except FSHTTPError as e:
                if e.status in COLLECTION_GONE and not self.folder_listed(udcid):
                    raise FSHTTPError(404, "", f"collection {udcid} answers {e.status} and "
                                      "is not in the folder list") from None
                raise
        return get

    def source_url(self, sid: str) -> str:
        """The description's resource URL, as a person's source reference names it."""
        return join_path(self._collection("source-descriptions"), sid)

    def attach_source(self, pid: str, sid: str) -> None:
        """Attach a tracked source to a tracked person (``Sources.attach_to_person``).

        Only this run's fixtures: the reference goes with the person at teardown.
        """
        if pid not in self._tracked["person"] or sid not in self._tracked["source"]:
            raise ValueError("attach_source attaches only this run's tracked fixtures")
        self._sources.attach_to_person(pid, self.source_url(sid), reason=self._reason)

    def _own_persons(self, *pids: str) -> None:
        if not pids or any(p not in self._tracked["person"] for p in pids):
            raise ValueError("relationship fixtures join only this run's tracked persons")

    def person_ref(self, pid: str) -> dict:
        """A tracked person as a relationship names it (clean URL + id, as the library)."""
        self._own_persons(pid)
        return {"resource": join_path(self._collection("persons"), pid), "resourceId": pid}

    def couple(self, person1: str, person2: str) -> str:
        """A couple of two tracked persons (``TreeWriter.create_couple``), tracked at once."""
        self._own_persons(person1, person2)
        rid = self._writer.create_couple(person1, person2, reason=self._reason)
        self.track("couple", rid)
        return rid

    def child_and_parents(self, child: str, parents: list[str]) -> str:
        """ONE child-and-parents relationship (``TreeWriter.create_parent_child``), tracked.

        Beta derives a couple for two parents that have none, which nothing here would
        track: give two parents a ``couple`` first.
        """
        self._own_persons(child, *parents)
        rid = self._writer.create_parent_child(child_id=child, parent_ids=list(parents),
                                               reason=self._reason)
        self.track("child_and_parents", rid)
        return rid

    # ---------------------------------------------------------------- private trees

    def genealogies_url(self, *segments: str) -> str:
        """A resource of the Genealogies (private tree) API; segments are quoted."""
        return join_path(self._client.base_url + GENEALOGIES_PATH, *segments)

    def _own_tree(self, tid: str) -> None:
        if tid not in self._tracked["genealogies_tree"]:
            raise ValueError("private tree fixtures live only in this run's tracked trees")

    def genealogies_tree(self, title: str) -> str:
        """Create a ZZSPIKE private tree with the raw client and track it at once.

        The id is the create's X-ENTITY-ID / Entity-Id / Location.
        """
        if not (isinstance(title, str) and title.startswith("ZZSPIKE")):
            raise ValueError("a fixture tree's title must start with ZZSPIKE")
        resp, _shape = create_tree(
            lambda body: self._client.post(self.genealogies_url("trees"), json=body,
                                           reason=self._reason), title)
        tid = created_header_id(resp.headers)
        if not tid:
            raise FSError("tree created but no id was returned")
        self.track("genealogies_tree", tid)
        return tid

    def genealogies_person(self, tid: str, given: str = "Test", gender: str = "Male", *,
                           facts: list[dict] | None = None, surname: str | None = None,
                           plain: bool = False) -> str:
        """Create a LIVING ZZSPIKE person in a tracked private tree; track it at once.

        The name is ``ZZSPIKE {given} Test<hex>`` unless ``surname`` is given; with
        ``plain`` ``given`` is used as is (it must already start with ZZSPIKE).
        """
        self._own_tree(tid)
        if plain and not given.startswith("ZZSPIKE"):
            raise ValueError("a fixture person's given name must start with ZZSPIKE")
        body = living_person_body(given if plain else f"ZZSPIKE {given}",
                                  surname or f"Test{uuid.uuid4().hex[:8]}", gender,
                                  facts=facts, local_id="p1")
        resp = self._client.post(self.genealogies_url("trees", tid, "persons"), json=body,
                                 reason=self._reason)
        pid = created_header_id(resp.headers)
        if not pid:
            raise FSError("private person created but no id was returned")
        self.track("genealogies_person", pid)
        return pid

    def genealogies_source(self, tid: str, title: str) -> str:
        """Create a ZZSPIKE source description in a tracked private tree; track it."""
        self._own_tree(tid)
        if not (isinstance(title, str) and title.startswith("ZZSPIKE")):
            raise ValueError("a fixture source's title must start with ZZSPIKE")
        resp = self._client.post(
            self.genealogies_url("trees", tid, "sources"),
            json=genealogies_description_body(f"https://example.org/{title.lower()}", title,
                                              f"{title} citation"), reason=self._reason)
        sdid = created_header_id(resp.headers)
        if not sdid:
            raise FSError("private source created but no id was returned")
        self.track("genealogies_source", sdid)
        return sdid

    def genealogies_couple(self, tid: str, marker: str) -> tuple[str, str]:
        """A living male and female ZZSPIKE person joined by a Couple, added to a tracked
        private tree with one tree update. Returns the two ids, both tracked at once.

        The ids are the update's answer (``persons`` ids that are not the local ones),
        else the two new persons found by ``marker`` in the tree's person list.
        """
        self._own_tree(tid)
        given = {"m1": f"ZZSPIKE {marker}M", "f1": f"ZZSPIKE {marker}F"}
        surname = f"Test{uuid.uuid4().hex[:8]}"
        male = living_person_body(given["m1"], surname, "Male")["persons"][0]
        female = living_person_body(given["f1"], surname, "Female")["persons"][0]
        resp = self._client.post(self.genealogies_url("trees", tid),
                                 json=couple_tree_body(male, female), reason=self._reason)
        body = resp.json() if resp.text.strip() else {}
        made = created_persons(body, {"m1", "f1"})
        for pid in made.values():
            self.track("genealogies_person", pid)
        if set(made) != {"m1", "f1"}:
            raise FSError(f"the couple update made {len(made)} persons, not 2; the update "
                          f"answered {resp.status_code} {(resp.text or '')[:300]!r}")
        return made["m1"], made["f1"]

    def _collection(self, rel: str) -> str:
        href = self._discovery.href(rel).split("?")[0]
        return href if href.startswith("http") else self._client.base_url + href

    def _url_and_delete(self, kind: str, oid: str):
        reason = self._reason
        if kind == "child_and_parents":
            return (child_and_parents_url(self._client.base_url, oid),
                    lambda: self._writer.delete_child_and_parents(oid, reason=reason))
        if kind == "couple":
            return (couple_url(self._client.base_url, oid),
                    lambda: self._writer.delete_couple(oid, reason=reason))
        if kind == "source":
            url = join_path(self._collection("source-descriptions"), oid)
            return url, lambda: self._client.delete(url, reason=reason)
        if kind == "collection":
            url = self.collection_url(oid)
            return url, lambda: self._client.delete(url, reason=reason)
        if kind == "memory":
            return (self._memories.memory_url(oid),
                    lambda: self._memories.delete(oid, reason=reason))
        if kind in GENEALOGIES_KINDS:
            url = self.genealogies_url(GENEALOGIES_KINDS[kind], oid)

            def delete_private():
                self._client.delete(url, reason=reason)
                if kind != "genealogies_source" or gone_after(
                        lambda: self._client.get(url), GENEALOGIES_GONE_POLLS):
                    return
                # Live run 2: private sources stayed readable after the documented
                # delete. Their reads answer a local id (s_<n> for 2:2:<n>): try the
                # delete by that form once; the strict gone-check then decides.
                local = "s_" + oid.rsplit(":", 1)[-1]
                try:
                    self._client.delete(self.genealogies_url("sources", local),
                                        reason=reason)
                except FSHTTPError:
                    pass
            return url, delete_private
        return (join_path(self._collection("persons"), oid),
                lambda: self._writer.delete_person(oid, reason=reason))

    def teardown(self) -> list[str]:
        """Delete everything tracked; return the problems (empty means clean)."""
        problems: list[str] = []
        for kind in KINDS:
            for oid in reversed(self._tracked[kind]):
                if (kind, oid) in self._deleted:
                    continue
                label = f"{kind} {oid}"
                try:
                    url, delete = self._url_and_delete(kind, oid)
                except Exception as e:  # noqa: BLE001 - record, keep cleaning up
                    problems.append(f"{label}: cannot build url: {e}")
                    continue
                get = (self._collection_get(oid, url) if kind == "collection"
                       else (lambda u=url: self._client.get(u)))
                verified_delete(label, url, get, delete, problems,
                                GENEALOGIES_GONE_POLLS if kind in GENEALOGIES_KINDS
                                else (1, 0.0))
            self._tracked[kind] = []
        return [redact_account(p) for p in problems]
