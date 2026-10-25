import json
from urllib.parse import urlsplit

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.memories import Memories

BASE = "https://beta.familysearch.org"
ART = BASE + "/platform/memories/memories"
Q = "?flag=fsh"


class FakeDiscovery:
    def href(self, rel):
        return {"artifacts": ART + Q}[rel]

    def person_links(self, pid):
        return {"artifacts": BASE + f"/platform/tree/persons/{pid}/memories" + Q}


def _mem():
    return Memories(FSClient("tkn", Environment.BETA, min_write_interval=0), FakeDiscovery())


@responses.activate
def test_upload_rejects_oversize():
    with pytest.raises(ValueError):
        _mem().upload(b"x" * (Memories.MAX_BYTES + 1), filename="a.png",
                      content_type="image/png", reason="r")
    assert len(responses.calls) == 0


@responses.activate
def test_upload_rejects_bad_type():
    with pytest.raises(ValueError):
        _mem().upload(b"hi", filename="a.txt", content_type="text/plain", reason="r")
    assert len(responses.calls) == 0


@responses.activate
def test_upload_posts_multipart_and_returns_id():
    responses.add(responses.POST, ART, status=201, headers={"X-ENTITY-ID": "MEM-1"})
    mid = _mem().upload(b"\x89PNGdata", filename="a.png", content_type="image/png",
                        title="Birth cert", reason="r1")
    assert mid == "MEM-1"
    req = responses.calls[0].request
    assert req.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert "x-fs-v1" not in req.headers["Content-Type"]
    assert b'name="artifact"; filename="a.png"' in req.body
    assert b"image/png" in req.body and b"\x89PNGdata" in req.body
    assert "flag=fsh" in req.url and "filename=a.png" in req.url and "title=Birth+cert" in req.url
    assert req.headers["X-Reason"] == "r1"


PERSON_ART = BASE + "/platform/tree/persons/PID-1/memories"


@responses.activate
def test_upload_to_person_rejects_oversize_and_bad_type():
    with pytest.raises(ValueError):
        _mem().upload_to_person("PID-1", b"x" * (Memories.MAX_BYTES + 1), filename="a.png",
                                content_type="image/png", reason="r")
    with pytest.raises(ValueError):
        _mem().upload_to_person("PID-1", b"hi", filename="a.txt",
                                content_type="text/plain", reason="r")
    assert len(responses.calls) == 0


def test_upload_to_person_without_artifacts_link_raises_link_not_found():
    from familysearch import LinkNotFoundError

    class NoLink(FakeDiscovery):
        def person_links(self, pid):
            return {}

    mem = Memories(FSClient("tkn", Environment.BETA, min_write_interval=0), NoLink())
    with pytest.raises(LinkNotFoundError):
        mem.upload_to_person("PID-1", b"x", filename="a.png", content_type="image/png",
                             reason="r")


@responses.activate
def test_upload_to_person_posts_multipart_to_persons_artifacts_link():
    responses.add(responses.POST, PERSON_ART, status=201, headers={
        "X-ENTITY-ID": "MEM-9",
        "Location": BASE + "/platform/tree/persons/PID-1/memory-references/MEM-9-1"})
    mid = _mem().upload_to_person("PID-1", b"\x89PNGdata", filename="a.png",
                                  content_type="image/png", title="Birth cert", reason="r1")
    assert mid == "MEM-9"
    req = responses.calls[0].request
    u = urlsplit(req.url)
    assert u.path == "/platform/tree/persons/PID-1/memories"
    assert "flag=fsh" in u.query and "filename=a.png" in u.query and "title=Birth+cert" in u.query
    assert req.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="artifact"; filename="a.png"' in req.body
    assert b"image/png" in req.body and b"\x89PNGdata" in req.body
    assert req.headers["X-Reason"] == "r1"


@responses.activate
def test_upload_to_person_without_title_omits_title_param():
    responses.add(responses.POST, PERSON_ART, status=201, headers={"X-ENTITY-ID": "MEM-9"})
    _mem().upload_to_person("PID-1", b"x", filename="a.png", content_type="image/png", reason="r")
    assert "title=" not in responses.calls[0].request.url


@responses.activate
def test_link_to_person_posts_persona_with_ark_and_media_description():
    url = ART + "/MEM-1/personas"
    responses.add(responses.POST, url, status=201, headers={"X-ENTITY-ID": "MEM-1-1"})
    assert _mem().link_to_person("PID-1", "MEM-1", reason="r2") is None
    req = responses.calls[0].request
    assert req.url == url  # clean: no query string
    assert json.loads(req.body) == {"persons": [{
        "media": [{"description": ART + "/MEM-1"}],
        "identifiers": {"http://familysearch.org/v1/FamilyTreePerson": [
            BASE + "/ark:/61903/4:1:PID-1"]},
    }]}
    assert req.headers["Content-Type"] == "application/x-fs-v1+json"
    assert req.headers["X-Reason"] == "r2"


def test_memory_url_is_the_url_delete_uses():
    mem = _mem()
    assert mem.memory_url("MEM-1") == ART + "/MEM-1" + Q


@responses.activate
def test_delete():
    responses.add(responses.DELETE, ART + "/MEM-1" + Q, status=204)
    _mem().delete("MEM-1", reason="cleanup")
    req = responses.calls[0].request
    assert req.method == "DELETE" and req.headers["X-Reason"] == "cleanup"
    u = urlsplit(req.url)
    assert u.path.endswith("/MEM-1") and u.query == "flag=fsh"
