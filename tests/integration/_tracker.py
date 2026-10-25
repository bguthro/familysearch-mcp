"""Teardown tracker for the MCP beta suite.

There is deliberately no MCP delete tool, so cleanup goes through the library
using the live Context. Everything created is registered the moment it exists;
run() deletes relationships (each kind with its own delete method), sources and memories first and persons last,
tolerating 404/410, and collects anything that could not be removed.
"""
from familysearch import FSHTTPError
from familysearch._urls import join_path

CLEANUP_REASON = "ZZSPIKE automated integration test cleanup"


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

    def _try(self, fn, label):
        try:
            self.ctx.call(fn)
        except FSHTTPError as e:
            if e.status not in (404, 410):  # already gone is fine
                self.errors.append(f"{label}: {e}")
        except Exception as e:  # noqa: BLE001 - keep cleaning up
            self.errors.append(f"{label}: {e}")

    def run(self) -> None:
        for rid in reversed(self.child_and_parents):
            self._try(
                lambda s, r=rid: s.writer.delete_child_and_parents(r, reason=CLEANUP_REASON),
                f"child-and-parents {rid}")
        for rid in reversed(self.couples):
            self._try(
                lambda s, r=rid: s.writer.delete_couple(r, reason=CLEANUP_REASON),
                f"couple {rid}")
        for sid in reversed(self.source_ids):
            self._try(
                lambda s, i=sid: s.client.delete(
                    join_path(s.discovery.href("source-descriptions").split("?")[0], i),
                    reason=CLEANUP_REASON),
                f"source {sid}")
        for mid in reversed(self.memory_ids):
            self._try(lambda s, m=mid: s.memories.delete(m, reason=CLEANUP_REASON),
                      f"memory {mid}")
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
