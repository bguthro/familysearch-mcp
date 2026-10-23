import pytest
import responses

from familysearch import client as client_mod
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSHTTPError, TokenExpiredError

BETA = "https://beta.familysearch.org"


@pytest.fixture
def fs():
    return FSClient("tkn", Environment.BETA, min_write_interval=0)


@responses.activate
def test_get_sets_auth_and_ua_headers(fs):
    responses.add(responses.GET, BETA + "/x", json={})
    fs.get("/x")
    sent = responses.calls[0].request.headers
    assert sent["Authorization"] == "Bearer tkn"
    assert sent["User-Agent"]
    assert sent["Accept"] == "application/x-fs-v1+json"


@responses.activate
def test_accept_overridable(fs):
    responses.add(responses.GET, BETA + "/x", json={})
    fs.get("/x", accept="text/html")
    assert responses.calls[0].request.headers["Accept"] == "text/html"


@responses.activate
def test_relative_path_joined_to_base(fs):
    responses.add(responses.GET, BETA + "/platform/users/current", json={})
    fs.get("/platform/users/current")
    assert responses.calls[0].request.url == BETA + "/platform/users/current"


@responses.activate
def test_absolute_url_used_asis(fs):
    responses.add(responses.GET, "https://other.example.com/a", json={})
    fs.get("https://other.example.com/a")
    assert responses.calls[0].request.url == "https://other.example.com/a"


@responses.activate
def test_post_sets_x_reason(fs):
    responses.add(responses.POST, BETA + "/w", json={})
    fs.post("/w", json={"a": 1}, reason="r")
    assert responses.calls[0].request.headers["X-Reason"] == "r"


@responses.activate
def test_401_raises_token_expired(fs):
    responses.add(responses.GET, BETA + "/x", status=401)
    with pytest.raises(TokenExpiredError):
        fs.get("/x")


@responses.activate
def test_4xx_raises_fshttperror_with_body(fs):
    responses.add(responses.GET, BETA + "/x", status=404, json={"err": "nope"})
    with pytest.raises(FSHTTPError) as ei:
        fs.get("/x")
    assert ei.value.status == 404
    assert "nope" in ei.value.body


@responses.activate
def test_write_throttle_enforced(monkeypatch):
    now = [100.0]
    sleeps = []
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))
    responses.add(responses.POST, BETA + "/w", json={})
    fs = FSClient("tkn", Environment.BETA, min_write_interval=0.2)
    fs.post("/w", reason="a")
    assert sleeps == []
    now[0] += 0.05
    fs.post("/w", reason="b")
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.15)
