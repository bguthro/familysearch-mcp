"""Throwaway ZZSPIKE fixtures on beta, tracked the moment they exist and torn down strictly.

Teardown deletes relationships (child-and-parents, then couples), sources, memories
and persons, each in reverse creation order, with the same resource URLs and library
delete methods as the live integration suites (tests/integration/_tracker.py).
"""
import uuid

from familysearch._urls import join_path
from familysearch.client import FSClient
from familysearch.discovery import Discovery
from familysearch.memories import Memories
from familysearch.models import FACT_BIRTH, build_fact, build_person
from familysearch.tree import TreeWriter, child_and_parents_url, couple_url

from .core import _require_beta
from .teardown import verified_delete

KINDS = ("child_and_parents", "couple", "source", "memory", "person")  # teardown order


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
        self, given: str = "Test", gender: str = "Male", birth_year: str | None = "1850"
    ) -> str:
        """Create a deceased ZZSPIKE person and track it before anything else runs."""
        facts = [build_fact(FACT_BIRTH, date=birth_year)] if birth_year else None
        pid = self._writer.create_person(
            build_person(
                given=f"ZZSPIKE {given}", surname=f"Test{uuid.uuid4().hex[:8]}",
                gender=gender, living=False, facts=facts,
            ),
            reason=self._reason,
        )
        self.track("person", pid)
        return pid

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
                verified_delete(
                    label, url, lambda u=url: self._client.get(u), delete, problems)
            self._tracked[kind] = []
        return problems
