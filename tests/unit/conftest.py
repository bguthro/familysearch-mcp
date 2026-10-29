"""Shared fixtures: the teardown gone-check never really sleeps in unit tests."""
import pytest

from tools.verify import teardown


@pytest.fixture(autouse=True)
def _no_teardown_sleep(monkeypatch):
    monkeypatch.setattr(teardown, "sleep", lambda seconds: None)
