"""Offline tests for the live suites' VERIFIED teardown helper (no network)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))

from _tracker import verified_delete  # noqa: E402

from familysearch import FSHTTPError  # noqa: E402


class Fake:
    """A resource: GET/DELETE callables with scripted behavior."""

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


def run(f, **kw):
    errors = []
    verified_delete("thing", f.get, f.delete, errors, **kw)
    return errors


def test_already_gone_is_ok_and_not_deleted():
    f = Fake(exists=False)
    assert run(f) == [] and f.deletes == 0


def test_delete_then_gone_is_ok():
    f = Fake()
    assert run(f) == [] and f.deletes == 1


def test_delete_then_still_readable_is_leftover():
    errs = run(Fake(survives=True))
    assert len(errs) == 1 and "still" in errs[0]


def test_delete_404_while_get_200_is_error():
    errs = run(Fake(delete_status=404))
    assert len(errs) == 1 and "404" in errs[0]


def test_delete_500_is_error():
    assert len(run(Fake(delete_status=500))) == 1


def test_get_without_verification_tolerates_delete_404_only_when_asked():
    errors = []
    f = Fake(delete_status=404)
    verified_delete("src", None, f.delete, errors, delete_404_ok=True)
    assert errors == []
    verified_delete("src", None, f.delete, errors)
    assert len(errors) == 1


def test_unexpected_get_error_recorded():
    def bad():
        raise FSHTTPError(500, "boom")
    errors = []
    verified_delete("t", bad, lambda: None, errors)
    assert len(errors) == 1
