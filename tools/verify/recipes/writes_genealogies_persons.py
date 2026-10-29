"""Private (Genealogies) person writes: create, update, conclusion delete, delete and restore.

The point of the private space is that a person may be LIVING, so every fixture here is a
fictional ZZSPIKE person created with ``living: true`` inside a tree this run created
(``FixtureSet.genealogies_tree``, tracked, torn down), and every recipe asserts the living
flag reads back (an update that dropped it would fail the sibling check). Nothing here
ever posts to the shared Family Tree. Each recipe claims only the write it calls through
``ctx.api`` and proves it by reading the person back (readGenealogiesPerson,
readGenealogiesPersons: unclaimed here, verified by the ``reads.genealogies.*`` cases)
before and after; the conclusions a write should not touch must read back as they were.

The bodies are the Family Tree's GEDCOM X ones (``persons`` with names that carry parts,
facts without ids to create, with ids to update); whether the private space takes them
the same way is unproven. Ids are only used as the server returns them.
"""
from __future__ import annotations

from familysearch import FSHTTPError
from familysearch.models import FACT_BIRTH, build_fact

from ..core import Recipe, VerifyContext
from ..fixtures import living_person_body
from .genealogies_common import persons, read_person
from .writes_common import (FACT_RESIDENCE, GONE, describe_box, expect_same, fact_update,
                            name_update, redacted, require_created_id, restore, status_of,
                            token)
from .writes_common import poll as _poll

SURNAME = "ZZSPIKEPrivate"


def poll(ctx: VerifyContext, fetch, done, what: str):
    """``writes_common.poll``, failing with the account-free skeleton."""
    return _poll(ctx, fetch, done, what, shown=describe_box)


def _own_person(ctx: VerifyContext, pid: str) -> None:
    if pid not in ctx.fixtures.tracked("genealogies_person"):
        raise AssertionError(f"refusing to write person {pid}: not created and tracked by "
                             "this run")


def _single(ids, what: str, person) -> str:
    if len(ids) != 1:
        raise AssertionError(f"expected exactly one {what}, found {len(ids)}: "
                             f"{person.view.summary()}; {describe_box(person.result)}")
    return next(iter(ids))


def _living(person, pid: str) -> None:
    if person.living is not True:
        raise AssertionError(f"person {pid} reads back living={person.living!r}, not true: "
                             f"{describe_box(person.result)}")


