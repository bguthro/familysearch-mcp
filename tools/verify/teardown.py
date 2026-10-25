"""Strict, read-back-verified deletion shared by the live suites and the verifier.

Every tracked object was created in THIS run, so each is verified by read-back:
  1. GET its resource URL: it MUST be readable (else ERROR: wrong URL or vanished);
  2. DELETE it (non-2xx is an ERROR);
  3. GET again: it MUST be 404/410 (else LEFTOVER).
A tolerated 404 once hid a wrong delete URL, so 404 is never accepted blindly.
"""
from familysearch import FSHTTPError


def _gone(get) -> bool:
    """True when the resource 404/410s; False when readable; other errors propagate."""
    try:
        get()
    except FSHTTPError as e:
        if e.status in (404, 410):
            return True
        raise
    return False


def verified_delete(label, url, get, delete, errors) -> None:
    """GET must succeed, DELETE must be 2xx, then GET must be 404/410."""
    try:
        try:
            get()
        except FSHTTPError as e:
            errors.append(
                f"{label}: tracked object not found at {url} "
                f"(wrong URL or vanished): HTTP {e.status}")
            return
        delete()
        if not _gone(get):
            errors.append(f"{label}: still readable at {url} after delete")
    except Exception as e:  # noqa: BLE001 - record, keep cleaning up
        errors.append(f"{label}: {e}")
