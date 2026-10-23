import json

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.memories import Memories

BASE = "https://beta.familysearch.org"
ART = BASE + "/platform/memories/memories"


class FakeDiscovery:
    def href(self, rel):
        return {"artifacts": ART}[rel]


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
    assert b'name="artifact"; filename="a.png"' in req.body
    assert b"image/png" in req.body and b"\x89PNGdata" in req.body
    assert "filename=a.png" in req.url and "title=Birth+cert" in req.url
    assert req.headers["X-Reason"] == "r1"


@responses.activate
def test_link_to_person_posts_persona():
    url = ART + "/MEM-1/personas"
    responses.add(responses.POST, url, status=204)
    _mem().link_to_person("PID-1", "MEM-1", reason="r2")
    req = responses.calls[0].request
    assert req.url == url
    assert json.loads(req.body) == {"persons": [{"id": "PID-1"}]}
    assert req.headers["X-Reason"] == "r2"


@responses.activate
def test_delete():
    responses.add(responses.DELETE, ART + "/MEM-1", status=204)
    _mem().delete("MEM-1")
    assert responses.calls[0].request.method == "DELETE"