@redacted
def person_create(ctx: VerifyContext) -> None:
    """Create a LIVING ZZSPIKE person in a fixture tree with createGenealogiesPerson.

    The body is the proven Family Tree person shape with ``living: true``, a marker name
    and a birth fact; the id is the create's X-ENTITY-ID / Location, tracked at once.
    Negative control: the id among the tree's persons (readGenealogiesPersons), before
    (absent; the tree is empty) and after (polled). The person reads back
    (readGenealogiesPerson) living, with the marker name, the birth fact and the gender;
    the list row agrees. Sibling: the other tree still lists no persons.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    other = ctx.fixtures.genealogies_tree(token("s"))
    p0, s0 = persons(ctx, tid), persons(ctx, other)
    marker, year = token("p"), "1990"
    body = living_person_body(marker, SURNAME, "Female",
                              facts=[build_fact(FACT_BIRTH, date=year)])
    result = ctx.api.create_genealogies_person(tid, reason=ctx.reason, body=body)
    lowered = {k.lower(): v for k, v in result.headers.items()}
    ctx.notes.append(f"person create answered {result.status} "
                     f"Location={lowered.get('location')!r} "
                     f"X-ENTITY-ID={lowered.get('x-entity-id')!r}")
    pid = require_created_id(result, "private person")
    ctx.fixtures.track("genealogies_person", pid)
    after = poll(ctx, lambda: persons(ctx, tid), lambda p: pid in p.items,
                 f"readGenealogiesPersons: person {pid}")
    ctx.expect_changed(pid in p0.items, pid in after.items,
                       f"presence of person {pid} in tree {tid}")
    name, living = after.items[pid]
    if marker not in name or living is not True or set(after.items) != {pid}:
        raise AssertionError(f"tree {tid} lists {after.items!r}, wanted only person {pid} "
                             f"named {marker}, living; {describe_box(after.result)}")
    person = read_person(ctx, pid)
    _living(person, pid)
    if not person.view.name_has(marker):
        raise AssertionError(f"person {pid} reads back names {dict(person.view.names)}, "
                             f"not {marker!r}; {describe_box(person.result)}")
    if not any(f.type == FACT_BIRTH and f.date == year for f in person.view.facts.values()):
        raise AssertionError(f"person {pid} reads back no birth in {year}: "
                             f"{person.view.summary()}; {describe_box(person.result)}")
    if person.view.gender_type != "http://gedcomx.org/Female":
        raise AssertionError(f"person {pid} reads back gender {person.view.gender_type!r}; "
                             f"{describe_box(person.result)}")
    expect_same(s0.items, persons(ctx, other).items, f"the persons of the other tree {other}")


@redacted
def person_update(ctx: VerifyContext) -> None:
    """Change a fixture's name and add a fact with updateGenealogiesPerson.

    Two LIVING ZZSPIKE persons in one fixture tree; only the first is updated (two
    posts), each read back from readGenealogiesPerson. Negative controls: a fresh marker
    is absent from the names before the name update and present after; a fresh place
    marker is on no fact before the fact is added and on exactly one new fact after.
    Siblings: the name update keeps the name count (an update by id adds none), drops
    the old text and leaves facts, gender and the living flag alone; adding a fact keeps
    every earlier fact, the names, gender and living; the other person reads back as it
    was.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    pid = ctx.fixtures.genealogies_person(tid, "Update",
                                          facts=[build_fact(FACT_BIRTH, date="1990")])
    sibling = ctx.fixtures.genealogies_person(tid, "Sibling")
    _own_person(ctx, pid)
    v0, s0 = read_person(ctx, pid), read_person(ctx, sibling)
    _living(v0, pid)
    name_id = _single(v0.view.names, "name on the fixture", v0)
    old_name = v0.view.names[name_id]

    marker = token("n")
    ctx.api.update_genealogies_person(pid, reason=ctx.reason,
                                      body=name_update(marker, SURNAME, name_id=name_id))
    v1 = read_person(ctx, pid)
    ctx.expect_changed(v0.view.name_has(marker), v1.view.name_has(marker),
                       f"presence of name marker {marker}")
    expect_same(len(v0.view.names), len(v1.view.names),
                "the number of names (an update by id adds none)", v1.view)
    if old_name and any(old_name in text for text in v1.view.names.values()):
        raise AssertionError(f"the old name {old_name!r} still reads back after the name "
                             f"update: {v1.view.summary()}")
    expect_same(v0.view.facts, v1.view.facts, "facts (on a name update)", v1.view)
    expect_same(v0.view.gender_type, v1.view.gender_type, "gender (on a name update)")
    expect_same(v0.living, v1.living, "the living flag (on a name update)")

    place = token("f")
    ctx.api.update_genealogies_person(
        pid, reason=ctx.reason, body=fact_update(build_fact(FACT_RESIDENCE, place=place)))
    v2 = read_person(ctx, pid)
    ctx.expect_changed(bool(v1.view.facts_with(place)), bool(v2.view.facts_with(place)),
                       f"presence of a fact marked {place}")
    fid = _single(set(v2.view.facts) - set(v1.view.facts), "new fact", v2)
    if v2.view.facts[fid].type != FACT_RESIDENCE or v2.view.facts[fid].place != place:
        raise AssertionError(f"the new fact {fid} is not the Residence at {place}: "
                             f"{v2.view.summary()}")
    expect_same({k: v2.view.facts.get(k) for k in v1.view.facts}, dict(v1.view.facts),
                "the earlier facts (on adding one)", v2.view)
    expect_same(v1.view.names, v2.view.names, "names (on adding a fact)", v2.view)
    expect_same(v1.view.gender_type, v2.view.gender_type, "gender (on adding a fact)")
    expect_same(v1.living, v2.living, "the living flag (on adding a fact)")
    expect_same(s0, read_person(ctx, sibling), f"the other person {sibling}")


