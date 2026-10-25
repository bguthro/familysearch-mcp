"""Shared STRICT teardown tracker for BOTH live beta suites.

Everything created is registered the moment it exists. run() deletes relationships,
sources, memories and persons (in that order, each in reverse creation order).

Every tracked object was created in THIS run, so each is verified by read-back:
  1. GET its resource URL: it MUST be readable (else ERROR: wrong URL or vanished);
  2. DELETE the SAME url family via the library method (non-2xx is an ERROR);
  3. GET again: it MUST be 404/410 (else LEFTOVER).
A tolerated 404 once hid a wrong delete URL, so 404 is never accepted blindly. Each
item is handled in its own try so one failure never stops the rest; callers assert
`not tracker.errors` so a failing teardown fails the run loudly.
"""
from familysearch import FSHTTPError
from familysearch._urls import join_path
from familysearch.tree import child_and_parents_url, couple_url

CLEANUP_REASON = "ZZSPIKE automated integration test cleanup"


def _gone(get) -> bool:
    """True when the resource 404/410s; False when readable; other errors propagate."""
    try:
        get()
    except FSHTTPError as e:
        if e.status in (404, 410):
            return True
        raise
    return False


def verified_delete(label, url, get, delete, errors) -> None:
    """GET must succeed, DELETE must be 2xx, then GET must be 404/410."""
    try:
        try:
            get()
        except FSHTTPError as e:
            errors.append(
                f"{label}: tracked object not found at {url} "
                f"(wrong URL or vanished): HTTP {e.status}")
            return
        delete()
        if not _gone(get):
            errors.append(f"{label}: still readable at {url} after delete")
    except Exception as e:  # noqa: BLE001 - record, keep cleaning up
        errors.append(f"{label}: {e}")


class Tracker:
    """`call(fn)` runs fn(services); services has client, discovery, writer, memories."""

    def __init__(self, call):
        self.call = call
        # Relationship ids by KIND: each kind has its own resource/delete endpoint.
        self.couples: list[str] = []
        self.child_and_parents: list[str] = []
        self.source_ids: list[str] = []
        self.memory_ids: list[str] = []
        self.persons: list[str] = []
        self.errors: list[str] = []
        self._deleted: set[str] = set()

    def mark_deleted(self, person_id: str) -> None:
        """A test deleted this person inline (and checked it): skip it at teardown."""
        self._deleted.add(person_id)

    def _verified(self, label, url_of, delete) -> None:
        """url_of(s) -> the ONE resource url used for both GET and (via the library)
        DELETE; delete(s) performs the library delete."""
        try:
            url = self.call(url_of)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{label}: cannot build url: {e}")
            return
        verified_delete(
            label, url,
            lambda: self.call(lambda s: s.client.get(url)),
            lambda: self.call(delete),
            self.errors)

    def run(self) -> None:
        for rid in reversed(self.child_and_parents):
            self._verified(
                f"child-and-parents {rid}",
                lambda s, r=rid: child_and_parents_url(s.client.base_url, r),
                lambda s, r=rid: s.writer.delete_child_and_parents(r, reason=CLEANUP_REASON))
        for rid in reversed(self.couples):
            self._verified(
                f"couple {rid}",
                lambda s, r=rid: couple_url(s.client.base_url, r),
                lambda s, r=rid: s.writer.delete_couple(r, reason=CLEANUP_REASON))
        for sid in reversed(self.source_ids):
            def source_url(s, i=sid):
                return join_path(s.discovery.href("source-descriptions").split("?")[0], i)
            self._verified(
                f"source {sid}", source_url,
                lambda s, i=sid: s.client.delete(source_url(s, i), reason=CLEANUP_REASON))
        for mid in reversed(self.memory_ids):
            self._verified(
                f"memory {mid}",
                lambda s, m=mid: s.memories.memory_url(m),
                lambda s, m=mid: s.memories.delete(m, reason=CLEANUP_REASON))
        for pid in reversed(self.persons):
            if pid in self._deleted:
                continue
            self._verified(
                f"person {pid}",
                lambda s, p=pid: join_path(
                    s.discovery.href("persons").split("?")[0], p),
                lambda s, p=pid: s.writer.delete_person(p, reason=CLEANUP_REASON))
