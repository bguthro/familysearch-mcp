"""Note writes: create, update and delete a note of a person, a couple, a child-and-parents.

One recipe per owner kind, each claiming its owner's three note writes and calling
each through ``ctx.api``. The owner is a ``ZZSPIKE`` fixture this run created and
tracked (a person; a couple of two persons; a one-parent child-and-parents
relationship, which derives no couple); its notes are embedded in it and go with it
at teardown. Read-backs are the verified note reads (the owner's notes list, and the
single-note read, which carries the text): none is claimed.

Every recipe creates note A, then a sibling note B, updates A, deletes A. Negative
controls (server values on both sides): A's id is absent from the list before its
create and present after; A's text differs before and after the update; A's id is
listed before the delete and not after. Siblings: the update keeps the owner's note
ids (it adds or removes none) and B as it was; the delete leaves B listed and as it was.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from familysearch.api._runtime import ApiResult

from ..core import Recipe, VerifyContext
from .writes_common import (ENVELOPES, Listing, Note, describe, expect_same, new_note_id,
                            note_body, note_listing, note_update, single_note, token)


@dataclass(frozen=True)
class Owner:
    """A note owner: its envelope and the note writes and reads bound to its id."""

    envelope: str
    list: Callable[[], ApiResult]
    read: Callable[[str], ApiResult]
    create: Callable[[dict], ApiResult]
    update: Callable[[str, dict], ApiResult]
    delete: Callable[[str], ApiResult]


def _listing(owner: Owner) -> Listing:
    return note_listing(owner.list(), owner.envelope)


def _note(owner: Owner, nid: str) -> Note:
    return single_note(owner.read(nid), owner.envelope, nid)


def _create(ctx: VerifyContext, owner: Owner, tag: str) -> tuple[str, str, Listing]:
    """Create a note marked with a fresh token; its id, the token and the list before."""
    before = _listing(owner)
    text = token(tag)
    nid = new_note_id(owner.create(note_body(owner.envelope, f"{text} subject", text)))
    if nid in before.items:
        raise AssertionError(f"the new note's id {nid} was already listed; "
                             f"{describe(before.result)}")
    return nid, text, before


def note_lifecycle(ctx: VerifyContext, owner: Owner, what: str) -> None:
    nid, text, before = _create(ctx, owner, "a")
    after = _listing(owner)
    ctx.expect_changed(nid in before.items, nid in after.items,
                       f"presence of {what} note {nid}")
    a0 = _note(owner, nid)
    if a0.text != text:
        raise AssertionError(f"note {nid} reads back text {a0.text!r}, not {text!r}; "
                             f"{describe(a0.result)}")

    sibling, sibling_text, _ = _create(ctx, owner, "b")
    listed = _listing(owner)
    if not {nid, sibling} <= set(listed.items):
        raise AssertionError(f"notes {nid} and {sibling} are not both listed; "
                             f"{describe(listed.result)}")
    b0 = _note(owner, sibling)
    if b0.text != sibling_text:
        raise AssertionError(f"note {sibling} reads back text {b0.text!r}, not "
                             f"{sibling_text!r}; {describe(b0.result)}")

    new = token("u")
    owner.update(nid, note_update(owner.envelope, nid, f"{new} subject", new))
    a1 = _note(owner, nid)
    ctx.expect_changed(a0.text, a1.text, f"text of {what} note {nid}")
    if a1.text != new or a1.has(text):
        raise AssertionError(f"note {nid} reads back {a1.subject!r} / {a1.text!r} after the "
                             f"update to {new!r}; {describe(a1.result)}")
    updated = _listing(owner)
    expect_same(sorted(listed.items), sorted(updated.items),
                f"the {what}'s note ids (an update by id adds or removes none)")
    expect_same(b0, _note(owner, sibling), f"the other note {sibling} (on the update)")

    owner.delete(nid)
    gone = _listing(owner)
    ctx.expect_changed(nid in updated.items, nid in gone.items,
                       f"presence of {what} note {nid} (deleted)")
    if sibling not in gone.items:
        raise AssertionError(f"the other note {sibling} is no longer listed after deleting "
                             f"{nid}; {describe(gone.result)}")
    expect_same(b0, _note(owner, sibling), f"the other note {sibling} (on the delete)")


def person_notes(ctx: VerifyContext) -> None:
    """Create, update and delete notes of a fixture person (read back: readPersonNotes,
    readPersonNote).

    Negative control: the note id's presence in the list (create, delete) and the
    note's text (update) differ before and after; see the module docstring.
    """
    pid = ctx.fixtures.person(given="Notes")
    api, reason = ctx.api, ctx.reason
    note_lifecycle(ctx, Owner(
        ENVELOPES["person"],
        list=lambda: api.read_person_notes(pid),
        read=lambda nid: api.read_person_note(pid, nid),
        create=lambda body: api.create_person_note(pid, reason=reason, body=body),
        update=lambda nid, body: api.update_person_note(pid, nid, reason=reason, body=body),
        delete=lambda nid: api.delete_person_note(pid, nid, reason=reason)), "person")


def couple_notes(ctx: VerifyContext) -> None:
    """Create, update and delete notes of a fixture couple (read back:
    getCoupleRelationshipNotes, readCoupleRelationshipNote).

    Negative control: the note id's presence in the list (create, delete) and the
    note's text (update) differ before and after; see the module docstring.
    """
    husband = ctx.fixtures.person(given="NotesHusband")
    wife = ctx.fixtures.person(given="NotesWife", gender="Female")
    crid = ctx.fixtures.couple(husband, wife)
    api, reason = ctx.api, ctx.reason
    note_lifecycle(ctx, Owner(
        ENVELOPES["couple"],
        list=lambda: api.get_couple_relationship_notes(crid),
        read=lambda nid: api.read_couple_relationship_note(crid, nid),
        create=lambda body: api.create_couple_relationship_note(crid, reason=reason,
                                                                body=body),
        update=lambda nid, body: api.update_couple_relationship_note(
            crid, nid, reason=reason, body=body),
        delete=lambda nid: api.delete_couple_relationship_note(crid, nid, reason=reason)),
        "couple")


def child_and_parents_notes(ctx: VerifyContext) -> None:
    """Create, update and delete notes of a fixture child-and-parents relationship
    (one parent, so beta derives no couple; read back:
    getChildAndParentsRelationshipNotes, readChildAndParentRelationshipNote).

    Negative control: the note id's presence in the list (create, delete) and the
    note's text (update) differ before and after; see the module docstring.
    """
    parent = ctx.fixtures.person(given="NotesParent")
    child = ctx.fixtures.person(given="NotesChild", birth_year="1880")
    caprid = ctx.fixtures.child_and_parents(child, [parent])
    api, reason = ctx.api, ctx.reason
    note_lifecycle(ctx, Owner(
        ENVELOPES["child_and_parents"],
        list=lambda: api.get_child_and_parents_relationship_notes(caprid),
        read=lambda nid: api.read_child_and_parent_relationship_note(caprid, nid),
        create=lambda body: api.create_child_and_parents_relationship_note(
            caprid, reason=reason, body=body),
        update=lambda nid, body: api.update_child_and_parents_relationship_note(
            caprid, nid, reason=reason, body=body),
        delete=lambda nid: api.delete_child_and_parents_relationship_note(
            caprid, nid, reason=reason)), "child-and-parents")


WRITES_TREE_NOTES = (
    Recipe("writes.tree.person_notes",
           ("createPersonNote", "updatePersonNote", "deletePersonNote"),
           "state-change", person_notes),
    Recipe("writes.tree.couple_notes",
           ("createCoupleRelationshipNote", "updateCoupleRelationshipNote",
            "deleteCoupleRelationshipNote"), "state-change", couple_notes),
    Recipe("writes.tree.child_and_parents_notes",
           ("createChildAndParentsRelationshipNote",
            "updateChildAndParentsRelationshipNote",
            "deleteChildAndParentsRelationshipNote"), "state-change",
           child_and_parents_notes),
)
