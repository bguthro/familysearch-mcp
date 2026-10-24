import pytest

from familysearch import Environment, FSError, TokenExpiredError
from familysearch_mcp import context as ctxmod
from familysearch_mcp.context import Context, ContextError

SECRET = "secret-token-value"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("FS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_ALLOW_PROD_WRITES", raising=False)


@pytest.fixture
def lifted(monkeypatch):
    """Patch token lifting; return the list recording each lift."""
    calls = []

    def fake_extract():
        calls.append(1)
        return SECRET

    monkeypatch.setattr(ctxmod, "extract_brave_token", fake_extract)
    return calls


def detect_as(monkeypatch, env):
    monkeypatch.setattr(ctxmod, "detect_environment", lambda token, http_get: env)


def test_default_environment_is_production():
    assert Context().environment is Environment.PRODUCTION


def test_unknown_environment_rejected(monkeypatch):
    monkeypatch.setenv("FS_ENVIRONMENT", "staging")
    with pytest.raises(ContextError):
        Context()


def test_context_error_is_fserror():
    assert issubclass(ContextError, FSError)


def test_environment_case_insensitive(monkeypatch):
    monkeypatch.setenv("FS_ENVIRONMENT", "BETA")
    assert Context().environment is Environment.BETA


def test_env_mismatch_raises_clear_error(monkeypatch, lifted):
    monkeypatch.setenv("FS_ENVIRONMENT", "beta")
    detect_as(monkeypatch, Environment.PRODUCTION)
    with pytest.raises(ContextError) as ei:
        Context().services()
    assert "production" in str(ei.value)
    assert SECRET not in str(ei.value)


def test_services_built_once_and_cached(monkeypatch, lifted):
    detect_as(monkeypatch, Environment.PRODUCTION)
    c = Context()
    s = c.services()
    assert c.services() is s
    assert len(lifted) == 1
    for attr in ("client", "discovery", "tree", "writer", "search", "memories", "sources"):
        assert getattr(s, attr) is not None
    assert SECRET not in repr(c) and SECRET not in repr(s)


def test_writes_allowed_beta_true(monkeypatch):
    monkeypatch.setenv("FS_ENVIRONMENT", "beta")
    assert Context().writes_allowed() is True
    Context().require_writes()


def test_writes_blocked_on_prod_without_flag():
    c = Context()
    assert c.writes_allowed() is False
    with pytest.raises(ContextError) as ei:
        c.require_writes()
    assert "FS_ALLOW_PROD_WRITES" in str(ei.value)


@pytest.mark.parametrize("val", ["1", "true", "YES"])
def test_writes_allowed_on_prod_with_flag(monkeypatch, val):
    monkeypatch.setenv("FS_ALLOW_PROD_WRITES", val)
    c = Context()
    assert c.writes_allowed() is True
    c.require_writes()


def test_call_relifts_once_on_token_expired(monkeypatch, lifted):
    detect_as(monkeypatch, Environment.PRODUCTION)
    c = Context()
    attempts = []

    def fn(services):
        attempts.append(services)
        if len(attempts) == 1:
            raise TokenExpiredError("expired")
        return "ok"

    assert c.call(fn) == "ok"
    assert len(lifted) == 2  # initial lift + exactly one re-lift
    assert attempts[0] is not attempts[1]


def test_call_reauth_message_when_relift_fails(monkeypatch, lifted):
    detect_as(monkeypatch, Environment.PRODUCTION)
    c = Context()

    def fn(services):
        raise TokenExpiredError("expired")

    with pytest.raises(ContextError) as ei:
        c.call(fn)
    msg = str(ei.value).lower()
    assert "log in" in msg and "production" in msg
    assert len(lifted) == 2
    assert SECRET not in str(ei.value)
