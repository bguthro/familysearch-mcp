"""Live beta round-trip: the acceptance gate for the write layer.

Every test creates ZZSPIKE throwaway data and removes all of it, even on failure.
"""
import base64
import os
import uuid

import pytest

from familysearch import (
    FACT_BIRTH, FSHTTPError, Memories, Search, Sources, TreeReader, TreeWriter,
    build_fact, build_person,
)

if not (os.environ.get("FS_BETA_TOKEN") or os.environ.get("FS_USE_BRAVE") == "1"):
    pytest.skip("no beta token (FS_BETA_TOKEN or FS_USE_BRAVE=1)", allow_module_level=True)

REASON = "ZZSPIKE automated integration test"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class Cleanup:
    """Tracks created resources; deletes them (best effort) at teardown."""

    def __init__(self, client, writer, memories, discovery):
        self.client, self.writer, self.memories = client, writer, memories
        self.discovery = discovery
        self.relationships: list[str] = []
        self.persons: list[str] = []
        self.memory_ids: list[str] = []
        self.source_urls: list[str] = []
        self.errors: list[str] = []

    def person(self, gender="Male") -> str:
        pid = self.writer.create_person(
            build_person(
                given="ZZSPIKE", surname=f"Test{uuid.uuid4().hex[:6]}",
                gender=gender, living=False,
            ),
            reason=REASON,
        )
        self.persons.append(pid)
        return pid

    def run(self):
        for rid in reversed(self.relationships):
            def _del(r=rid):
                base = self.discovery.href("relationships").split("?")[0].rstrip("/")
                self.client.delete(f"{base}/{r}", reason="spike cleanup")
            self._try(_del, f"relationship {rid}")
        for url in reversed(self.source_urls):
            self._try(lambda u=url: self.client.delete(u, reason=REASON), f"source {url}")
        for mid in reversed(self.memory_ids):
            self._try(lambda m=mid: self.memories.delete(m, reason=REASON), f"memory {mid}")
        for pid in reversed(self.persons):
            self._try(lambda p=pid: self.writer.delete_person(p, reason=REASON), f"person {pid}")

    def _try(self, fn, label):
        try:
            fn()
        except FSHTTPError as e:
            if e.status not in (404, 410):  # already gone is fine
                self.errors.append(f"{label}: {e}")
        except Exception as e:  # noqa: BLE001 - keep cleaning up
            self.errors.append(f"{label}: {e}")


@pytest.fixture
def cleanup(beta_client, discovery):
    c = Cleanup(
        beta_client, TreeWriter(beta_client, discovery), Memories(beta_client, discovery),
        discovery,
    )
    yield c
    c.run()
    assert not c.errors, f"LEFTOVER ZZSPIKE data on beta: {c.errors}"


@pytest.fixture
def reader(beta_client, discovery):
    return TreeReader(beta_client, discovery)


def test_read_current_user(beta_client):
    body = beta_client.get("/platform/users/current").json()
    assert body["users"][0]["id"]


def test_create_addfact_delete_person(cleanup, reader):
    pid = cleanup.person()
    cleanup.writer.add_fact(
        pid, build_fact(FACT_BIRTH, date="1 January 1900", place="Salt Lake City, Utah"),
        reason=REASON,
    )
    person = reader.get_person(pid)
    births = [f for f in person.get("facts", []) if f.get("type") == FACT_BIRTH]
    assert births, f"birth fact missing: {person}"
    assert "changeMessage" in births[0].get("attribution", {}), births[0]
    cleanup.writer.delete_person(pid, reason=REASON)
    with pytest.raises(FSHTTPError) as exc:
        reader.get_person(pid)
    assert exc.value.status in (404, 410)


def test_create_couple_relationship(cleanup):
    a = cleanup.person("Male")
    b = cleanup.person("Female")
    rid = cleanup.writer.create_couple(a, b, reason=REASON)
    cleanup.relationships.append(rid)
    assert rid


def test_create_parent_child_relationship(cleanup):
    child = cleanup.person()
    dad = cleanup.person("Male")
    mom = cleanup.person("Female")
    ids = cleanup.writer.create_parent_child(child_id=child, parent_ids=[dad, mom], reason=REASON)
    cleanup.relationships.extend(i for i in ids if i)
    assert len(ids) == 2 and all(ids)


def test_upload_link_delete_memory(cleanup):
    pid = cleanup.person()
    mid = cleanup.memories.upload(
        PNG, filename="zzspike.png", content_type="image/png",
        title="ZZSPIKE test image", reason=REASON,
    )
    cleanup.memory_ids.append(mid)  # tracked before anything else can fail
    assert mid
    cleanup.memories.link_to_person(pid, mid, reason=REASON)
    cleanup.memories.delete(mid, reason=REASON)
    cleanup.memory_ids.remove(mid)


def test_source_create_and_attach(cleanup, beta_client, discovery):
    pid = cleanup.person()
    sources = Sources(beta_client, discovery)
    sd_id = sources.create_description(
        "https://example.org/zzspike", "ZZSPIKE source", "ZZSPIKE citation", reason=REASON
    )
    sd_url = f"{discovery.href('source-descriptions').rstrip('/')}/{sd_id}"
    cleanup.source_urls.append(sd_url)
    assert sd_id
    sources.attach_to_person(pid, sd_url, reason=REASON)


def test_tree_search_runs(beta_client, discovery):
    out = Search(beta_client, discovery).tree_search(given="John", surname="Smith", count=5)
    assert isinstance(out, list)


def test_record_search_runs(beta_client, discovery):
    out = Search(beta_client, discovery).record_search(given="John", surname="Smith", count=5)
    assert out["total"] >= 0
