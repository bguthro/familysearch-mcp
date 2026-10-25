"""Exercise the REAL auth probe and REAL detect_environment (HTTP mocked)."""

import pytest
import requests
import responses

from familysearch import FSError
from familysearch.client import DEFAULT_USER_AGENT
from familysearch_mcp import context as ctxmod
from familysearch_mcp.context import Context, ContextError

SECRET = "secret-token-value"
BETA = "https://beta.familysearch.org"
PROD = "https://www.familysearch.org"
PATH = "/platform/users/current"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("FS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_ALLOW_PROD_WRITES", raising=False)
    monkeypatch.setattr(ctxmod, "extract_brave_token", lambda: SECRET)


@responses.activate
@pytest.mark.parametrize("status", [200, 401, 403, 500])
def test_probe_request_shape_and_status(status):
    responses.add(responses.GET, BETA + PATH, status=status)
    assert ctxmod._status_of_current_user(SECRET)(BETA) == status
    req = responses.calls[0].request
    assert req.url == BETA + PATH
    assert req.headers["Authorization"] == f"Bearer {SECRET}"
    assert req.headers["Accept"] == "application/x-fs-v1+json"
    assert req.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert not req.headers["User-Agent"].startswith("python-requests")


@responses.activate
def test_probe_does_not_follow_redirects():
    responses.add(responses.GET, BETA + PATH, status=302,
                  headers={"Location": BETA + "/elsewhere"})
    assert ctxmod._status_of_current_user(SECRET)(BETA) == 302
    assert len(responses.calls) == 1


@responses.activate
def test_probe_wraps_request_exceptions():
    responses.add(responses.GET, BETA + PATH,
                  body=requests.ConnectionError(f"boom {SECRET}"))
    with pytest.raises(ContextError) as ei:
        ctxmod._status_of_current_user(SECRET)(BETA)
    msg = str(ei.value)
    assert "beta.familysearch.org" in msg and "ConnectionError" in msg
    assert SECRET not in msg and "Bearer" not in msg


def mock_hosts(beta, prod):
    responses.add(responses.GET, BETA + PATH, status=beta)
    responses.add(responses.GET, PROD + PATH, status=prod)


@responses.activate
def test_build_end_to_end_beta(monkeypatch):
    monkeypatch.setenv("FS_ENVIRONMENT", "beta")
    mock_hosts(beta=200, prod=401)
    assert Context().services().client.base_url == BETA


@responses.activate
def test_build_end_to_end_mismatch(monkeypatch):
    monkeypatch.setenv("FS_ENVIRONMENT", "beta")
    mock_hosts(beta=401, prod=200)
    with pytest.raises(ContextError) as ei:
        Context().services()
    assert "production" in str(ei.value)
    assert SECRET not in str(ei.value)


@responses.activate
def test_build_both_401_says_log_in():
    mock_hosts(beta=401, prod=401)
    with pytest.raises(FSError) as ei:  # library's own error, re-raised as-is
        Context().services()
    assert "log in" in str(ei.value).lower()
    assert SECRET not in str(ei.value)


@responses.activate
def test_build_both_403_reports_block_not_login():
    mock_hosts(beta=403, prod=403)
    with pytest.raises(ContextError) as ei:
        Context().services()
    msg = str(ei.value)
    assert "blocked" in msg.lower() and "403" in msg
    assert "User-Agent" in msg and "edge" in msg.lower()
    assert "log in" not in msg.lower()
    assert SECRET not in msg