@redacted
def person_conclusions(ctx: VerifyContext) -> None:
    """Delete one fact of a fixture by its conclusion id with deleteGenealogiesConclusion.

    The fixture (living, in a fixture tree) is created with a birth and a Residence marked
    with a fresh place; the Residence's id is the one readGenealogiesPerson answers.
    Negative control: that id is among the facts before the delete and not after.
    Siblings: every other fact (by id and value), the names, the gender and the living
    flag read back unchanged, and so does a second person in the tree.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    place = token("c")
    pid = ctx.fixtures.genealogies_person(
        tid, "Conclusion", facts=[build_fact(FACT_BIRTH, date="1990"),
                                  build_fact(FACT_RESIDENCE, place=place)])
    sibling = ctx.fixtures.genealogies_person(tid, "Sibling")
    _own_person(ctx, pid)
    v0, s0 = read_person(ctx, pid), read_person(ctx, sibling)
    cid = _single(v0.view.facts_with(place), f"fact marked {place}", v0)
    others = {k: f for k, f in v0.view.facts.items() if k != cid}
    if not any(f.type == FACT_BIRTH for f in others.values()):
        raise AssertionError(f"the fixture's birth fact is missing: {v0.view.summary()}")
    ctx.api.delete_genealogies_conclusion(pid, cid, reason=ctx.reason)
    v1 = read_person(ctx, pid)
    ctx.expect_changed(cid in v0.view.facts, cid in v1.view.facts, f"presence of fact {cid}")
    if v1.view.facts_with(place):
        raise AssertionError(f"a fact marked {place} still reads back: {v1.view.summary()}")
    expect_same(others, dict(v1.view.facts), "the other facts", v1.view)
    expect_same(v0.view.names, v1.view.names, "names", v1.view)
    expect_same(v0.view.gender_type, v1.view.gender_type, "gender")
    expect_same(v0.living, v1.living, "the living flag")
    expect_same(s0, read_person(ctx, sibling), f"the other person {sibling}")


@redacted
def person_delete_restore(ctx: VerifyContext) -> None:
    """Delete a fixture with deleteGenealogiesPerson, read it gone, restore it, read it back.

    Two LIVING ZZSPIKE persons in one fixture tree; only the first is deleted and
    restored. Negative controls, both on readGenealogiesPerson's status (readable ->
    404/410 after the delete; gone -> 200 after the restore, polled), and on the tree's
    persons (listed, unlisted after the delete, polled; listed again after the restore). The
    restore is a bare POST (retried once with ``{}`` only if beta answers 415). The
    restored person reads back as it did (names, facts, gender, living). Sibling: the
    other person is untouched throughout. The restored person stays tracked: strict
    teardown deletes it again and checks; one read gone and never restored is marked
    deleted so teardown skips it.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    pid = ctx.fixtures.genealogies_person(tid, "DeleteRestore")
    sibling = ctx.fixtures.genealogies_person(tid, "Sibling")
    _own_person(ctx, pid)
    v0, s0 = read_person(ctx, pid), read_person(ctx, sibling)
    _living(v0, pid)
    poll(ctx, lambda: persons(ctx, tid), lambda p: pid in p.items,
         f"readGenealogiesPersons: person {pid} listed before the delete")
    ctx.api.delete_genealogies_person(pid, reason=ctx.reason)
    after = status_of(lambda: ctx.api.read_genealogies_person(pid))
    ctx.expect_changed(200, after, f"readability of person {pid} after the delete")
    if after not in GONE:
        raise AssertionError(f"deleted person {pid} reads back {after}, not 404/410")
    poll(ctx, lambda: persons(ctx, tid), lambda p: pid not in p.items,
         f"readGenealogiesPersons: person {pid} unlisted after the delete")
    expect_same(s0, read_person(ctx, sibling), f"the other person {sibling} (on the delete)")
    try:
        restored = restore(lambda body: ctx.api.restore_genealogies_person(
            pid, reason=ctx.reason, body=body))
        ctx.notes.append(f"person restore answered {restored.status}")
        back = poll(ctx, lambda: status_of(lambda: ctx.api.read_genealogies_person(pid)),
                    lambda s: s == 200, f"readGenealogiesPerson: person {pid} readable again")
        ctx.expect_changed(after, back, f"readability of person {pid} after the restore")
    except (AssertionError, FSHTTPError):
        # Never restored: it reads gone, so teardown must not look for it.
        if status_of(lambda: ctx.api.read_genealogies_person(pid)) in GONE:
            ctx.fixtures.mark_deleted("genealogies_person", pid)
        raise
    expect_same(v0, read_person(ctx, pid), f"person {pid} (restored)")
    poll(ctx, lambda: persons(ctx, tid), lambda p: pid in p.items,
         f"readGenealogiesPersons: person {pid} listed again after the restore")
    expect_same(s0, read_person(ctx, sibling), f"the other person {sibling} (on the restore)")


WRITES_GENEALOGIES_PERSONS = (
    Recipe("writes.genealogies.person_create", ("createGenealogiesPerson",), "state-change",
           person_create),
    Recipe("writes.genealogies.person_update", ("updateGenealogiesPerson",), "state-change",
           person_update),
    Recipe("writes.genealogies.person_conclusions", ("deleteGenealogiesConclusion",),
           "state-change", person_conclusions),
    Recipe("writes.genealogies.person_delete_restore",
           ("deleteGenealogiesPerson", "restoreGenealogiesPerson"), "state-change",
           person_delete_restore),
)
