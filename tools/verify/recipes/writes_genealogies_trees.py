"""Private (Genealogies) tree writes: create, update and delete a tree.

One recipe per write, each claiming only the write it calls through ``ctx.api``. Every
tree and person written is a ZZSPIKE fixture this run created and tracked the moment its
id was known (``FixtureSet.genealogies_tree`` with the raw client, or the create's own
answer), so strict teardown deletes it: persons, then sources, then the tree. The account's
current tree and any tree it already had are never touched. A tree is only ever read by id: readGenealogiesTrees answers a list of PUBLIC collections (live),
not the user's trees, so no recipe uses it.

A tree is created with a ``sourceDescriptions`` title (live: 201, id ``2:2:2:<id>``; the
create's status and headers go into ``ctx.notes``). The spec says
updateGenealogiesTree "updates a genealogies tree with persons and/or relationships": it
does not rename, it adds, so it is proven by adding a LIVING person and reading the tree's
persons back. Ids are only ever used as the server returns them.
"""
from __future__ import annotations

from ..core import Recipe, VerifyContext
from ..fixtures import create_tree, created_persons, living_person_body
from .genealogies_common import NO_SUCH_GENEALOGIES, persons, read_person, read_tree
from .writes_common import (GONE, created_id, describe_box, expect_same, redacted, status_of,
                            token)
from .writes_common import poll as _poll

SURNAME = "ZZSPIKETreeUpdate"


def poll(ctx: VerifyContext, fetch, done, what: str):
    """``writes_common.poll``, failing with the account-free skeleton."""
    return _poll(ctx, fetch, done, what, shown=describe_box)


def _own(ctx: VerifyContext, tid: str) -> None:
    if tid not in ctx.fixtures.tracked("genealogies_tree"):
        raise AssertionError(f"refusing to write tree {tid}: not created and tracked by "
                             "this run")


def _header_note(result) -> str:
    lowered = {k.lower(): v for k, v in result.headers.items()}
    return (f"answered {result.status} Location={lowered.get('location')!r} "
            f"X-ENTITY-ID={lowered.get('x-entity-id')!r} Entity-Id={lowered.get('entity-id')!r}")


@redacted
def tree_create(ctx: VerifyContext) -> None:
    """Create a ZZSPIKE private tree with createGenealogiesTree; read it back by id.

    The body is a ``sourceDescriptions`` title (the one beta took live); the id is the
    create's X-ENTITY-ID / Entity-Id / Location (``2:2:2:<id>``), tracked at once, and the
    create must answer one. Negative control: readGenealogiesTree's status for an id that
    does not exist (an error) against the new id's (200). The read must name the id with
    the marker title. (GET /genealogies/trees lists public collections, not the user's
    trees, so it proves nothing about the create.)
    """
    title = token("t")
    unknown = status_of(lambda: ctx.api.read_genealogies_tree(NO_SUCH_GENEALOGIES))
    result, shape = create_tree(
        lambda body: ctx.api.create_genealogies_tree(reason=ctx.reason, body=body), title)
    ctx.notes.append(f"tree create ({shape} body) {_header_note(result)}")
    tid = created_id(result, "tree")
    if not tid:
        raise AssertionError(f"the create answered no X-ENTITY-ID, Entity-Id or Location; "
                             f"{describe_box(result)}")
    ctx.fixtures.track("genealogies_tree", tid)
    tree = read_tree(ctx, tid)
    ctx.expect_changed(unknown, tree.result.status,
                       f"readability of tree {tid} (vs an unknown id)")
    if tree.shown != title or tree.size != 0:
        raise AssertionError(f"tree {tid} reads back {tree.shown!r} with {tree.size!r} persons, "
                             f"not {title!r} with none; {describe_box(tree.result)}")


