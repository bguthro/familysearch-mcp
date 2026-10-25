"""Pilot recipes: a person's create/read and delete/restore, proven by read-back.

Every assertion is on what the server reads back afterwards, never on a write's
status code. Created persons are deceased ``ZZSPIKE`` fixtures, tracked the moment
they have an id so strict teardown removes them whatever happens next.
"""
from familysearch import FSHTTPError
from familysearch.models import FACT_BIRTH, build_fact, build_person

from ..core import Recipe, VerifyContext

GONE = (404, 410)
NO_SUCH_PERSON = "ZZZZ-ZZZ"  # not a valid tree id: it can never exist


def _status(read) -> int:
    """The status of a read: 200-ish when it answers, the HTTP error status when not."""
    try:
        return read().status
    except FSHTTPError as e:
        return e.status


def _display_name(result) -> str:
    return result.body["persons"][0]["display"]["name"]


def _marker_in_search(ctx: VerifyContext, marker: str) -> bool:
    """Does the tree search show a person whose display name carries ``marker``?

    204 / an empty body means no hits. Any other error propagates: a broken search is
    a problem to see, not a "no".
    """
    result = ctx.api.search_tree_persons(query={"q.surname": marker, "count": 20})
    entries = (result.body or {}).get("entries") or []
    for entry in entries:
        for person in ((entry.get("content") or {}).get("gedcomx") or {}).get("persons") or []:
            if marker in ((person.get("display") or {}).get("name") or ""):
                return True
    return False


def person_create_read(ctx: VerifyContext) -> None:
    """Create a deceased ZZSPIKE person bearing a fresh marker and read it back.

    The negative control asks the server one question twice: "does it hold a person
    bearing ``ctx.unique``?" Before the create, the answer comes from a tree search
    (the marker is a fresh UUID, so it must be absent; a search that already shows
    it leaves the create nothing to prove). After, it comes from readPerson on the
    new id: the marker can only be in that read-back if the create took effect.
    Search index lag cannot cause a false pass: only the "before" uses search.
    Reading a never-valid id must 404/410, so the 200 read-back is not a catch-all.
    """
    marker = ctx.unique
    before = _marker_in_search(ctx, marker)
    person = build_person(given="ZZSPIKE Pilot", surname=marker, gender="Male", living=False,
                          facts=[build_fact(FACT_BIRTH, date="1850")])
    created = ctx.api.create_person(reason=ctx.reason, body={"persons": [person]})
    pid = created.entity_id
    if not pid:
        raise AssertionError("createPerson returned no new person id")
    ctx.fixtures.track("person", pid)
    name = _display_name(ctx.api.read_person(pid))
    ctx.expect_changed(before, marker in name, f"presence of person marked {marker}")
    expected = f"ZZSPIKE Pilot {marker}"
    if name != expected:
        raise AssertionError(f"read-back name {name!r} != created {expected!r}")
    unknown = _status(lambda: ctx.api.read_person(NO_SUCH_PERSON))
    if unknown not in GONE:
        raise AssertionError(f"reading {NO_SUCH_PERSON} answered {unknown}, not 404/410")


def person_delete_restore(ctx: VerifyContext) -> None:
    """Delete a fixture person, check it is gone, restore it, check it is back.

    Two negative controls, both on readPerson's status: readable -> gone after the
    delete, gone -> readable after the restore (a no-op write leaves the status
    unchanged and fails). The restored person must read back with its original name.
    The person is not marked deleted: strict teardown deletes it again and checks.
    """
    pid = ctx.fixtures.person(given="Pilot")
    original = ctx.api.read_person(pid)
    before, name = original.status, _display_name(original)
    ctx.api.delete_person(pid, reason=ctx.reason)
    after = _status(lambda: ctx.api.read_person(pid))
    ctx.expect_changed(before, after, "person readability after delete")
    if after not in GONE:
        raise AssertionError(f"deleted person {pid} read back {after}, not 404/410")
    # The spec takes */* and answers 204: a bare POST assumes the least.
    ctx.api.restore_person(pid, reason=ctx.reason)
    restored = _status(lambda: ctx.api.read_person(pid))
    ctx.expect_changed(after, restored, "person readability after restore")
    if restored != 200:
        raise AssertionError(f"restored person {pid} read back {restored}, not 200")
    back = _display_name(ctx.api.read_person(pid))
    if back != name:
        raise AssertionError(f"restored person {pid} reads {back!r}, was {name!r}")


PILOT = (
    Recipe("family_tree.person_create_read", ("createPerson", "readPerson"),
           "state-change", person_create_read),
    Recipe("family_tree.person_delete_restore", ("deletePerson", "restorePerson"),
           "state-change", person_delete_restore),
)
