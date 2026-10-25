"""Offline tests for the live suites' shared STRICT teardown (no network)."""
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))

from _tracker import Tracker, verified_delete  # noqa: E402

from familysearch import Environment, FSClient, FSHTTPError, Memories, TreeWriter  # noqa: E402

BASE = "https://beta.familysearch.org"


class Res:
    """A scripted resource: GET/DELETE callables."""

    def __init__(self, exists=True, delete_status=None, survives=False):
        self.exists, self.delete_status, self.survives = exists, delete_status, survives
        self.deletes = 0

    def get(self):
        if not self.exists:
            raise FSHTTPError(404, "gone")

    def delete(self):
        self.deletes += 1
        if self.delete_status:
            raise FSHTTPError(self.delete_status, "x")
        if not self.survives:
            self.exists = False


def run(r):
    errors = []
    verified_delete("thing", "https://x/y", r.get, r.delete, errors)
    return errors


def test_happy_path():
    r = Res()
    assert run(r) == [] and r.deletes == 1


def test_pre_delete_404_on_tracked_object_is_error_and_not_deleted():
    r = Res(exists=False)
    errs = run(r)
    assert len(errs) == 1 and "https://x/y" in errs[0] and "wrong URL" in errs[0]
    assert r.deletes == 0


def test_delete_then_still_readable_is_leftover():
    errs = run(Res(survives=True))
    assert len(errs) == 1 and "still readable" in errs[0]


@pytest.mark.parametrize("status", [404, 500, 403])
def test_delete_non_2xx_is_error(status):
    errs = run(Res(delete_status=status))
    assert len(errs) == 1 and str(status) in errs[0]


def test_unexpected_get_error_recorded():
    def bad():
        raise FSHTTPError(500, "boom")
    errors = []
    verified_delete("t", "u", bad, lambda: None, errors)
    assert len(errors) == 1


# ---- the Tracker, driven by the REAL library delete methods over a fake server ----

class FakeServer:
    """Resources by URL (query ignored). Records every GET/DELETE url."""

    def __init__(self):
        self.live = set()
        self.gets, self.deletes = [], []
        self.base_url = BASE
        self.sticky = set()

    @staticmethod
    def key(url):
        u = urlsplit(url)
        return u.scheme + "://" + u.netloc + u.path

    def add(self, url):
        self.live.add(self.key(url))

    def get(self, url, **kw):
        self.gets.append(url)
        if self.key(url) not in self.live:
            raise FSHTTPError(404, "nope")
        return SimpleNamespace(status_code=200)

    def delete(self, url, *, reason, **kw):
        self.deletes.append(url)
        k = self.key(url)
        if k not in self.live:
            raise FSHTTPError(404, "nope")
        if k not in self.sticky:
            self.live.discard(k)


class FakeDiscovery:
    def href(self, rel):
        return {
            "persons": BASE + "/platform/tree/persons?flag=fsh",
            "relationships": BASE + "/platform/tree/relationships?flag=fsh",
            "artifacts": BASE + "/platform/memories/memories?flag=fsh",
            "source-descriptions": BASE + "/platform/sources/descriptions?flag=fsh",
        }[rel]


def make_tracker():
    srv = FakeServer()
    disc = FakeDiscovery()
    client = FSClient("t", Environment.BETA, min_write_interval=0)
    srv.base_url = client.base_url
    ns = SimpleNamespace(
        client=srv, discovery=disc, writer=TreeWriter(srv, disc),
        memories=Memories(srv, disc), tree=None)
    return Tracker(lambda fn: fn(ns)), srv


def test_tracker_get_and_delete_use_same_resource_for_every_kind():
    t, srv = make_tracker()
    P = BASE + "/platform/tree/persons/"
    for url in (BASE + "/platform/tree/couple-relationships/R1",
                BASE + "/platform/tree/child-and-parents-relationships/C1",
                BASE + "/platform/memories/memories/M1",
                BASE + "/platform/sources/descriptions/S1", P + "A", P + "B"):
        srv.add(url)
    t.couples.append("R1")
    t.child_and_parents.append("C1")
    t.memory_ids.append("M1")
    t.source_ids.append("S1")
    t.persons.extend(["A", "B"])
    t.run()
    assert t.errors == [] and srv.live == set()
    assert len(srv.deletes) == 6
    got = {FakeServer.key(u) for u in srv.gets}
    assert {FakeServer.key(u) for u in srv.deletes} <= got


def test_tracker_wrong_delete_url_is_not_hidden():
    """A delete that leaves the resource readable is reported as a leftover."""
    t, srv = make_tracker()
    srv.add(BASE + "/platform/tree/couple-relationships/R1")
    srv.sticky.add(BASE + "/platform/tree/couple-relationships/R1")
    t.couples.append("R1")
    t.run()
    assert any("still readable" in e for e in t.errors)


def test_tracker_missing_tracked_object_is_error():
    t, srv = make_tracker()
    t.memory_ids.append("GHOST")
    t.run()
    assert len(t.errors) == 1 and "GHOST" in t.errors[0] and srv.deletes == []


def test_tracker_mark_deleted_person_is_skipped():
    t, srv = make_tracker()
    t.persons.append("A")
    t.mark_deleted("A")
    t.run()
    assert t.errors == [] and srv.gets == [] and srv.deletes == []


def test_one_failure_does_not_stop_the_rest():
    t, srv = make_tracker()
    srv.add(BASE + "/platform/memories/memories/M2")
    t.memory_ids.extend(["M2", "GHOST"])  # GHOST runs first (reversed) and fails
    t.run()
    assert len(t.errors) == 1 and srv.live == set()
