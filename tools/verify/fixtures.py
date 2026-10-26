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
import uuid

from familysearch._urls import join_path
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.errors import FSError, FSHTTPError
from familysearch.memories import Memories
from familysearch.models import FACT_BIRTH, build_fact, build_person
from familysearch.sources import Sources
from familysearch.tree import TreeWriter, child_and_parents_url, couple_url

from .core import _require_beta, redact_account
from .teardown import verified_delete

# teardown order: a collection after its member descriptions, so it is empty
KINDS = ("child_and_parents", "couple", "source", "collection", "memory", "person")
COLLECTIONS_PATH = "/platform/sources/collections"
# An unknown collection id: beta answered 400 (API recon), so 400 joins 404/410 here,
# but only together with the folder list not naming the collection.
COLLECTION_GONE = (400, 404, 410)


def collection_body(title: str, *, udcid: str | None = None) -> dict:
    """A user-defined collection (a source folder); with ``udcid`` it updates that one."""
    collection: dict = {"title": title}
    if udcid:
        collection = {"id": udcid, **collection}
    return {"collections": [collection]}


def descriptions_body(sids) -> dict:
    """Source descriptions by id, as added (or moved) to a collection."""
    return {"sourceDescriptions": [{"id": sid} for sid in sids]}


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
                verified_delete(label, url, get, delete, problems)
            self._tracked[kind] = []
        return [redact_account(p) for p in problems]
