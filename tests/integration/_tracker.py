"""Teardown tracker for the MCP beta suite.

There is deliberately no MCP delete tool, so cleanup goes through the library
using the live Context. Everything created is registered the moment it exists;
run() deletes relationships (each kind with its own delete method), sources and memories first and persons last,
tolerating 404/410, and collects anything that could not be removed.
"""
from familysearch import FSHTTPError
from familysearch._urls import join_path

CLEANUP_REASON = "ZZSPIKE automated integration test cleanup"

COUPLE_PATH = "/platform/tree/couple-relationships"
CHILD_AND_PARENTS_PATH = "/platform/tree/child-and-parents-relationships"
MEMORY_PATH = "/platform/memories/memories"
SOURCE_PATH = "/platform/sources/descriptions"


def _gone(get) -> bool:
    """True when the resource 404/410s; False when readable; other errors propagate."""
    try:
        get()
    except FSHTTPError as e:
        if e.status in (404, 410):
            return True
        raise
    return False


def verified_delete(label, get, delete, errors, *, delete_404_ok=False) -> None:
    """Delete a resource and PROVE it is gone. Never blanket-tolerates 404.

    GET first (404/410 => already gone). Otherwise DELETE (a non-2xx is an error),
    then GET again and REQUIRE 404/410. A tolerated 404 once hid a wrong delete URL.
    `get=None` skips the read-back and `delete_404_ok=True` accepts a 404/410 on
    DELETE: used ONLY for source descriptions, whose GET Accept is not established.
    """
    try:
        if get is not None and _gone(get):
            return
        try:
            delete()
        except FSHTTPError as e:
            if not (delete_404_ok and e.status in (404, 410)):
                raise
        if get is not None and not _gone(get):
            errors.append(f"{label}: still readable after delete")
    except Exception as e:  # noqa: BLE001 - record, keep cleaning up
        errors.append(f"{label}: {e}")


class Tracker:
    def __init__(self, ctx):
        self.ctx = ctx
        # Relationship ids by KIND: each kind has its own delete endpoint.
        self.couples: list[str] = []
        self.child_and_parents: list[str] = []
        self.source_ids: list[str] = []
        self.memory_ids: list[str] = []
        self.persons: list[str] = []
        self.errors: list[str] = []

    def _verified(self, label, path, delete, *, verify=True):
        """Verified delete of the resource at base_url + path, via the live Context."""
        def get():
            self.ctx.call(lambda s: s.client.get(s.client.base_url + path))

        def do_delete():
            self.ctx.call(delete)

        verified_delete(label, get if verify else None, do_delete, self.errors,
                        delete_404_ok=not verify)

    def _try(self, fn, label):
        try:
            self.ctx.call(fn)
        except FSHTTPError as e:
            if e.status not in (404, 410):  # persons only: re-read verification follows
                self.errors.append(f"{label}: {e}")
        except Exception as e:  # noqa: BLE001 - keep cleaning up
            self.errors.append(f"{label}: {e}")

    def run(self) -> None:
        for rid in reversed(self.child_and_parents):
            self._verified(
                f"child-and-parents {rid}", join_path(CHILD_AND_PARENTS_PATH, rid),
                lambda s, r=rid: s.writer.delete_child_and_parents(r, reason=CLEANUP_REASON))
        for rid in reversed(self.couples):
            self._verified(
                f"couple {rid}", join_path(COUPLE_PATH, rid),
                lambda s, r=rid: s.writer.delete_couple(r, reason=CLEANUP_REASON))
        for sid in reversed(self.source_ids):
            self._verified(
                f"source {sid}", join_path(SOURCE_PATH, sid),
                lambda s, i=sid: s.client.delete(
                    join_path(s.discovery.href("source-descriptions").split("?")[0], i),
                    reason=CLEANUP_REASON),
                verify=False)
        for mid in reversed(self.memory_ids):
            self._verified(
                f"memory {mid}", join_path(MEMORY_PATH, mid),
                lambda s, m=mid: s.memories.delete(m, reason=CLEANUP_REASON))
        for pid in reversed(self.persons):
            self._try(lambda s, p=pid: s.writer.delete_person(p, reason=CLEANUP_REASON),
                      f"person {pid}")
        # Verify the persons are really gone.
        for pid in self.persons:
            try:
                self.ctx.call(lambda s, p=pid: s.tree.get_person(p))
            except FSHTTPError as e:
                if e.status not in (404, 410):
                    self.errors.append(f"person {pid} still readable after delete: {e}")
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"person {pid} verification failed: {e}")
            else:
                self.errors.append(f"person {pid} still readable after delete")
