import browser_cookie3
import pytest

from familysearch.auth import Environment, detect_environment, extract_brave_token
from familysearch.errors import FSError


class FakeCookie:
    def __init__(self, name, domain, value):
        self.name = name
        self.domain = domain
        self.value = value


def test_extract_returns_fssessionid_value(monkeypatch):
    jar = [
        FakeCookie("other", ".familysearch.org", "x"),
        FakeCookie("fssessionid", ".familysearch.org", "tok123"),
    ]
    monkeypatch.setattr(browser_cookie3, "brave", lambda *a, **k: jar)
    assert extract_brave_token() == "tok123"


def test_extract_raises_when_absent(monkeypatch):
    jar = [FakeCookie("other", ".familysearch.org", "x")]
    monkeypatch.setattr(browser_cookie3, "brave", lambda *a, **k: jar)
    with pytest.raises(FSError, match="log"):
        extract_brave_token()


def test_environment_base_urls():
    assert Environment.BETA.base_url == "https://beta.familysearch.org"
    assert Environment.PRODUCTION.base_url == "https://www.familysearch.org"
    assert Environment.BETA.other is Environment.PRODUCTION
    assert Environment.PRODUCTION.other is Environment.BETA


def test_detect_environment_picks_200_host():
    statuses = {
        Environment.PRODUCTION.base_url: 200,
        Environment.BETA.base_url: 401,
    }
    assert detect_environment("t", statuses.__getitem__) is Environment.PRODUCTION


def test_detect_environment_raises_when_none():
    with pytest.raises(FSError):
        detect_environment("t", lambda base_url: 401)