@redacted
def tree_update(ctx: VerifyContext) -> None:
    """Add a LIVING fixture person to a fixture tree with updateGenealogiesTree.

    The spec: the update adds persons (and relationships) and answers new ids for the
    persons created. Two ZZSPIKE trees; only the first is updated, with
    ``{"persons": [{"id": "zz1", living: true, a marker name}]}``. The answer names the
    created person as a ``sourceDescriptions`` entry keyed ``zz1`` whose ark identifier
    ends in the real id, tracked at once. Negative control: that id among the tree's
    persons (readGenealogiesPersons: ids only), absent before and, polled, the only one
    after; the person reads back (readGenealogiesPerson) living with the marker name, and
    the tree reports one person. Siblings: the other tree still lists no persons, and
    both trees keep their titles.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sibling = ctx.fixtures.genealogies_tree(token("s"))
    _own(ctx, tid)
    p0, s0 = persons(ctx, tid), persons(ctx, sibling)
    t0, st0 = read_tree(ctx, tid), read_tree(ctx, sibling)
    marker = token("n")
    result = ctx.api.update_genealogies_tree(
        tid, reason=ctx.reason, body=living_person_body(marker, SURNAME, "Female",
                                                        local_id="zz1"))
    ctx.notes.append(f"tree update answered {result.status}")
    made = created_persons(result.body, {"zz1"})
    for pid in made.values():
        ctx.fixtures.track("genealogies_person", pid)
    if len(made) != 1:
        # The answer did not name the created id(s): whatever the tree lists now that it
        # did not before is ours, so track it before failing (no untracked leak).
        try:
            for pid in sorted(set(persons(ctx, tid).items) - set(p0.items) - set(made.values())):
                ctx.fixtures.track("genealogies_person", pid)
        except Exception:  # noqa: BLE001 - best effort; the failure below is the report
            pass
        raise AssertionError(f"the update named {len(made)} created persons, wanted one; "
                             f"{describe_box(result)}")
    new_id = next(iter(made.values()))
    p1 = poll(ctx, lambda: persons(ctx, tid), lambda p: new_id in p.items,
              f"readGenealogiesPersons: person {new_id}")
    ctx.expect_changed(new_id in p0.items, new_id in p1.items,
                       f"presence of person {new_id} in tree {tid}")
    if set(p1.items) != {new_id}:
        raise AssertionError(f"tree {tid} lists persons {sorted(p1.items)}, wanted exactly "
                             f"{new_id}; {describe_box(p1.result)}")
    found = [new_id]
    person = read_person(ctx, found[0])
    if person.living is not True or not person.view.name_has(marker):
        raise AssertionError(f"person {found[0]} reads back living={person.living!r}, "
                             f"names {dict(person.view.names)}; {describe_box(person.result)}")
    expect_same(s0.items, persons(ctx, sibling).items, f"the persons of the other tree {sibling}")
    t1 = read_tree(ctx, tid)
    ctx.expect_changed(t0.size, t1.size, f"the person count of tree {tid}")
    if t1.size != 1:
        raise AssertionError(f"tree {tid} reports {t1.size!r} persons, wanted 1; "
                             f"{describe_box(t1.result)}")
    expect_same(t0.shown, t1.shown, f"the title of tree {tid}")
    expect_same(st0, read_tree(ctx, sibling), f"the other tree {sibling}")


@redacted
def tree_delete(ctx: VerifyContext) -> None:
    """Delete a fixture tree with deleteGenealogiesTree; read it back gone.

    Two empty ZZSPIKE trees; only the first is deleted (an empty tree, so a cascade to
    persons cannot hide the proof). Negative control: readGenealogiesTree's status for it,
    before (200) and after (404/410); only then is it marked deleted. Sibling: the other
    tree still reads back as it was.
    """
    tid = ctx.fixtures.genealogies_tree(token("t"))
    sibling = ctx.fixtures.genealogies_tree(token("s"))
    _own(ctx, tid)
    before = read_tree(ctx, tid).result.status
    s0 = read_tree(ctx, sibling)
    ctx.api.delete_genealogies_tree(tid, reason=ctx.reason)
    after = status_of(lambda: ctx.api.read_genealogies_tree(tid))
    ctx.expect_changed(before, after, f"readability of tree {tid}")
    if after not in GONE:
        raise AssertionError(f"deleted tree {tid} reads back {after}, not 404/410")
    ctx.fixtures.mark_deleted("genealogies_tree", tid)
    expect_same(s0, read_tree(ctx, sibling), f"the other tree {sibling}")


WRITES_GENEALOGIES_TREES = (
    Recipe("writes.genealogies.tree_create", ("createGenealogiesTree",), "state-change",
           tree_create),
    Recipe("writes.genealogies.tree_update", ("updateGenealogiesTree",), "state-change",
           tree_update),
    Recipe("writes.genealogies.tree_delete", ("deleteGenealogiesTree",), "state-change",
           tree_delete),
)
